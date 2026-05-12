# shared/db — database ORM models + migrations

Single database shared by all modules. Both `llm_service` and `voice` read/write here.

## Files to create

- `models.py` — SQLAlchemy 2.x async declarative models:

  - `Tenant` — `id`, `name`, `created_at`, `active`

  - `TenantKey` — `id`, `tenant_id` (FK), `provider`, `key_format` (`api_key` | `aws_credentials` | `endpoint_pair`), `encrypted_payload` (bytes — JSON-encoded shape varies by `key_format`), `key_id` (for KMS rotation tracking), `created_at`, `rotated_at`
    - `key_format=api_key`: payload = `{"api_key": "..."}` (OpenAI, Anthropic, HF, any LiteLLM-routed provider)
    - `key_format=aws_credentials`: payload = `{"access_key_id": "...", "secret_access_key": "...", "region": "..."}` (Bedrock)
    - `key_format=endpoint_pair`: payload = `{"endpoint_url": "...", "token": "..."}` (HF Inference Endpoints, custom OpenAI-compatible endpoints)

  - `CallerService` — `id`, `name`, `encrypted_token` (bytes), `allowed_tenant_ids` (JSONB), `created_at`, `rotated_at`
    Resolved during eng review (2026-05-07): caller→service auth is per-service bearer token stored in this table. `shared/auth.py` validates `Authorization: Bearer <token>` against this table and `X-Tenant-Id` against `allowed_tenant_ids`.

  - `Policy` — `id`, `tenant_id`, `rule_type`, `config` (JSONB), `active`
    Shared: same policy rows apply to both LLM and voice calls.

  - `LedgerEntry` — all fields from `shared/ledger/TODO.md` schema.
    Covers both LLM calls (token fields populated, voice fields null) and voice calls (vice versa).
    `module` column (`llm` | `voice`) distinguishes them for billing queries.

## Rule

Once `LedgerEntry` is live in production: **schema changes via Alembic migrations only**.

## Phase

Phase 1
