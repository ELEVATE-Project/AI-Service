"""GET /v1/models marks which models POST /v1/chat will admit. Catalog and secrets are mocked."""
from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from src.llm_service.api.rest import models as models_api
from src.llm_service.schemas.models import ModelInfo, ModelPricing
from src.shared.ledger.pricing import pricing_table
from src.shared.secrets.backend import MissingTenantKeyError


def _info(provider: str, model_id: str, source: str = "litellm", mode: str = "chat") -> ModelInfo:
    return ModelInfo(provider=provider, id=model_id, name=model_id, mode=mode,
                     pricing=ModelPricing(), source=source, raw={})


@pytest.fixture
def catalog(monkeypatch):
    monkeypatch.setattr(models_api, "known_litellm_providers", lambda: {"anthropic", "openai", "groq"})
    litellm_models = [
        _info("anthropic", "claude-sonnet-4-5"),      # priced in pricing/models.yaml
        _info("groq", "llama-3.1-70b-versatile"),     # not priced
        _info("openai", "text-embedding-3-small", mode="embedding"),  # not priced, non-chat
    ]
    monkeypatch.setattr(models_api, "list_litellm_models",
                        lambda wanted: [m for m in litellm_models if m.provider in wanted])
    monkeypatch.setattr(models_api, "list_openrouter_models", AsyncMock(return_value=[
        _info("openrouter", "vendor/unpriced-model", source="openrouter"),
    ]))
    backend = SimpleNamespace(get_key=AsyncMock(return_value=SimpleNamespace(data={"api_key": "k"})))
    return backend


async def _list(backend, **kwargs):
    args = {"provider": None, "mode": None, "q": None}
    args.update(kwargs)
    return await models_api.list_models(
        tenant=SimpleNamespace(id="t"), secret_backend=backend, **args,
    )


@pytest.mark.asyncio
async def test_models_list_marks_chat_admission(catalog) -> None:
    response = await _list(catalog)
    admitted = {(m.provider, m.id): m.chat_admitted for m in response.data}
    assert admitted == {
        ("openrouter", "vendor/unpriced-model"): True,
        ("anthropic", "claude-sonnet-4-5"): True,
        ("groq", "llama-3.1-70b-versatile"): False,
        ("openai", "text-embedding-3-small"): False,
    }
    assert pricing_table.has_model("anthropic", "claude-sonnet-4-5")


@pytest.mark.asyncio
async def test_models_list_does_not_filter_unadmitted_models(catalog) -> None:
    response = await _list(catalog, provider="groq")
    assert [m.id for m in response.data] == ["llama-3.1-70b-versatile"]
    assert response.data[0].chat_admitted is False


@pytest.mark.asyncio
async def test_models_list_without_openrouter_key_still_marks_litellm_models(catalog) -> None:
    catalog.get_key = AsyncMock(side_effect=MissingTenantKeyError("no key"))
    response = await _list(catalog)
    assert {m.provider for m in response.data} == {"anthropic", "groq", "openai"}
    assert all(isinstance(m.chat_admitted, bool) for m in response.data)
