# shared — common infrastructure

Imported by every AI module in this service (`llm_service`, `voice`, and any future module added under `src/`).

## Dependency rule

```
shared  ←  llm_service
shared  ←  voice
shared  ←  (future: image_service, code_service, …)
```

`shared` must never import from `llm_service` or `voice`. Dependencies only flow inward.

## Modules

| Module | Purpose |
|--------|---------|
| `config.py` | All env-driven config via Pydantic-settings (DB URL, Redis URL, Langfuse, LiteLLM tunables, etc.) |
| `auth.py` | Caller identity verification + tenant resolution for every incoming request |
| `secrets/` | BYOK key storage: SecretBackend interface + implementations + local-dev keys CLI |
| `ledger/` | Per-call usage recording (tokens for LLM, duration/chars for voice) + pricing YAML |
| `observability/` | Langfuse trace client (self-hosted, OTel-compatible) + structlog config |
| `policy/` | Per-tenant rate limits, cost budgets, model/provider allow-deny — applies to all AI calls |
| `guardrails/` | PII detection/redaction (Presidio) + safety classification (Llama-Guard) — text in/out for both LLM and voice |
| `db/` | SQLAlchemy 2.x async ORM models shared across modules + Alembic migrations |
| `queue/` | Arq worker entrypoint + background tasks (ledger_flush, retry_failed, batch_submit) |
| `schemas/` | Shared response envelope sub-models reused by both LLM and voice responses |

## Files to create directly here

- `config.py` — `Settings` class (Pydantic-settings). Fields: `db_url`, `redis_url`, `secret_backend` selector, `litellm_request_timeout_s`, `litellm_max_retries`, `langfuse_host/public_key/secret_key`, `pricing_staleness_days`, `log_level`. (No `portkey_endpoint` — LiteLLM is in-process, no gateway URL.)
- `auth.py` — two responsibilities:
  1. Verify caller identity (JWT / API key / mTLS — per open question in `docs/architecture.md`)
  2. Resolve `tenant_id` from verified identity; expose `get_tenant(request) → Tenant`
  BYOK key loading is handled by `secrets/` — `auth.py` only resolves *who* is calling.

## Phase

Phase 1 (config, auth, secrets, ledger, observability, db, queue),
Phase 2 (cache policy via Redis counters),
Phase 3 (policy, guardrails)
