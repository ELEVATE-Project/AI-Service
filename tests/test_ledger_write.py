"""Unit tests for the ledger-write helper (_write_ledger_entry)."""
from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import litellm  # noqa: F401 — importing this appends the repo root to sys.path, required for `from src...` below
import pytest
from sqlalchemy.exc import IntegrityError

from src.llm_service.api.rest.chat import _compute_cost, _write_ledger_entry
from src.llm_service.providers.litellm import LiteLLMTransport
from src.llm_service.schemas.chat import UsageBlock
from src.shared.db.enums import Feature, LedgerStatus, Transport
from src.shared.schemas.envelope import CostBlock
from src.shared.ledger.pricing import pricing_table


class _FakeSession:
    """`commit_side_effects` is a queue of exceptions (or None for success) consumed
    one per commit() call, in order — lets a test script "fails once, then succeeds"
    or "fails every time" without needing a real database."""

    def __init__(self, commit_side_effects: list[Exception | None] | None = None) -> None:
        self.added: list = []
        self.commit_count = 0
        self.rollback_count = 0
        self._commit_side_effects = list(commit_side_effects) if commit_side_effects else []

    def add(self, obj) -> None:
        self.added.append(obj)

    async def commit(self) -> None:
        self.commit_count += 1
        effect = self._commit_side_effects.pop(0) if self._commit_side_effects else None
        if effect is not None:
            raise effect

    async def rollback(self) -> None:
        self.rollback_count += 1


@pytest.mark.asyncio
async def test_write_ledger_entry_success_populates_usage_and_cost() -> None:
    db = _FakeSession()
    usage = UsageBlock(input_tokens=100, output_tokens=50, total_tokens=150)
    cost = CostBlock(computed_usd=0.0025, pricing_version=3, provider_reported_usd=0.0024)

    await _write_ledger_entry(
        db, request_id="req_abc", tenant_id="tenant_acme",
        provider="openai", model="gpt-4o", feature=Feature.CHAT,
        status=LedgerStatus.SUCCESS, latency_ms=1200,
        usage=usage, cost=cost, provider_reported_usage={"prompt_tokens": 100},
    )

    assert db.commit_count == 1
    assert len(db.added) == 1
    entry = db.added[0]
    assert entry.request_id == "req_abc"
    assert entry.tenant_id == "tenant_acme"
    assert entry.transport == Transport.LITELLM
    assert entry.status == LedgerStatus.SUCCESS
    assert entry.tokens_in == 100
    assert entry.tokens_out == 50
    assert entry.our_cost_usd == 0.0025
    assert entry.pricing_version == 3
    assert entry.provider_reported_cost_usd == 0.0024
    assert entry.provider_reported_usage == {"prompt_tokens": 100}


@pytest.mark.asyncio
async def test_write_ledger_entry_error_status_has_no_usage() -> None:
    db = _FakeSession()

    await _write_ledger_entry(
        db, request_id="req_err", tenant_id="tenant_acme",
        provider="openai", model="gpt-4o", feature=Feature.CHAT,
        status=LedgerStatus.ERROR, latency_ms=300, error_code="upstream_timeout",
    )

    entry = db.added[0]
    assert entry.status == LedgerStatus.ERROR
    assert entry.error_code == "upstream_timeout"
    assert entry.tokens_in == 0
    assert entry.tokens_out == 0
    assert entry.provider_reported_usage is None


@pytest.mark.asyncio
async def test_write_ledger_entry_cache_hit_flag() -> None:
    db = _FakeSession()
    usage = UsageBlock(input_tokens=10, output_tokens=5, total_tokens=15)
    cost = CostBlock(computed_usd=0.0, pricing_version=3)

    await _write_ledger_entry(
        db, request_id="req_cache", tenant_id="tenant_acme",
        provider="openai", model="gpt-4o", feature=Feature.STREAM,
        status=LedgerStatus.SUCCESS, latency_ms=5,
        usage=usage, cost=cost, our_cache_hit=True,
        provider_reported_usage=usage.model_dump(),
    )

    entry = db.added[0]
    assert entry.our_cache_hit is True
    assert entry.our_cost_usd == 0.0


@pytest.mark.asyncio
async def test_write_ledger_entry_request_id_collision_retries_with_disambiguated_id() -> None:
    """A request_id collision means a DIFFERENT request already used this id — the
    current call's provider work already happened and must not lose its audit row,
    so it's retried under a disambiguated id rather than silently dropped."""
    db = _FakeSession(commit_side_effects=[IntegrityError("stmt", {}, Exception("dup")), None])
    usage = UsageBlock(input_tokens=20, output_tokens=10, total_tokens=30)
    cost = CostBlock(computed_usd=0.001, pricing_version=3)

    await _write_ledger_entry(
        db, request_id="req_collide", tenant_id="tenant_acme",
        provider="openai", model="gpt-4o", feature=Feature.CHAT,
        status=LedgerStatus.SUCCESS, latency_ms=10, usage=usage, cost=cost,
        provider_reported_usage=usage.model_dump(),
    )

    assert db.commit_count == 2
    assert db.rollback_count == 1
    assert len(db.added) == 2
    first, retried = db.added
    assert first.request_id == "req_collide"
    assert retried.request_id.startswith("req_collide:dup:")
    assert retried.request_id != first.request_id
    # the retried row must still carry the real cost/usage — not an empty placeholder
    assert retried.our_cost_usd == 0.001
    assert retried.tokens_in == 20


@pytest.mark.asyncio
async def test_write_ledger_entry_gives_up_after_one_retry() -> None:
    db = _FakeSession(commit_side_effects=[
        IntegrityError("stmt", {}, Exception("dup")),
        IntegrityError("stmt", {}, Exception("dup again")),
    ])

    await _write_ledger_entry(
        db, request_id="req_dup", tenant_id="tenant_acme",
        provider="openai", model="gpt-4o", feature=Feature.CHAT,
        status=LedgerStatus.ERROR, latency_ms=10, error_code="upstream_timeout",
    )

    assert db.commit_count == 2
    assert db.rollback_count == 2
    assert len(db.added) == 2


@pytest.mark.asyncio
async def test_write_ledger_entry_is_non_fatal_on_any_db_failure() -> None:
    """Per docs/architecture.md pipeline step 9: a ledger write failure must never
    propagate and turn an already-produced response into a 500. A non-collision
    failure (e.g. connection loss) is not retried — there's nothing to disambiguate."""
    db = _FakeSession(commit_side_effects=[ConnectionError("db unreachable")])

    await _write_ledger_entry(
        db, request_id="req_dberr", tenant_id="tenant_acme",
        provider="openai", model="gpt-4o", feature=Feature.CHAT,
        status=LedgerStatus.SUCCESS, latency_ms=10,
    )

    assert db.commit_count == 1
    assert db.rollback_count == 1
    assert len(db.added) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("feature", [Feature.CHAT, Feature.STREAM])
async def test_cache_read_cost_preserves_usage_and_ledger_tokens(feature) -> None:
    raw_usage = SimpleNamespace(
        prompt_tokens=33000, completion_tokens=10, total_tokens=33010,
        cache_read_input_tokens=32000, cache_creation_input_tokens=0,
    )
    usage = LiteLLMTransport()._extract_usage(SimpleNamespace(usage=raw_usage))
    before = usage.model_dump()
    cost = _compute_cost("anthropic", "claude-haiku-4-5", usage)
    assert cost.computed_usd == pytest.approx(0.0042 + 10 * 0.005 / 1000)
    assert cost.pricing_version == 4
    assert usage.model_dump() == before
    assert usage.input_tokens == 33000
    assert usage.input_tokens_cache_read == 32000
    assert usage.input_tokens_cache_write is None
    assert usage.total_tokens == 33010

    db = _FakeSession()
    await _write_ledger_entry(
        db, request_id="req_read", tenant_id="tenant_acme",
        provider="anthropic", model="claude-haiku-4-5", feature=feature,
        status=LedgerStatus.SUCCESS, latency_ms=10, usage=usage, cost=cost,
        provider_reported_usage=vars(raw_usage),
    )
    entry = db.added[0]
    assert entry.tokens_in == 33000
    assert entry.tokens_out == 10
    assert entry.input_tokens_cache_read == 32000
    assert entry.input_tokens_cache_write is None
    assert entry.our_cost_usd == cost.computed_usd
    assert entry.provider_reported_usage == vars(raw_usage)


@pytest.mark.parametrize("ttl,expected", [(None, 0.041), ("5m", 0.041), ("1h", 0.065)])
def test_cache_write_ttl_fallback(ttl, expected) -> None:
    usage = UsageBlock(input_tokens=33000, input_tokens_cache_write=32000)
    cost = _compute_cost("anthropic", "claude-haiku-4-5", usage, cache_ttl=ttl)
    assert cost.computed_usd == pytest.approx(expected)


@pytest.mark.parametrize("ttl", [None, "5m", "1h"])
@pytest.mark.parametrize("split", [(32000, 0), (0, 32000), (12000, 20000)])
def test_response_write_split_overrides_request_ttl(ttl, split) -> None:
    five_m, one_h = split
    usage = UsageBlock(
        input_tokens=33000, output_tokens=10, input_tokens_cache_write=32000,
        input_tokens_cache_write_5m=five_m, input_tokens_cache_write_1h=one_h,
    )
    cost = _compute_cost("anthropic", "claude-haiku-4-5", usage, cache_ttl=ttl)
    assert cost.computed_usd == pytest.approx(
        0.001 + 10 * 0.005 / 1000 + five_m * 0.00125 / 1000 + one_h * 0.002 / 1000
    )


def test_openai_cached_prompt_details_cost() -> None:
    raw = SimpleNamespace(usage=SimpleNamespace(
        prompt_tokens=33000, completion_tokens=10, total_tokens=33010,
        prompt_tokens_details=SimpleNamespace(cached_tokens=32000),
    ))
    usage = LiteLLMTransport()._extract_usage(raw)
    assert usage.input_tokens == 33000
    assert usage.total_tokens == 33010
    assert usage.input_tokens_cache_read == 32000
    assert _compute_cost("openai", "gpt-4.1-mini", usage).computed_usd == pytest.approx(0.003616)


@pytest.mark.parametrize("provider,model", [
    ("anthropic", "claude-haiku-4-5"), ("anthropic", "claude-sonnet-4-5"),
    ("openai", "gpt-4.1-mini"), ("bedrock", "us.meta.llama3-3-70b-instruct-v1:0"),
])
def test_no_cache_cost_matches_previous_formula(provider, model) -> None:
    rates = pricing_table._models[f"{provider}/{model}"]
    old_cost = round(33000 * rates["input_rate_per_1k_tokens"] / 1000
                     + 10 * rates["output_rate_per_1k_tokens"] / 1000, 10)
    usage = LiteLLMTransport()._extract_usage(SimpleNamespace(usage=SimpleNamespace(
        prompt_tokens=33000, completion_tokens=10, total_tokens=33010,
    )))
    assert _compute_cost(provider, model, usage).computed_usd == old_cost
    assert usage.input_tokens == 33000
    assert usage.total_tokens == 33010
    assert usage.input_tokens_cache_read is None
    assert usage.input_tokens_cache_write is None


def test_one_hour_rate_falls_back_to_five_minute_rate(monkeypatch) -> None:
    # Synthetic entry with no cache_write_1h rate: tests the generic missing-rate fallback.
    monkeypatch.setitem(pricing_table._models, "anthropic/synthetic-no-1h-rate", {
        "input_rate_per_1k_tokens": 0.003, "output_rate_per_1k_tokens": 0.015,
        "cache_write_rate_per_1k_tokens": 0.00375, "cache_read_rate_per_1k_tokens": 0.0003,
    })
    usage = UsageBlock(input_tokens=33000, input_tokens_cache_write=32000)
    cost = _compute_cost("anthropic", "synthetic-no-1h-rate", usage, cache_ttl="1h")
    assert cost.computed_usd == pytest.approx(0.123)


def test_sonnet_4_5_one_hour_cache_write_uses_one_hour_rate() -> None:
    # $6/MTok for 1h writes: 1000 uncached * 0.003/1k + 32000 * 0.006/1k = 0.195
    explicit = UsageBlock(input_tokens=33000, input_tokens_cache_write=32000,
                          input_tokens_cache_write_1h=32000)
    assert _compute_cost("anthropic", "claude-sonnet-4-5", explicit).computed_usd == pytest.approx(0.195)
    ttl_only = UsageBlock(input_tokens=33000, input_tokens_cache_write=32000)
    assert _compute_cost("anthropic", "claude-sonnet-4-5", ttl_only, cache_ttl="1h").computed_usd == pytest.approx(0.195)


def test_cache_subtraction_is_floored_and_reads_and_writes_are_priced_once() -> None:
    cost = pricing_table.compute_cost("anthropic", "claude-haiku-4-5", 100, 0,
                                      cache_read_tokens=200, cache_write_tokens=300)
    assert cost.computed_usd == pytest.approx(0.00002 + 0.000375)


def test_inconsistent_write_split_is_priced_not_raised() -> None:
    # Cost runs after the provider has billed the call, so a split larger than
    # the reported total must still price (from the split), never fail the request.
    cost = pricing_table.compute_cost("anthropic", "claude-haiku-4-5", 3000, 0,
                                      cache_write_tokens=1000, cache_write_1h_tokens=2000)
    assert cost.computed_usd == pytest.approx(1000 * 0.001 / 1000 + 2000 * 0.002 / 1000)


@pytest.mark.parametrize("read,five_m,one_h", [(32000, 0, 0), (0, 32000, 0), (0, 0, 32000)])
def test_cache_cost_matches_litellm_offline_within_one_percent(read, five_m, one_h) -> None:
    from litellm.types.utils import CacheCreationTokenDetails, PromptTokensDetailsWrapper, Usage

    # conftest forces LITELLM_LOCAL_MODEL_COST_MAP before any LiteLLM import.
    llm_usage = Usage(
        prompt_tokens=33000, completion_tokens=10, total_tokens=33010,
        cache_read_input_tokens=read, cache_creation_input_tokens=five_m + one_h,
        prompt_tokens_details=PromptTokensDetailsWrapper(
            cached_tokens=read, cache_creation_tokens=five_m + one_h,
            cache_creation_token_details=CacheCreationTokenDetails(
                ephemeral_5m_input_tokens=five_m, ephemeral_1h_input_tokens=one_h,
            ),
        ),
    )
    reference = sum(litellm.cost_per_token(
        model="claude-haiku-4-5", custom_llm_provider="anthropic", usage_object=llm_usage,
    ))
    usage = LiteLLMTransport()._extract_usage(SimpleNamespace(usage=llm_usage))
    assert _compute_cost("anthropic", "claude-haiku-4-5", usage).computed_usd == pytest.approx(reference, rel=0.01)


@pytest.mark.asyncio
@pytest.mark.parametrize("streaming", [False, True])
@pytest.mark.parametrize("ttl,split,expected", [
    (None, None, 0.04105),
    ("5m", None, 0.04105),
    ("1h", None, 0.06505),
    ("5m", (0, 32000), 0.06505),
    ("1h", (32000, 0), 0.04105),
    ("1h", (12000, 20000), 0.05605),
])
async def test_chat_and_stream_price_writes_with_ttl_and_preserve_ledger(
    monkeypatch, streaming, ttl, split, expected,
) -> None:
    from src.llm_service.api.rest.chat import chat, chat_stream, registry
    from src.llm_service.schemas.chat import CacheOptions, ChatParams, ChatRequest, MessageParam
    from src.shared.db.enums import KeyFormat
    from src.shared.guardrails.base import GuardrailsResult
    from src.shared.secrets.backend import TenantKeyPayload
    from litellm.types.utils import CacheCreationTokenDetails, PromptTokensDetailsWrapper, Usage

    # Exercise the actual transport's usage extraction, then the route and ledger.
    # Only the provider call and external dependencies are mocked.
    details = PromptTokensDetailsWrapper(cache_creation_tokens=32000)
    if split is not None:
        details.cache_creation_token_details = CacheCreationTokenDetails(
            ephemeral_5m_input_tokens=split[0], ephemeral_1h_input_tokens=split[1],
        )
    raw_usage = Usage(
        prompt_tokens=33000, completion_tokens=10, total_tokens=33010,
        cache_creation_input_tokens=32000, prompt_tokens_details=details,
    )

    async def _fake_acompletion(**kwargs):
        if not kwargs.get("stream"):
            return litellm.ModelResponse(usage=raw_usage)

        async def _chunks():
            yield SimpleNamespace(choices=[], usage=raw_usage)
        return _chunks()

    monkeypatch.setattr(litellm, "acompletion", _fake_acompletion)
    monkeypatch.setattr(registry, "resolve", lambda *args: LiteLLMTransport())
    key = TenantKeyPayload(key_format=KeyFormat.API_KEY, data={"api_key": "test-only"})
    cache = SimpleNamespace(get=AsyncMock(return_value=None), set=AsyncMock())
    db = _FakeSession()
    req = ChatRequest(
        provider="anthropic", model="claude-haiku-4-5",
        messages=[MessageParam(role="user", content="hi")],
        params=ChatParams(cache_options=CacheOptions(ttl=ttl)) if ttl else None,
    )
    endpoint = chat_stream if streaming else chat
    response = await endpoint(
        body=req, request=SimpleNamespace(headers={}), tenant=SimpleNamespace(id="tenant_acme"),
        secret_backend=SimpleNamespace(get_key=AsyncMock(return_value=key)),
        policy_checker=SimpleNamespace(check=AsyncMock()),
        guardrails=SimpleNamespace(check_input=AsyncMock(return_value=GuardrailsResult())),
        cache=cache, db=db,
    )
    if streaming:
        events = [event async for event in response.body_iterator]
        payload = json.loads(next(event.split("data: ", 1)[1] for event in events
                                  if event.startswith("event: finish")))
    else:
        payload = response.model_dump()
    assert payload["cost"]["computed_usd"] == pytest.approx(expected)
    assert payload["usage"]["input_tokens"] == 33000
    assert payload["usage"]["input_tokens_cache_write"] == 32000
    assert payload["usage"]["total_tokens"] == 33010
    entry = db.added[0]
    assert entry.tokens_in == 33000
    assert entry.input_tokens_cache_write == 32000
    assert entry.our_cost_usd == pytest.approx(expected)
    assert entry.feature == (Feature.STREAM if streaming else Feature.CHAT)
    assert cache.set.await_args.args[1].cost.computed_usd == pytest.approx(expected)
