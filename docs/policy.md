# Policy Engine

The policy engine runs before any upstream LLM call. It enforces per-tenant usage controls — model allowlists, token limits, and monthly spend caps. A policy violation returns `429` immediately, before touching any provider and before spending any money.

---

## When the policy runs

```
Request pipeline
  │
  ├─ Auth + tenant resolution       ← 401/403 on failure
  ├─ Request normalisation
  ► Policy check                    ← 429 on violation (you are here)
  ├─ Guardrails — input
  ├─ Cache lookup
  └─ LLM call
```

The policy check runs after auth (we need a resolved tenant to look up their policy) and before the cache (no point checking the cache if the request is going to be rejected anyway).

---

## The policy table

Each tenant has at most one `policies` row. All limits are optional — a `null` value means that limit is not enforced.

| Column | Type | What it controls |
|--------|------|-----------------|
| `max_tokens_per_request` | `int?` | Hard cap on `params.max_tokens` per request |
| `rate_limit_rpm` | `int?` | Maximum requests per minute *(stored, not yet enforced in real-time)* |
| `budget_usd_monthly` | `float?` | Monthly spend ceiling in USD |
| `allowed_models` | `string[]?` | Whitelist — only these models are allowed. `null` = all |
| `denied_models` | `string[]?` | Blacklist — these models are always rejected. `null` = none |

If a tenant has no `policies` row at all, all limits are treated as `null` (no restrictions).

---

## Checks in order

The policy checker (`src/shared/policy/checker.py`) runs four checks in sequence. The first violation raises immediately.

### 1. Denied model check

```python
if policy.denied_models and request.model in policy.denied_models:
    raise PolicyExceededError(f"model denied: {request.model}")
```

If the requested model is on the tenant's deny list → `429`.

### 2. Allowed model check

```python
if policy.allowed_models and request.model not in policy.allowed_models:
    raise PolicyExceededError(f"model not in allowlist: {request.model}")
```

If an allowlist is configured and the model is not on it → `429`. If `allowed_models` is `null`, this check is skipped.

### 3. Max tokens per request

```python
if (policy.max_tokens_per_request is not None
        and request.max_tokens is not None
        and request.max_tokens > policy.max_tokens_per_request):
    raise PolicyExceededError(...)
```

If the request asks for more tokens than the tenant's per-request cap → `429`. Only fires when both the policy limit and the request's `max_tokens` are set.

### 4. Monthly budget

```python
first_day = datetime.now(UTC).replace(day=1, hour=0, minute=0, second=0, microsecond=0)
spent = SELECT SUM(our_cost_usd) FROM ledger_entries
        WHERE tenant_id = ? AND created_at >= first_day

if spent >= policy.budget_usd_monthly:
    raise PolicyExceededError("monthly budget exceeded")
```

Sums `our_cost_usd` from `ledger_entries` for the current calendar month. If the tenant has already hit or exceeded their budget → `429`. This uses the computed cost from the pricing YAML, not the provider-reported figure.

This check is only as accurate as the ledger write path — every successful, cache-hit, and errored request writes a row (see [Database Models → ledger_entries](models.md#ledger_entries)), including `our_cost_usd=0` rows for cache hits, so cached requests don't inflate spend. `request_id` (the client-supplied `X-Request-Id`) collisions don't drop a row either — the underlying provider call already happened by the time a collision is detected, so the write is retried once under a disambiguated id rather than treated as an already-recorded duplicate. A ledger write failure is logged but non-fatal (it never blocks the response), which means a genuine, non-retryable DB failure is also invisible to this SUM — a rare dropped write undercounts spend for that month rather than blocking the tenant.

**`budget_usd_monthly` is a best-effort, soft ceiling — not an atomic hard cap.** The check reads `SUM(our_cost_usd)` *before* the provider call; the current request's own cost is only written to the ledger *after* it completes. There is no reservation step and no locking, so:

- Two concurrent requests near the cap can both pass the check (neither's cost is recorded yet), both call the provider, and combined spend can land past the budget.
- A tenant can overshoot their monthly budget by roughly the cost of whatever's in flight at the moment they cross it — the check stops *further* requests once the ledger catches up, it doesn't prevent a brief overshoot.

If you need a hard, atomic ceiling (e.g. reserve the estimated cost off `params.max_tokens` before dispatch, settle with actual usage after, backed by an atomic per-tenant counter instead of a `SUM()` over historical rows), that's a separate piece of work — this check as implemented is a spend-limiting guardrail, not a billing-grade enforcement mechanism.

---

## Error response

When any check fails, the handler returns:

```
HTTP 429 Too Many Requests

{
  "detail": "policy_exceeded"
}
```

The `detail` field is always the string `"policy_exceeded"` regardless of which specific limit was hit. The internal `PolicyExceededError` message (which includes the specific reason) is logged server-side but not exposed to the caller.

---

## How to configure a policy

Policies are rows in the `policies` table. There is no CLI for this yet — insert directly via SQL or a migration during provisioning:

```sql
INSERT INTO policies (id, tenant_id, budget_usd_monthly, allowed_models, max_tokens_per_request)
VALUES (
    gen_random_uuid(),
    'saathi',
    50.00,                            -- $50/month cap
    ARRAY['claude-sonnet-4-5', 'gpt-4o-mini'],  -- only these models
    4096                              -- max 4096 output tokens per request
);
```

All columns are nullable — set only what you want to enforce and leave the rest `null`.

---

## What the caller receives

The `policy` block appears on every successful response, letting callers track remaining headroom:

```json
{
  "policy": {
    "rate_limit_remaining": null,
    "budget_remaining_usd": 49.97
  }
}
```

`rate_limit_remaining` is `null` until real-time rate limiting is wired up. `budget_remaining_usd` reflects current-month spend subtracted from the configured cap.

---

See [Guardrails](guardrails.md) for content-level controls that run after the policy check.
