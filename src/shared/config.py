from __future__ import annotations

from typing import Optional

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    db_url: str
    redis_url: str
    log_level: str
    pricing_staleness_days: int
    secret_backend: str
    guardrails_presidio_enabled: bool
    guardrails_llama_guard_enabled: bool
    guardrails_llama_guard_model: str
    guardrails_llama_guard_api_key: Optional[str]
    guardrails_llama_guard_api_base: Optional[str]
    guardrails_size_cap_input_chars: int
    guardrails_size_cap_output_chars: int
    cache_ttl_seconds: int
    llm_retry_max_attempts: int
    llm_retry_backoff_base_s: float

    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")


settings = Settings()
