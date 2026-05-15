from __future__ import annotations

import asyncio
import random
import time
from collections.abc import AsyncIterator
from typing import Any, Optional

import httpx
import litellm

from src.llm_service.providers.base import (
    BaseLLMProvider, StreamEvent, TransportFinishData, UpstreamTransportError,
)
from src.llm_service.schemas.chat import (
    CacheBlock, ChatResponse, Choice, ChoiceMessage, ErrorData,
    NormalisedLLMRequest, TokenData, ToolUseData, UsageBlock,
)
from src.shared.config import settings
from src.shared.db.enums import KeyFormat, Transport
from src.shared.schemas.envelope import CostBlock, GuardrailsBlock, LatencyBlock, PolicyBlock
from src.shared.secrets.backend import TenantKeyPayload

_PROVIDER_REMAP: dict[str, str] = {
    "custom_endpoint": "http://<endpoint>",
}

_RETRYABLE_ERRORS = (
    litellm.Timeout,
    litellm.ServiceUnavailableError,
    litellm.APIConnectionError,
    litellm.InternalServerError,
    litellm.RateLimitError,
)


def _should_retry(error: Exception) -> bool:
    return isinstance(error, _RETRYABLE_ERRORS)


def _retry_delay(error: Exception, attempt: int) -> float:
    """Return how long to sleep before the next attempt.
    For rate limits, respect the provider's Retry-After header if present. Otherwise use exponential backoff with jitter.
    """
    if isinstance(error, litellm.RateLimitError):
        response = getattr(error, "response", None)
        if response is not None:
            headers = getattr(response, "headers", None)
            if headers is not None:
                retry_after = headers.get("retry-after")
                if retry_after is not None:
                    return float(retry_after)
    return settings.llm_retry_backoff_base_s * (2 ** attempt) + random.uniform(0, 0.5)


def _upstream_status(error: Exception) -> Optional[int]:
    # Only return a status code when a real upstream provider was contacted.
    if not getattr(error, "llm_provider", ""):
        return None
    response = getattr(error, "response", None)
    if response is None:
        return None
    return getattr(response, "status_code", None)


def _wrap_litellm_error(error: Exception) -> UpstreamTransportError:
    """Convert a LiteLLM exception into a structured UpstreamTransportError."""
    if isinstance(error, litellm.Timeout):
        return UpstreamTransportError(
            code="upstream_timeout", message=str(error), http_status=504,
        )
    if isinstance(error, litellm.RateLimitError):
        retry_after: Optional[str] = None
        response = getattr(error, "response", None)
        if response is not None:
            headers = getattr(response, "headers", None)
            if headers is not None:
                retry_after = headers.get("retry-after")
        return UpstreamTransportError(
            code="upstream_rate_limited", message=str(error), http_status=502,
            retry_after=retry_after,
        )
    if isinstance(error, litellm.AuthenticationError):
        return UpstreamTransportError(
            code="tenant_key_rejected", message=str(error), http_status=502,
        )
    return UpstreamTransportError(code="upstream_error", message=str(error), http_status=502)


def _build_fallbacks(
    model_str: str, regions: list[str], credential_kwargs: dict[str, Any],
) -> list[dict[str, Any]]:
    """Build LiteLLM fallback entries for secondary regions.
    Returns empty list when only one (or zero) regions are configured.
    """
    if len(regions) <= 1:
        return []
    region_key = (
        "aws_region_name" if "aws_region_name" in credential_kwargs
        else "api_base" if "api_base" in credential_kwargs
        else None
    )
    if region_key is None:
        return []
    return [
        {"model": model_str, **{**credential_kwargs, region_key: region}}
        for region in regions[1:]
    ]


class LiteLLMTransport(BaseLLMProvider):
    """Routes any provider through the LiteLLM in-process SDK."""

    def __init__(self, regions: Optional[list[str]] = None) -> None:
        self._regions: list[str] = regions or []

    def _model_string(self, provider: str, model: str) -> str:
        prefix = _PROVIDER_REMAP.get(provider, provider)
        return f"{prefix}/{model}"

    def _credential_kwargs(self, key: TenantKeyPayload) -> dict[str, Any]:
        """Build per-call credential kwargs — never sets any global key state."""
        if key.key_format == KeyFormat.API_KEY:
            kwargs: dict[str, Any] = {"api_key": key.data["api_key"]}
            if "api_base" in key.data:
                kwargs["api_base"] = key.data["api_base"]
            return kwargs
        if key.key_format == KeyFormat.AWS_CREDENTIALS:
            return {
                "aws_access_key_id": key.data["access_key_id"],
                "aws_secret_access_key": key.data["secret_access_key"],
                "aws_region_name": key.data.get("region", "us-east-1"),
            }
        if key.key_format == KeyFormat.ENDPOINT_PAIR:
            return {
                "api_base": key.data["endpoint_url"],
                "api_key": key.data["token"],
            }
        raise ValueError(f"Unsupported key_format: {key.key_format!r}")

    def _param_kwargs(self, params: Optional[Any]) -> dict[str, Any]:
        if not params:
            return {}
        kwargs: dict[str, Any] = {}
        if params.temperature is not None:
            kwargs["temperature"] = params.temperature
        if params.max_tokens is not None:
            kwargs["max_tokens"] = params.max_tokens
        if params.top_p is not None:
            kwargs["top_p"] = params.top_p
        if params.stop is not None:
            kwargs["stop"] = params.stop
        if params.seed is not None:
            kwargs["seed"] = params.seed
        if params.connect_timeout is not None or params.read_timeout is not None:
            kwargs["timeout"] = httpx.Timeout(
                None, connect=params.connect_timeout, read=params.read_timeout,
            )
        return kwargs

    def _serialize_messages(
        self, messages: list[Any], provider: str
    ) -> list[dict[str, Any]]:
        result: list[dict[str, Any]] = []
        for message in messages:
            serialised = message.model_dump(exclude={"cache"}, exclude_none=True)
            # Anthropic requires explicit cache_control blocks for prompt caching;
            # LiteLLM forwards them unchanged when present.
            if (
                provider in ("anthropic", "bedrock")
                and message.cache == "ephemeral"
                and isinstance(message.content, str)
            ):
                serialised["content"] = [
                    {
                        "type": "text",
                        "text": message.content,
                        "cache_control": {"type": "ephemeral"},
                    }
                ]
            result.append(serialised)
        return result

    def _extract_usage(self, raw: Any) -> UsageBlock:
        raw_usage = getattr(raw, "usage", None)
        if raw_usage is None:
            return UsageBlock()

        cache_read = 0
        cache_write = 0

        if getattr(raw_usage, "prompt_tokens_details", None):
            cache_read = getattr(raw_usage.prompt_tokens_details, "cached_tokens", 0) or 0

        cache_read = getattr(raw_usage, "cache_read_input_tokens", cache_read) or cache_read
        cache_write = getattr(raw_usage, "cache_creation_input_tokens", 0) or 0

        return UsageBlock(
            input_tokens=getattr(raw_usage, "prompt_tokens", 0) or 0,
            output_tokens=getattr(raw_usage, "completion_tokens", 0) or 0,
            input_tokens_cache_read=cache_read or None,
            input_tokens_cache_write=cache_write or None,
            total_tokens=getattr(raw_usage, "total_tokens", 0) or 0,
        )

    def _map_choices(self, raw: Any) -> list[Choice]:
        choices = []
        for raw_choice in raw.choices:
            raw_message = raw_choice.message
            tool_calls: Optional[list[dict[str, Any]]] = None
            if getattr(raw_message, "tool_calls", None):
                tool_calls = [
                    {
                        "id": tool_call.id,
                        "type": "function",
                        "function": {
                            "name": tool_call.function.name,
                            "arguments": tool_call.function.arguments,
                        },
                    }
                    for tool_call in raw_message.tool_calls
                ]
            choices.append(Choice(
                index=raw_choice.index,
                message=ChoiceMessage(
                    role=raw_message.role,
                    content=raw_message.content,
                    tool_calls=tool_calls,
                ),
                finish_reason=raw_choice.finish_reason or "stop",
            ))
        return choices

    async def chat(
        self, request: NormalisedLLMRequest, key: TenantKeyPayload
    ) -> ChatResponse:
        model_str = self._model_string(request.provider, request.model)
        messages = self._serialize_messages(request.messages, request.provider)
        credential_kwargs = self._credential_kwargs(key)
        param_kwargs = self._param_kwargs(request.params)

        if self._regions:
            if "aws_region_name" in credential_kwargs:
                credential_kwargs["aws_region_name"] = self._regions[0]
            elif "api_base" in credential_kwargs:
                credential_kwargs["api_base"] = self._regions[0]

        tool_kwargs: dict[str, Any] = {}
        if request.tools:
            tool_kwargs["tools"] = [t.model_dump() for t in request.tools]
        if request.tool_choice is not None:
            tool_kwargs["tool_choice"] = request.tool_choice

        fallback_kwargs: dict[str, Any] = {}
        fallbacks = _build_fallbacks(model_str, self._regions, credential_kwargs)
        if fallbacks:
            fallback_kwargs["fallbacks"] = fallbacks

        raw = None
        for attempt in range(settings.llm_retry_max_attempts):
            try:
                raw = await litellm.acompletion(
                    model=model_str, messages=messages, drop_params=True,
                    **tool_kwargs, **credential_kwargs, **param_kwargs, **fallback_kwargs,
                )
                break
            except Exception as error:
                print(f"Attempt {attempt + 1} failed: {error}")
                if not _should_retry(error) or attempt == settings.llm_retry_max_attempts - 1:
                    raise _wrap_litellm_error(error) from error
                await asyncio.sleep(_retry_delay(error, attempt))

        usage = self._extract_usage(raw)
        choices = self._map_choices(raw)

        return ChatResponse(
            id="",
            created=int(time.time()),
            tenant_id="",
            provider=request.provider,
            model=request.model,
            transport=Transport.LITELLM,
            region=self._regions[0] if self._regions else None,
            choices=choices,
            usage=usage,
            cost=CostBlock(),
            latency_ms=LatencyBlock(),
            cache=CacheBlock(
                our_cache_hit=False,
                upstream_prompt_cache_hit=(usage.input_tokens_cache_read or 0) > 0,
            ),
            guardrails=GuardrailsBlock(),
            policy=PolicyBlock(),
            provider_raw=raw.model_dump(),
        )

    async def stream(
        self, request: NormalisedLLMRequest, key: TenantKeyPayload
    ) -> AsyncIterator[StreamEvent]:
        model_str = self._model_string(request.provider, request.model)
        messages = self._serialize_messages(request.messages, request.provider)
        credential_kwargs = self._credential_kwargs(key)
        param_kwargs = self._param_kwargs(request.params)

        if self._regions:
            if "aws_region_name" in credential_kwargs:
                credential_kwargs["aws_region_name"] = self._regions[0]
            elif "api_base" in credential_kwargs:
                credential_kwargs["api_base"] = self._regions[0]

        tool_kwargs: dict[str, Any] = {}
        if request.tools:
            tool_kwargs["tools"] = [t.model_dump() for t in request.tools]
        if request.tool_choice is not None:
            tool_kwargs["tool_choice"] = request.tool_choice

        fallback_kwargs: dict[str, Any] = {}
        fallbacks = _build_fallbacks(model_str, self._regions, credential_kwargs)
        if fallbacks:
            fallback_kwargs["fallbacks"] = fallbacks

        # Retry connection setup before any tokens are sent to the caller.
        response_stream = None
        for attempt in range(settings.llm_retry_max_attempts):
            try:
                response_stream = await litellm.acompletion(
                    model=model_str,
                    messages=messages,
                    stream=True,
                    stream_options={"include_usage": True},
                    drop_params=True,
                    **tool_kwargs,
                    **credential_kwargs,
                    **param_kwargs,
                    **fallback_kwargs,
                )
                break
            except Exception as error:
                if not _should_retry(error) or attempt == settings.llm_retry_max_attempts - 1:
                    upstream_error = _wrap_litellm_error(error)
                    yield StreamEvent(
                        type="error",
                        data=ErrorData(
                            code=upstream_error.code, message=str(error),
                            upstream_status=_upstream_status(error),
                            retry_after=upstream_error.retry_after,
                        ),
                    )
                    return
                await asyncio.sleep(_retry_delay(error, attempt))

        final_usage = UsageBlock()
        final_finish_reason = "stop"

        # Once iteration starts we cannot retry — emit an error event on failure.
        try:
            async for chunk in response_stream:
                if not chunk.choices:
                    if getattr(chunk, "usage", None):
                        final_usage = self._extract_usage(chunk)
                    continue

                choice = chunk.choices[0]
                delta_content = getattr(choice.delta, "content", None) or ""

                if delta_content:
                    yield StreamEvent(
                        type="token",
                        data=TokenData(index=choice.index, delta=delta_content),
                    )

                for tool_call_delta in (getattr(choice.delta, "tool_calls", None) or []):
                    fn = getattr(tool_call_delta, "function", None)
                    args_fragment = (fn.arguments if fn else "") or ""
                    tool_id = tool_call_delta.id or ""
                    tool_name = (fn.name or "") if fn else ""
                    if tool_id or tool_name or args_fragment:
                        yield StreamEvent(
                            type="tool_use",
                            data=ToolUseData(
                                index=tool_call_delta.index,
                                id=tool_id,
                                name=tool_name,
                                arguments_delta=args_fragment,
                            ),
                        )

                if choice.finish_reason:
                    final_finish_reason = choice.finish_reason

                if getattr(chunk, "usage", None):
                    final_usage = self._extract_usage(chunk)

        except Exception as stream_error:
            yield StreamEvent(
                type="error",
                data=ErrorData(
                    code="upstream_disconnected", message=str(stream_error),
                    upstream_status=_upstream_status(stream_error),
                ),
            )
            return

        yield StreamEvent(
            type="finish",
            data=TransportFinishData(
                finish_reason=final_finish_reason,
                usage=final_usage,
            ),
        )
