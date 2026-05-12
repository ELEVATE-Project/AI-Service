# Auth and Tenants

This explains who is allowed to call the gateway and how the gateway knows who they are.

---

## Two-level identity model

Every request carries two pieces of identity:

1. **Who is calling** — a `calling service` (your chatbot backend, your data pipeline, etc.)
2. **On behalf of whom** — a `tenant` (the customer or team whose API keys and budget apply)

These are kept separate on purpose. One calling service can act on behalf of multiple tenants. One tenant can be served by multiple calling services. The rules about which service can act for which tenant are stored in the database.

---

## What is a tenant?

A tenant is an isolated unit. Each tenant has:

- Their own encrypted API keys (one per provider)
- Their own usage policy (rate limits, monthly budget, model allowlist)
- Their own ledger rows (every call is attributed to a tenant)

In local dev you have one: `tenant_dev`. In production there would be one per customer or team.

Tenant IDs are simple strings like `tenant_acme` or `tenant_govx`. They're the foreign key that connects keys, ledger entries, and policies.

---

## What is a calling service?

A calling service is a registered application that sends requests to the gateway. Examples: a tax chatbot backend, a document summariser, an internal tool.

Each calling service has:

- A name (just a label)
- A **bearer token hash** — the SHA-256 of the token it will use in requests. The raw token is never stored.
- A list of tenants it's allowed to act on behalf of

---

## How a request is authenticated

When a request comes in:

```
POST /v1/chat
Authorization: Bearer my-dev-token-123
X-Tenant-Id: tenant_dev
```

The gateway does this, in order:

```
1. Extract the token from the Authorization header
        ↓
2. SHA-256 hash it
        ↓
3. Look up that hash in the calling_services table
   → No match: 401 Invalid bearer token
        ↓
4. Check that tenant_dev is in the service's allowed_tenant_ids
   → Not there: 403 Not authorised for this tenant
        ↓
5. Look up tenant_dev in the tenants table
   → Not found: 403 Tenant not found
        ↓
6. Pass the Tenant object to the handler
```

If all steps pass, the request continues. If any step fails, the request stops right there.

---

## Why is the token hashed?

The raw token is never stored anywhere. Only its SHA-256 hash goes into the database.

This means if the database is ever leaked, attackers get a list of hashes — useless without the original tokens. It's the same reason passwords are stored as hashes.

To rotate a token: generate a new one, hash it, update the `bearer_token_hash` column for that service. Old tokens stop working immediately.

---

## Managing calling services

Register a new service:

```bash
uv run llm-service services add \
  --name my-service \
  --token some-secret-token \
  --tenants tenant_dev,tenant_acme
```

List all registered services:

```bash
uv run llm-service services list
```

There's no `services remove` command yet — to remove a service, delete its row from the `calling_services` table directly in Postgres.

---

## Managing tenants

Tenants are created automatically when you run `keys set` for the first time — the CLI creates the tenant row if it doesn't exist yet.

To create a tenant without setting a key:

```sql
INSERT INTO tenants (id, name) VALUES ('tenant_acme', 'Acme Corp');
```

Or just run `keys set` and the tenant gets created as a side effect.