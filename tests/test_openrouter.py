"""Unit tests for OpenRouter support routed through LiteLLMTransport."""
from __future__ import annotations

import litellm
import pytest

from src.llm_service.cache.keys import make_cache_key
from src.llm_service.providers import litellm as litellm_transport
from src.llm_service.providers.litellm import LiteLLMTransport, _extract_response_cost
from src.llm_service.schemas.chat import MessageParam, NormalisedLLMRequest
from src.shared.db.enums import KeyFormat
from src.shared.secrets.backend import TenantKeyPayload


# ── fakes ──────────────────────────────────────────────────────────────────────

class _FakeMessage:
    def __init__(self) -> None:
        self.role = "assistant"
        self.content = "hello"
        self.tool_calls = None
        self.provider_specific_fields = None


class _FakeChoice:
    def __init__(self) -> None:
        self.index = 0
        self.message = _FakeMessage()
        self.finish_reason = "stop"


class _FakeUsage:
    def __init__(self, cost: float | None = None) -> None:
        self.prompt_tokens = 10
        self.completion_tokens = 5
        self.total_tokens = 15
        self.prompt_tokens_details = None
        if cost is not None:
            self.cost = cost


class _FakeRaw:
    def __init__(self, hidden: dict | None = None, cost: float | None = None) -> None:
        self.usage = _FakeUsage(cost=cost)
        self.choices = [_FakeChoice()]
        self._hidden_params = hidden or {}

    def model_dump(self) -> dict:
        return {"ok": True}


def _req(provider: str = "openrouter", provider_options: dict | None = None) -> NormalisedLLMRequest:
    return NormalisedLLMRequest(
        provider=provider,
        model="openai/gpt-4o",
        messages=[MessageParam(role="user", content="hi")],
        provider_options=provider_options,
    )


_KEY = TenantKeyPayload(key_format=KeyFormat.API_KEY, data={"api_key": "sk-or-test"})


# ── model string ─────────────────────────────────────────────────────────────

def test_model_string_openrouter() -> None:
    assert LiteLLMTransport()._model_string("openrouter", "openai/gpt-4o") == "openrouter/openai/gpt-4o"


# ── cost extraction helper ───────────────────────────────────────────────────

def test_extract_cost_prefers_hidden_params() -> None:
    raw = _FakeRaw(hidden={"response_cost": 0.0123}, cost=0.99)
    assert _extract_response_cost(raw) == 0.0123


def test_extract_cost_falls_back_to_usage_cost() -> None:
    raw = _FakeRaw(hidden={}, cost=0.05)
    assert _extract_response_cost(raw) == 0.05


def test_extract_cost_none_when_absent() -> None:
    assert _extract_response_cost(_FakeRaw()) is None


def test_extract_cost_rejects_negative() -> None:
    assert _extract_response_cost(_FakeRaw(hidden={"response_cost": -0.01})) is None


def test_extract_cost_rejects_nan() -> None:
    assert _extract_response_cost(_FakeRaw(hidden={"response_cost": float("nan")})) is None


def test_extract_cost_rejects_infinite() -> None:
    assert _extract_response_cost(_FakeRaw(hidden={"response_cost": float("inf")})) is None


def test_extract_cost_rejects_negative_usage_cost() -> None:
    assert _extract_response_cost(_FakeRaw(hidden={}, cost=-0.05)) is None


# ── chat() surfaces provider-reported cost ──────────────────────────────────

@pytest.mark.asyncio
async def test_chat_sets_provider_reported_cost(monkeypatch) -> None:
    captured: dict = {}

    async def _fake_acompletion(**kwargs):
        captured.update(kwargs)
        return _FakeRaw(hidden={"response_cost": 0.0077})

    monkeypatch.setattr(litellm, "acompletion", _fake_acompletion)
    resp = await LiteLLMTransport().chat(_req(), _KEY)
    assert resp.cost.provider_reported_usd == 0.0077
    assert captured["model"] == "openrouter/openai/gpt-4o"


# ── provider_options → extra_body / extra_headers ───────────────────────────

def test_openrouter_kwargs_maps_provider_options() -> None:
    opts = {
        "provider": {"order": ["Anthropic"], "allow_fallbacks": False},
        "models": ["anthropic/claude-3.5-sonnet"],
        "referer": "https://app.example.com",
        "title": "ai-service",
    }
    kwargs = LiteLLMTransport()._openrouter_kwargs(_req(provider_options=opts))
    assert kwargs["extra_body"]["provider"] == opts["provider"]
    assert kwargs["extra_body"]["models"] == opts["models"]
    assert kwargs["extra_headers"] == {
        "HTTP-Referer": "https://app.example.com",
        "X-Title": "ai-service",
    }


def test_openrouter_kwargs_empty_for_other_provider() -> None:
    opts = {"models": ["anthropic/claude-3.5-sonnet"]}
    assert LiteLLMTransport()._openrouter_kwargs(_req(provider="openai", provider_options=opts)) == {}


def test_openrouter_kwargs_uses_settings_defaults(monkeypatch) -> None:
    monkeypatch.setattr(litellm_transport.settings, "openrouter_app_url", "https://default.example")
    monkeypatch.setattr(litellm_transport.settings, "openrouter_app_title", "default-title")
    kwargs = LiteLLMTransport()._openrouter_kwargs(_req(provider_options=None))
    assert kwargs["extra_headers"] == {
        "HTTP-Referer": "https://default.example",
        "X-Title": "default-title",
    }
    assert "extra_body" not in kwargs


@pytest.mark.asyncio
async def test_chat_forwards_openrouter_kwargs_to_litellm(monkeypatch) -> None:
    captured: dict = {}

    async def _fake_acompletion(**kwargs):
        captured.update(kwargs)
        return _FakeRaw()

    monkeypatch.setattr(litellm, "acompletion", _fake_acompletion)
    opts = {"provider": {"order": ["OpenAI"]}, "title": "ai-service"}
    await LiteLLMTransport().chat(_req(provider_options=opts), _KEY)
    assert captured["extra_body"] == {"provider": {"order": ["OpenAI"]}}
    assert captured["extra_headers"] == {"X-Title": "ai-service"}


# ── cache key isolates provider_options ─────────────────────────────────────

def test_cache_key_changes_with_provider_options() -> None:
    base = make_cache_key("tenant", _req(provider_options=None))
    with_opts = make_cache_key("tenant", _req(provider_options={"models": ["x/y"]}))
    assert base != with_opts


# ── _compute_cost prefers reported cost ─────────────────────────────────────

def test_compute_cost_prefers_reported_over_yaml() -> None:
    from src.llm_service.api.rest.chat import _compute_cost
    from src.llm_service.schemas.chat import UsageBlock

    # Unknown OpenRouter model → YAML has no pricing entry, so computed_usd falls back
    # to the provider-reported cost (callers should only ever need to read computed_usd).
    cost = _compute_cost(
        "openrouter", "openai/gpt-4o", UsageBlock(input_tokens=10, output_tokens=5),
        provider_reported_usd=0.0042,
    )
    assert cost.provider_reported_usd == 0.0042
    assert cost.computed_usd == 0.0042


def test_compute_cost_none_reported_leaves_field_unset() -> None:
    from src.llm_service.api.rest.chat import _compute_cost
    from src.llm_service.schemas.chat import UsageBlock

    cost = _compute_cost(
        "openrouter", "openai/gpt-4o", UsageBlock(input_tokens=10, output_tokens=5),
        provider_reported_usd=None,
    )
    assert cost.provider_reported_usd is None
