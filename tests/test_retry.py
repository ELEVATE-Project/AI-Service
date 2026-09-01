"""Unit tests for the per-request retry override (params.retry)."""
from __future__ import annotations

import litellm
import pytest
from pydantic import ValidationError

from src.llm_service.providers.base import UpstreamTransportError
from src.llm_service.providers.litellm import LiteLLMTransport, _retry_settings
from src.llm_service.schemas.chat import (
    RETRY_BACKOFF_BASE_S_CEILING, RETRY_MAX_ATTEMPTS_CEILING,
    ChatParams, MessageParam, NormalisedLLMRequest, RetryOptions,
)
from src.shared.config import settings
from src.shared.db.enums import KeyFormat
from src.shared.secrets.backend import TenantKeyPayload


def _req(params: ChatParams | None = None) -> NormalisedLLMRequest:
    return NormalisedLLMRequest(
        provider="openai",
        model="gpt-4o",
        messages=[MessageParam(role="user", content="hi")],
        params=params,
    )


_KEY = TenantKeyPayload(key_format=KeyFormat.API_KEY, data={"api_key": "sk-test"})


# ── _retry_settings resolution ──────────────────────────────────────────────────

def test_retry_settings_defaults_to_service_settings() -> None:
    assert _retry_settings(_req()) == (settings.llm_retry_max_attempts, settings.llm_retry_backoff_base_s)


def test_retry_settings_disabled_forces_single_attempt() -> None:
    req = _req(ChatParams(retry=RetryOptions(enabled=False)))
    max_attempts, _ = _retry_settings(req)
    assert max_attempts == 1


def test_retry_settings_overrides_max_attempts_and_backoff() -> None:
    req = _req(ChatParams(retry=RetryOptions(max_attempts=7, backoff_base_s=0.5)))
    assert _retry_settings(req) == (7, 0.5)


def test_retry_settings_partial_override_falls_back_to_settings() -> None:
    req = _req(ChatParams(retry=RetryOptions(max_attempts=5)))
    max_attempts, backoff_base_s = _retry_settings(req)
    assert max_attempts == 5
    assert backoff_base_s == settings.llm_retry_backoff_base_s


# ── schema-boundary validation ──────────────────────────────────────────────────
# max_attempts=0 (or negative) would leave the retry loop's raw/response_stream as
# None, crashing further down (AttributeError -> 500) instead of erroring cleanly.

@pytest.mark.parametrize("max_attempts", [0, -1, RETRY_MAX_ATTEMPTS_CEILING + 1])
def test_retry_options_rejects_out_of_range_max_attempts(max_attempts: int) -> None:
    with pytest.raises(ValidationError):
        RetryOptions(max_attempts=max_attempts)


@pytest.mark.parametrize("backoff_base_s", [-0.1, RETRY_BACKOFF_BASE_S_CEILING + 1])
def test_retry_options_rejects_out_of_range_backoff(backoff_base_s: float) -> None:
    with pytest.raises(ValidationError):
        RetryOptions(backoff_base_s=backoff_base_s)


@pytest.mark.parametrize("max_attempts", [1, RETRY_MAX_ATTEMPTS_CEILING])
def test_retry_options_accepts_boundary_max_attempts(max_attempts: int) -> None:
    RetryOptions(max_attempts=max_attempts)


@pytest.mark.parametrize("backoff_base_s", [0.0, RETRY_BACKOFF_BASE_S_CEILING])
def test_retry_options_accepts_boundary_backoff(backoff_base_s: float) -> None:
    RetryOptions(backoff_base_s=backoff_base_s)


# ── end-to-end retry loop behavior ──────────────────────────────────────────────

@pytest.mark.asyncio
async def test_chat_retries_up_to_service_default(monkeypatch) -> None:
    calls = {"n": 0}

    async def _fake_acompletion(**kwargs):
        calls["n"] += 1
        raise litellm.ServiceUnavailableError(
            message="down", llm_provider="openai", model="gpt-4o",
        )

    monkeypatch.setattr(litellm, "acompletion", _fake_acompletion)
    with pytest.raises(UpstreamTransportError):
        await LiteLLMTransport().chat(_req(), _KEY)
    assert calls["n"] == settings.llm_retry_max_attempts


@pytest.mark.asyncio
async def test_chat_retry_disabled_makes_a_single_attempt(monkeypatch) -> None:
    calls = {"n": 0}

    async def _fake_acompletion(**kwargs):
        calls["n"] += 1
        raise litellm.ServiceUnavailableError(
            message="down", llm_provider="openai", model="gpt-4o",
        )

    monkeypatch.setattr(litellm, "acompletion", _fake_acompletion)
    req = _req(ChatParams(retry=RetryOptions(enabled=False)))
    with pytest.raises(UpstreamTransportError):
        await LiteLLMTransport().chat(req, _KEY)
    assert calls["n"] == 1


@pytest.mark.asyncio
async def test_chat_retry_max_attempts_override(monkeypatch) -> None:
    calls = {"n": 0}

    async def _fake_acompletion(**kwargs):
        calls["n"] += 1
        raise litellm.ServiceUnavailableError(
            message="down", llm_provider="openai", model="gpt-4o",
        )

    monkeypatch.setattr(litellm, "acompletion", _fake_acompletion)
    req = _req(ChatParams(retry=RetryOptions(max_attempts=2, backoff_base_s=0.0)))
    with pytest.raises(UpstreamTransportError):
        await LiteLLMTransport().chat(req, _KEY)
    assert calls["n"] == 2
