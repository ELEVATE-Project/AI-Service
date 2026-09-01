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
    def __init__(self, raise_on_commit: Exception | None = None) -> None:
        self.added: list = []
        self.committed = False
        self.rolled_back = False
        self._raise_on_commit = raise_on_commit

    def add(self, obj) -> None:
        self.added.append(obj)

    async def commit(self) -> None:
        if self._raise_on_commit:
            raise self._raise_on_commit
        self.committed = True

    async def rollback(self) -> None:
        self.rolled_back = True


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

    assert db.committed is True
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
async def test_write_ledger_entry_swallows_duplicate_request_id() -> None:
    db = _FakeSession(raise_on_commit=IntegrityError("stmt", {}, Exception("dup")))

    await _write_ledger_entry(
        db, request_id="req_dup", tenant_id="tenant_acme",
        provider="openai", model="gpt-4o", feature=Feature.CHAT,
        status=LedgerStatus.ERROR, latency_ms=10, error_code="upstream_timeout",
    )

    assert db.committed is False
    assert db.rolled_back is True


@pytest.mark.asyncio
async def test_write_ledger_entry_is_non_fatal_on_any_db_failure() -> None:
    """Per docs/architecture.md pipeline step 9: a ledger write failure must never
    propagate and turn an already-produced response into a 500."""
    db = _FakeSession(raise_on_commit=ConnectionError("db unreachable"))

    await _write_ledger_entry(
        db, request_id="req_dberr", tenant_id="tenant_acme",
        provider="openai", model="gpt-4o", feature=Feature.CHAT,
        status=LedgerStatus.SUCCESS, latency_ms=10,
    )

    assert db.committed is False
    assert db.rolled_back is True
