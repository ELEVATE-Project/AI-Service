from __future__ import annotations

from fastapi import Depends
from sqlalchemy.ext.asyncio import AsyncSession

from src.shared.auth import get_tenant as _get_tenant
from src.shared.config import settings
from src.shared.db import get_db
from src.shared.db.enums import SecretBackendType
from src.shared.secrets.backend import SecretBackend
from src.shared.secrets.postgres_encrypted import PostgresEncryptedBackend

# re-exported so all handlers import from one place
get_tenant = _get_tenant


async def get_secret_backend(db: AsyncSession = Depends(get_db)) -> SecretBackend:
    if settings.secret_backend == SecretBackendType.POSTGRES:
        return PostgresEncryptedBackend(db)
    if settings.secret_backend == SecretBackendType.VAULT:
        raise NotImplementedError("VaultBackend is not implemented yet")
    if settings.secret_backend == SecretBackendType.AWS:
        raise NotImplementedError("AWSSecretsManagerBackend is not implemented yet")
    raise RuntimeError(f"Unknown secret_backend value: {settings.secret_backend!r}")