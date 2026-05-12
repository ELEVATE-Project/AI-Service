# tests/contract — provider contract tests (vcrpy)

Guards against upstream response shape drift without hitting real APIs in CI.

## Structure

Resolved during eng review (2026-05-07): Phase 1 ships 4 transport adapters covering 7 routes. Contract tests align 1:1 with the adapter files.

```
tests/contract/
    cassettes/
        litellm/                # exercise SDK against ≥1 representative model per route
        openai_compatible/      # covers openai, hf_endpoint, hf_self_hosted (record one per registry route)
        anthropic/
        bedrock/                # covers Anthropic + Llama models on Bedrock
    test_litellm.py
    test_openai_compatible.py   # 3 cassette sets — exercise all three base_url configurations
    test_anthropic.py
    test_bedrock.py
```

## What to test per adapter

1. Non-streaming chat — response maps correctly to `ChatResponse` envelope
2. Streaming chat — all expected event types (`token`, `tool_use`, `usage`, `finish`) present
3. Tool call — tool invocation in response parsed into `choices[].message.tool_calls[]`
4. Prompt cache — `input_tokens_cache_write` / `input_tokens_cache_read` extracted when present

## BYOK negative tests (no cassette needed — mock the transport)

- Missing tenant key → `422 missing_tenant_key`
- Bad key (upstream 401) → `502 tenant_key_rejected` with upstream error surfaced
- Expired key → same path as bad key
- **No code path produces a fallback to a Gritworks-default key** — backed by `tests/unit/test_no_fallback_invariant.py` static analyzer
- **AWS credential shape**: missing `region` in `aws_credentials` payload → `422 invalid_key_format` with explicit field-level error
- **Endpoint-pair shape**: missing `endpoint_url` or `token` in `endpoint_pair` payload → `422 invalid_key_format`

## Caller-auth negative tests

- Missing `Authorization` header → `401 unauthenticated`
- Bearer token not in `caller_services` table → `401 invalid_caller_token`
- Bearer token valid but `X-Tenant-Id` not in `allowed_tenant_ids` → `403 tenant_not_allowed_for_caller`
- Token rotation (token replaced in DB) → cached lookup invalidated within configured TTL

## Phase

Phase 1+. Record cassettes during development, commit them.
