"""T33 pricing and admission tests; all upstream calls and DB writes are mocked."""
from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import httpx
import pytest
from fastapi import FastAPI

from src.llm_service.api.rest import chat as chat_api
from src.llm_service.providers.base import StreamEvent, TransportFinishData
from src.llm_service.schemas.chat import (
    CacheBlock, ChatResponse, Choice, ChoiceMessage, UsageBlock,
)
from src.shared.db.enums import KeyFormat
from src.shared.guardrails.base import GuardrailsResult
from src.shared.schemas.envelope import CostBlock, GuardrailsBlock, LatencyBlock, PolicyBlock
from src.shared.secrets.backend import TenantKeyPayload
from src.shared.ledger.pricing import UnknownModelError, pricing_table


@pytest.fixture
def gateway(monkeypatch):
    def build(provider="anthropic", model="claude-haiku-5-5", *, usage=None, reported=None):
        usage = usage or UsageBlock(input_tokens=1000, output_tokens=100, total_tokens=1100)
        response = ChatResponse(
            id="upstream-id", created=0, tenant_id="test-tenant", provider=provider,
            model=model, transport="mock", choices=[Choice(
                index=0, message=ChoiceMessage(role="assistant", content="hello"),
                finish_reason="stop",
            )], usage=usage, cost=CostBlock(provider_reported_usd=reported),
            cache=CacheBlock(), guardrails=GuardrailsBlock(), latency_ms=LatencyBlock(),
            policy=PolicyBlock(),
        )

        async def stream(*args):
            yield StreamEvent("finish", TransportFinishData(
                finish_reason="stop", usage=usage, provider_reported_usd=reported,
            ))

        transport = SimpleNamespace(chat=AsyncMock(return_value=response), stream=Mock(side_effect=stream))
        monkeypatch.setattr(chat_api.registry, "resolve", Mock(return_value=transport))
        entries = []
        db = SimpleNamespace(add=Mock(side_effect=entries.append), commit=AsyncMock(), rollback=AsyncMock())
        cache = SimpleNamespace(get=AsyncMock(return_value=None), set=AsyncMock())
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

        async def send(streaming=False):
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
                return await client.post("/v1/chat/stream" if streaming else "/v1/chat", json={
                    "provider": provider, "model": model,
                    "messages": [{"role": "user", "content": "hi"}],
                })

        return SimpleNamespace(send=send, entries=entries, db=db, cache=cache, transport=transport)

    return build


def _payload(response, streaming):
    assert response.status_code == 200
    if streaming:
        return json.loads(next(event.split("data: ", 1)[1] for event in response.text.split("\n\n")
                               if event.startswith("event: finish")))
    return response.json()


@pytest.fixture
def haiku_usage():
    return UsageBlock(input_tokens=100000, output_tokens=1000, total_tokens=101000)


# (a) Exactly 100k inclusive input tokens still use the low tier.
def test_pricing_haiku_55_low_tier(haiku_usage):
    cost = chat_api._compute_cost("anthropic", "claude-haiku-5-5", haiku_usage)
    assert cost.computed_usd == pytest.approx(0.0105)
    assert cost.pricing_version == 4


# (b) One token over the threshold reprices the entire request, including output.
def test_pricing_haiku_55_long_prompt_tier(haiku_usage):
    usage = haiku_usage.model_copy(update={"input_tokens": 100001, "total_tokens": 101001})
    cost = chat_api._compute_cost("anthropic", "claude-haiku-5-5", usage)
    assert cost.computed_usd == pytest.approx(0.0525005)


# (c) Both TTL write prices and cache reads, without double charging input.
@pytest.mark.parametrize("tokens_in,expected", [(10000, 0.001495), (110000, 0.057475)])
def test_pricing_haiku_55_cache_rates(tokens_in, expected):
    usage = UsageBlock(
        input_tokens=tokens_in, output_tokens=1000, total_tokens=tokens_in + 1000,
        input_tokens_cache_read=2000, input_tokens_cache_write=4000,
        input_tokens_cache_write_5m=3000, input_tokens_cache_write_1h=1000,
    )
    assert chat_api._compute_cost("anthropic", "claude-haiku-5-5", usage).computed_usd == pytest.approx(expected)


# (d) Refusal is an HTTP error before either provider entrypoint or any ledger write.
@pytest.mark.asyncio
@pytest.mark.parametrize("streaming", [False, True])
@pytest.mark.parametrize("provider,model", [
    ("anthropic", "claude-unpriced"), ("openai", "unpriced-model"),
])
async def test_pricing_refuses_unpriced_model_before_provider(gateway, streaming, provider, model):
    case = gateway(provider, model)
    response = await case.send(streaming)
    assert response.status_code == 422
    detail = response.json()["detail"]
    assert detail["code"] == "unpriced_model"
    assert f"{provider}/{model}" in detail["message"]
    case.transport.chat.assert_not_called()
    case.transport.stream.assert_not_called()
    case.db.add.assert_not_called()
    case.db.commit.assert_not_awaited()
    case.cache.set.assert_not_awaited()
    assert case.entries == []


# (e) Provider-priced OpenRouter models remain admitted and billed as reported.
@pytest.mark.asyncio
@pytest.mark.parametrize("streaming", [False, True])
@pytest.mark.parametrize("reported", [0.0042, 0.0])
async def test_pricing_openrouter_reported_cost_fallback(gateway, streaming, reported):
    case = gateway("openrouter", "vendor/unpriced-model", reported=reported)
    payload = _payload(await case.send(streaming), streaming)
    assert payload["cost"]["computed_usd"] == reported
    assert payload["cost"]["provider_reported_usd"] == reported
    assert len(case.entries) == 1
    assert case.entries[0].our_cost_usd == reported
    assert case.entries[0].provider_reported_cost_usd == reported
    if streaming:
        case.transport.stream.assert_called_once()
    else:
        case.transport.chat.assert_awaited_once()


# (f) Cache reads AND either write TTL participate in the inclusive threshold.
@pytest.mark.asyncio
@pytest.mark.parametrize("streaming", [False, True])
@pytest.mark.parametrize("plain,read,five_m,one_h,expected", [
    (50000, 50000, 0, 0, 0.006),        # exactly 100000: low
    (50001, 50000, 0, 0, 0.0300005),    # exactly 100001: high
    (60000, 50000, 0, 0, 0.035),        # 60k plain + 50k cached: high
    (60000, 0, 50000, 0, 0.06375),
    (60000, 0, 0, 50000, 0.0825),
    (60000, 20000, 10000, 20000, 0.05975),
])
async def test_pricing_inclusive_cache_threshold_and_ledger(
    gateway, streaming, plain, read, five_m, one_h, expected,
):
    tokens_in = plain + read + five_m + one_h
    usage = UsageBlock(
        input_tokens=tokens_in, output_tokens=1000, total_tokens=tokens_in + 1000,
        input_tokens_cache_read=read, input_tokens_cache_write=five_m + one_h,
        input_tokens_cache_write_5m=five_m, input_tokens_cache_write_1h=one_h,
    )
    case = gateway(usage=usage)
    payload = _payload(await case.send(streaming), streaming)
    assert payload["cost"]["computed_usd"] == pytest.approx(expected)
    assert payload["cost"]["pricing_version"] == 4
    assert payload["usage"] == usage.model_dump()
    assert len(case.entries) == 1
    entry = case.entries[0]
    assert entry.our_cost_usd == pytest.approx(expected)
    assert entry.pricing_version == 4
    assert entry.tokens_in == tokens_in
    assert entry.input_tokens_cache_read == read
    assert entry.input_tokens_cache_write == five_m + one_h


def test_pricing_non_openrouter_post_call_unknown_is_loud(caplog):
    with pytest.raises(UnknownModelError, match="anthropic/claude-unpriced"):
        chat_api._compute_cost("anthropic", "claude-unpriced", UsageBlock(input_tokens=100))
    assert "anthropic/claude-unpriced" in caplog.text
    assert "ERROR" in caplog.text


def test_pricing_haiku_55_source_and_date():
    rates = pricing_table._models["anthropic/claude-haiku-5-5"]
    assert rates["source_url"] == "https://platform.claude.com/docs/en/about-claude/pricing"
    assert rates["last_updated"] == "2026-10-25"


# (g) OpenRouter Claude models have a YAML fallback. The recorded cost is the higher of
# the YAML cost and OpenRouter's figure, so neither can lower what the budget counts.
@pytest.mark.parametrize("reported,expected", [
    (0.02, 0.02),        # provider figure above YAML (0.006): provider figure
    (0.0042, 0.006),     # provider figure below YAML: YAML cost
    (0.0, 0.006),        # zero: YAML cost
    (None, 0.006),       # stream dropped the figure: YAML cost
])
def test_pricing_openrouter_priced_model_never_under_records(reported, expected):
    usage = UsageBlock(input_tokens=1000, output_tokens=1000, total_tokens=2000)
    cost = chat_api._compute_cost("openrouter", "anthropic/claude-haiku-4.5", usage, reported)
    assert cost.computed_usd == pytest.approx(expected)
    assert cost.provider_reported_usd == reported


# (h) PyYAML reads `1e-05` (no dot) as a string, which breaks cost maths at call time.
def test_pricing_every_yaml_rate_is_a_number():
    def check(key, rates):
        for name, value in rates.items():
            if name == "long_prompt_tier":
                check(f"{key}.long_prompt_tier", value)
            elif name.endswith(("_rate_per_1k_tokens", "_tokens")):
                assert isinstance(value, (int, float)), f"{key}.{name} is {value!r}"
    for key, rates in pricing_table._models.items():
        check(key, rates)
