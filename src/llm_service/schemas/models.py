from __future__ import annotations

from typing import Any, Literal, Optional

from pydantic import BaseModel


class ModelPricing(BaseModel):
    input_per_1k: Optional[float] = None
    output_per_1k: Optional[float] = None
    cache_read_per_1k: Optional[float] = None
    cache_write_per_1k: Optional[float] = None


class ModelInfo(BaseModel):
    provider: str
    id: str
    name: str
    mode: Optional[str] = None
    context_length: Optional[int] = None
    max_output_tokens: Optional[int] = None
    pricing: ModelPricing
    input_modalities: Optional[list[str]] = None
    output_modalities: Optional[list[str]] = None
    supports_tools: Optional[bool] = None
    supports_vision: Optional[bool] = None
    supports_reasoning: Optional[bool] = None
    source: Literal["litellm", "openrouter"]
    # Complete, unmodified source record — every field the upstream source
    # exposes, so new fields show up here without a schema change.
    raw: dict[str, Any]


class ModelsListResponse(BaseModel):
    data: list[ModelInfo]


class ProviderInfo(BaseModel):
    name: str
    source: Literal["litellm", "openrouter"]


class ProvidersListResponse(BaseModel):
    data: list[ProviderInfo]


class ModesListResponse(BaseModel):
    data: list[str]


class ModelEndpointsResponse(BaseModel):
    # Raw pass-through of OpenRouter's per-model endpoints response —
    # the upstream hosting providers (DeepInfra, Nebius, ...) for one model.
    data: dict[str, Any]