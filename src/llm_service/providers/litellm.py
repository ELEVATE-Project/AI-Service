from __future__ import annotations

import asyncio
import inspect
import io
import json
import math
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
    CACHE_CAPABLE_PROVIDERS, CACHE_TARGET_DEFAULT, CacheBlock, ChatResponse, Choice,
    ChoiceMessage, ErrorData, NormalisedLLMRequest, TokenData, ToolUseData, UsageBlock,
)
from src.shared.config import settings
from src.shared.db.enums import BatchJobStatus, KeyFormat, Transport
from src.shared.db.models import BatchJob
from src.shared.schemas.envelope import CostBlock, GuardrailsBlock, LatencyBlock, PolicyBlock
from src.shared.secrets.backend import TenantKeyPayload

_PROVIDER_REMAP: dict[str, str] = {
    "custom_endpoint": "http://<endpoint>",
}

_WEB_SEARCH_CONTEXT_SIZE_TO_MAX_RESULTS: dict[str, int] = {
    "low": 3,
    "medium": 5,
    "high": 8,
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


def _patch_anthropic_hidden_original_response() -> None:
    """Work around a LiteLLM bug (confirmed against litellm==1.83.0) that discards the
    raw, ordered Anthropic content-block array before ``litellm.acompletion()`` ever
    returns it to us.

    ``AnthropicConfig.transform_response`` sets it, then two lines later clobbers it:

        model_response._hidden_params["original_response"] = completion_response["content"]
        ...
        _hidden_params["provider_specific_fields"] = provider_specific_fields
        model_response._hidden_params = _hidden_params  # <- different dict, wipes the line above

    ``_dedupe_leading_narration`` below needs those raw blocks to undo LiteLLM's lossy
    text-flattening for interleaved server-tool responses (e.g. native ``web_search``).
    We can't read this from ``_hidden_params`` after the fact — LiteLLM's async logging
    callback does get the raw body, but it's dispatched to a background worker with no
    guaranteed ordering relative to when ``acompletion()`` returns, so there's no
    race-free way to consume it there either. Instead, wrap the (buggy) method itself:
    call the original unchanged, then re-derive the same data from the same
    ``raw_response`` httpx.Response it was already given, and restore it onto the
    ``model_response`` it already produced — after LiteLLM's own clobber has happened.

    Idempotent (safe under uvicorn --reload) and fails silently if LiteLLM's internals
    no longer match this shape — in which case ``_dedupe_leading_narration`` just falls
    back to returning the unmodified (still-flattened) content, exactly as it did before
    this patch existed.
    """
    try:
        from litellm.llms.anthropic.chat.transformation import AnthropicConfig
        if getattr(AnthropicConfig.transform_response, "_ai_service_patched", False):
            return
        original = AnthropicConfig.transform_response
        signature = inspect.signature(original)
    except (ImportError, AttributeError, TypeError, ValueError):
        return

    def _patched(self: Any, *args: Any, **kwargs: Any) -> Any:
        model_response = original(self, *args, **kwargs)
        try:
            hidden = getattr(model_response, "_hidden_params", None)
            if isinstance(hidden, dict) and "original_response" not in hidden:
                bound = signature.bind(self, *args, **kwargs)
                bound.apply_defaults()
                raw_response = bound.arguments.get("raw_response")
                if raw_response is not None:
                    hidden["original_response"] = raw_response.json().get("content")
        except Exception:
            pass
        return model_response

    _patched._ai_service_patched = True  # type: ignore[attr-defined]
    AnthropicConfig.transform_response = _patched


_patch_anthropic_hidden_original_response()


def _dedupe_leading_narration(raw: Any, flattened_content: Optional[str]) -> Optional[str]:
    """Undo LiteLLM's lossy flattening of interleaved Anthropic content blocks.

    A server-side tool (e.g. native ``web_search``) runs mid-generation, so a single
    Anthropic turn can look like ``[text, server_tool_use, tool_result, text]`` — narration
    before the search, then the real answer after it. LiteLLM's Anthropic adapter
    concatenates every ``text`` block into one string with no boundary marker
    (``litellm/llms/anthropic/chat/transformation.py:extract_response_content``), so that
    leading narration ends up glued onto the final answer.

    The pre-flatten block array survives on ``raw._hidden_params["original_response"]``.
    Group its blocks into runs of consecutive ``text`` blocks; if more than one run exists,
    keep only the last one (the model's true final answer). A single run — including the
    ordinary "narration ending in a client tool_use" pattern, where the tool call is the
    last block and there's nothing after it — is left untouched.
    """
    hidden = getattr(raw, "_hidden_params", None)
    original_blocks = hidden.get("original_response") if isinstance(hidden, dict) else None
    if not isinstance(original_blocks, list):
        return flattened_content

    text_runs: list[str] = []
    current_run = ""
    in_run = False
    for block in original_blocks:
        if not isinstance(block, dict):
            continue
        if block.get("type") == "text":
            current_run += block.get("text") or ""
            in_run = True
        elif in_run:
            text_runs.append(current_run)
            current_run = ""
            in_run = False
    if in_run:
        text_runs.append(current_run)

    if len(text_runs) <= 1:
        return flattened_content

    final_answer = text_runs[-1].strip()
    return final_answer or flattened_content


def _cache_control_block(ttl: Optional[str]) -> dict[str, Any]:
    block: dict[str, Any] = {"type": "ephemeral"}
    if ttl:
        block["ttl"] = ttl
    return block


def _extract_response_cost(raw: Any) -> Optional[float]:
    """Best-effort provider-reported response cost in USD.

    LiteLLM populates ``_hidden_params['response_cost']`` for providers that
    report cost (OpenRouter among them); OpenRouter also returns ``usage.cost``.
    Returns None when neither is available (caller falls back to YAML pricing).
    """
    hidden = getattr(raw, "_hidden_params", None)
    if isinstance(hidden, dict) and hidden.get("response_cost") is not None:
        try:
            value = float(hidden["response_cost"])
            if math.isfinite(value) and value >= 0:
                return value
        except (TypeError, ValueError, OverflowError):
            pass
    usage = getattr(raw, "usage", None)
    cost = getattr(usage, "cost", None) if usage is not None else None
    if cost is not None:
        try:
            value = float(cost)
            if math.isfinite(value) and value >= 0:
                return value
        except (TypeError, ValueError, OverflowError):
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

        OpenRouter reads routing prefs (``provider``), a fallback list
        (``models``), and plugin config (``plugins``, e.g. the web search
        plugin) from the request body, and app attribution from the
        HTTP-Referer / X-Title headers. Returns {} for any other provider.

        If the caller set the provider-agnostic ``params.web_search_options``
        but didn't already pass an explicit ``plugins`` override, the web
        search plugin is synthesised automatically so callers don't need to
        know OpenRouter's plugin format.
        """
        if request.provider != "openrouter":
            return {}
        options = request.provider_options or {}

        extra_body: dict[str, Any] = {}
        if options.get("provider") is not None:
            extra_body["provider"] = options["provider"]
        if options.get("models") is not None:
            extra_body["models"] = options["models"]
        if options.get("plugins") is not None:
            extra_body["plugins"] = options["plugins"]
        elif request.params is not None and request.params.web_search_options is not None:
            web_plugin: dict[str, Any] = {"id": "web"}
            context_size = request.params.web_search_options.search_context_size
            if context_size in _WEB_SEARCH_CONTEXT_SIZE_TO_MAX_RESULTS:
                web_plugin["max_results"] = _WEB_SEARCH_CONTEXT_SIZE_TO_MAX_RESULTS[context_size]
            extra_body["plugins"] = [web_plugin]

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

    def _cache_options_state(
        self, provider: str, params: Optional[Any]
    ) -> tuple[bool, set[str], Optional[str]]:
        """Resolve (auto_enabled, targets, ttl) for provider-side prompt caching.

        ``auto_enabled`` is False whenever the provider doesn't support prompt
        caching at all, regardless of what the caller asked for.
        """
        cache_options = getattr(params, "cache_options", None) if params else None
        if provider not in CACHE_CAPABLE_PROVIDERS or not cache_options or not cache_options.enabled:
            return False, set(), (cache_options.ttl if cache_options else None)
        targets = set(cache_options.targets) if cache_options.targets else set(CACHE_TARGET_DEFAULT)
        return True, targets, cache_options.ttl

    def _serialize_messages(
        self, messages: list[Any], provider: str, params: Optional[Any] = None
    ) -> list[dict[str, Any]]:
        auto, targets, ttl = self._cache_options_state(provider, params)
        auto_prompt = auto and "prompt" in targets
        last_system_idx = None
        if auto_prompt:
            for i, message in enumerate(messages):
                if message.role == "system":
                    last_system_idx = i

        result: list[dict[str, Any]] = []
        for i, message in enumerate(messages):
            serialised = message.model_dump(exclude={"cache"}, exclude_none=True)
            should_cache = message.cache == "ephemeral" or i == last_system_idx
            if not should_cache or provider not in CACHE_CAPABLE_PROVIDERS:
                result.append(serialised)
                continue

            # Anthropic (direct/Bedrock) requires cache_control nested in a content
            # block; OpenRouter takes it as a top-level key and relocates it itself
            # (litellm/llms/openrouter/chat/transformation.py:_move_cache_control_to_content),
            # silently dropping it for models that don't support it.
            if provider == "openrouter":
                serialised["cache_control"] = _cache_control_block(ttl)
            elif isinstance(message.content, str):
                serialised["content"] = [
                    {
                        "type": "text",
                        "text": message.content,
                        "cache_control": _cache_control_block(ttl),
                    }
                ]
            result.append(serialised)
        return result

    def _serialize_tools(
        self, tools: Optional[list[Any]], provider: str, params: Optional[Any] = None
    ) -> Optional[list[dict[str, Any]]]:
        if not tools:
            return None
        serialised = [t.model_dump(exclude_none=True) for t in tools]
        auto, targets, ttl = self._cache_options_state(provider, params)
        if auto and "tools" in targets:
            # Anthropic allows one cache_control breakpoint per tool list; LiteLLM
            # accepts it as a top-level key on the last tool dict for both the direct
            # Anthropic transform and (for supported models) OpenRouter's passthrough.
            serialised[-1]["cache_control"] = _cache_control_block(ttl)
        return serialised

    def _extract_usage(self, raw: Any) -> UsageBlock:
        raw_usage = getattr(raw, "usage", None)
        if raw_usage is None:
            return UsageBlock()

        cache_read = 0
        cache_write = 0

        if getattr(raw_usage, "prompt_tokens_details", None):
            cache_read = getattr(raw_usage.prompt_tokens_details, "cached_tokens", 0) or 0
            cache_write = getattr(raw_usage.prompt_tokens_details, "cache_write_tokens", 0) or 0

        # Anthropic's direct transport reports these as top-level attributes;
        # OpenRouter (and other OpenAI-shaped providers) nest them under
        # prompt_tokens_details instead (checked above) — prefer whichever is present.
        cache_read = getattr(raw_usage, "cache_read_input_tokens", cache_read) or cache_read
        cache_write = getattr(raw_usage, "cache_creation_input_tokens", cache_write) or cache_write

        return UsageBlock(
            input_tokens=getattr(raw_usage, "prompt_tokens", 0) or 0,
            output_tokens=getattr(raw_usage, "completion_tokens", 0) or 0,
            input_tokens_cache_read=cache_read or None,
            input_tokens_cache_write=cache_write or None,
            total_tokens=getattr(raw_usage, "total_tokens", 0) or 0,
        )

    def _normalize_citations(self, raw_message: Any) -> Optional[list[Any]]:
        """Anthropic's shape is List[List[{type, url, title, cited_text, ...}]] via
        provider_specific_fields. OpenRouter/OpenAI web search instead returns a flat
        List[{type: "url_citation", url_citation: {url, title, content, ...}}] via
        `annotations` — reshape it to match Anthropic's so callers need only one shape.
        """
        provider_fields = getattr(raw_message, "provider_specific_fields", None) or {}
        provider_citations = provider_fields.get("citations") or provider_fields.get("web_search_results")
        if provider_citations:
            return provider_citations if isinstance(provider_citations, list) else [provider_citations]

        annotations = getattr(raw_message, "annotations", None)
        if not annotations:
            return None
        normalized = [
            {
                "type": annotation.get("type", "url_citation"),
                "url": (annotation.get("url_citation") or {}).get("url"),
                "title": (annotation.get("url_citation") or {}).get("title"),
                "cited_text": (annotation.get("url_citation") or {}).get("content"),
            }
            for annotation in annotations
            if annotation.get("type") == "url_citation"
        ]
        return [normalized] if normalized else None

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
            citations = self._normalize_citations(raw_message)
            content = _dedupe_leading_narration(raw, raw_message.content)
            choices.append(Choice(
                index=raw_choice.index,
                message=ChoiceMessage(
                    role=raw_message.role,
                    content=content,
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
        messages = self._serialize_messages(request.messages, request.provider, request.params)
        credential_kwargs = self._credential_kwargs(key)
        param_kwargs = self._param_kwargs(request.params)

        if self._regions:
            if "aws_region_name" in credential_kwargs:
                credential_kwargs["aws_region_name"] = self._regions[0]
            elif "api_base" in credential_kwargs:
                credential_kwargs["api_base"] = self._regions[0]

        tool_kwargs: dict[str, Any] = {}
        serialised_tools = self._serialize_tools(request.tools, request.provider, request.params)
        if serialised_tools is not None:
            tool_kwargs["tools"] = serialised_tools
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
        messages = self._serialize_messages(request.messages, request.provider, request.params)
        credential_kwargs = self._credential_kwargs(key)
        param_kwargs = self._param_kwargs(request.params)

        if self._regions:
            if "aws_region_name" in credential_kwargs:
                credential_kwargs["aws_region_name"] = self._regions[0]
            elif "api_base" in credential_kwargs:
                credential_kwargs["api_base"] = self._regions[0]

        tool_kwargs: dict[str, Any] = {}
        serialised_tools = self._serialize_tools(request.tools, request.provider, request.params)
        if serialised_tools is not None:
            tool_kwargs["tools"] = serialised_tools
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
                    assembled_message = assembled.choices[0].message
                    final_citations = self._normalize_citations(assembled_message)
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
