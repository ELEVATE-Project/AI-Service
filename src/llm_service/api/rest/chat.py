from __future__ import annotations

import time
import uuid
from collections.abc import AsyncIterator
from typing import Optional, Union

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import JSONResponse, StreamingResponse
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from src.llm_service.api.deps import (
    get_cache, get_guardrails, get_policy_checker, get_secret_backend, get_tenant,
)
from src.llm_service.cache.base import CacheBackend
from src.llm_service.cache.keys import make_cache_key
from src.llm_service.normaliser import normalise
from src.llm_service.providers import registry
from src.llm_service.providers.base import StreamEvent, TransportFinishData, UpstreamTransportError
from src.llm_service.schemas.batch import BatchAcceptedResponse
from src.llm_service.schemas.chat import (
    CacheBlock, ChatRequest, ChatResponse, Choice, ChoiceMessage,
    ErrorData, FinishData, MessageParam, TokenData, ToolUseData, UsageBlock,
)
from src.shared.config import settings
from src.shared.db import get_db
from src.shared.db.enums import BatchJobStatus, Feature, LedgerStatus, Transport
from src.shared.db.models import BatchJob, LedgerEntry, Tenant
from src.shared.guardrails.base import GuardrailsChecker
from src.shared.policy.checker import PolicyChecker, PolicyContext, PolicyExceededError
from src.shared.queue.tasks import BATCH_ELIGIBLE_PROVIDERS
from src.shared.ledger.pricing import UnknownModelError, pricing_table
from src.shared.schemas.envelope import CostBlock, GuardrailsBlock, LatencyBlock, PolicyBlock
from src.shared.secrets.backend import MissingTenantKeyError, SecretBackend

router = APIRouter(prefix="/v1")


def _new_request_id() -> str:
    return f"req_{uuid.uuid4().hex[:24]}"


def _compute_cost(
    provider: str, model: str, usage: "UsageBlock",
    provider_reported_usd: Optional[float] = None,
) -> CostBlock:
    """Build the cost envelope: YAML-computed cost, falling back to provider-reported cost.

    computed_usd is always the figure callers should use: it comes from YAML pricing
    when available, otherwise from provider_reported_usd. provider_reported_usd is kept
    alongside as the raw provider figure for reference/auditing.
    """
    try:
        cost = pricing_table.compute_cost(
            provider=provider,
            model=model,
            tokens_in=usage.input_tokens,
            tokens_out=usage.output_tokens,
            cache_write_tokens=usage.input_tokens_cache_write or 0,
            cache_read_tokens=usage.input_tokens_cache_read or 0,
        )
    except UnknownModelError:
        cost = CostBlock(computed_usd=provider_reported_usd or 0.0)
    if provider_reported_usd is not None:
        cost = cost.model_copy(update={"provider_reported_usd": provider_reported_usd})
    return cost


async def _write_ledger_entry(
    db: AsyncSession, *, request_id: str, tenant_id: str, provider: str, model: str,
    feature: Feature, status: LedgerStatus, latency_ms: int,
    usage: Optional["UsageBlock"] = None, cost: Optional[CostBlock] = None,
    time_to_first_token_ms: Optional[int] = None, error_code: Optional[str] = None,
    our_cache_hit: bool = False, guardrail_flags: Optional[dict] = None,
    provider_reported_usage: Optional[dict] = None,
) -> None:
    """Write one audit row to ledger_entries.

    Non-fatal by design (see docs/architecture.md pipeline step 9): the response has
    already been produced, so a ledger write failure must never turn it into a 500.

    request_id is client-supplied (X-Request-Id) and only unique per genuine retry of
    the same call — by the time we get here, this request's own provider call (if any)
    has already happened and was already billed. A request_id collision from an
    unrelated call (client bug, or a reused header) must not silently drop this row,
    so on a unique-constraint violation we retry once with a disambiguated id rather
    than treating it as an already-recorded duplicate.
    """
    def _build_entry(entry_request_id: str) -> LedgerEntry:
        return LedgerEntry(
            request_id=entry_request_id,
            tenant_id=tenant_id,
            provider=provider,
            model=model,
            transport=Transport.LITELLM,
            feature=feature,
            tokens_in=usage.input_tokens if usage else 0,
            tokens_out=usage.output_tokens if usage else 0,
            input_tokens_cache_write=usage.input_tokens_cache_write if usage else None,
            input_tokens_cache_read=usage.input_tokens_cache_read if usage else None,
            upstream_prompt_cache_hit=((usage.input_tokens_cache_read or 0) > 0) if usage else None,
            our_cost_usd=cost.computed_usd if cost else 0.0,
            pricing_version=cost.pricing_version if cost else 0,
            provider_reported_usage=provider_reported_usage,
            provider_reported_cost_usd=cost.provider_reported_usd if cost else None,
            latency_ms=latency_ms,
            time_to_first_token_ms=time_to_first_token_ms,
            status=status,
            error_code=error_code,
            our_cache_hit=our_cache_hit,
            batched=False,
            guardrail_flags=guardrail_flags,
        )

    db.add(_build_entry(request_id))
    try:
        await db.commit()
        return
    except IntegrityError as exc:
        await db.rollback()
        print(f"[ledger] request_id={request_id} collided with an existing row, retrying with a disambiguated id: {exc!r}")
    except Exception as exc:
        print(f"[ledger] write failed for request_id={request_id}: {exc!r}")
        await db.rollback()
        return

    disambiguated_id = f"{request_id}:dup:{uuid.uuid4().hex[:8]}"
    db.add(_build_entry(disambiguated_id))
    try:
        await db.commit()
    except Exception as exc:
        print(f"[ledger] retry write failed for request_id={disambiguated_id}: {exc!r}")
        await db.rollback()


@router.post("/chat", response_model=ChatResponse, responses={202: {"model": BatchAcceptedResponse}})
async def chat(
    body: ChatRequest, request: Request, tenant: Tenant = Depends(get_tenant),
    secret_backend: SecretBackend = Depends(get_secret_backend),
    policy_checker: PolicyChecker = Depends(get_policy_checker),
    guardrails: GuardrailsChecker = Depends(get_guardrails),
    cache: CacheBackend = Depends(get_cache),
    db: AsyncSession = Depends(get_db),
) -> Union[ChatResponse, JSONResponse]:
    start = time.monotonic()
    request_id = request.headers.get("x-request-id") or _new_request_id()
    print(f"[chat] request: {body.model_dump_json(indent=2)}")
    normalised = await normalise(body, db, tenant.id)
    try:
        tenant_key = await secret_backend.get_key(tenant.id, normalised.provider)
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
    print(f"[chat] guardrails input_result: {input_result!r}")
    if input_result.blocked:
        raise HTTPException(status_code=400, detail="guardrails_blocked")
    if input_result.modified_messages is not None:
        normalised = normalised.model_copy(update={
            "messages": [MessageParam(**m) for m in input_result.modified_messages]
        })
    cache_key = make_cache_key(tenant.id, normalised)
    cached = await cache.get(cache_key)
    if cached:
        await _write_ledger_entry(
            db, request_id=request_id, tenant_id=tenant.id,
            provider=normalised.provider, model=normalised.model,
            feature=Feature.CHAT, status=LedgerStatus.SUCCESS,
            latency_ms=int((time.monotonic() - start) * 1000),
            usage=cached.usage, cost=cached.cost.model_copy(update={"computed_usd": 0.0}),
            our_cache_hit=True,
            provider_reported_usage=(cached.provider_raw or {}).get("usage") or cached.usage.model_dump(),
        )
        return cached.model_copy(update={"cache": CacheBlock(our_cache_hit=True)})

    if body.metadata and body.metadata.get("batch") is True:
        if normalised.provider not in BATCH_ELIGIBLE_PROVIDERS:
            raise HTTPException(status_code=422, detail="provider_not_batch_eligible")
        batch_job = BatchJob(
            request_id=request_id,
            tenant_id=tenant.id,
            provider=normalised.provider,
            model=normalised.model,
            normalised_request=normalised.model_dump(exclude_none=True),
            status=BatchJobStatus.PENDING,
        )
        db.add(batch_job)
        await db.commit()
        return JSONResponse(
            status_code=202,
            content=BatchAcceptedResponse(
                job_id=str(batch_job.id), request_id=request_id, status="pending",
            ).model_dump(),
        )

    transport = registry.resolve(normalised.provider, normalised.model, "chat")
    upstream_start = time.monotonic()
    try:
        response = await transport.chat(normalised, tenant_key)
    except UpstreamTransportError as upstream_error:
        await _write_ledger_entry(
            db, request_id=request_id, tenant_id=tenant.id,
            provider=normalised.provider, model=normalised.model,
            feature=Feature.CHAT, status=LedgerStatus.ERROR,
            latency_ms=int((time.monotonic() - start) * 1000),
            error_code=upstream_error.code,
        )
        extra_headers = {"Retry-After": upstream_error.retry_after} if upstream_error.retry_after else None
        raise HTTPException(
            status_code=upstream_error.http_status, detail=upstream_error.code,
            headers=extra_headers,
        )
    upstream_ms = int((time.monotonic() - upstream_start) * 1000)

    elapsed_ms = int((time.monotonic() - start) * 1000)
    response.id = request_id
    response.tenant_id = tenant.id
    response.latency_ms = LatencyBlock(total=elapsed_ms, upstream=upstream_ms)
    response.guardrails = GuardrailsBlock(
        input_flags=input_result.flags,
        output_flags=[],
        redactions_applied=input_result.redactions,
    )
    response.cost = _compute_cost(
        normalised.provider, normalised.model, response.usage,
        provider_reported_usd=response.cost.provider_reported_usd,
    )
    await _write_ledger_entry(
        db, request_id=request_id, tenant_id=tenant.id,
        provider=normalised.provider, model=normalised.model,
        feature=Feature.CHAT, status=LedgerStatus.SUCCESS,
        latency_ms=elapsed_ms, usage=response.usage, cost=response.cost,
        guardrail_flags=response.guardrails.model_dump() if response.guardrails else None,
        provider_reported_usage=(response.provider_raw or {}).get("usage") or response.usage.model_dump(),
    )
    await cache.set(cache_key, response, ttl_seconds=settings.cache_ttl_seconds)
    print(f"[chat] response: {response.model_dump_json(indent=2)}")
    return response


@router.post("/chat/stream")
async def chat_stream(
    body: ChatRequest, request: Request, tenant: Tenant = Depends(get_tenant),
    secret_backend: SecretBackend = Depends(get_secret_backend),
    policy_checker: PolicyChecker = Depends(get_policy_checker),
    guardrails: GuardrailsChecker = Depends(get_guardrails),
    cache: CacheBackend = Depends(get_cache),
    db: AsyncSession = Depends(get_db),
) -> StreamingResponse:
    start = time.monotonic()
    request_id = request.headers.get("x-request-id") or _new_request_id()
    normalised = await normalise(body, db, tenant.id)
    try:
        tenant_key = await secret_backend.get_key(tenant.id, normalised.provider)
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
        await _write_ledger_entry(
            db, request_id=request_id, tenant_id=tenant.id,
            provider=normalised.provider, model=normalised.model,
            feature=Feature.STREAM, status=LedgerStatus.SUCCESS,
            latency_ms=int((time.monotonic() - start) * 1000),
            usage=cached.usage, cost=cached.cost.model_copy(update={"computed_usd": 0.0}),
            our_cache_hit=True,
            provider_reported_usage=(cached.provider_raw or {}).get("usage") or cached.usage.model_dump(),
        )
        finish = FinishData(
            id=request_id,
            finish_reason=cached.choices[0].finish_reason,
            usage=cached.usage,
            cost=cached.cost,
            latency_ms=cached.latency_ms,
            cache=CacheBlock(our_cache_hit=True),
            guardrails=cached.guardrails,
            policy=cached.policy,
            citations=cached.choices[0].message.citations,
        )

        async def _cached_stream() -> AsyncIterator[str]:
            cached_msg = cached.choices[0].message
            if cached_msg.content:
                yield f"event: token\ndata: {TokenData(index=0, delta=cached_msg.content).model_dump_json()}\n\n"
            for tool_index, tool_call in enumerate(cached_msg.tool_calls or []):
                if isinstance(tool_call, dict):
                    function_data = tool_call.get("function", {})
                    yield f"event: tool_use\ndata: {ToolUseData(index=tool_index, id=tool_call.get('id', ''), name=function_data.get('name', ''), arguments_delta=function_data.get('arguments', '')).model_dump_json()}\n\n"
            yield f"event: finish\ndata: {finish.model_dump_json()}\n\n"

        return StreamingResponse(
            _cached_stream(), media_type="text/event-stream",
            headers={"X-Request-Id": request_id, "Cache-Control": "no-cache", "Connection": "keep-alive"},
        )

    transport = registry.resolve(normalised.provider, normalised.model, "stream")

    async def event_generator() -> AsyncIterator[str]:
        upstream_start = time.monotonic()
        first_token_ms: Optional[int] = None
        final_usage = UsageBlock()
        final_finish_reason = "stop"
        upstream_cache_hit: Optional[bool] = None
        accumulated_content = ""
        accumulated_tool_calls: dict[int, dict[str, str]] = {}

        final_citations: Optional[list] = None
        final_reported_cost: Optional[float] = None

        async for event in transport.stream(normalised, tenant_key):
            print(f"[stream] chunk type: {event.type}, data type: {type(event.data).__name__}, data: {event.data}")
            if event.type == "token":
                token_data: TokenData = event.data
                accumulated_content += token_data.delta
                if first_token_ms is None:
                    first_token_ms = int((time.monotonic() - upstream_start) * 1000)
                yield f"event: token\ndata: {token_data.model_dump_json()}\n\n"
            elif event.type == "tool_use":
                tool_data: ToolUseData = event.data
                if tool_data.index not in accumulated_tool_calls:
                    accumulated_tool_calls[tool_data.index] = {
                        "id": tool_data.id, "name": tool_data.name, "args": "",
                    }
                accumulated_tool_calls[tool_data.index]["args"] += tool_data.arguments_delta
                yield f"event: tool_use\ndata: {tool_data.model_dump_json()}\n\n"
            elif event.type == "finish":
                finish_data: TransportFinishData = event.data
                final_usage = finish_data.usage
                final_finish_reason = finish_data.finish_reason
                final_citations = finish_data.citations
                final_reported_cost = finish_data.provider_reported_usd
                upstream_cache_hit = (final_usage.input_tokens_cache_read or 0) > 0
            elif event.type == "error":
                error_data: ErrorData = event.data
                await _write_ledger_entry(
                    db, request_id=request_id, tenant_id=tenant.id,
                    provider=normalised.provider, model=normalised.model,
                    feature=Feature.STREAM, status=LedgerStatus.ERROR,
                    latency_ms=int((time.monotonic() - start) * 1000),
                    error_code=error_data.code,
                )
                yield f"event: error\ndata: {error_data.model_dump_json()}\n\n"
                return

        upstream_ms = int((time.monotonic() - upstream_start) * 1000)
        elapsed_ms = int((time.monotonic() - start) * 1000)

        computed_cost = _compute_cost(
            normalised.provider, normalised.model, final_usage,
            provider_reported_usd=final_reported_cost,
        )
        finish = FinishData(
            id=request_id,
            finish_reason=final_finish_reason,
            usage=final_usage,
            cost=computed_cost,
            latency_ms=LatencyBlock(
                total=elapsed_ms,
                upstream=upstream_ms,
                time_to_first_token=first_token_ms,
            ),
            cache=CacheBlock(our_cache_hit=False, upstream_prompt_cache_hit=upstream_cache_hit),
            guardrails=GuardrailsBlock(
                input_flags=input_result.flags,
                output_flags=[],
                redactions_applied=input_result.redactions,
            ),
            policy=PolicyBlock(),
            citations=final_citations,
        )
        yield f"event: finish\ndata: {finish.model_dump_json()}\n\n"

        tool_calls = [
            {"id": tc["id"], "function": {"name": tc["name"], "arguments": tc["args"]}}
            for _, tc in sorted(accumulated_tool_calls.items())
        ] if accumulated_tool_calls else None
        cached_response = ChatResponse(
            id=request_id,
            created=int(time.time()),
            tenant_id=tenant.id,
            provider=normalised.provider,
            model=normalised.model,
            transport=type(transport).__name__,
            choices=[Choice(
                index=0,
                message=ChoiceMessage(
                    role="assistant",
                    content=accumulated_content or None,
                    tool_calls=tool_calls,
                    citations=final_citations,
                ),
                finish_reason=final_finish_reason,
            )],
            usage=final_usage,
            cost=computed_cost,
            latency_ms=finish.latency_ms,
            cache=CacheBlock(our_cache_hit=False, upstream_prompt_cache_hit=upstream_cache_hit),
            guardrails=finish.guardrails,
            policy=PolicyBlock(),
        )
        await cache.set(cache_key, cached_response, ttl_seconds=settings.cache_ttl_seconds)
        await _write_ledger_entry(
            db, request_id=request_id, tenant_id=tenant.id,
            provider=normalised.provider, model=normalised.model,
            feature=Feature.STREAM, status=LedgerStatus.SUCCESS,
            latency_ms=elapsed_ms, time_to_first_token_ms=first_token_ms,
            usage=final_usage, cost=computed_cost,
            guardrail_flags=finish.guardrails.model_dump() if finish.guardrails else None,
            provider_reported_usage=final_usage.model_dump(),
        )

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={"X-Request-Id": request_id, "Cache-Control": "no-cache", "Connection": "keep-alive"},
    )
