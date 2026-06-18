from __future__ import annotations

import asyncio
import io
import json
import random
import time
from collections.abc import AsyncIterator
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
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
from src.shared.db.enums import BatchJobStatus, KeyFormat, Transport
from src.shared.db.models import BatchJob
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
                    try:
                        return max(0.0, float(retry_after))
                    except (TypeError, ValueError):
                        dt = parsedate_to_datetime(retry_after)
                        if dt.tzinfo is None:
                            dt = dt.replace(tzinfo=timezone.utc)
                        return max(0.0, (dt - datetime.now(timezone.utc)).total_seconds())
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


def _extract_response_cost(raw: Any) -> Optional[float]:
    """Best-effort provider-reported response cost in USD.

    LiteLLM populates ``_hidden_params['response_cost']`` for providers that
    report cost (OpenRouter among them); OpenRouter also returns ``usage.cost``.
    Returns None when neither is available (caller falls back to YAML pricing).
    """
    hidden = getattr(raw, "_hidden_params", None)
    if isinstance(hidden, dict) and hidden.get("response_cost") is not None:
        try:
            return float(hidden["response_cost"])
        except (TypeError, ValueError):
            pass
    usage = getattr(raw, "usage", None)
    cost = getattr(usage, "cost", None) if usage is not None else None
    if cost is not None:
        try:
            return float(cost)
        except (TypeError, ValueError):
            pass
    return None


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
            kwargs = {
                "aws_access_key_id": key.data["access_key_id"],
                "aws_secret_access_key": key.data["secret_access_key"],
                "aws_region_name": key.data.get("region", "us-east-1"),
            }
            for optional_key in ("aws_session_token", "aws_role_name", "s3_bucket_name"):
                if key.data.get(optional_key):
                    kwargs[optional_key] = key.data[optional_key]
            return kwargs
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
        if params.web_search_options is not None:
            kwargs["web_search_options"] = params.web_search_options.model_dump(exclude_none=True)
        return kwargs

    def _openrouter_kwargs(self, request: NormalisedLLMRequest) -> dict[str, Any]:
        """Map provider_options to LiteLLM extra_body / extra_headers for OpenRouter.

        OpenRouter reads routing prefs (``provider``) and a fallback list
        (``models``) from the request body, and app attribution from the
        HTTP-Referer / X-Title headers. Returns {} for any other provider.
        """
        if request.provider != "openrouter":
            return {}
        options = request.provider_options or {}

        extra_body: dict[str, Any] = {}
        if options.get("provider") is not None:
            extra_body["provider"] = options["provider"]
        if options.get("models") is not None:
            extra_body["models"] = options["models"]

        referer = options.get("referer") or settings.openrouter_app_url
        title = options.get("title") or settings.openrouter_app_title
        extra_headers: dict[str, str] = {}
        if referer:
            extra_headers["HTTP-Referer"] = referer
        if title:
            extra_headers["X-Title"] = title

        kwargs: dict[str, Any] = {}
        if extra_body:
            kwargs["extra_body"] = extra_body
        if extra_headers:
            kwargs["extra_headers"] = extra_headers
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
            provider_fields = getattr(raw_message, "provider_specific_fields", None) or {}
            citations = provider_fields.get("citations") or None
            choices.append(Choice(
                index=raw_choice.index,
                message=ChoiceMessage(
                    role=raw_message.role,
                    content=raw_message.content,
                    tool_calls=tool_calls,
                    citations=citations,
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
            tool_kwargs["tools"] = [t.model_dump(exclude_none=True) for t in request.tools]
        if request.tool_choice is not None:
            tool_kwargs["tool_choice"] = request.tool_choice

        fallback_kwargs: dict[str, Any] = {}
        fallbacks = _build_fallbacks(model_str, self._regions, credential_kwargs)
        if fallbacks:
            fallback_kwargs["fallbacks"] = fallbacks

        openrouter_kwargs = self._openrouter_kwargs(request)

        raw = None
        for attempt in range(settings.llm_retry_max_attempts):
            try:
                raw = await litellm.acompletion(
                    model=model_str, messages=messages, drop_params=True,
                    **tool_kwargs, **credential_kwargs, **param_kwargs,
                    **fallback_kwargs, **openrouter_kwargs,
                )
                break
            except Exception as error:
                print(f"[litellm.chat] attempt={attempt} error_type={type(error).__name__} error={error}")
                if not _should_retry(error) or attempt == settings.llm_retry_max_attempts - 1:
                    raise _wrap_litellm_error(error) from error
                await asyncio.sleep(_retry_delay(error, attempt))

        usage = self._extract_usage(raw)
        choices = self._map_choices(raw)
        reported_cost = _extract_response_cost(raw)

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
            cost=CostBlock(provider_reported_usd=reported_cost),
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
            tool_kwargs["tools"] = [t.model_dump(exclude_none=True) for t in request.tools]
        if request.tool_choice is not None:
            tool_kwargs["tool_choice"] = request.tool_choice

        fallback_kwargs: dict[str, Any] = {}
        fallbacks = _build_fallbacks(model_str, self._regions, credential_kwargs)
        if fallbacks:
            fallback_kwargs["fallbacks"] = fallbacks

        openrouter_kwargs = self._openrouter_kwargs(request)

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
                    **openrouter_kwargs,
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
        final_citations: Optional[list[Any]] = None
        reported_cost: Optional[float] = None
        all_chunks: list[Any] = []

        # Once iteration starts we cannot retry — emit an error event on failure.
        try:
            async for chunk in response_stream:
                all_chunks.append(chunk)

                if not chunk.choices:
                    if getattr(chunk, "usage", None):
                        final_usage = self._extract_usage(chunk)
                        chunk_cost = _extract_response_cost(chunk)
                        if chunk_cost is not None:
                            reported_cost = chunk_cost
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
                    chunk_cost = _extract_response_cost(chunk)
                    if chunk_cost is not None:
                        reported_cost = chunk_cost

        except Exception as stream_error:
            yield StreamEvent(
                type="error",
                data=ErrorData(
                    code="upstream_disconnected", message=str(stream_error),
                    upstream_status=_upstream_status(stream_error),
                ),
            )
            return

        # Citations are only on the assembled response, not individual chunks.
        # stream_chunk_builder rebuilds the full ModelResponse from collected chunks.
        if all_chunks:
            try:
                assembled = litellm.stream_chunk_builder(all_chunks)
                if assembled and assembled.choices:
                    psf = getattr(assembled.choices[0].message, "provider_specific_fields", None) or {}
                    if isinstance(psf, dict):
                        citations = psf.get("citations") or psf.get("web_search_results") or None
                        if citations and not isinstance(citations, list):
                            citations = [citations]
                        final_citations = citations or None
                    if reported_cost is None:
                        reported_cost = _extract_response_cost(assembled)
            except Exception:
                pass

        yield StreamEvent(
            type="finish",
            data=TransportFinishData(
                finish_reason=final_finish_reason,
                usage=final_usage,
                citations=final_citations,
                provider_reported_usd=reported_cost,
            ),
        )

    async def batch_submit(self, jobs: list[BatchJob], key: TenantKeyPayload) -> None:
        provider = jobs[0].provider
        credential_kwargs = self._credential_kwargs(key)
        lines = []
        for job in jobs:
            req = job.normalised_request
            body: dict[str, Any] = {"model": req["model"], "messages": req["messages"]}
            if req.get("tools"):
                body["tools"] = req["tools"]
            if req.get("tool_choice") is not None:
                body["tool_choice"] = req["tool_choice"]
            for param_key in ("temperature", "max_tokens", "top_p", "stop", "seed"):
                value = (req.get("params") or {}).get(param_key)
                if value is not None:
                    body[param_key] = value
            lines.append(json.dumps({
                "custom_id": str(job.id), "method": "POST",
                "url": "/v1/chat/completions", "body": body,
            }))
        jsonl_bytes = "\n".join(lines).encode()
        file_obj = await litellm.acreate_file(
            file=("batch.jsonl", io.BytesIO(jsonl_bytes), "application/jsonl"),
            purpose="batch",
            custom_llm_provider=provider,
            **credential_kwargs,
        )
        batch = await litellm.acreate_batch(
            input_file_id=file_obj.id,
            endpoint="/v1/chat/completions",
            completion_window="24h",
            custom_llm_provider=provider,
            **credential_kwargs,
        )
        submitted_at = datetime.now(timezone.utc)
        for job in jobs:
            job.status = BatchJobStatus.SUBMITTED
            job.upstream_batch_id = batch.id
            job.upstream_file_id = file_obj.id
            job.submitted_at = submitted_at

    async def batch_poll(
        self, upstream_batch_id: str, jobs: list[BatchJob], key: TenantKeyPayload,
    ) -> None:
        provider = jobs[0].provider
        credential_kwargs = self._credential_kwargs(key)
        batch = await litellm.aretrieve_batch(
            batch_id=upstream_batch_id,
            custom_llm_provider=provider,
            **credential_kwargs,
        )
        if batch.status not in ("completed", "failed", "cancelled", "expired"):
            return
        jobs_by_id = {str(job.id): job for job in jobs}
        completed_at = datetime.now(timezone.utc)
        if batch.status == "completed" and batch.output_file_id:
            content = await litellm.afile_content(
                file_id=batch.output_file_id,
                custom_llm_provider=provider,
                **credential_kwargs,
            )
            for line in content.text.splitlines():
                if not line.strip():
                    continue
                result_item = json.loads(line)
                job = jobs_by_id.get(result_item.get("custom_id", ""))
                if job is None:
                    continue
                if result_item.get("error"):
                    job.status = BatchJobStatus.FAILED
                    job.error_code = result_item["error"].get("code", "upstream_error")
                else:
                    result_body = result_item["response"]["body"]
                    raw_usage = result_body.get("usage", {})
                    job.result = ChatResponse(
                        id=job.request_id, created=int(time.time()), tenant_id=job.tenant_id,
                        provider=job.provider, model=job.model, transport=Transport.LITELLM,
                        choices=[
                            Choice(
                                index=c["index"],
                                message=ChoiceMessage(
                                    role=c["message"]["role"],
                                    content=c["message"].get("content"),
                                    tool_calls=c["message"].get("tool_calls"),
                                ),
                                finish_reason=c.get("finish_reason") or "stop",
                            )
                            for c in result_body.get("choices", [])
                        ],
                        usage=UsageBlock(
                            input_tokens=raw_usage.get("prompt_tokens", 0),
                            output_tokens=raw_usage.get("completion_tokens", 0),
                            total_tokens=raw_usage.get("total_tokens", 0),
                        ),
                        cost=CostBlock(), latency_ms=LatencyBlock(),
                        cache=CacheBlock(our_cache_hit=False),
                        guardrails=GuardrailsBlock(), policy=PolicyBlock(),
                    ).model_dump()
                    job.status = BatchJobStatus.COMPLETE
                job.completed_at = completed_at
        else:
            for job in jobs:
                job.status = BatchJobStatus.FAILED
                job.error_code = f"batch_{batch.status}"
                job.completed_at = completed_at
