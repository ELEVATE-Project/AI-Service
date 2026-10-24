"""Unit tests for tenant-default resolution in normalise()."""
from __future__ import annotations

from types import SimpleNamespace

import litellm  # noqa: F401 — importing this appends the repo root to sys.path, required for `from src...` below
import pytest
from fastapi import HTTPException

from src.llm_service.normaliser import normalise
from src.llm_service.schemas.chat import ChatParams, ChatRequest, MessageParam
from src.shared.db.models import TenantDefaults
from src.llm_service.providers.litellm import LiteLLMTransport


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


@pytest.mark.parametrize("shape", ["anthropic_dict", "anthropic_object", "litellm"])
def test_extract_usage_adds_ttl_split_without_changing_existing_fields(shape) -> None:
    split = {"ephemeral_5m_input_tokens": 12000, "ephemeral_1h_input_tokens": 20000}
    raw_usage = SimpleNamespace(
        prompt_tokens=35000, completion_tokens=10, total_tokens=35010,
        cache_read_input_tokens=2000, cache_creation_input_tokens=32000,
    )
    if shape == "anthropic_dict":
        raw_usage.cache_creation = split
    elif shape == "anthropic_object":
        raw_usage.cache_creation = SimpleNamespace(**split)
    else:
        from litellm.types.utils import CacheCreationTokenDetails, PromptTokensDetailsWrapper, Usage
        raw_usage = Usage(
            **vars(raw_usage),
            prompt_tokens_details=PromptTokensDetailsWrapper(
                cached_tokens=2000, cache_creation_tokens=32000,
                cache_creation_token_details=CacheCreationTokenDetails(**split),
            ),
        )
    usage = LiteLLMTransport()._extract_usage(SimpleNamespace(usage=raw_usage))
    assert usage.input_tokens == 35000
    assert usage.output_tokens == 10
    assert usage.total_tokens == 35010
    assert usage.input_tokens_cache_read == 2000
    assert usage.input_tokens_cache_write == 32000
    assert usage.input_tokens_cache_write_5m == 12000
    assert usage.input_tokens_cache_write_1h == 20000


def test_extract_usage_without_split_keeps_it_unknown() -> None:
    usage = LiteLLMTransport()._extract_usage(SimpleNamespace(usage=SimpleNamespace(
        prompt_tokens=33000, cache_creation_input_tokens=32000,
    )))
    assert usage.input_tokens_cache_write_5m is None
    assert usage.input_tokens_cache_write_1h is None
