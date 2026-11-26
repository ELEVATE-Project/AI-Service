"""Streamed Anthropic calls with mixed cache TTLs keep the 5m/1h write split.

Anthropic sends `usage.cache_creation` only in `message_start`. LiteLLM's real
stream wrapper builds the final usage chunk; the split must survive it.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone

from types import SimpleNamespace
from unittest.mock import AsyncMock

import litellm
import pytest

from src.llm_service.providers.litellm import LiteLLMTransport
from src.llm_service.schemas.chat import MessageParam, NormalisedLLMRequest
from src.shared.db.enums import KeyFormat
from src.shared.secrets.backend import TenantKeyPayload

KEY = TenantKeyPayload(key_format=KeyFormat.API_KEY, data={"api_key": "offline-test-key"})


def _sse_lines():
    events = [
        {"type": "message_start", "message": {
            "id": "msg_t", "type": "message", "role": "assistant", "model": "claude-haiku-4-5",
            "content": [], "usage": {
                "input_tokens": 1000, "output_tokens": 1,
                "cache_creation_input_tokens": 32000, "cache_read_input_tokens": 0,
                "cache_creation": {"ephemeral_5m_input_tokens": 12000, "ephemeral_1h_input_tokens": 20000},
            }}},
        {"type": "content_block_start", "index": 0, "content_block": {"type": "text", "text": ""}},
        {"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": "Answer"}},
        {"type": "content_block_stop", "index": 0},
        {"type": "message_delta", "delta": {"stop_reason": "end_turn", "stop_sequence": None},
         "usage": {"output_tokens": 10}},
        {"type": "message_stop"},
    ]
    return [f"data: {json.dumps(e)}" for e in events]


def _real_anthropic_wrapper(lines):
    from litellm.litellm_core_utils.litellm_logging import Logging
    from litellm.litellm_core_utils.streaming_handler import CustomStreamWrapper
    from litellm.llms.anthropic.chat.handler import ModelResponseIterator

    async def sse():
        for line in lines:
            yield line

    model = "claude-haiku-4-5"
    return CustomStreamWrapper(
        completion_stream=ModelResponseIterator(streaming_response=sse(), sync_stream=False),
        model=model, custom_llm_provider="anthropic",
        logging_obj=Logging(model=model, messages=[], stream=True, call_type="acompletion",
                            start_time=datetime.now(timezone.utc), litellm_call_id="x", function_id="x"),
        stream_options={"include_usage": True},
    )


@pytest.fixture
def real_anthropic_stream(monkeypatch):
    async def completion(**kwargs):
        assert kwargs.get("stream")
        return _real_anthropic_wrapper(_sse_lines())

    monkeypatch.setattr(litellm, "acompletion", completion)
    monkeypatch.setattr("litellm.litellm_core_utils.streaming_chunk_builder_utils.token_counter",
                        lambda **kwargs: 1)


def _request():
    return NormalisedLLMRequest(provider="anthropic", model="claude-haiku-4-5",
                                messages=[MessageParam(role="user", content="hi")])


@pytest.mark.asyncio
async def test_stream_keeps_mixed_ttl_write_split_through_litellm_wrapper(real_anthropic_stream):
    events = [e async for e in LiteLLMTransport().stream(_request(), KEY)]
    assert events[-1].type == "finish", events
    usage = events[-1].data.usage
    assert usage.input_tokens_cache_write == 32000
    assert usage.input_tokens_cache_write_5m == 12000
    assert usage.input_tokens_cache_write_1h == 20000


@pytest.mark.asyncio
async def test_stream_route_prices_and_ledgers_mixed_ttl_split(real_anthropic_stream, monkeypatch):
    """12k 5m + 20k 1h writes: split pricing (0.05605), not all at the 1h request-TTL rate (0.06505)."""
    from src.llm_service.api.rest.chat import chat_stream, registry
    from src.llm_service.schemas.chat import CacheOptions, ChatParams, ChatRequest
    from src.shared.guardrails.base import GuardrailsResult
    from tests.test_ledger_write import _FakeSession

    monkeypatch.setattr(registry, "resolve", lambda *args: LiteLLMTransport())
    db = _FakeSession()
    response = await chat_stream(
        body=ChatRequest(provider="anthropic", model="claude-haiku-4-5",
                         messages=[MessageParam(role="user", content="hi")],
                         params=ChatParams(cache_options=CacheOptions(ttl="1h"))),
        request=SimpleNamespace(headers={}), tenant=SimpleNamespace(id="tenant_acme"),
        secret_backend=SimpleNamespace(get_key=AsyncMock(return_value=KEY)),
        policy_checker=SimpleNamespace(check=AsyncMock()),
        guardrails=SimpleNamespace(check_input=AsyncMock(return_value=GuardrailsResult())),
        # acquire() returns None: no lock is held, so the route skips the cache write.
        cache=SimpleNamespace(get=AsyncMock(return_value=None), set=AsyncMock(),
                              acquire=AsyncMock(return_value=None), release=AsyncMock()), db=db,
    )
    events = [event async for event in response.body_iterator]
    payload = json.loads(next(e.split("data: ", 1)[1] for e in events if e.startswith("event: finish")))
    assert payload["usage"]["input_tokens_cache_write"] == 32000
    assert payload["cost"]["computed_usd"] == pytest.approx(0.05605)
    assert db.added[0].our_cost_usd == pytest.approx(0.05605)


def _usage_chunk(prompt, writes, split):
    from litellm.types.utils import CacheCreationTokenDetails, PromptTokensDetailsWrapper, Usage
    details = PromptTokensDetailsWrapper(cache_creation_tokens=writes)
    if split:
        details.cache_creation_token_details = CacheCreationTokenDetails(
            ephemeral_5m_input_tokens=split[0], ephemeral_1h_input_tokens=split[1])
    return {"usage": Usage(prompt_tokens=prompt, completion_tokens=1, cache_creation_input_tokens=writes,
                           prompt_tokens_details=details)}


def test_split_that_does_not_sum_to_aggregate_is_not_applied():
    from litellm.litellm_core_utils.streaming_chunk_builder_utils import ChunkProcessor
    chunks = [_usage_chunk(100, 32000, (1, 2)), _usage_chunk(100, 32000, None)]
    result = ChunkProcessor(chunks=chunks)._calculate_usage_per_chunk(chunks)
    assert result["cache_creation_input_tokens"] == 32000
    assert getattr(result["prompt_tokens_details"], "cache_creation_token_details", None) is None


def test_split_patch_is_in_place_and_idempotent():
    from litellm.litellm_core_utils.streaming_chunk_builder_utils import ChunkProcessor
    from src.llm_service.providers.litellm import _patch_stream_keeps_cache_write_ttl_split
    assert ChunkProcessor._calculate_usage_per_chunk._ai_service_patched is True
    patched = ChunkProcessor._calculate_usage_per_chunk
    _patch_stream_keeps_cache_write_ttl_split()
    assert ChunkProcessor._calculate_usage_per_chunk is patched
