from __future__ import annotations

from typing import Optional

import httpx
from fastapi import APIRouter, Depends, HTTPException, Query

from src.llm_service.api.deps import get_secret_backend, get_tenant
from src.llm_service.providers.catalog import (
    get_openrouter_model_endpoints, known_litellm_providers, known_modes,
    list_litellm_models, list_openrouter_models,
)
from src.llm_service.schemas.chat import (
    CACHE_CAPABLE_PROVIDERS, CACHE_TARGET_DEFAULT, CACHE_TARGET_VALUES, CACHE_TTL_VALUES,
)
from src.llm_service.schemas.models import (
    CacheOptionsInfo, CacheOptionsResponse, ModelEndpointsResponse, ModelInfo,
    ModelsListResponse, ModesListResponse, ProviderInfo, ProvidersListResponse,
)
from src.shared.db.models import Tenant
from src.shared.secrets.backend import MissingTenantKeyError, SecretBackend

router = APIRouter(prefix="/v1")


@router.get("/providers", response_model=ProvidersListResponse)
async def list_providers(tenant: Tenant = Depends(get_tenant)) -> ProvidersListResponse:
    providers = [ProviderInfo(name="openrouter", source="openrouter")] + [
        ProviderInfo(name=name, source="litellm") for name in sorted(known_litellm_providers())
    ]
    return ProvidersListResponse(data=providers)


@router.get("/models/modes", response_model=ModesListResponse)
async def list_modes(tenant: Tenant = Depends(get_tenant)) -> ModesListResponse:
    return ModesListResponse(data=sorted(known_modes()))


@router.get("/models", response_model=ModelsListResponse)
async def list_models(
    provider: Optional[str] = Query(default=None),
    mode: Optional[str] = Query(default=None, description="Filter by mode, e.g. chat, embedding"),
    q: Optional[str] = Query(default=None, description="Substring filter on model id/name"),
    tenant: Tenant = Depends(get_tenant),
    secret_backend: SecretBackend = Depends(get_secret_backend),
) -> ModelsListResponse:
    litellm_providers = known_litellm_providers()
    if provider is not None and provider != "openrouter" and provider not in litellm_providers:
        raise HTTPException(status_code=422, detail="unknown_provider")

    models: list[ModelInfo] = []

    if provider is None or provider == "openrouter":
        try:
            key = await secret_backend.get_key(tenant.id, "openrouter")
        except MissingTenantKeyError:
            if provider == "openrouter":
                raise HTTPException(status_code=422, detail="missing_tenant_key")
            key = None
        else:
            try:
                models.extend(await list_openrouter_models(key.data["api_key"]))
            except httpx.HTTPError as exc:
                if provider == "openrouter":
                    raise HTTPException(status_code=502, detail="openrouter_catalog_unavailable") from exc

    if provider is None or provider in litellm_providers:
        wanted = {provider} if provider else litellm_providers
        models.extend(list_litellm_models(wanted))

    if mode:
        models = [m for m in models if m.mode == mode]

    if q:
        needle = q.lower()
        models = [m for m in models if needle in m.id.lower() or needle in m.name.lower()]

    return ModelsListResponse(data=models)


@router.get("/cache/options", response_model=CacheOptionsResponse)
async def get_cache_options(tenant: Tenant = Depends(get_tenant)) -> CacheOptionsResponse:
    """Supported values for `params.cache_options` — poll this instead of hardcoding
    ttl/target values on the calling-service side."""
    return CacheOptionsResponse(
        data=CacheOptionsInfo(
            providers=list(CACHE_CAPABLE_PROVIDERS),
            ttl_values=list(CACHE_TTL_VALUES),
            ttl_default=None,
            target_values=list(CACHE_TARGET_VALUES),
            target_default=list(CACHE_TARGET_DEFAULT),
        )
    )


@router.get("/models/endpoints", response_model=ModelEndpointsResponse)
async def list_model_endpoints(
    provider: str = Query(...),
    model: str = Query(...),
    tenant: Tenant = Depends(get_tenant),
    secret_backend: SecretBackend = Depends(get_secret_backend),
) -> ModelEndpointsResponse:
    if provider != "openrouter":
        raise HTTPException(status_code=422, detail="unsupported_provider")
    try:
        key = await secret_backend.get_key(tenant.id, "openrouter")
    except MissingTenantKeyError:
        raise HTTPException(status_code=422, detail="missing_tenant_key")
    try:
        data = await get_openrouter_model_endpoints(key.data["api_key"], model)
    except httpx.HTTPError as exc:
        raise HTTPException(status_code=502, detail="openrouter_endpoints_unavailable") from exc
    return ModelEndpointsResponse(data=data)