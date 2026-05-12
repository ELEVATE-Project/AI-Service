# shared/schemas — shared response envelope sub-models

Pydantic v2 sub-models that appear in **both** LLM and voice responses.
Import these in `llm_service/schemas/` and `voice/schemas/` — do not duplicate them.

## Files to create

- `envelope.py` — sub-models for the response metadata envelope:

  ```python
  class UsageBlock(BaseModel):
      # LLM fields (None for voice)
      input_tokens: int | None
      output_tokens: int | None
      input_tokens_cache_write: int | None
      input_tokens_cache_read: int | None
      total_tokens: int | None
      # Voice fields (None for LLM)
      audio_duration_ms: int | None
      character_count: int | None

  class CostBlock(BaseModel):
      computed_usd: float
      pricing_version: int
      provider_reported_usd: float | None
      currency: str = "USD"

  class LatencyBlock(BaseModel):
      total: int                          # ms
      guardrails_in: int | None
      guardrails_out: int | None
      upstream: int
      time_to_first_token: int | None     # streams / STT only

  class CacheBlock(BaseModel):
      our_cache_hit: bool
      upstream_prompt_cache_hit: bool | None  # LLM only

  class GuardrailsBlock(BaseModel):
      input_flags: list[str]
      output_flags: list[str]
      redactions_applied: list[str]

  class PolicyBlock(BaseModel):
      rate_limit_remaining: int
      budget_remaining_usd: float
  ```

## Why shared?

Both LLM and voice responses surface the same metadata (cost, latency, guardrails, policy).
Callers get a consistent envelope regardless of which AI capability they called.

## Phase

Phase 1
