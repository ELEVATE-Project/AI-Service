"""T10: response-cache boundaries and lock ownership, without Redis or providers."""
from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import httpx
import pytest
from fastapi import FastAPI
from starlette.requests import Request

from src.llm_service.api.rest import chat as api
from src.llm_service.cache.keys import make_cache_key
from src.llm_service.cache.redis_cache import RedisCache
from src.llm_service.providers.base import StreamEvent, TransportFinishData, UpstreamTransportError
from src.llm_service.schemas.chat import (
    CacheBlock, ChatParams, ChatRequest, ChatResponse, Choice, ChoiceMessage,
    ErrorData, MessageParam, NormalisedLLMRequest, TokenData, UsageBlock,
)
from src.shared.db.enums import KeyFormat
from src.shared.guardrails.base import GuardrailsResult
from src.shared.schemas.envelope import CostBlock, GuardrailsBlock, LatencyBlock, PolicyBlock
from src.shared.secrets.backend import TenantKeyPayload


class FakeRedis:
    """Only the Redis operations used by the response cache; expiry is explicit."""

    def __init__(self):
        self.values = {}
        self.get = AsyncMock(side_effect=lambda key: self.values.get(key))
        self.set = AsyncMock(side_effect=self._set)
        self.eval = AsyncMock(side_effect=self._eval)

    def _set(self, key, value, *, nx=False, ex=None, px=None):
        if nx and key in self.values:
            return None
        assert ex is None or ex > 0
        assert px is None or px > 0
        self.values[key] = value
        return True

    def _eval(self, script, numkeys, key, token):
        assert numkeys == 1
        if self.values.get(key) == token:
            del self.values[key]
            return 1
        return 0

    def assert_unused(self):
        self.get.assert_not_awaited()
        self.set.assert_not_awaited()
        self.eval.assert_not_awaited()


def response():
    return ChatResponse(
        id="upstream-id", created=0, tenant_id="test-tenant", provider="anthropic",
        model="claude-haiku-5-5", transport="mock", choices=[Choice(
            index=0, message=ChoiceMessage(role="assistant", content="Example title"),
            finish_reason="stop",
        )], usage=UsageBlock(input_tokens=10, output_tokens=2, total_tokens=12),
        cost=CostBlock(), latency_ms=LatencyBlock(), cache=CacheBlock(),
        guardrails=GuardrailsBlock(), policy=PolicyBlock(),
    )


def normalised(**changes):
    return NormalisedLLMRequest(
        provider="anthropic", model="claude-haiku-5-5",
        messages=[MessageParam(role="user", content="Example request")], **changes,
    )


@pytest.fixture
def cache_factory(monkeypatch):
    def build(ttl=300):
        redis = FakeRedis()
        monkeypatch.setattr("src.llm_service.cache.redis_cache.aioredis.from_url", lambda *a, **kw: redis)
        return RedisCache("redis://unused", ttl_seconds=ttl), redis
    return build


@pytest.mark.asyncio
@pytest.mark.parametrize("ttl", [0, -1])
async def test_disabled_cache_performs_no_redis_operations(cache_factory, ttl):
    cache, redis = cache_factory(ttl)
    assert await cache.get("key") is None
    assert await cache.acquire("key") is None
    await cache.set("key", response(), ttl_seconds=ttl)
    await cache.release("key", "not-acquired")
    redis.assert_unused()


@pytest.mark.asyncio
async def test_expired_owner_cannot_release_the_next_owners_lock(cache_factory):
    cache, redis = cache_factory()
    first = await cache.acquire("key")
    assert first
    assert await cache.acquire("key") is None
    del redis.values["lock:key"]  # A's lease expires while its provider call is running.
    second = await cache.acquire("key")
    assert second and first != second
    await cache.release("key", first)
    assert redis.values["lock:key"] == second
    await cache.release("key", second)
    assert "lock:key" not in redis.values


@pytest.mark.parametrize("params", [
    ChatParams(read_timeout=90), ChatParams(connect_timeout=7),
    ChatParams(retry={"max_attempts": 2}),
])
def test_timeouts_and_retry_settings_do_not_split_the_key(params):
    assert make_cache_key("tenant", normalised()) == make_cache_key("tenant", normalised(params=params))


def test_tool_choice_is_part_of_the_key():
    assert make_cache_key("tenant", normalised(tool_choice="auto")) != make_cache_key(
        "tenant", normalised(tool_choice="none"),
    )


@pytest.mark.parametrize("changes", [
    {"params": ChatParams(max_tokens=10)}, {"params": ChatParams(seed=7)},
    {"params": ChatParams(top_p=0.5)}, {"provider_options": {"models": ["other/model"]}},
    {"messages": [MessageParam(role="user", content="Different request")]},
])
def test_answer_affecting_inputs_remain_separate(changes):
    original = normalised()
    assert make_cache_key("tenant", original) != make_cache_key("tenant", original.model_copy(update=changes))
    assert make_cache_key("tenant", original) != make_cache_key("other-tenant", original)


@pytest.fixture
def gateway(monkeypatch, cache_factory):
    def build(*, ttl=300, result=None, failure=None, set_failure=False):
        monkeypatch.setattr(api.settings, "cache_ttl_seconds", ttl)
        cache, redis = cache_factory(ttl)
        result = result or response()
        entries = []

        async def stream(*args):
            if failure == "raised":
                raise RuntimeError("provider failed")
            if failure:
                yield StreamEvent("error", ErrorData(code="upstream_timeout", message="timeout"))
                return
            yield StreamEvent("token", TokenData(index=0, delta=result.choices[0].message.content or ""))
            yield StreamEvent("finish", TransportFinishData(
                finish_reason="stop", usage=result.usage,
                citations=result.choices[0].message.citations,
                thinking_blocks=result.choices[0].message.thinking_blocks,
            ))

        error = UpstreamTransportError(code="upstream_timeout", message="timeout", http_status=504)
        transport = SimpleNamespace(
            chat=AsyncMock(side_effect=error) if failure else AsyncMock(side_effect=lambda *a: result.model_copy(deep=True)),
            stream=Mock(side_effect=stream),
        )
        monkeypatch.setattr(api.registry, "resolve", Mock(return_value=transport))
        if set_failure:
            original_set = redis.set.side_effect

            def failing_set(key, value, **kwargs):
                if not key.startswith("lock:"):
                    raise ConnectionError("cache write failed")
                return original_set(key, value, **kwargs)
            redis.set.side_effect = failing_set

        dependencies = dict(
            tenant=SimpleNamespace(id="test-tenant"),
            secret_backend=SimpleNamespace(get_key=AsyncMock(return_value=TenantKeyPayload(
                key_format=KeyFormat.API_KEY, data={"api_key": "test-only"},
            ))),
            policy_checker=SimpleNamespace(check=AsyncMock()),
            guardrails=SimpleNamespace(check_input=AsyncMock(return_value=GuardrailsResult())),
            cache=cache,
            db=SimpleNamespace(add=Mock(side_effect=entries.append), commit=AsyncMock(), rollback=AsyncMock()),
        )
        app = FastAPI()
        app.include_router(api.router)

        def override(value):
            async def dependency():
                return value
            return dependency

        for name, value in dependencies.items():
            dependency = api.get_db if name == "db" else getattr(api, f"get_{name}")
            app.dependency_overrides[dependency] = override(value)

        def body(**changes):
            return {"provider": "anthropic", "model": "claude-haiku-5-5",
                    "messages": [{"role": "user", "content": "Example request"}], **changes}

        async def send(streaming=False, **changes):
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
                return await client.post("/v1/chat/stream" if streaming else "/v1/chat", json=body(**changes))

        async def open_stream(**changes):
            return await api.chat_stream(
                ChatRequest(**body(**changes)), Request({"type": "http", "headers": []}), **dependencies,
            )

        return SimpleNamespace(send=send, open_stream=open_stream, redis=redis, cache=cache,
                               transport=transport, entries=entries, result=result)
    return build


@pytest.mark.asyncio
@pytest.mark.parametrize("streaming", [False, True])
@pytest.mark.parametrize("ttl", [0, -1])
async def test_disabled_routes_bypass_redis(gateway, streaming, ttl):
    gw = gateway(ttl=ttl)
    assert (await gw.send(streaming)).status_code == 200
    assert (await gw.send(streaming)).status_code == 200
    gw.redis.assert_unused()
    assert (gw.transport.stream if streaming else gw.transport.chat).call_count == 2


@pytest.mark.asyncio
@pytest.mark.parametrize("streaming", [False, True])
async def test_eligible_repeat_is_a_hit_without_a_leftover_lock(gateway, streaming):
    gw = gateway()
    first = await gw.send(streaming, params={"temperature": 0, "thinking": {"type": "disabled"}})
    second = await gw.send(streaming, params={"temperature": 0, "thinking": {"type": "disabled"}})
    assert first.status_code == second.status_code == 200
    assert (gw.transport.stream if streaming else gw.transport.chat).call_count == 1
    assert [entry.our_cache_hit for entry in gw.entries] == [False, True]
    assert gw.entries[1].our_cost_usd == 0
    assert not any(key.startswith("lock:") for key in gw.redis.values)
    payload = json.loads(next(event.split("data: ", 1)[1] for event in second.text.split("\n\n")
                              if event.startswith("event: finish"))) if streaming else second.json()
    assert payload["cache"]["our_cache_hit"] is True


@pytest.mark.asyncio
@pytest.mark.parametrize("streaming", [False, True])
async def test_busy_lock_does_not_delay_the_request_or_release_another_owner(gateway, streaming):
    gw = gateway()
    key = make_cache_key("test-tenant", normalised())
    gw.redis.values[f"lock:{key}"] = "other-owner"
    result = await asyncio.wait_for(gw.send(streaming), timeout=1)
    assert result.status_code == 200
    assert gw.redis.values == {f"lock:{key}": "other-owner"}
    gw.redis.eval.assert_not_awaited()
    assert (gw.transport.stream if streaming else gw.transport.chat).call_count == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("streaming", [False, True])
async def test_unsafe_stored_response_is_not_replayed(gateway, streaming):
    gw = gateway()
    key = make_cache_key("test-tenant", normalised())
    stale = response()
    stale.choices[0].message.citations = [{"url": "https://example.invalid/stale"}]
    gw.redis.values[key] = stale.model_dump_json()
    result = await gw.send(streaming)
    assert result.status_code == 200
    assert (gw.transport.stream if streaming else gw.transport.chat).call_count == 1
    assert gw.entries[0].our_cache_hit is False
    assert "example.invalid/stale" not in result.text


@pytest.mark.asyncio
async def test_batch_admission_does_not_read_cache_or_take_a_lock(gateway):
    gw = gateway()
    assert (await gw.send(metadata={"batch": True})).status_code == 202
    gw.redis.assert_unused()
    gw.transport.chat.assert_not_awaited()


@pytest.mark.asyncio
async def test_unconsumed_stream_does_not_acquire_a_lock(gateway):
    gw = gateway()
    result = await gw.open_stream()
    gw.redis.set.assert_not_awaited()
    await result.body_iterator.aclose()


UNSAFE_REQUESTS = [
    {"params": {"temperature": 0.5}},
    {"params": {"thinking": {"type": "adaptive"}}},
    {"params": {"thinking": {"type": "enabled", "budget_tokens": 2048}}},
    {"params": {"output_config": {"effort": "low"}}},
    {"params": {"web_search_options": {}}},
    {"tools": [{"type": "web_search_20250305", "name": "web_search"}]},
    {"tools": [{"type": "openrouter:web_search", "name": "web_search"}]},
    {"provider_options": {"plugins": [{"id": "web"}]}},
]


@pytest.mark.asyncio
@pytest.mark.parametrize("streaming", [False, True])
@pytest.mark.parametrize("changes", UNSAFE_REQUESTS)
async def test_ineligible_requests_neither_read_nor_write_cache(gateway, streaming, changes):
    gw = gateway()
    assert (await gw.send(streaming, **changes)).status_code == 200
    gw.redis.assert_unused()


@pytest.mark.asyncio
@pytest.mark.parametrize("streaming", [False, True])
@pytest.mark.parametrize("signal", ["citations", "thinking_blocks", "search_usage"])
async def test_ineligible_responses_are_not_saved_and_release_the_lock(gateway, streaming, signal):
    result = response()
    if signal == "search_usage":
        result.usage.web_search_requests = 1
    else:
        setattr(result.choices[0].message, signal, [{"example": "opaque provider data"}])
    gw = gateway(result=result)
    assert (await gw.send(streaming)).status_code == 200
    assert gw.redis.values == {}
    gw.redis.eval.assert_awaited_once()


@pytest.mark.asyncio
@pytest.mark.parametrize("streaming", [False, True])
async def test_provider_error_releases_the_lock(gateway, streaming):
    gw = gateway(failure="error")
    result = await gw.send(streaming)
    assert result.status_code == (200 if streaming else 504)
    assert gw.redis.values == {}
    gw.redis.eval.assert_awaited_once()


@pytest.mark.asyncio
@pytest.mark.parametrize("streaming", [False, True])
async def test_cache_write_failure_does_not_fail_the_answer_or_leave_a_lock(gateway, streaming):
    gw = gateway(set_failure=True)
    assert (await gw.send(streaming)).status_code == 200
    assert gw.redis.values == {}
    gw.redis.eval.assert_awaited_once()


@pytest.mark.asyncio
async def test_stream_closed_early_releases_the_lock(gateway):
    gw = gateway()
    result = await gw.open_stream()
    assert (await anext(result.body_iterator)).startswith("event: token")
    assert any(key.startswith("lock:") for key in gw.redis.values)
    await result.body_iterator.aclose()
    assert gw.redis.values == {}


@pytest.mark.asyncio
async def test_unexpected_stream_exception_releases_the_lock(gateway):
    gw = gateway(failure="raised")
    result = await gw.open_stream()
    with pytest.raises(RuntimeError, match="provider failed"):
        await anext(result.body_iterator)
    assert gw.redis.values == {}
    gw.redis.eval.assert_awaited_once()


@pytest.mark.asyncio
async def test_cancellation_releases_the_lock(gateway):
    gw = gateway()
    result = await gw.open_stream()
    iterator = result.body_iterator
    await anext(iterator)
    with pytest.raises(asyncio.CancelledError):
        await iterator.athrow(asyncio.CancelledError())
    assert gw.redis.values == {}
