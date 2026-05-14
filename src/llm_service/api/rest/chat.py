from __future__ import annotations

import asyncio
import time
import uuid
from collections.abc import AsyncIterator

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import StreamingResponse

from src.llm_service.api.deps import (
    get_cache, get_guardrails, get_policy_checker, get_secret_backend, get_tenant,
)
from src.llm_service.cache.base import CacheBackend
from src.llm_service.cache.keys import make_cache_key
from src.llm_service.normaliser import normalise
from src.llm_service.schemas.chat import (
    CacheBlock, ChatRequest, ChatResponse, Choice, ChoiceMessage,
    ErrorData, FinishData, MessageParam, TokenData, UsageBlock,
)
from src.shared.config import settings
from src.shared.db.models import Tenant
from src.shared.guardrails.base import GuardrailsChecker, GuardrailsResult
from src.shared.policy.checker import PolicyChecker, PolicyContext, PolicyExceededError
from src.shared.schemas.envelope import CostBlock, GuardrailsBlock, LatencyBlock, PolicyBlock
from src.shared.secrets.backend import MissingTenantKeyError, SecretBackend

router = APIRouter(prefix="/v1")


def _new_request_id() -> str:
    return f"req_{uuid.uuid4().hex[:24]}"


_STUB_CONTENT = "[stub] Real LLM not wired yet."


def _stub_non_stream(
    body: ChatRequest, tenant: Tenant, request_id: str, elapsed_ms: int,
    input_result: GuardrailsResult, output_result: GuardrailsResult,
    final_content: str,
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
                message=ChoiceMessage(role="assistant", content=final_content),
                finish_reason="stop",
            )
        ],
        usage=UsageBlock(input_tokens=10, output_tokens=7, total_tokens=17),
        cost=CostBlock(computed_usd=0.0, pricing_version=0),
        latency_ms=LatencyBlock(total=elapsed_ms, upstream=elapsed_ms),
        cache=CacheBlock(our_cache_hit=False),
        guardrails=GuardrailsBlock(
            input_flags=input_result.flags,
            output_flags=output_result.flags,
            redactions_applied=input_result.redactions + output_result.redactions,
        ),
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
    policy_checker: PolicyChecker = Depends(get_policy_checker),
    guardrails: GuardrailsChecker = Depends(get_guardrails),
    cache: CacheBackend = Depends(get_cache),
) -> ChatResponse:
    start = time.monotonic()
    request_id = request.headers.get("x-request-id") or _new_request_id()
    normalised = normalise(body)
    try:
        tenant_key = await secret_backend.get_key(tenant.id, body.provider)
    except MissingTenantKeyError:
        raise HTTPException(status_code=422, detail="missing_tenant_key")
    try:
        await policy_checker.check(tenant.id, PolicyContext(
            provider=normalised.provider, model=normalised.model,
            max_tokens=normalised.params.max_tokens if normalised.params else None,
        ))
    except PolicyExceededError as e:
        raise HTTPException(status_code=429, detail=e.detail)
    input_result = await guardrails.check_input([m.model_dump() for m in normalised.messages])
    if input_result.blocked:
        raise HTTPException(status_code=400, detail="guardrails_blocked")
    if input_result.modified_messages is not None:
        normalised = normalised.model_copy(update={
            "messages": [MessageParam(**m) for m in input_result.modified_messages]
        })
    cache_key = make_cache_key(tenant.id, normalised)
    cached = await cache.get(cache_key)
    if cached:
        return cached.model_copy(update={"cache": CacheBlock(our_cache_hit=True)})
    # TODO: Step 8 — route to provider + upstream call
    output_result = await guardrails.check_output(_STUB_CONTENT)
    if output_result.blocked:
        raise HTTPException(status_code=400, detail="guardrails_blocked")
    final_content = output_result.modified_content or _STUB_CONTENT
    # TODO: Step 10 — ledger write
    elapsed_ms = int((time.monotonic() - start) * 1000)
    response = _stub_non_stream(body, tenant, request_id, elapsed_ms, input_result, output_result, final_content)
    await cache.set(cache_key, response, ttl_seconds=settings.cache_ttl_seconds)
    return response


@router.post("/chat/stream")
async def chat_stream(
    body: ChatRequest, request: Request, tenant: Tenant = Depends(get_tenant),
    secret_backend: SecretBackend = Depends(get_secret_backend),
    policy_checker: PolicyChecker = Depends(get_policy_checker),
    guardrails: GuardrailsChecker = Depends(get_guardrails),
    cache: CacheBackend = Depends(get_cache),
) -> StreamingResponse:
    start = time.monotonic()
    request_id = request.headers.get("x-request-id") or _new_request_id()
    normalised = normalise(body)
    try:
        tenant_key = await secret_backend.get_key(tenant.id, body.provider)
    except MissingTenantKeyError:
        raise HTTPException(status_code=422, detail="missing_tenant_key")
    try:
        await policy_checker.check(tenant.id, PolicyContext(
            provider=normalised.provider, model=normalised.model,
            max_tokens=normalised.params.max_tokens if normalised.params else None,
        ))
    except PolicyExceededError as e:
        raise HTTPException(status_code=429, detail=e.detail)
    input_result = await guardrails.check_input([m.model_dump() for m in normalised.messages])
    if input_result.blocked:
        raise HTTPException(status_code=400, detail="guardrails_blocked")
    if input_result.modified_messages is not None:
        normalised = normalised.model_copy(update={
            "messages": [MessageParam(**m) for m in input_result.modified_messages]
        })
    cache_key = make_cache_key(tenant.id, normalised)
    cached = await cache.get(cache_key)
    if cached:
        finish = FinishData(
            id=request_id,
            finish_reason=cached.choices[0].finish_reason,
            usage=cached.usage,
            cost=cached.cost,
            latency_ms=cached.latency_ms,
            cache=CacheBlock(our_cache_hit=True),
            guardrails=cached.guardrails,
            policy=cached.policy,
        )

        async def _cached_stream() -> AsyncIterator[str]:
            content = cached.choices[0].message.content or ""
            if content:
                yield f"event: token\ndata: {TokenData(index=0, delta=content).model_dump_json()}\n\n"
            yield f"event: finish\ndata: {finish.model_dump_json()}\n\n"

        return StreamingResponse(
            _cached_stream(), media_type="text/event-stream",
            headers={"X-Request-Id": request_id, "Cache-Control": "no-cache", "Connection": "keep-alive"},
        )
    # TODO: Step 8 — route to provider + upstream call
    # TODO: Step 9 (streaming) — check_output_chunk per token, check_output at finish
    # TODO: Step 10 — ledger write
    elapsed_ms = int((time.monotonic() - start) * 1000)
    stub_response = _stub_non_stream(
        body, tenant, request_id, elapsed_ms, input_result, GuardrailsResult(), _STUB_CONTENT,
    )
    await cache.set(cache_key, stub_response, ttl_seconds=settings.cache_ttl_seconds)
    return StreamingResponse(
        _stub_stream(body, request_id),
        media_type="text/event-stream",
        headers={
            "X-Request-Id": request_id,
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
        },
    )
