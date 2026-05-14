from __future__ import annotations

import asyncio
from typing import Optional

import redis.asyncio as aioredis

from src.llm_service.cache.base import CacheBackend
from src.llm_service.schemas.chat import ChatResponse

_LOCK_TTL_SECONDS = 10
_POLL_INTERVAL_SECONDS = 0.2


class RedisCache(CacheBackend):
    def __init__(self, redis_url: str) -> None:
        self._client: aioredis.Redis = aioredis.from_url(redis_url, decode_responses=True)

    async def get(self, key: str) -> Optional[ChatResponse]:
        try:
            raw_json = await self._client.get(key)
            if raw_json:
                return ChatResponse.model_validate_json(raw_json)

            lock_key = f"lock:{key}"
            acquired = await self._client.set(lock_key, "1", nx=True, ex=_LOCK_TTL_SECONDS)
            if acquired:
                # This request won the lock and will populate the cache via set()
                return None

            # Another request holds the lock — wait for the data key to appear
            deadline = asyncio.get_running_loop().time() + _LOCK_TTL_SECONDS
            while asyncio.get_running_loop().time() < deadline:
                await asyncio.sleep(_POLL_INTERVAL_SECONDS)
                raw_json = await self._client.get(key)
                if raw_json:
                    return ChatResponse.model_validate_json(raw_json)
            return None
        except Exception:
            return None

    async def set(self, key: str, response: ChatResponse, ttl_seconds: int) -> None:
        try:
            await self._client.set(key, response.model_dump_json(), ex=ttl_seconds)
            await self._client.delete(f"lock:{key}")
        except Exception:
            pass
