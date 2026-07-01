"""Test bootstrap.

`src.shared.config.Settings` is instantiated at import time and requires its
config to be present, so seed sane defaults here (before any `src` import)
so unit tests run without a populated .env. setdefault never overrides a real
value, so a developer's .env / environment still wins.
"""
from __future__ import annotations

import os

_DEFAULTS = {
    "DB_URL": "postgresql+asyncpg://postgres:postgres@localhost:5432/llm_service",
    "REDIS_URL": "redis://localhost:6379",
    "LOG_LEVEL": "INFO",
    "PRICING_STALENESS_DAYS": "30",
    "SECRET_BACKEND": "postgres",
    "GUARDRAILS_PRESIDIO_ENABLED": "false",
    "GUARDRAILS_LLAMA_GUARD_ENABLED": "false",
    "GUARDRAILS_LLAMA_GUARD_MODEL": "meta-llama/Llama-Guard-3-8B",
    "GUARDRAILS_SIZE_CAP_INPUT_CHARS": "100000",
    "GUARDRAILS_SIZE_CAP_OUTPUT_CHARS": "100000",
    "CACHE_TTL_SECONDS": "300",
    "LLM_RETRY_MAX_ATTEMPTS": "3",
    "LLM_RETRY_BACKOFF_BASE_S": "0.01",
    "BATCH_MAX_SUBMIT_ATTEMPTS": "3",
}

for _key, _value in _DEFAULTS.items():
    os.environ.setdefault(_key, _value)
