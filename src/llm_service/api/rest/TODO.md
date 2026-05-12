# llm_service/api/rest — REST HTTP routers

## Files to create

- `chat.py`
  - `POST /v1/chat` — non-streaming: runs full pipeline, returns `ChatResponse`
  - `POST /v1/chat` with `stream=true` — SSE: yields `token`, `tool_use`, `usage`, `finish`, `error` events
  - Both depend on `deps.py`: `get_tenant`, `get_policy`, `get_guardrails`, `get_cache`, `get_db`
  - Response shape must match `schemas/ChatResponse` exactly — never strip fields

- `embed.py`
  - `POST /v1/embed`

- `models.py`
  - `GET /v1/models` — list models available to calling tenant per routing registry + policy

- `admin.py`
  - Tenant management, key rotation status, pricing version info (internal, not public)

## Full pipeline per request

1. `shared/auth` → resolve tenant (hard-fail if missing/invalid)
2. `llm_service/normaliser` → normalize `ChatRequest` to provider-agnostic schema
3. `shared/policy` → check rate limit + cost budget → `429` if exceeded
4. `shared/guardrails` → filter input (PII redaction, safety check)
5. `llm_service/cache` → exact-match lookup → return cached response if hit
6. `llm_service/providers` → route to LiteLLM SDK or direct adapter → upstream call
7. `shared/guardrails` → filter output (or wrap streaming chunks)
8. `shared/ledger` → enqueue ledger write (non-blocking via Arq)
9. Return `ChatResponse` or SSE stream

## Phase

Phase 1 (non-streaming + `/v1/models`), Phase 2 (SSE streaming)
