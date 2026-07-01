from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass

from src.shared.db.enums import KeyFormat


class MissingTenantKeyError(Exception):
    """No key registered for (tenant_id, provider). Maps to 422 missing_tenant_key at the handler."""


@dataclass
class TenantKeyPayload:
    """Decrypted BYOK credential returned by any SecretBackend implementation.
    Holds the decrypted key data once fetched."""

    key_format: KeyFormat
    data: dict[str, str]


class SecretBackend(ABC):
    """Interface for loading and storing encrypted BYOK tenant keys.
    Swap implementations via config — same interface, different master-key source.
    """

    @abstractmethod
    async def get_key(self, tenant_id: str, provider: str) -> TenantKeyPayload:
        """Return the decrypted key payload for (tenant_id, provider).
        Raises MissingTenantKeyError if no key is registered — never falls back
        to a default key.
        """
        ...

    @abstractmethod
    async def set_key(
        self, tenant_id: str, provider: str, key_format: KeyFormat, data: dict[str, str]
    ) -> None:
        """Encrypt and persist (or overwrite) the key for (tenant_id, provider)."""
        ...
