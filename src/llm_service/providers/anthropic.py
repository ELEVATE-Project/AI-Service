from __future__ import annotations

import json
import time
from collections.abc import AsyncIterator
from datetime import datetime, timezone
from typing import Any, Optional

import anthropic

from src.llm_service.providers.base import BaseLLMProvider, StreamEvent
from src.llm_service.schemas.chat import (
    CacheBlock, ChatResponse, Choice, ChoiceMessage, NormalisedLLMRequest, UsageBlock,
)
from src.shared.db.enums import BatchJobStatus, Transport
from src.shared.db.models import BatchJob
from src.shared.schemas.envelope import CostBlock, GuardrailsBlock, LatencyBlock, PolicyBlock
from src.shared.secrets.backend import TenantKeyPayload


class AnthropicTransport(BaseLLMProvider):
    """Direct Anthropic SDK transport.
    """

    def __init__(self, regions: Optional[list[str]] = None) -> None:
        pass

    async def chat(self, request: NormalisedLLMRequest, key: TenantKeyPayload) -> ChatResponse:
        raise NotImplementedError("AnthropicTransport does not handle chat — routed via LiteLLMTransport")

    async def stream(
        self, request: NormalisedLLMRequest, key: TenantKeyPayload
    ) -> AsyncIterator[StreamEvent]:
        raise NotImplementedError("AnthropicTransport does not handle stream — routed via LiteLLMTransport")

    async def batch_submit(self, jobs: list[BatchJob], key: TenantKeyPayload) -> None:
        client = anthropic.AsyncAnthropic(api_key=key.data["api_key"])
        requests = []
        for job in jobs:
            req = job.normalised_request
            params = req.get("params") or {}
            message_params: dict[str, Any] = {
                "model": req["model"],
                "messages": req["messages"],
                "max_tokens": params.get("max_tokens") or 1024,
            }
            if req.get("tools"):
                message_params["tools"] = req["tools"]
            for param_key in ("temperature", "top_p"):
                if params.get(param_key) is not None:
                    message_params[param_key] = params[param_key]
            requests.append({"custom_id": str(job.id), "params": message_params})
        batch = await client.messages.batches.create(requests=requests)
        submitted_at = datetime.now(timezone.utc)
        for job in jobs:
            job.status = BatchJobStatus.SUBMITTED
            job.upstream_batch_id = batch.id
            job.submitted_at = submitted_at

    async def batch_poll(
        self, upstream_batch_id: str, jobs: list[BatchJob], key: TenantKeyPayload,
    ) -> None:
        client = anthropic.AsyncAnthropic(api_key=key.data["api_key"])
        batch = await client.messages.batches.retrieve(upstream_batch_id)
        if batch.processing_status != "ended":
            return
        jobs_by_id = {str(job.id): job for job in jobs}
        completed_at = datetime.now(timezone.utc)
        async for result_item in await client.messages.batches.results(upstream_batch_id):
            job = jobs_by_id.get(result_item.custom_id)
            if job is None:
                continue
            if result_item.result.type == "succeeded":
                message = result_item.result.message
                content_text = None
                tool_calls = None
                for block in (message.content or []):
                    if getattr(block, "type", None) == "text":
                        content_text = block.text
                    elif getattr(block, "type", None) == "tool_use":
                        if tool_calls is None:
                            tool_calls = []
                        tool_calls.append({
                            "id": block.id, "type": "function",
                            "function": {"name": block.name, "arguments": json.dumps(block.input)},
                        })
                input_tokens = getattr(message.usage, "input_tokens", 0) or 0
                output_tokens = getattr(message.usage, "output_tokens", 0) or 0
                job.result = ChatResponse(
                    id=job.request_id, created=int(time.time()), tenant_id=job.tenant_id,
                    provider=job.provider, model=job.model, transport=Transport.DIRECT,
                    choices=[Choice(
                        index=0,
                        message=ChoiceMessage(role="assistant", content=content_text, tool_calls=tool_calls),
                        finish_reason=getattr(message, "stop_reason", None) or "stop",
                    )],
                    usage=UsageBlock(
                        input_tokens=input_tokens, output_tokens=output_tokens,
                        total_tokens=input_tokens + output_tokens,
                    ),
                    cost=CostBlock(), latency_ms=LatencyBlock(),
                    cache=CacheBlock(our_cache_hit=False),
                    guardrails=GuardrailsBlock(), policy=PolicyBlock(),
                ).model_dump()
                job.status = BatchJobStatus.COMPLETE
            else:
                job.status = BatchJobStatus.FAILED
                job.error_code = f"batch_{result_item.result.type}"
            job.completed_at = completed_at
