from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Optional

from src.llm_service.schemas.chat import ChatResponse


class CacheBackend(ABC):
    @abstractmethod
    async def get(self, key: str) -> Optional[ChatResponse]: ...

    @abstractmethod
    async def set(self, key: str, response: ChatResponse, ttl_seconds: int) -> None: ...

    @abstractmethod
    async def acquire(self, key: str) -> Optional[str]:
        """Try to reserve cache population without waiting; return the owner's token."""

    @abstractmethod
    async def release(self, key: str, token: str) -> None:
        """Release only the reservation owned by this token."""
