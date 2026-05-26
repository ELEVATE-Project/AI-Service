# Auth & Tenants

Every request to the gateway carries two pieces of identity: **who is calling** and **on behalf of whom**. This page explains how those are resolved and what happens when they don't check out.

---

## Two-level identity model

```
Request
  │
  ├─ Authorization: Bearer <token>   →  resolves to a CallingService
  └─ X-Tenant-Id: tenant_acme       →  resolves to a Tenant
                                        (only if the service is allowed to act for it)
```

These are kept separate on purpose. One calling service can act on behalf of multiple tenants. One tenant can be served by multiple calling services. The permission table (`calling_service_tenants`) is the join between them.

---

## What is a tenant?

A tenant is an isolated unit. Everything in the system belongs to a tenant:

- Their encrypted API keys — one per provider (Anthropic, Bedrock, OpenAI, etc.)
- Their usage policy — monthly budget cap, model allowlist, per-request token limit
- Their ledger rows — every upstream LLM call is attributed to exactly one tenant

Tenant IDs are slug-style strings like `saathi` or `my_project`. They appear as foreign keys in every other table.

---

## What is a calling service?

A calling service is a registered backend application that sends requests to the gateway — a chatbot, a document pipeline, an internal tool. Each has:

- A name (label only)
- A **bearer token hash** — SHA-256 of the token it sends in the `Authorization` header. The raw token is never stored anywhere.
- Access grants to specific tenants

---

## How a request is authenticated

The auth logic lives in `src/shared/auth.py`. It runs as a FastAPI dependency before any handler executes.

**Step 1 — verify the calling service**

```
Authorization: Bearer svc_<your-token>
                        ↓
                   SHA-256 hash
                        ↓
    SELECT * FROM calling_services WHERE bearer_token_hash = '<hash>'
```

If no row matches → `401 Invalid bearer token`.

The raw token is never stored. If the database leaks, attackers get useless hashes. To rotate a token, generate a new one and update the hash — the old token stops working immediately.

**Step 2 — verify tenant access**

```
X-Tenant-Id: saathi
                ↓
    SELECT tenants.*
    FROM tenants
    JOIN calling_service_tenants ON tenant_id = tenants.id
    WHERE calling_service_id = <resolved service id>
      AND tenants.id = 'saathi'
```

If no row matches (tenant doesn't exist, or this service isn't granted access) → `403 Service is not authorised to act on behalf of tenant 'saathi'`.

---

## Error responses

| Situation | Status | Detail |
|-----------|--------|--------|
| Missing or malformed `Authorization` header | 401 | `Missing or malformed Authorization header.` |
| Token not found in database | 401 | `Invalid bearer token.` |
| `X-Tenant-Id` header missing | 400 | `Missing X-Tenant-Id header.` |
| Tenant not found or service not granted access | 403 | `Service is not authorised to act on behalf of tenant '<id>'.` |

---

## What the handler receives

After auth passes, the handler receives a `Tenant` ORM object:

```python
class Tenant(Base):
    id: str        # e.g. "saathi"
    name: str      # e.g. "Saathi"
    created_at: datetime
```

The `tenant.id` is then used throughout the pipeline — to look up the BYOK key, enforce policy, key the cache, and write the ledger row.

---

## Database tables

Three tables implement this model:

**`tenants`** — one row per tenant.

**`calling_services`** — one row per registered backend application. Stores `name` and `bearer_token_hash`. The raw token is never persisted.

**`calling_service_tenants`** — join table. A row here means "this calling service is allowed to act on behalf of this tenant." Cascade-deletes when either side is removed.

---

## Security properties

- **No fallback identity** — if auth fails, the request is rejected hard. There is no guest or default tenant.
- **No raw token storage** — SHA-256 pre-image resistance means a database dump does not yield live tokens.
- **Tenant isolation** — a calling service that has access to `tenant_a` cannot access `tenant_b`'s keys, ledger, or policy, even if it knows the tenant ID, unless a `calling_service_tenants` row grants it explicitly.

---

See [Keys & Secrets](keys-cli.md) for how provider API keys are stored for each tenant.
