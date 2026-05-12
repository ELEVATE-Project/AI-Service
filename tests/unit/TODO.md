# tests/unit — unit tests

No real provider calls. Mock / stub all external dependencies.

## Priority test targets

- `test_normaliser.py` — request normalization, `cache_policy=auto` marker injection per provider threshold, tools[] passthrough, `cache_policy=explicit` passthrough, `cache_policy=off` strips markers
- `test_pricing.py` — four-rate cost computation (input, output, cache_write, cache_read); pricing_version included; `UnknownModelError` on missing model; **property test: `tokens × rates == our_cost_usd` for all combinations** (reconciliation invariant)
- `test_cache_key.py` — cache key is stable across equivalent requests; changes on any field diff
- `test_cache_singleflight.py` — concurrent misses on the same key produce exactly 1 upstream call; lock-timeout fallback to independent upstream call works
- `test_policy_evaluators.py` — rate limit, cost budget, model allow/deny, size cap rules fire correctly (Phase 3)
- `test_guardrail_chain.py` — PII redaction changes content per chunk (Presidio); Llama-Guard runs at completion only (split strategy resolved 2026-05-07); both end up in `guardrail_flags` (Phase 3)
- `test_secret_backend.py` — `SecretBackend` contract: get/set/delete/list; missing key raises correct error; `key_format` discrimination works for all three shapes (api_key, aws_credentials, endpoint_pair)
- `test_caller_auth.py` — `Authorization: Bearer <token>` validates against `caller_services` table; `X-Tenant-Id` checked against `allowed_tenant_ids`; missing/expired/wrong-tenant all return correct 401/403 codes
- `test_no_fallback_invariant.py` — **static analyzer test**: AST-walk all transports, fail if any code path can succeed with `key=None` or accepts a Gritworks-default key. The most load-bearing CLAUDE.md invariant.
- `test_bedrock_failures.py` — STS credential expiry → `502 tenant_key_rejected` with `code: aws_credentials_expired`; provider rate-limit → `502 upstream_rate_limited` with `Retry-After` passthrough; missing region → `422 invalid_key_format`

## Phase

All phases. Add tests alongside each feature.
