# Database Models

The gateway uses PostgreSQL with eight SQLAlchemy models. This page documents each table — what it stores, why each column exists, and how the tables relate.

---

## `tenants`

The root of everything. Every key, ledger row, policy, and batch job belongs to a tenant.

| Column | Type | Description |
|--------|------|-------------|
| `id` | `string` (PK) | Slug-style identifier, e.g. `saathi`. Used as FK in every other table. |
| `name` | `string` | Human-readable display name. |
| `created_at` | `timestamptz` | Row creation timestamp (UTC). |

Tenant IDs are strings, not UUIDs, so they can be meaningful slugs that appear in logs and API responses.

---

## `calling_services`

A registered backend application that calls the gateway on behalf of tenants.

| Column | Type | Description |
|--------|------|-------------|
| `id` | `uuid` (PK) | UUID primary key. |
| `name` | `string` | Service name, e.g. `taxbot-inc`. Label only. |
| `bearer_token_hash` | `string` (unique) | SHA-256 of the bearer token. The raw token is never stored. |
| `created_at` | `timestamptz` | Row creation timestamp (UTC). |

The raw token shown by `scripts/add_tenant_key.py` is hashed before storage. Rotating a token means generating a new one, hashing it, and updating this row.

---

## `calling_service_tenants`

Join table granting a calling service permission to act on behalf of a tenant.

| Column | Type | Description |
|--------|------|-------------|
| `calling_service_id` | `uuid` (PK, FK → calling_services) | Cascade-deletes when the service is removed. |
| `tenant_id` | `string` (PK, FK → tenants) | Cascade-deletes when the tenant is removed. |
| `created_at` | `timestamptz` | When this grant was created (UTC). |

A calling service can have multiple tenant grants, and a tenant can be served by multiple calling services.

---

## `tenant_keys`

Encrypted BYOK credentials. One row per `(tenant, provider)` pair.

| Column | Type | Description |
|--------|------|-------------|
| `id` | `uuid` (PK) | UUID primary key. |
| `tenant_id` | `string` (FK → tenants) | Owning tenant. |
| `provider` | `string` | Provider identifier, e.g. `openai`, `anthropic`, `bedrock`. Not an enum — new providers don't require a migration. |
| `key_format` | `enum` | Shape of the decrypted payload: `api_key`, `aws_credentials`, or `endpoint_pair`. |
| `encrypted_payload` | `text` | Fernet-encrypted JSON blob. Shape varies by `key_format`. |
| `created_at` | `timestamptz` | Row creation timestamp (UTC). |

Unique constraint on `(tenant_id, provider)` — one active key per provider per tenant at a time.

**Payload shapes by `key_format`:**

```
api_key          → {"api_key": "sk-..."}
aws_credentials  → {"access_key_id": "...", "secret_access_key": "...", "region": "us-east-1",
                     "aws_session_token"?: "...", "aws_role_name"?: "...",
                     "s3_bucket_name"?: "...", "role_arn"?: "..."}
endpoint_pair    → {"endpoint_url": "https://...", "token": "hf_..."}
```

---

## `ledger_entries`

One row per upstream LLM call. The audit log for every request — tokens, cost, latency, guardrails.

| Column | Type | Description |
|--------|------|-------------|
| `id` | `uuid` (PK) | UUID primary key. |
| `request_id` | `string` (unique) | Client-supplied or gateway-generated idempotency key. Idempotent inserts use `ON CONFLICT DO NOTHING`. |
| `tenant_id` | `string` (FK) | Owning tenant. |
| `created_at` | `timestamptz` | Row creation timestamp (UTC). |
| `provider` | `string` | e.g. `anthropic`, `openai`, `bedrock`. |
| `model` | `string` | Canonical model ID as sent in the request. |
| `transport` | `enum` | `litellm` or `direct` — which adapter handled the call. |
| `region` | `string?` | Cloud region, e.g. `us-east-1`. Null when not applicable. |
| `feature` | `enum` | `chat`, `stream`, `embed`, or `tool`. |
| `tokens_in` | `int` | Prompt tokens reported by the provider. |
| `tokens_out` | `int` | Completion tokens reported by the provider. |
| `input_tokens_cache_write` | `int?` | Tokens written to provider prompt cache (cache write rate applies). |
| `input_tokens_cache_read` | `int?` | Tokens served from provider prompt cache (cache read rate applies). |
| `upstream_prompt_cache_hit` | `bool?` | `true` if any input tokens were served from the provider's prompt cache. |
| `our_cost_usd` | `float` | Cost computed from `pricing/models.yaml` at request time. |
| `pricing_version` | `int` | `pricing_version` from the YAML used to compute `our_cost_usd`. Allows historical re-computation. |
| `provider_reported_usage` | `jsonb?` | Raw usage block from the provider response, stored verbatim for billing audit. |
| `provider_reported_cost_usd` | `float?` | Provider-reported cost where available (e.g. Bedrock). |
| `latency_ms` | `int` | Total wall-clock time from request receipt to response sent. |
| `time_to_first_token_ms` | `int?` | Time to first streaming token. Null for non-streaming requests. |
| `status` | `enum` | `success`, `error`, or `partial_response`. |
| `error_code` | `string?` | Structured error code when status is `error` or `partial_response`. |
| `our_cache_hit` | `bool` | `true` if served from Redis — no upstream call was made. |
| `batched` | `bool` | `true` if submitted via batch API. |
| `guardrail_flags` | `jsonb?` | Which guardrails fired: `{"input_flags": [...], "output_flags": [...], "redactions_applied": [...]}`. |

Two cost figures are always recorded: `our_cost_usd` (computed by us from YAML rates) and `provider_reported_usage` (raw upstream response). Both must be present for a request to be considered complete.

---

## `batch_jobs`

One row per async batch request. Tracks the full lifecycle from enqueueing to result retrieval.

| Column | Type | Description |
|--------|------|-------------|
| `id` | `uuid` (PK) | UUID primary key. Also used as `custom_id` in the upstream batch API request, linking each result line back to a row. |
| `request_id` | `string` (unique) | Gateway request ID — same idempotency key as `ledger_entries.request_id`. |
| `tenant_id` | `string` (FK) | Owning tenant. Used to reload the BYOK key at submission time. |
| `provider` | `string` | e.g. `anthropic`, `bedrock`, `openai`. |
| `model` | `string` | Canonical model ID. |
| `normalised_request` | `jsonb` | Full `NormalisedLLMRequest` as JSONB. Stored so the worker can reconstruct the exact request without re-parsing the original HTTP body. |
| `upstream_batch_id` | `string?` | Provider batch job ID (e.g. Anthropic `msgbatch_xxx`, Bedrock job ARN). Set when `status` becomes `submitted`. |
| `upstream_file_id` | `string?` | OpenAI input file ID. Null for Anthropic and Bedrock. |
| `status` | `enum` | `pending` → `submitted` → `complete` or `failed`. |
| `created_at` | `timestamptz` | When the `202` was issued. |
| `submitted_at` | `timestamptz?` | When the provider accepted the batch. |
| `completed_at` | `timestamptz?` | When the result was retrieved from the provider. |
| `result` | `jsonb?` | `ChatResponse` serialised as JSONB. Populated when `status == complete`. |
| `submit_attempts` | `int` | How many times `batch_submit` has tried and failed for this job. |
| `error_code` | `string?` | Structured error code when `status == failed`. |

---

## `policies`

Per-tenant usage controls. One row per tenant. All limits are optional (`null` = no limit).

| Column | Type | Description |
|--------|------|-------------|
| `id` | `uuid` (PK) | UUID primary key. |
| `tenant_id` | `string` (FK, unique) | One policy row per tenant. |
| `max_tokens_per_request` | `int?` | Hard cap on `params.max_tokens` per request. |
| `rate_limit_rpm` | `int?` | Maximum requests per minute. |
| `budget_usd_monthly` | `float?` | Monthly cost ceiling in USD. |
| `allowed_models` | `string[]?` | Whitelist of model IDs. `null` = all models allowed. |
| `denied_models` | `string[]?` | Blacklist of model IDs. `null` = none denied. |
| `created_at` | `timestamptz` | Row creation timestamp (UTC). |
| `updated_at` | `timestamptz?` | Last modification timestamp. `null` until first update. |

---

## `tenant_defaults`

Per-tenant defaults for `provider`/`model`/params, one row per tenant. Applied by the normaliser only when a request explicitly opts in with `use_defaults: true` — a request that omits `use_defaults` (or sets it `false`) still requires `provider`/`model` itself, unchanged from before this table existed.

| Column | Type | Description |
|--------|------|-------------|
| `id` | `uuid` (PK) | UUID primary key. |
| `tenant_id` | `string` (FK, unique) | One defaults row per tenant. |
| `default_provider` | `string?` | Used when the request omits `provider`. |
| `default_model` | `string?` | Used when the request omits `model`. |
| `default_params` | `jsonb?` | A `ChatParams`-shaped object. Each field is merged in only where the request left that field unset — request values always win. |
| `created_at` | `timestamptz` | Row creation timestamp (UTC). |
| `updated_at` | `timestamptz?` | Last modification timestamp. `null` until first update. |

Provider and model defaults are independent — a tenant can set just one, both, or neither. Set up via `scripts/add_tenant_key.py`, which prompts for defaults right after tenant creation. See [Auth & Tenants → Tenant defaults](auth-and-tenants.md#tenant-defaults) for the field-by-field merge rule and a worked override example.

---

## Migrations

Migrations live in `src/shared/db/migrations/versions/`. Apply them with:

```bash
alembic upgrade head
```

Generate a new migration after changing a model:

```bash
alembic revision --autogenerate -m "describe the change"
alembic upgrade head
```

---

See [Adding a Provider](adding-a-provider.md) for how to extend the system with a new LLM provider.
