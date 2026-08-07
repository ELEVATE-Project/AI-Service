from __future__ import annotations

from typing import Optional

import httpx
import litellm

from src.llm_service.schemas.models import ModelInfo, ModelPricing

_OPENROUTER_MODELS_URL = "https://openrouter.ai/api/v1/models/user"

# litellm_provider tags that are cost-table categories, not routable providers.
_PROVIDER_ALIASES: dict[str, str] = {
    "bedrock_converse": "bedrock",
}


def _canonical_provider(litellm_provider: str) -> Optional[str]:
    canonical = _PROVIDER_ALIASES.get(litellm_provider, litellm_provider)
    if canonical.startswith("vertex_ai-"):
        canonical = "vertex_ai"
    return canonical if canonical in litellm.provider_list else None


def _strip_provider_prefix(provider: str, key: str) -> str:
    """Strip the provider prefix litellm embeds in model_cost keys (e.g.
    "azure/ada" -> "ada"). Matched by splitting on the first "/" rather than
    requiring an exact match against `litellm_provider` — some entries embed a
    differently-spelled prefix (e.g. key "amazon-nova/nova-micro-v1" for
    litellm_provider "amazon_nova"), so an exact-match strip silently fails and
    leaks the raw key through as the model id.
    """
    exact_prefix = f"{provider}/"
    if key.startswith(exact_prefix):
        return key[len(exact_prefix):]
    return key.split("/", 1)[1] if "/" in key else key


def known_litellm_providers() -> set[str]:
    """Every provider in litellm's bundled catalog, minus openrouter (handled
    separately via its own live API). Derived from litellm.model_cost rather
    than a fixed list, so a new provider shows up automatically the moment the
    installed litellm version knows about it — no code change required.
    """
    providers = set()
    for info in litellm.model_cost.values():
        canonical = _canonical_provider(info.get("litellm_provider") or "")
        if canonical and canonical != "openrouter":
            providers.add(canonical)
    return providers


def list_litellm_models(providers: Optional[set[str]] = None) -> list[ModelInfo]:
    """Filter litellm's bundled model_cost catalog to the given providers
    (defaults to every known provider), across all modes (chat, embedding,
    image_generation, ...). This is a static snapshot shipped with the
    installed litellm package version — it can lag behind what a provider
    actually serves, but a model missing here still routes fine via
    LiteLLMTransport passthrough.
    """
    wanted = providers if providers is not None else known_litellm_providers()
    models: list[ModelInfo] = []
    for key, info in litellm.model_cost.items():
        raw_provider = info.get("litellm_provider")
        provider = _canonical_provider(raw_provider or "")
        if provider is None or provider not in wanted:
            continue
        model_id = _strip_provider_prefix(raw_provider, key)
        if provider == "bedrock" and "/" in model_id:
            continue  # region/commitment-tier pricing duplicate, not a real model id
        input_cost = info.get("input_cost_per_token")
        output_cost = info.get("output_cost_per_token")
        cache_read = info.get("cache_read_input_token_cost")
        cache_write = info.get("cache_creation_input_token_cost")
        models.append(ModelInfo(
            provider=provider,
            id=model_id,
            name=model_id,
            mode=info.get("mode"),
            context_length=info.get("max_input_tokens") or info.get("max_tokens"),
            max_output_tokens=info.get("max_output_tokens"),
            pricing=ModelPricing(
                input_per_1k=input_cost * 1000 if input_cost is not None else None,
                output_per_1k=output_cost * 1000 if output_cost is not None else None,
                cache_read_per_1k=cache_read * 1000 if cache_read is not None else None,
                cache_write_per_1k=cache_write * 1000 if cache_write is not None else None,
            ),
            input_modalities=info.get("supported_modalities"),
            output_modalities=info.get("supported_output_modalities"),
            supports_tools=info.get("supports_function_calling"),
            supports_vision=info.get("supports_vision"),
            supports_reasoning=info.get("supports_reasoning"),
            source="litellm",
            raw=info,
        ))
    return models


async def get_openrouter_model_endpoints(api_key: str, model: str) -> dict:
    """Live fetch of the upstream hosting providers for one OpenRouter model
    (e.g. DeepInfra, Nebius) — a different, per-model resource from the bulk
    catalog. Not cached, same as list_openrouter_models.
    """
    async with httpx.AsyncClient(timeout=15.0) as client:
        resp = await client.get(
            f"{_OPENROUTER_MODELS_URL.rsplit('/', 1)[0]}/{model}/endpoints",
            headers={"Authorization": f"Bearer {api_key}"},
        )
        resp.raise_for_status()
    return resp.json()["data"]


async def list_openrouter_models(api_key: str) -> list[ModelInfo]:
    """Live fetch from OpenRouter's own catalog — deliberately not cached here;
    callers that need caching should do it on their side.
    """
    async with httpx.AsyncClient(timeout=15.0) as client:
        resp = await client.get(
            _OPENROUTER_MODELS_URL,
            params={"limit": 500},
            headers={"Authorization": f"Bearer {api_key}"},
        )
        resp.raise_for_status()
    payload = resp.json()

    models: list[ModelInfo] = []
    for entry in payload.get("data", []):
        pricing = entry.get("pricing") or {}
        architecture = entry.get("architecture") or {}
        top_provider = entry.get("top_provider") or {}
        supported_params = entry.get("supported_parameters")
        input_modalities = architecture.get("input_modalities")

        def _rate(field: str) -> Optional[float]:
            raw = pricing.get(field)
            return float(raw) * 1000 if raw not in (None, "") else None

        models.append(ModelInfo(
            provider="openrouter",
            id=entry["id"],
            name=entry.get("name", entry["id"]),
            context_length=entry.get("context_length"),
            max_output_tokens=top_provider.get("max_completion_tokens"),
            pricing=ModelPricing(
                input_per_1k=_rate("prompt"),
                output_per_1k=_rate("completion"),
                cache_read_per_1k=_rate("input_cache_read"),
                cache_write_per_1k=_rate("input_cache_write"),
            ),
            input_modalities=input_modalities,
            output_modalities=architecture.get("output_modalities"),
            supports_tools=("tools" in supported_params) if supported_params is not None else None,
            supports_vision=("image" in input_modalities) if input_modalities is not None else None,
            supports_reasoning=entry.get("reasoning") is not None,
            source="openrouter",
            raw=entry,
        ))
    return models