from __future__ import annotations

import hashlib

from fastapi import Depends, HTTPException, Request
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from src.shared.db import get_db
from src.shared.db.models import CallingService, CallingServiceTenant, Tenant


async def get_tenant(request: Request, db: AsyncSession = Depends(get_db)) -> Tenant:
    """Resolves caller identity and tenant from request headers. Reads Authorization: Bearer <token> and X-Tenant-Id.
    Returns the Tenant ORM object or raises 401/403. Never falls back to a default tenant on error.
    """
    auth_header = request.headers.get("Authorization", "")
    if not auth_header.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Missing or malformed Authorization header.")

    token = auth_header.removeprefix("Bearer ").strip()
    token_hash = hashlib.sha256(token.encode()).hexdigest()

    result = await db.execute(
        select(CallingService).where(CallingService.bearer_token_hash == token_hash)
    )
    service = result.scalar_one_or_none()
    if service is None:
        raise HTTPException(status_code=401, detail="Invalid bearer token.")

    x_tenant_id = request.headers.get("X-Tenant-Id", "")
    if not x_tenant_id:
        raise HTTPException(status_code=400, detail="Missing X-Tenant-Id header.")

    tenant_result = await db.execute(
        select(Tenant)
        .join(CallingServiceTenant, CallingServiceTenant.tenant_id == Tenant.id)
        .where(
            CallingServiceTenant.calling_service_id == service.id,
            Tenant.id == x_tenant_id,
        )
    )
    tenant = tenant_result.scalar_one_or_none()
    if tenant is None:
        raise HTTPException(
            status_code=403,
            detail=f"Service is not authorised to act on behalf of tenant {x_tenant_id!r}.",
        )

    return tenant
