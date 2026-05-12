# shared/ledger — usage recording + pricing

Records every AI call — both LLM and voice — with full cost traceability.

## Files to create

- `writer.py` — `LedgerWriter`:
  - Sync `asyncpg` insert from the request handler (Phase 1 — resolved during eng review 2026-05-07; Arq queue rejected for simplicity)
  - Idempotent on `request_id` via `INSERT ... ON CONFLICT (request_id) DO NOTHING`
  - Accepts a `LedgerEntryPayload` that covers both LLM fields (tokens) and voice fields (duration, characters)
  - A request is not considered complete until both `our_cost_usd` and `provider_reported_usage` are present
  - Insert failure is non-fatal for the response: the request returns success, the ledger error is logged + traced. (Audit invariant: failed-ledger requests are recoverable from Langfuse traces.)
  - When traffic warrants (load test shows Postgres as latency floor), flip to Arq queue — `LedgerWriter` interface stays identical

- `pricing.py` — `PricingTable`:
  - Loads and caches `pricing/models.yaml`
  - **LLM**: `compute_cost(provider, model, usage) → CostBlock` using four-rate model:
    ```
    cost = (tokens_in × input_rate)
         + (tokens_out × output_rate)
         + (cache_write_tokens × cache_write_rate)
         + (cache_read_tokens × cache_read_rate)
    ```
  - **Voice**: same structure but rates are per-second (STT/TTS) or per-character (translation/transliteration)
  - Always includes `pricing_version` in returned `CostBlock`
  - Raises `UnknownModelError` if model not in YAML — never computes $0 silently

## Ledger row shape (all fields required)

```
id, tenant_id, request_id (unique), created_at,
module (llm | voice),
provider, model, transport, region, feature,
-- LLM fields (null for voice):
tokens_in, tokens_out,
input_tokens_cache_write, input_tokens_cache_read,
upstream_prompt_cache_hit,
-- Voice fields (null for LLM):
audio_duration_ms, character_count,
-- Shared:
our_cost_usd, pricing_version,
provider_reported_usage (JSONB — verbatim upstream response),
provider_reported_cost_usd (nullable),
latency_ms, time_to_first_token_ms (nullable, streams/STT),
status, error_code (nullable),
our_cache_hit, batched,
guardrail_flags (JSONB)
```

## Phase

Phase 1 (sync insert; Arq deferred to Phase 4 when `batch_submit` lands)
