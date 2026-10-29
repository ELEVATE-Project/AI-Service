"""T04 call metadata on ledger rows; all upstream calls and DB writes are mocked."""
from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import httpx
import pytest
from fastapi import FastAPI

from src.llm_service.api.rest import chat as chat_api
from src.llm_service.providers.base import StreamEvent, TransportFinishData, UpstreamTransportError
from src.llm_service.schemas.chat import CacheBlock, ErrorData, ChatResponse, Choice, ChoiceMessage, UsageBlock
from src.shared.db.enums import KeyFormat
from src.shared.guardrails.base import GuardrailsResult
from src.shared.schemas.envelope import CostBlock, GuardrailsBlock, LatencyBlock, PolicyBlock
from src.shared.secrets.backend import TenantKeyPayload

METADATA = {
    "session_id": "sess-42", "bot_id": 7, "bot_history_id": None, "turn_id": "t-1",
    "call_kind": "answer", "language": "hi",
    "unknown_key": "dropped", "turn_text": "How do I teach fractions?",
}
STORED = {"session_id": "sess-42", "bot_id": 7, "turn_id": "t-1", "call_kind": "answer", "language": "hi"}


def _response():
    usage = UsageBlock(input_tokens=1000, output_tokens=100, total_tokens=1100)
    return ChatResponse(
        id="upstream-id", created=0, tenant_id="test-tenant", provider="anthropic",
        model="claude-haiku-5-5", transport="mock", choices=[Choice(
            index=0, message=ChoiceMessage(role="assistant", content="hello"), finish_reason="stop",
        )], usage=usage, cost=CostBlock(), cache=CacheBlock(), guardrails=GuardrailsBlock(),
        latency_ms=LatencyBlock(), policy=PolicyBlock(),
    )


@pytest.fixture
def gateway(monkeypatch):
    def build(*, cached=None, chat_error=None, stream_error=None):
        response = _response()

        async def stream(*args):
            if stream_error:
                yield StreamEvent("error", ErrorData(code=stream_error, message="upstream failed"))
                return
            yield StreamEvent("finish", TransportFinishData(finish_reason="stop", usage=response.usage))

        chat = AsyncMock(side_effect=chat_error) if chat_error else AsyncMock(return_value=response)
        transport = SimpleNamespace(chat=chat, stream=Mock(side_effect=stream))
        monkeypatch.setattr(chat_api.registry, "resolve", Mock(return_value=transport))
        entries = []
        db = SimpleNamespace(add=Mock(side_effect=entries.append), commit=AsyncMock(), rollback=AsyncMock())
        cache = SimpleNamespace(get=AsyncMock(return_value=cached), set=AsyncMock())
        key = TenantKeyPayload(key_format=KeyFormat.API_KEY, data={"api_key": "test-only"})
        app = FastAPI()
        app.include_router(chat_api.router)

        def override(value):
            async def dependency():
                return value
            return dependency

        app.dependency_overrides.update({
            chat_api.get_tenant: override(SimpleNamespace(id="test-tenant")),
            chat_api.get_secret_backend: override(SimpleNamespace(get_key=AsyncMock(return_value=key))),
            chat_api.get_policy_checker: override(SimpleNamespace(check=AsyncMock())),
            chat_api.get_guardrails: override(SimpleNamespace(check_input=AsyncMock(return_value=GuardrailsResult()))),
            chat_api.get_cache: override(cache),
            chat_api.get_db: override(db),
        })

        async def send(streaming=False, metadata=None):
            body = {"provider": "anthropic", "model": "claude-haiku-5-5",
                    "messages": [{"role": "user", "content": "hi"}]}
            if metadata is not None:
                body["metadata"] = metadata
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
                return await client.post("/v1/chat/stream" if streaming else "/v1/chat", json=body)

        return SimpleNamespace(send=send, entries=entries, transport=transport)

    return build


CASES = {
    "chat success": dict(streaming=False),
    "chat cache hit": dict(streaming=False, cached=True),
    "chat error": dict(streaming=False, chat_error=UpstreamTransportError(
        code="upstream_timeout", message="timeout", http_status=504)),
    "stream success": dict(streaming=True),
    "stream cache hit": dict(streaming=True, cached=True),
    "stream error": dict(streaming=True, stream_error="upstream_timeout"),
}


@pytest.mark.asyncio
@pytest.mark.parametrize("case", CASES)
async def test_every_ledger_path_stores_call_metadata(gateway, case):
    options = dict(CASES[case])
    streaming = options.pop("streaming")
    if options.pop("cached", False):
        options["cached"] = _response()
    gw = gateway(**options)
    await gw.send(streaming=streaming, metadata=METADATA)
    assert len(gw.entries) == 1
    entry = gw.entries[0]
    assert entry.call_metadata == STORED
    assert entry.session_id == "sess-42"


@pytest.mark.asyncio
@pytest.mark.parametrize("streaming", [False, True])
async def test_request_without_metadata_works_as_before(gateway, streaming):
    gw = gateway()
    without = await gw.send(streaming=streaming)
    with_metadata = await gw.send(streaming=streaming, metadata=METADATA)
    assert without.status_code == 200
    assert with_metadata.status_code == 200
    if not streaming:
        assert without.json().keys() == with_metadata.json().keys()
    assert gw.entries[0].call_metadata is None
    assert gw.entries[0].session_id is None


@pytest.mark.asyncio
async def test_metadata_is_not_changed_for_the_upstream_call(gateway):
    gw = gateway()
    await gw.send(metadata=METADATA)
    sent = gw.transport.chat.call_args.args[0]
    assert sent.metadata == METADATA


def test_ledger_metadata_keeps_allowed_short_scalars_only():
    assert chat_api._ledger_metadata(None) is None
    assert chat_api._ledger_metadata({}) is None
    assert chat_api._ledger_metadata({"other": 1}) is None
    assert chat_api._ledger_metadata({
        "session_id": "x" * 65, "turn_id": ["a"], "bot_id": {"id": 1}, "call_kind": "retry", "batch": True,
    }) == {"call_kind": "retry", "batch": True}
    assert chat_api._ledger_metadata({"session_id": 12}) == {"session_id": 12}
