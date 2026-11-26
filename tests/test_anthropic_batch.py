"""Direct-Anthropic batch path: assistant ``thinking_blocks`` are moved into the
native ``content`` list before ``messages.batches.create``. The SDK client is mocked;
no network and no paid call happens here.
"""
from __future__ import annotations

import copy
import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from src.llm_service.providers import anthropic as anthropic_mod
from src.llm_service.providers.anthropic import AnthropicTransport
from src.llm_service.schemas.chat import ChatParams, MessageParam, NormalisedLLMRequest
from src.shared.db.enums import BatchJobStatus, KeyFormat
from src.shared.secrets.backend import TenantKeyPayload

KEY = TenantKeyPayload(key_format=KeyFormat.API_KEY, data={"api_key": "offline-test-key"})
THINKING_BLOCKS = [
    {"type": "thinking", "thinking": "plan the lookup", "signature": "sig-1"},
    {"type": "redacted_thinking", "data": "opaque"},
]

def make_job(messages, *, tools=None, params=None):
    req = NormalisedLLMRequest(
        provider="anthropic", model="claude-haiku-5-5",
        messages=messages, tools=tools, params=params,
    )
    return SimpleNamespace(
        id=uuid.uuid4(), normalised_request=req.model_dump(exclude_none=True),
        status=None, upstream_batch_id=None, submitted_at=None,
    )


async def submit(monkeypatch, job):
    client = MagicMock()
    client.messages.batches.create = AsyncMock(return_value=SimpleNamespace(id="msgbatch_1"))
    monkeypatch.setattr(anthropic_mod.anthropic, "AsyncAnthropic", MagicMock(return_value=client))
    await AnthropicTransport().batch_submit([job], KEY)
    requests = client.messages.batches.create.await_args.kwargs["requests"]
    assert len(requests) == 1 and requests[0]["custom_id"] == str(job.id)
    assert job.status == BatchJobStatus.SUBMITTED and job.upstream_batch_id == "msgbatch_1"
    return requests[0]["params"]


@pytest.mark.asyncio
async def test_thinking_blocks_move_into_content_first(monkeypatch):
    job = make_job([
        MessageParam(role="user", content="hi"),
        MessageParam(role="assistant", content="let me check",
                     thinking_blocks=copy.deepcopy(THINKING_BLOCKS)),
        MessageParam(role="user", content="and then?"),
    ])
    body = await submit(monkeypatch, job)
    assert body["messages"] == [
        {"role": "user", "content": "hi"},
        {"role": "assistant", "content": [*THINKING_BLOCKS, {"type": "text", "text": "let me check"}]},
        {"role": "user", "content": "and then?"},
    ]
    assert all("thinking_blocks" not in m for m in body["messages"])


@pytest.mark.asyncio
async def test_thinking_blocks_without_text(monkeypatch):
    job = make_job([
        MessageParam(role="user", content="hi"),
        MessageParam(role="assistant", thinking_blocks=copy.deepcopy(THINKING_BLOCKS)),
    ])
    body = await submit(monkeypatch, job)
    assert body["messages"][1] == {"role": "assistant", "content": THINKING_BLOCKS}


@pytest.mark.asyncio
async def test_params_still_forwarded(monkeypatch):
    job = make_job(
        [MessageParam(role="user", content="hi")],
        params=ChatParams(max_tokens=77, temperature=0.2, thinking={"type": "adaptive"},
                          output_config={"effort": "low"}),
    )
    body = await submit(monkeypatch, job)
    assert body["model"] == "claude-haiku-5-5" and body["max_tokens"] == 77
    assert body["temperature"] == 0.2
    assert body["thinking"] == {"type": "adaptive"} and body["output_config"] == {"effort": "low"}
