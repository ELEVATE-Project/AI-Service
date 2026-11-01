from __future__ import annotations

import asyncio
import logging
import uuid
from typing import Optional

import redis.asyncio as aioredis

from src.llm_service.cache.base import CacheBackend
from src.llm_service.schemas.chat import ChatResponse

_LOCK_TTL_SECONDS = 10
_RELEASE_LOCK = """
if redis.call('get', KEYS[1]) == ARGV[1] then
    return redis.call('del', KEYS[1])
end
return 0
"""
logger = logging.getLogger(__name__)


class RedisCache(CacheBackend):
    def __init__(self, redis_url: str, *, ttl_seconds: int) -> None:
        self._ttl_seconds = ttl_seconds
        self._client: aioredis.Redis = aioredis.from_url(redis_url, decode_responses=True)

    async def get(self, key: str) -> Optional[ChatResponse]:
        if self._ttl_seconds <= 0:
            return None
        try:
            raw_json = await self._client.get(key)
            if raw_json:
                return ChatResponse.model_validate_json(raw_json)
        except Exception:
            logger.warning("Response cache read failed")
        return None

    async def set(self, key: str, response: ChatResponse, ttl_seconds: int) -> None:
        if self._ttl_seconds <= 0 or ttl_seconds <= 0:
            return
        try:
            await self._client.set(key, response.model_dump_json(), ex=ttl_seconds)
        except Exception:
            logger.warning("Response cache write failed")

    async def acquire(self, key: str) -> Optional[str]:
        if self._ttl_seconds <= 0:
            return None
        token = uuid.uuid4().hex
        try:
            acquired = await self._client.set(
                f"lock:{key}", token, nx=True, px=_LOCK_TTL_SECONDS * 1000,
            )
            return token if acquired else None
        except asyncio.CancelledError:
            # Redis may have stored the token before cancellation interrupted its reply.
            await asyncio.shield(self.release(key, token))
            raise
        except Exception:
            logger.warning("Response cache lock acquisition failed")
            return None

    async def release(self, key: str, token: str) -> None:
        if self._ttl_seconds <= 0:
            return
        try:
            await self._client.eval(_RELEASE_LOCK, 1, f"lock:{key}", token)
        except Exception:
            logger.warning("Response cache lock release failed")
