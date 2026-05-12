# shared/policy — per-tenant policy engine

Applies to **all AI calls** — both LLM and voice. A tenant's rate limit bucket and cost budget
are shared across both modules (one pool, not separate LLM and voice pools).

## Files to create

- `base.py` — abstract `PolicyRule`:
  ```python
  async def evaluate(ctx: RequestContext) -> PolicyResult: ...
  ```
  `RequestContext` carries: `tenant_id`, `module` (llm/voice), `provider`, `model`, `estimated_tokens_or_duration`

- `registry.py` — `PolicyRegistry`:
  - Loads per-tenant rules from `shared/db` → `policies` table
  - In-memory cache with TTL (rules don't change per-request)
  - Evaluates rules in order; first failure short-circuits

- `evaluators.py` — concrete rules:
  - `RateLimitRule` — sliding-window limiter using Redis counters; covers LLM + voice calls in one bucket
  - `CostBudgetRule` — checks remaining monthly budget against `ledger_entries` aggregates (sum of `our_cost_usd` for tenant across both modules)
  - `ModelAllowDenyRule` — allowlist/denylist per `(provider, model)` pair; works for both LLM models and voice providers
  - `SizeCapRule` — max input tokens (LLM) / max audio duration (voice) / max characters (translation)

## Failure response

`429 policy_exceeded` with body indicating which rule fired and (where applicable) `Retry-After` header.

## Phase

Phase 3
