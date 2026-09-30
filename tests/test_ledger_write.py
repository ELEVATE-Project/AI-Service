"""Unit tests for the ledger-write helper (_write_ledger_entry)."""
from __future__ import annotations

import litellm  # noqa: F401 — importing this appends the repo root to sys.path, required for `from src...` below
import pytest
from sqlalchemy.exc import IntegrityError

from src.llm_service.api.rest.chat import _write_ledger_entry
from src.llm_service.schemas.chat import UsageBlock
from src.shared.db.enums import Feature, LedgerStatus, Transport
from src.shared.schemas.envelope import CostBlock


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
