from __future__ import annotations

import time
from collections.abc import AsyncIterator
from typing import Any, Optional

import litellm

from src.llm_service.providers.base import BaseLLMProvider, StreamEvent, TransportFinishData
from src.llm_service.schemas.chat import (
    CacheBlock, ChatResponse, Choice, ChoiceMessage,
    NormalisedLLMRequest, TokenData, ToolUseData, UsageBlock,
)
from src.shared.db.enums import KeyFormat, Transport
from src.shared.schemas.envelope import CostBlock, GuardrailsBlock, LatencyBlock, PolicyBlock
from src.shared.secrets.backend import TenantKeyPayload

# Providers where our name differs from LiteLLM's expected model prefix.
# All other provider names are passed through as-is (e.g. "groq", "gemini").
#
# custom_endpoint — any server that speaks the OpenAI wire protocol at a custom URL.
#   Covers HuggingFace Inference Endpoints, self-hosted TGI, vLLM, Ollama, etc.
#   Tenant stores: key_format=api_key, data={"api_key": "<token or 'none'>",
#                  "api_base": "https://xyz.us-east-1.aws.endpoints.huggingface.cloud"}
#   Request:       {"provider": "custom_endpoint", "model": "meta-llama/Llama-3-8b-instruct"}
_PROVIDER_REMAP: dict[str, str] = {
    "custom_endpoint": "openai",
}


class LiteLLMTransport(BaseLLMProvider):
    """Routes any provider through the LiteLLM in-process SDK."""

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

        # OpenAI prompt cache path
        if getattr(raw_usage, "prompt_tokens_details", None):
            cache_read = getattr(raw_usage.prompt_tokens_details, "cached_tokens", 0) or 0

        # Anthropic prompt cache path via LiteLLM (overrides OpenAI value if both present)
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
        print("model_str: ", model_str)
        messages = self._serialize_messages(request.messages, request.provider)
        print("messages: ", messages)
        credential_kwargs = self._credential_kwargs(key)
        param_kwargs = self._param_kwargs(request.params)
        print("param_kwargs: ", param_kwargs)

        tool_kwargs: dict[str, Any] = {}
        if request.tools:
            tool_kwargs["tools"] = [t.model_dump() for t in request.tools]
        if request.tool_choice is not None:
            tool_kwargs["tool_choice"] = request.tool_choice

        raw = await litellm.acompletion(
            model=model_str,
            messages=messages,
            drop_params=True,
            **tool_kwargs,
            **credential_kwargs,
            **param_kwargs,
        )

        usage = self._extract_usage(raw)
        choices = self._map_choices(raw)
        print("usage: ", usage)
        print("choices: ", choices)

        return ChatResponse(
            id="",
            created=int(time.time()),
            tenant_id="",
            provider=request.provider,
            model=request.model,
            transport=Transport.LITELLM,
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

        tool_kwargs: dict[str, Any] = {}
        if request.tools:
            tool_kwargs["tools"] = [t.model_dump() for t in request.tools]
        if request.tool_choice is not None:
            tool_kwargs["tool_choice"] = request.tool_choice

        response_stream = await litellm.acompletion(
            model=model_str,
            messages=messages,
            stream=True,
            stream_options={"include_usage": True},
            drop_params=True,
            **tool_kwargs,
            **credential_kwargs,
            **param_kwargs,
        )
        final_usage = UsageBlock()
        final_finish_reason = "stop"

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

            # Usage may arrive on the finish chunk (Anthropic) or a trailing
            # chunk after it (OpenAI with include_usage=True) — capture either.
            if getattr(chunk, "usage", None):
                final_usage = self._extract_usage(chunk)

        yield StreamEvent(
            type="finish",
            data=TransportFinishData(
                finish_reason=final_finish_reason,
                usage=final_usage,
            ),
        )