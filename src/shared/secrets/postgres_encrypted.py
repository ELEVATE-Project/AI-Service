from __future__ import annotations

import json

import keyring
from cryptography.fernet import Fernet
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from src.shared.db.enums import KeyFormat
from src.shared.db.models import TenantKey
from src.shared.secrets.backend import MissingTenantKeyError, SecretBackend, TenantKeyPayload

_KEYRING_SERVICE = "ai-service"
_KEYRING_KEY = "master-key"


def _get_fernet() -> Fernet:
    master_key = keyring.get_password(_KEYRING_SERVICE, _KEYRING_KEY)
    if not master_key:
        raise RuntimeError(
            "Master key not found in OS keyring. Run: uv run llm-service keys init"
        )
    return Fernet(master_key.encode())


class PostgresEncryptedBackend(SecretBackend):
    """SecretBackend backed by Postgres with Fernet-encrypted payloads.

    Master key lives in the OS keyring locally (macOS Keychain / Linux Secret Service).
    Swap to VaultBackend or AWSSecretsManagerBackend via config — this class stays unchanged.
    """

    def __init__(self, db: AsyncSession) -> None:
        self._db = db

    async def get_key(self, tenant_id: str, provider: str) -> TenantKeyPayload:
        result = await self._db.execute(
            select(TenantKey).where(
                TenantKey.tenant_id == tenant_id,
                TenantKey.provider == provider,
            )
        )
        row = result.scalar_one_or_none()
        if row is None:
            raise MissingTenantKeyError(
                f"Tenant {tenant_id!r} has no key registered for provider={provider!r}"
            )
        fernet = _get_fernet()
        data: dict[str, str] = json.loads(fernet.decrypt(row.encrypted_payload.encode()))
        return TenantKeyPayload(key_format=row.key_format, data=data)

    async def set_key(
        self, tenant_id: str, provider: str, key_format: KeyFormat, data: dict[str, str]
    ) -> None:
        fernet = _get_fernet()
        encrypted = fernet.encrypt(json.dumps(data).encode()).decode()
        stmt = (
            insert(TenantKey)
            .values(
                tenant_id=tenant_id, provider=provider, key_format=key_format, encrypted_payload=encrypted,
            )
            .on_conflict_do_update(
                constraint="uq_tenant_provider",
                set_={"key_format": key_format, "encrypted_payload": encrypted},
            )
        )
        await self._db.execute(stmt)
        await self._db.commit()
