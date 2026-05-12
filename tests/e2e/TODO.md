# tests/e2e — end-to-end tests

Runs against a live service stack (docker-compose or k8s dev namespace).
Requires: Postgres, Redis, and at least one provider with a test key.
(LiteLLM is in-process — no separate gateway container to stand up.)

## Test files to create

- `test_chat_nonstream.py`
  - Full round-trip via `POST /v1/chat`
  - Assert response envelope has all required fields
  - Assert `ledger_entries` row exists with both `our_cost_usd` and `provider_reported_usage`
  - **Reconciliation invariant**: `tokens_in × input_rate + tokens_out × output_rate + cache_write × cache_write_rate + cache_read × cache_read_rate == our_cost_usd` (full four-rate model) using current pricing YAML
  - `pricing_version` stamp present on ledger row and matches YAML version

- `test_chat_sse.py`
  - SSE streaming: assert all event types received in correct order
  - Assert `finish` event contains complete metadata envelope
  - Assert ledger row written after stream completes

- `test_chat_ws.py` (Phase 2)
  - WebSocket streaming: same assertions as SSE

- `test_prompt_cache.py`
  - Two identical requests to Anthropic/OpenAI with same system prompt
  - First response: `ledger_entries.input_tokens_cache_write > 0`, `input_tokens_cache_read == 0`
  - Second response: `input_tokens_cache_read > 0`
  - `our_cost_usd` on second call reflects cheaper read rate

- `test_failover.py` (Phase 3)
  - Kill primary region endpoint mid-request
  - Assert response served from next region
  - Assert ledger row records the failover

- `test_batch.py` (Phase 4)
  - N async requests within consolidation window → 1 batch API submission
  - Assert N ledger rows exist, each with correct per-request cost from batch result

## Phase

Phase 1 (nonstream + ledger assertions), Phase 2 (SSE + WS), Phase 3 (failover), Phase 4 (batch)
