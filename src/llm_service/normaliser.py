from __future__ import annotations

from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from src.llm_service.schemas.chat import ChatParams, ChatRequest, NormalisedLLMRequest
from src.shared.db.models import TenantDefaults


async def normalise(request: ChatRequest, db: AsyncSession, tenant_id: str) -> NormalisedLLMRequest:
    provider = request.provider
    model = request.model
    params = request.params

    if request.use_defaults is True:
        result = await db.execute(select(TenantDefaults).where(TenantDefaults.tenant_id == tenant_id))
        defaults = result.scalar_one_or_none()
        if defaults is not None:
            provider = provider or defaults.default_provider
            model = model or defaults.default_model
            if defaults.default_params:
                default_params = ChatParams(**defaults.default_params)
                merged = (params.model_dump() if params else {})
                for field, default_value in default_params.model_dump().items():
                    if merged.get(field) is None and default_value is not None:
                        merged[field] = default_value
                params = ChatParams(**merged)

    if not provider or not model:
        raise HTTPException(status_code=400, detail="provider and model are required")

    return NormalisedLLMRequest(
        provider=provider,
        model=model,
        messages=request.messages,
        tools=request.tools,
        tool_choice=request.tool_choice,
        params=params,
        metadata=request.metadata,
        provider_options=request.provider_options,
    )
