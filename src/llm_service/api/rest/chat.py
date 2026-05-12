from __future__ import annotations
import asyncio
import time
import uuid
from collections.abc import AsyncIterator
from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import StreamingResponse

from src.llm_service.api.deps import get_secret_backend, get_tenant
from src.llm_service.normaliser import normalise
from src.llm_service.schemas.chat import (
    CacheBlock, ChatRequest, ChatResponse, Choice, ChoiceMessage,
    ErrorData, FinishData, TokenData, UsageBlock,
)
from src.shared.db.models import Tenant
from src.shared.schemas.envelope import CostBlock, GuardrailsBlock, LatencyBlock, PolicyBlock
from src.shared.secrets.backend import MissingTenantKeyError, SecretBackend

router = APIRouter(prefix="/v1")


def _new_request_id() -> str:
    return f"req_{uuid.uuid4().hex[:24]}"


def _stub_non_stream(
    body: ChatRequest, tenant: Tenant, request_id: str, elapsed_ms: int
) -> ChatResponse:
    return ChatResponse(
        id=request_id,
        created=int(time.time()),
        tenant_id=tenant.id,
        provider=body.provider,
        model=body.model,
        transport="stub",
        choices=[
            Choice(
                index=0,
                message=ChoiceMessage(
                    role="assistant",
                    content="[stub] Real LLM not wired yet.",
                ),
                finish_reason="stop",
            )
        ],
        usage=UsageBlock(input_tokens=10, output_tokens=7, total_tokens=17),
        cost=CostBlock(computed_usd=0.0, pricing_version=0),
        latency_ms=LatencyBlock(total=elapsed_ms, upstream=elapsed_ms),
        cache=CacheBlock(our_cache_hit=False),
        guardrails=GuardrailsBlock(),
        policy=PolicyBlock(),
    )


async def _stub_stream(body: ChatRequest, request_id: str) -> AsyncIterator[str]:
    tokens = ["[stub] ", "Real ", "LLM ", "not ", "wired ", "yet."]
    start = time.monotonic()
    first_token_ms: int | None = None

    for token in tokens:
        await asyncio.sleep(0.08)  # simulate per-token latency
        if first_token_ms is None:
            first_token_ms = int((time.monotonic() - start) * 1000)
        data = TokenData(index=0, delta=token)
        yield f"event: token\ndata: {data.model_dump_json()}\n\n"

    elapsed_ms = int((time.monotonic() - start) * 1000)
    finish = FinishData(
        id=request_id,
        finish_reason="stop",
        usage=UsageBlock(input_tokens=10, output_tokens=6, total_tokens=16),
        cost=CostBlock(computed_usd=0.0, pricing_version=0),
        latency_ms=LatencyBlock(
            total=elapsed_ms,
            upstream=elapsed_ms,
            time_to_first_token=first_token_ms,
        ),
        cache=CacheBlock(our_cache_hit=False),
        guardrails=GuardrailsBlock(),
        policy=PolicyBlock(),
    )
    yield f"event: finish\ndata: {finish.model_dump_json()}\n\n"


@router.post("/chat", response_model=ChatResponse)
async def chat(
    body: ChatRequest, request: Request, tenant: Tenant = Depends(get_tenant),
    secret_backend: SecretBackend = Depends(get_secret_backend),
) -> ChatResponse:
    start = time.monotonic()
    request_id = request.headers.get("x-request-id") or _new_request_id()
    normalised = normalise(body)
    try:
        tenant_key = await secret_backend.get_key(tenant.id, body.provider)
    except MissingTenantKeyError:
        raise HTTPException(status_code=422, detail="missing_tenant_key")
    # TODO: Step 5 — policy check
    # TODO: Step 6 — guardrails input
    # TODO: Step 7 — cache lookup
    # TODO: Step 8 — route to provider + upstream call
    # TODO: Step 9 — guardrails output
    # TODO: Step 10 — ledger write
    elapsed_ms = int((time.monotonic() - start) * 1000)
    return _stub_non_stream(body, tenant, request_id, elapsed_ms)


@router.post("/chat/stream")
async def chat_stream(
    body: ChatRequest, request: Request, tenant: Tenant = Depends(get_tenant),
    secret_backend: SecretBackend = Depends(get_secret_backend),
) -> StreamingResponse:
    request_id = request.headers.get("x-request-id") or _new_request_id()
    normalised = normalise(body)                                        # Step 4
    try:
        tenant_key = await secret_backend.get_key(tenant.id, body.provider)   # Step 3
    except MissingTenantKeyError:
        raise HTTPException(status_code=422, detail="missing_tenant_key")
    # TODO: Step 5 — policy check
    # TODO: Step 6 — guardrails input
    # TODO: Step 7 — cache lookup
    # TODO: Step 8 — route to provider + upstream call
    # TODO: Step 9 — guardrails output
    # TODO: Step 10 — ledger write
    return StreamingResponse(
        _stub_stream(body, request_id),
        media_type="text/event-stream",
        headers={
            "X-Request-Id": request_id,
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
        },
    )
