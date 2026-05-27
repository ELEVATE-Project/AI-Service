from __future__ import annotations

import functools

from fastapi import Depends
from sqlalchemy.ext.asyncio import AsyncSession

from src.llm_service.cache.base import CacheBackend
from src.llm_service.cache.redis_cache import RedisCache
from src.shared.auth import get_tenant as _get_tenant
from src.shared.config import settings
from src.shared.db import get_db
from src.shared.db.enums import SecretBackendType
from src.shared.guardrails.base import GuardrailsChecker
from src.shared.guardrails.chain import GuardrailsChain
from src.shared.guardrails.llama_guard import LlamaGuardChecker
from src.shared.guardrails.presidio import PresidioGuardrails
from src.shared.guardrails.size_caps import SizeCapsGuardrails
from src.shared.guardrails.stub import StubGuardrails
from src.shared.policy.checker import PolicyChecker
from src.shared.secrets.backend import SecretBackend
from src.shared.secrets.postgres_encrypted import PostgresEncryptedBackend

# re-exported so all handlers import from one place
get_tenant = _get_tenant


async def get_policy_checker(db: AsyncSession = Depends(get_db)) -> PolicyChecker:
    return PolicyChecker(db)


async def get_secret_backend(db: AsyncSession = Depends(get_db)) -> SecretBackend:
    if settings.secret_backend == SecretBackendType.POSTGRES:
        return PostgresEncryptedBackend(db)
    if settings.secret_backend == SecretBackendType.VAULT:
        raise NotImplementedError("VaultBackend is not implemented yet")
    if settings.secret_backend == SecretBackendType.AWS:
        raise NotImplementedError("AWSSecretsManagerBackend is not implemented yet")
    raise RuntimeError(f"Unknown secret_backend value: {settings.secret_backend!r}")


@functools.lru_cache(maxsize=1)
def _build_guardrails() -> GuardrailsChecker:
    # lru_cache makes this a singleton — PresidioGuardrails loads spaCy once at first call.
    checkers: list[GuardrailsChecker] = []
    if settings.guardrails_size_cap_input_chars > 0:
        checkers.append(SizeCapsGuardrails(
            settings.guardrails_size_cap_input_chars, settings.guardrails_size_cap_output_chars,
        ))
    if settings.guardrails_presidio_enabled:
        checkers.append(PresidioGuardrails())
    if settings.guardrails_llama_guard_enabled:
        checkers.append(LlamaGuardChecker(
            model=settings.guardrails_llama_guard_model,
            api_key=settings.guardrails_llama_guard_api_key,
            api_base=settings.guardrails_llama_guard_api_base,
        ))
    if not checkers:
        return StubGuardrails()
    if len(checkers) == 1:
        return checkers[0]
    return GuardrailsChain(checkers)


def get_guardrails() -> GuardrailsChecker:
    return _build_guardrails()


@functools.lru_cache(maxsize=1)
def _build_cache() -> CacheBackend:
    return RedisCache(settings.redis_url)


def get_cache() -> CacheBackend:
    return _build_cache()
