from __future__ import annotations

from fastapi import HTTPException

from src.llm_service.schemas.chat import ChatRequest, NormalisedLLMRequest


def normalise(request: ChatRequest) -> NormalisedLLMRequest:
    if not request.provider or not request.model:
        raise HTTPException(status_code=400, detail="provider and model are required")
    return NormalisedLLMRequest(
        provider=request.provider,
        model=request.model,
        messages=request.messages,
        tools=request.tools,
        tool_choice=request.tool_choice,
        params=request.params,
        metadata=request.metadata,
    )
