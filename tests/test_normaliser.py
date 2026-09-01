"""Unit tests for tenant-default resolution in normalise()."""
from __future__ import annotations

import litellm  # noqa: F401 — importing this appends the repo root to sys.path, required for `from src...` below
import pytest
from fastapi import HTTPException

from src.llm_service.normaliser import normalise
from src.llm_service.schemas.chat import ChatParams, ChatRequest, MessageParam
from src.shared.db.models import TenantDefaults


class _FakeResult:
    def __init__(self, row) -> None:
        self._row = row

    def scalar_one_or_none(self):
        return self._row


class _FakeSession:
    """Stands in for AsyncSession — normalise() only ever calls db.execute(select(...))."""

    def __init__(self, tenant_defaults: TenantDefaults | None = None) -> None:
        self._tenant_defaults = tenant_defaults

    async def execute(self, _stmt):
        return _FakeResult(self._tenant_defaults)


def _messages() -> list[MessageParam]:
    return [MessageParam(role="user", content="hi")]


@pytest.mark.asyncio
async def test_no_use_defaults_requires_provider_and_model() -> None:
    req = ChatRequest(messages=_messages())
    with pytest.raises(HTTPException) as exc_info:
        await normalise(req, _FakeSession(), "tenant_acme")
    assert exc_info.value.status_code == 400


@pytest.mark.asyncio
async def test_use_defaults_false_ignores_existing_tenant_defaults_row() -> None:
    defaults = TenantDefaults(tenant_id="tenant_acme", default_provider="anthropic", default_model="claude-3")
    req = ChatRequest(messages=_messages(), use_defaults=False)
    with pytest.raises(HTTPException) as exc_info:
        await normalise(req, _FakeSession(defaults), "tenant_acme")
    assert exc_info.value.status_code == 400


@pytest.mark.asyncio
async def test_use_defaults_true_fills_missing_provider_and_model() -> None:
    defaults = TenantDefaults(tenant_id="tenant_acme", default_provider="anthropic", default_model="claude-3")
    req = ChatRequest(messages=_messages(), use_defaults=True)
    normalised = await normalise(req, _FakeSession(defaults), "tenant_acme")
    assert normalised.provider == "anthropic"
    assert normalised.model == "claude-3"


@pytest.mark.asyncio
async def test_use_defaults_true_leaves_explicit_request_fields_untouched() -> None:
    defaults = TenantDefaults(
        tenant_id="tenant_acme", default_provider="anthropic", default_model="claude-3",
        default_params={"temperature": 0.9},
    )
    req = ChatRequest(
        messages=_messages(), provider="openai", model="gpt-4o",
        params=ChatParams(temperature=0.1), use_defaults=True,
    )
    normalised = await normalise(req, _FakeSession(defaults), "tenant_acme")
    assert normalised.provider == "openai"
    assert normalised.model == "gpt-4o"
    assert normalised.params.temperature == 0.1


@pytest.mark.asyncio
async def test_use_defaults_true_partially_fills_params_only_for_missing_fields() -> None:
    defaults = TenantDefaults(
        tenant_id="tenant_acme", default_provider="anthropic", default_model="claude-3",
        default_params={"temperature": 0.9, "max_tokens": 512},
    )
    req = ChatRequest(
        messages=_messages(), params=ChatParams(temperature=0.1), use_defaults=True,
    )
    normalised = await normalise(req, _FakeSession(defaults), "tenant_acme")
    assert normalised.params.temperature == 0.1
    assert normalised.params.max_tokens == 512


@pytest.mark.asyncio
async def test_use_defaults_true_with_no_tenant_defaults_row_still_400s() -> None:
    req = ChatRequest(messages=_messages(), use_defaults=True)
    with pytest.raises(HTTPException) as exc_info:
        await normalise(req, _FakeSession(None), "tenant_acme")
    assert exc_info.value.status_code == 400
