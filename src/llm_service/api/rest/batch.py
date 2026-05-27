from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession

from src.llm_service.api.deps import get_tenant
from src.llm_service.schemas.batch import BatchJobStatusResponse
from src.llm_service.schemas.chat import ChatResponse
from src.shared.db import get_db
from src.shared.db.models import BatchJob
from src.shared.db.models import Tenant

router = APIRouter(prefix="/v1")


@router.get("/chat/batch/{job_id}", response_model=BatchJobStatusResponse)
async def get_batch_job(
    job_id: str, tenant: Tenant = Depends(get_tenant), db: AsyncSession = Depends(get_db),
) -> BatchJobStatusResponse:
    try:
        job_uuid = uuid.UUID(job_id)
    except ValueError:
        raise HTTPException(status_code=404, detail="batch_job_not_found")

    job = await db.get(BatchJob, job_uuid)
    if job is None or job.tenant_id != tenant.id:
        raise HTTPException(status_code=404, detail="batch_job_not_found")

    result = ChatResponse.model_validate(job.result) if job.result else None
    return BatchJobStatusResponse(
        id=str(job.id),
        request_id=job.request_id,
        status=job.status,
        provider=job.provider,
        model=job.model,
        created_at=job.created_at,
        completed_at=job.completed_at,
        result=result,
        error_code=job.error_code,
    )
