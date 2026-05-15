# llm-service — Multi-provider LLM gateway (REST + WS, MCP-ready)

## Context

Every Gritworks product that wants to call an LLM today has to integrate the
provider SDK, handle streaming, normalize tool-calling shapes, manage keys,
log usage, and re-do all of it for the next provider. That's both slow per
service and produces inconsistent observability/policy across the org.

`llm-service` will absorb that integration work behind a single API. Calling
services send a normalized request and get a normalized response, with the
gateway picking the provider/transport, enforcing policy, recording the
ledger, and surfacing traces in Langfuse. The same service hosts a sibling
voice module (Bhashini, Sarvam, Google STT/TTS, etc.) so the AI surface area
is unified across the org.

The central design tension is the integration layer. We standardise on
**LiteLLM as an in-process Python SDK** — already proven in our existing
codebase — as the gravity-well default, with **direct provider adapters as
escape hatches** for the routes where LiteLLM has known gaps (historically
Llama tool-calling) or where we want first-class control. A configurable
routing table decides per (provider, model, feature) which transport
handles the call, so a single known gap doesn't force us off LiteLLM
entirely. There is no separate gateway container — LiteLLM runs in-process
inside the FastAPI worker, which keeps ops simple (no extra service to
deploy, secure, or monitor) and removes a network hop on the hot path.

A second hard constraint is BYOK with no org-default fallback — govt
customers must hold their own keys, and silent fallback to Gritworks keys
would create real cost exposure. Missing/invalid tenant keys must produce a
clear, actionable error, never a fallback charge.

## Resolved decisions (eng review 2026-05-07)

These decisions were reached during the architecture review and override
specific items in the sections below where they conflict.

1. **Phase 1 provider list** — OpenAI direct, Anthropic direct (api.anthropic.com),
   Anthropic via Bedrock, Llama via Bedrock, HuggingFace Inference Endpoints
   (dedicated, tenant-provisioned), HuggingFace self-hosted TGI/vLLM (Gritworks
   GPU infra). LiteLLM (in-process SDK) remains the gravity-well default for
   any future provider that doesn't have an explicit override.

2. **Adapter shape** — Phase 1 ships **4 adapter files** covering 7 routes:
   - `litellm.py` — `LiteLLMTransport`, gravity-well default. Wraps the
     LiteLLM Python SDK (`litellm.acompletion`, `litellm.aembedding`,
     streaming generators). Tenant BYOK key + `api_base` injected per call,
     no shared key state.
   - `openai_compatible.py` — handles OpenAI proper, HF Endpoints, and HF
     self-hosted (all OpenAI wire-format compatible — TGI/vLLM expose
     `/v1/chat/completions`); single adapter, three registry entries with
     different `base_url`/token sources
   - `anthropic.py` — direct `api.anthropic.com`
   - `bedrock.py` — Anthropic + Llama models via boto3 (no LiteLLM gap on
     Llama tool_calls because Bedrock owns Llama natively)

3. **Streaming output guardrails** — split strategy: Presidio per chunk
   (cheap regex, microseconds — PII redacted in real time, no leak window)
   plus Llama-Guard at completion and at large buffer thresholds (bounded
   safety leak window). Per-chunk Llama-Guard rejected: N inference calls
   per stream is a real perf cliff.

4. **Caller→service auth** — per-calling-service bearer token stored
   encrypted in `shared/secrets/` (same backend, same encryption envelope as
   tenant BYOK keys). Caller passes `Authorization: Bearer <token>` plus
   `X-Tenant-Id`. `shared/auth.py` resolves token → calling-service identity
   → allowed tenants. Govt deployments add mTLS at the network/mesh layer
   (Istio sidecar / F5 / nginx) — `llm-service` application code stays
   identical.

5. **Ledger flush** — sync `asyncpg` insert from the request handler in
   Phase 1. No Arq queue. `LedgerWriter` interface is unchanged so a flip
   to Arq later is a one-day refactor when load tests show it's needed.
   `shared/queue/` shifts to Phase 4 (only `batch_submit` truly needs
   persistent scheduling).

6. **`tenant_keys` schema** — adds `key_format` column with values
   `api_key | aws_credentials | endpoint_pair`. Encrypted payload is JSON,
   shape varies by `key_format`:
   - `api_key`: `{"api_key": "..."}` (OpenAI, Anthropic, HF, LiteLLM-routed)
   - `aws_credentials`: `{"access_key_id", "secret_access_key", "region"}` (Bedrock)
   - `endpoint_pair`: `{"endpoint_url", "token"}` (HF Endpoints, custom)

7. **Cache stampede** — single-flight via Redis SETNX lock on cache miss.
   `redis-py` `redis.lock(blocking_timeout=10)`. Bounded upstream calls per
   key regardless of concurrent request count.

8. **LiteLLM kill switch** — if the Phase 1 spike or production reveals a
   LiteLLM SDK gap on a specific (provider, model, feature) combination,
   the registry override table flips that route to its direct adapter
   without code change. The 4-adapter design already covers all three
   Phase 1 providers without depending on LiteLLM for any specific route —
   LiteLLM is the safety net for future providers, not a single point of
   failure for Phase 1. Because LiteLLM is in-process, there's no separate
   container to crash; the failure mode is "raises on a known model" not
   "gateway down."

9. **HuggingFace Self-hosted TGI/vLLM is a parallel workstream** — the GPU
   infrastructure (k8s GPU node pool / SageMaker / RunPod, autoscaling,
   model warmup) is a separate project. `llm-service` only consumes the
   resulting endpoint URL via `settings.hf_internal_url`.

## In scope (v1) / out (later)

**In v1:**
- REST + WebSocket transports (MCP transport designed for, not built).
- LiteLLM in-process SDK + direct adapters for OpenAI, Anthropic, Bedrock,
  Groq, Llama.
- BYOK auth (no fallback) with pluggable secret backends.
- Exact-match cache (Redis).
- Full usage ledger with **dual cost recording**: our YAML-computed cost +
  provider-reported usage/cost block stored verbatim, for traceability
  and reconciliation.
- Policy engine: per-tenant rate limits, cost budgets, model allow/deny,
  size caps.
- **Full guardrails**: Microsoft Presidio (PII detection/redaction on
  input + output), Meta Llama-Guard (safety classification on input +
  output), plus size/token caps.
- Langfuse self-hosted traces + structlog.
- **Voice module with real providers wired**: Bhashini (govt languages),
  Sarvam (Indic), Google (STT/TTS). Same BYOK + ledger + observability
  story as LLM side.
- **Batch-API consolidation queue**: opportunistic batching of eligible
  requests onto OpenAI/Anthropic batch endpoints (50% cheaper, async)
  via Arq jobs; sync requests stay on the live path.
- **Multi-region failover**: per-(provider, model) ordered region list in
  the routing table; the `LiteLLMTransport` translates an entry's region
  list into per-call `fallbacks=[...]` for SDK-side rotation, direct
  adapters do their own region rotation on 5xx/timeout.

**Deferred:**
- MCP transport (server + client) — designed for, build later.
- Semantic (embedding-similarity) cache — exact-match only in v1.

## Architecture

```
                    REST  /  WebSocket  /  (MCP later)
                            │
                    ┌───────▼────────┐
                    │   API Layer    │   FastAPI routers + WS handlers
                    └───────┬────────┘
                            │
                    ┌───────▼────────┐
                    │  Auth/Tenant   │   resolves tenant, loads BYOK key
                    │   Resolver     │   from Secret Backend; hard-fails
                    └───────┬────────┘   if missing/invalid
                            │
                    ┌───────▼────────┐
                    │    Request     │   provider-agnostic schema:
                    │   Normaliser   │   messages, tools, params, tenant
                    └───────┬────────┘
                            │
                    ┌───────▼────────┐
                    │  Policy Engine │   rate limits, cost budgets,
                    │                │   model allow/deny, size caps
                    └───────┬────────┘
                            │
                    ┌───────▼────────┐
                    │   Guardrails   │   input filters (in), output
                    │   (in / out)   │   filters wrap streaming chunks
                    └───────┬────────┘
                            │
                    ┌───────▼────────┐
                    │     Cache      │   exact-match (Redis) keyed on
                    │  (lookup-only) │   (tenant, provider, model, hash)
                    └───────┬────────┘
                            │
                    ┌───────▼────────┐
                    │ Routing Table  │   (provider, model, feature_set)
                    │                │   → transport: litellm | direct
                    │                │   → ordered region list per entry
                    │                │     (for failover)
                    └───────┬────────┘
                            │
            ┌───────────────┼───────────────┐
            │                               │
    ┌───────▼────────┐             ┌────────▼────────┐
    │  LiteLLM SDK   │             │  Direct Adapters│
    │  (in-process,  │             │ (OpenAI, Anthr, │
    │   default)     │             │  Bedrock, Groq, │
    │                │             │  Llama, …)      │
    └───────┬────────┘             └────────┬────────┘
            │                               │
            └───────────────┬───────────────┘
                            │
                    ┌───────▼────────┐
                    │  Usage Ledger  │   Postgres row per call: tokens,
                    │   (Postgres)   │   cost, latency, transport, status
                    └───────┬────────┘
                            │
                    ┌───────▼────────┐
                    │   Langfuse +   │
                    │   structlog    │
                    └────────────────┘

         Async tasks (Arq): retries, batch jobs, ledger flush
```

## Integration strategy: LiteLLM SDK + direct adapters

A single `BaseLLMProvider` interface (`chat`, `stream`, `embed`, future
`tool_call`) with two transport implementations:

- `LiteLLMTransport` — uses the LiteLLM Python SDK in-process
  (`litellm.acompletion`, `litellm.aembedding`, async streaming
  generators). Tenant BYOK key, `api_base`, and AWS creds are passed as
  per-call kwargs — LiteLLM holds no shared key state and `litellm.api_key`
  is never set globally (would defeat BYOK).
- `DirectTransport` — per-provider, uses the official SDK directly.

A **routing registry** (`llm_service/providers/registry.py`) maps:

```python
RoutingKey = (provider: str, model: str, feature_set: frozenset[str])
# feature_set examples: {"chat"}, {"chat", "tool_calls"}, {"stream"}
```

Default: every key resolves to `LiteLLMTransport` (the gravity well — any
new provider that doesn't have an explicit override falls back to it).
Phase 1 ships explicit overrides for all five non-LiteLLM routes (resolved
2026-05-07):

```python
overrides = {
    ("openai",         "*", "*"): OpenAICompatibleTransport,  # api.openai.com
    ("anthropic",      "*", "*"): AnthropicTransport,         # api.anthropic.com
    ("bedrock",        "*", "*"): BedrockTransport,           # Anthropic + Llama via boto3
    ("hf_endpoint",    "*", "*"): OpenAICompatibleTransport,  # tenant-supplied URL+token
    ("hf_self_hosted", "*", "*"): OpenAICompatibleTransport,  # gritworks GPU infra
}
```

The original "Llama tool-calling gap" concern (which motivated the
hybrid design) is moot in Phase 1 because Llama is served via Bedrock,
not via LiteLLM. LiteLLM's role is forward-looking: any future provider
added without an override automatically gets LiteLLM's
retries/timeouts/normalised response shape. Adding a new LLM provider =
one registry line if it's OpenAI wire-format compatible (point to the
existing `OpenAICompatibleTransport` with a different `base_url`),
otherwise one new adapter file plus one registry line — or simply leave
it on the LiteLLM default if LiteLLM already supports the provider
cleanly.

**Why in-process, not LiteLLM Proxy:** LiteLLM also ships a self-hosted
proxy gateway, but running it adds a container to deploy/secure/monitor
and a network hop on every LLM call for no gain we need. BYOK injection,
cost tracking, ledger writes, and policy all live in our application
code already; the proxy would duplicate or fight with them. We use the
SDK directly inside the FastAPI worker.

**MCP-readiness:** request normaliser already accepts `tools[]` in the
schema. When we add MCP later, a new transport (`MCPTransport` consumer)
plus a new API surface (`api/mcp/` server) plug into the same interfaces;
nothing in the core flow changes.

## Tenancy & secrets — strict BYOK

`shared/auth.py` resolves the calling tenant and loads its provider key from
a pluggable `SecretBackend`:

- `PostgresEncryptedBackend` (default) — keys stored encrypted-at-rest in
  Postgres, master key from env/KMS. Works on bare metal.
- `VaultBackend` — HashiCorp Vault.
- `AWSSecretsManagerBackend` — for AWS-hosted deployments.

Govt deployments pick the backend that matches their infra; no Gritworks
servers ever hold their key.

**Failure modes** (all hard errors, never fallbacks):

- Tenant has no key for requested provider → `422 missing_tenant_key`.
- Key present but rejected by upstream → `502 tenant_key_rejected` with
  upstream error surfaced.
- Tenant exceeds policy budget/limit → `429 policy_exceeded`.

### Local development — no `.env` exposure

Keys must never live in committed `.env` files (and ideally not in any
`.env`, given how easily they leak). Local-dev path mirrors production:
the same `PostgresEncryptedBackend` runs against the dev Postgres, with
the master key sourced from the OS keyring (macOS Keychain / Linux Secret
Service, via the `keyring` lib) instead of from cloud KMS. Devs onboard
with a CLI, never editing files:

```bash
uv run llm-service keys init                # generates dev master key,
                                            # stores in OS keyring
uv run llm-service keys set \               # encrypts & writes to dev DB
    --tenant=dev --provider=openai \
    --key=sk-...
uv run llm-service keys list --tenant=dev   # masked listing for sanity
```

Production swaps the backend to `VaultBackend` or
`AWSSecretsManagerBackend` via config — same code path, different
provider for the master key and for the encrypted-key store. No code
change between environments.

## Tech stack

| Layer            | Choice                              | Rationale                                        |
| ---------------- | ----------------------------------- | ------------------------------------------------ |
| Web framework    | **FastAPI** (uvicorn + uvloop)      | Async-native, fits WS + SSE streaming + light DB |
| Validation       | Pydantic v2                         | Already in the shortlist                         |
| DB ORM           | SQLAlchemy 2.x async + asyncpg      | Mature async story for ledger writes             |
| Migrations       | Alembic                             | Standard with SQLAlchemy                         |
| Queue            | **Arq** (Redis) — Phase 4 only       | Phase 1 ledger flush is sync `asyncpg` insert    |
|                  |                                     | (resolved 2026-05-07). Arq lands when            |
|                  |                                     | `batch_submit` needs persistent scheduling.      |
| Cache            | Redis                               | Reused for queue                                 |
| Observability    | Langfuse self-hosted + structlog    | Self-hostable for govt; OTel-compatible          |
| Provider SDKs    | openai, anthropic, boto3 (Bedrock), | Direct mode only                                 |
|                  | groq, transformers/llama-cpp client |                                                  |
| Multi-provider   | **LiteLLM (Python SDK in-process)** | Default transport; reused from existing codebase |
| Package mgmt     | uv                                  | As requested                                     |
| Tests            | pytest + pytest-asyncio + vcrpy     | vcrpy for recorded provider fixtures             |

**Langchain:** Use selectively — for prompt templates, output parsers,
tool schema normalization where it adds value. **Not** as the provider
abstraction (LiteLLM + our adapters do that better). Avoids Langchain's
dependency weight bleeding into core paths.

## Pricing & ledger — full traceability

Two cost numbers are recorded per call so we can reconcile and audit:

1. **Our computed cost** — derived at request-completion time from a
   hand-maintained `pricing/models.yaml`, keyed on (provider, model),
   with input/output rates per 1K tokens, `last_updated`, `source_url`,
   and a `pricing_version` integer. The version stamp lets us re-compute
   historical costs when YAML changes without losing the original
   billing snapshot.
2. **Provider-reported usage/cost** — every upstream response includes a
   usage block (and Bedrock includes a cost figure). We store the raw
   provider usage object verbatim as JSONB, plus the parsed token counts
   and (where available) provider-reported cost. This is the
   source-of-truth audit trail when a customer disputes billing.

Pricing YAML is reviewed/updated on a cadence (recommend: weekly cron job
that lints the file, plus a manual review-and-bump after each
provider's published price change). YAML changes bump `pricing_version`.

**Ledger row shape** (`ledger_entries`):

```
id, tenant_id, request_id, created_at,
provider, model, transport (litellm|direct), region,
feature (chat|stream|embed|tool|...),
tokens_in, tokens_out,
input_tokens_cache_write, input_tokens_cache_read,
upstream_prompt_cache_hit (bool),
our_cost_usd, pricing_version,
provider_reported_usage (JSONB, raw),
provider_reported_cost_usd (nullable; populated where upstream returns it),
latency_ms, time_to_first_token_ms (nullable; streams only),
status, error_code (nullable),
our_cache_hit (bool), batched (bool),
guardrail_flags (JSONB; what fired, redactions applied)
```

Ledger writes go through a sync `asyncpg` insert from the request handler
in Phase 1 (resolved 2026-05-07; Arq queue rejected for simplicity until
load testing demands it). Idempotency on `request_id` via `INSERT ... ON
CONFLICT (request_id) DO NOTHING`. Insert failure is non-fatal for the
response — the request returns success, the ledger error is logged + traced
in Langfuse so the row can be reconstructed if needed. When traffic warrants,
the `LedgerWriter` interface flips to enqueueing an Arq task without
changing any caller — the interface is identical.

## Upstream prompt caching

Distinct from our exact-match Redis cache (which short-circuits the
upstream call entirely), **prompt caching** is a provider-side feature
that lets repeated long prefixes (system prompts, tool definitions,
large context blocks) be billed at a fraction of normal input rates.
Failing to use it leaves real money on the table for any service with a
stable system prompt.

Coverage today:

- **Anthropic** — explicit `cache_control: {"type": "ephemeral"}` markers
  on content blocks; cache writes ~25% more than input tokens, cache
  reads ~10% of input tokens. 5-min TTL.
- **OpenAI** — automatic for prompts ≥1024 tokens, no opt-in needed;
  cached input tokens billed at ~50%.
- **Bedrock (Anthropic models)** — same `cache_control` mechanism passed
  through.
- **Groq, Llama (direct)** — no provider-side prompt caching today;
  Llama via vLLM has prefix caching server-side if we host the model
  ourselves.

**Where it lives in the architecture:**

The request normaliser accepts an optional `cache_segments` hint on
messages and tool definitions:

```python
{
  "messages": [
    {"role": "system", "content": "...", "cache": "ephemeral"},
    {"role": "user",   "content": "..."}
  ],
  "tools": [...],   # tool defs cacheable as a block
  "cache_policy": "auto"   # auto | explicit | off
}
```

`auto` (default): the normaliser inspects message lengths and applies
cache markers to the system prompt + tool defs whenever they exceed the
provider's threshold (1024 tokens for OpenAI; 1024 for Anthropic
Sonnet/Haiku, 2048 for Opus).

Each transport translates the hint into provider-specific syntax:
Anthropic adds `cache_control` blocks; OpenAI relies on the prompt being
stable (we ensure prefix stability — never inject per-request strings
into the system slot); Bedrock+Anthropic passes through; Groq/Llama
ignore the hint silently.

**Ledger captures cache outcomes:**

- `input_tokens_cache_write` — billed at write rate
- `input_tokens_cache_read`  — billed at read rate
- `upstream_prompt_cache_hit` (bool, derived)

`pricing/models.yaml` includes per-model `cache_write_rate` and
`cache_read_rate` alongside the standard input/output rates. `our_cost_usd`
is computed from the four-rate model when caching applied.

**LiteLLM pass-through:** LiteLLM forwards `cache_control` blocks on
Anthropic-format requests unchanged and surfaces OpenAI prompt-cache
metadata on the response (`usage.prompt_tokens_details.cached_tokens`).
Verify during the phase-1 spike that both write and read events are
captured correctly through the SDK.

## Repo layout

`shared/` is imported by every AI module. `llm_service/` and `voice/` are peer modules —
neither imports from the other. New AI capabilities (image, code, etc.) follow the same
pattern: add a sibling under `src/`, import from `shared/`.

```
src/
    shared/                    # common infrastructure — imported by all modules
        config.py              # pydantic-settings; env-driven (DB, Redis, Langfuse, …)
        auth.py                # caller identity verification + tenant resolution
        secrets/               # SecretBackend interface + impls + local-dev keys CLI
        ledger/                # async writer; pricing table; cost calc (LLM + voice)
        observability/         # langfuse self-hosted + structlog
        policy/                # per-tenant rate limits, budgets, allow/deny
        guardrails/            # Presidio (PII) + Llama-Guard (safety); text in/out
        db/
            models.py          # SQLAlchemy ORM (tenants, keys, ledger, policies)
            migrations/        # alembic
        queue/                 # PHASE 4 — only batch_submit. Phase 1 ledger flush is sync asyncpg.
            worker.py          # arq worker (shared Redis) — Phase 4
            tasks.py           # batch_submit (Phase 4); retry_failed uses FastAPI BackgroundTasks in Phase 2
        schemas/
            envelope.py        # shared response sub-models: UsageBlock, CostBlock, …

    llm_service/               # LLM inference module
        api/
            rest/              # FastAPI routers (chat, embed, models, admin)
            ws/                # WebSocket handlers (streaming chat, sessions)
            mcp/               # placeholder; build in later phase
            deps.py            # FastAPI dependencies (auth, db, policy, guardrails)
        normaliser.py          # LLM request normalisation + cache-marker injection
        providers/
            base.py                # BaseLLMProvider interface
            registry.py            # routing table: (prov, model, feats) -> transport
            litellm.py             # LiteLLMTransport (gravity-well default; in-process SDK)
            openai_compatible.py   # OpenAI + HF Endpoints + HF self-hosted (3 routes, 1 file)
            anthropic.py           # api.anthropic.com direct
            bedrock.py             # Anthropic + Llama via boto3 (no LiteLLM gap on Llama)
        cache/                 # exact-match Redis cache + single-flight SETNX lock; semantic stub
        schemas/               # LLM Pydantic DTOs (ChatRequest, ChatResponse, …)

    voice/                     # voice module — peer to llm_service
        api/                   # STT/TTS/translate/transliterate endpoints
        normaliser.py          # voice request normalisation (language codes, formats)
        providers/             # Bhashini, Sarvam, Google STT/TTS adapters
        schemas/               # voice Pydantic DTOs (TranscribeRequest, …)

tests/
    unit/
    contract/                  # vs LiteLLM SDK + each direct adapter (vcrpy)
    e2e/
deploy/
    docker/                    # Dockerfile + docker-compose (Postgres+Redis+
                               # Langfuse) — LiteLLM is in-process, no extra container
    helm/                      # k8s chart
pricing/
    models.yaml                # per-model rates (input, output, cache_write, cache_read)
pyproject.toml                 # uv
```

## API request and response shapes

Both request and response shapes are pinned here so contract tests,
mocks, and consuming-service stubs can be written in parallel with the
gateway implementation. The request schema is provider-agnostic; the
normaliser translates to provider-specific wire formats. The response
shape is identical across providers; the normaliser/ledger fills in
every signal we collect, so callers never have to guess what an LLM call
cost or how it behaved.

### Common request headers (all endpoints)

```
Authorization: Bearer <calling-service-token>     # required; resolved by shared/auth.py
X-Tenant-Id: tenant_acme                          # required; tenant whose BYOK key + policy applies
X-Request-Id: req_01HZ7K9X3MNVQYZ0PJ5R8DTGAB      # optional; client-supplied idempotency key.
                                                  # If omitted the gateway generates one and returns
                                                  # it on the response. Used as the ledger PK.
Content-Type: application/json
Accept: application/json                          # or text/event-stream for SSE streaming
```

Errors use the same envelope across endpoints:

```jsonc
{
  "error": {
    "code": "missing_tenant_key",   // see "Failure scenarios" for the full list
    "message": "Tenant tenant_acme has no key registered for provider=anthropic",
    "request_id": "req_01H...",
    "upstream_status": null         // populated when the failure originated upstream
  }
}
```

### LLM chat — non-streaming request (`POST /v1/chat`)

```jsonc
{
  "provider": "anthropic",                  // anthropic | openai | bedrock | hf_endpoint | hf_self_hosted | <future>
  "model": "claude-sonnet-4-5",             // canonical model id; routing table maps to upstream id
  "messages": [
    {
      "role": "system",
      "content": "You are a careful tax assistant.",
      "cache": "ephemeral"                  // optional cache_segments hint; honoured per cache_policy
    },
    {
      "role": "user",
      "content": "Summarise section 80C in two bullets."
    },
    {
      "role": "assistant",                  // prior turn (multi-turn conversations)
      "content": null,
      "tool_calls": [
        { "id": "call_01", "name": "lookup_section", "arguments": { "section": "80C" } }
      ]
    },
    {
      "role": "tool",
      "tool_call_id": "call_01",
      "content": "Section 80C: deductions up to ₹1.5L on …"
    }
  ],
  "tools": [                                // optional; OpenAI-style tool defs, normalised per provider
    {
      "type": "function",
      "function": {
        "name": "lookup_section",
        "description": "Look up an Income Tax Act section by number.",
        "parameters": {
          "type": "object",
          "properties": { "section": { "type": "string" } },
          "required": ["section"]
        }
      }
    }
  ],
  "tool_choice": "auto",                    // auto | none | required | {"type":"function","function":{"name":"..."}}
  "params": {
    "temperature": 0.2,
    "max_tokens": 1024,
    "top_p": 1.0,
    "stop": null,
    "seed": null
  },
  "cache_policy": "auto",                   // auto | explicit | off  (governs cache_segments handling)
  "stream": false,                          // explicit; mirrors the endpoint choice
  "metadata": {                             // free-form; echoed to Langfuse trace, not sent upstream
    "session_id": "sess_42",
    "user_hash": "u_9f2c…"
  }
}
```

Notes:
- `provider` + `model` together resolve a routing table entry. Either one missing → `400 invalid_request`.
- `messages[].cache` is optional and only honoured when `cache_policy` is `auto` or `explicit`.
- `params.max_tokens` is hard-capped per tenant policy; over-cap requests are rejected with `429 policy_exceeded`.

### LLM chat — streaming request (`POST /v1/chat/stream`, REST SSE)

Identical body to the non-streaming request with `"stream": true`.
Client sends `Accept: text/event-stream`. Response is the SSE event
stream documented under "Streaming response" below.

### LLM chat — streaming request (`WS /v1/chat/ws`)

WebSocket subprotocol `gritworks.llm.v1`. Auth happens in the
`Sec-WebSocket-Protocol` handshake (Bearer token + `X-Tenant-Id` echoed
as `gritworks.llm.v1, bearer.<token>, tenant.<id>`) so cookies aren't
required. Per-message frames:

```jsonc
// client → server, on connect
{
  "type": "start",
  "request_id": "req_01H...",            // optional; server generates if omitted
  "request": { /* identical body to POST /v1/chat, with stream=true */ }
}

// client → server, mid-stream (optional)
{ "type": "cancel", "request_id": "req_01H..." }

// server → client (one of):
{ "type": "token",     "data": { "index": 0, "delta": "Hello" } }
{ "type": "tool_use",  "data": { "index": 0, "id": "call_…", "name": "…",
                                 "arguments_delta": "{\"city\":\"Pun" } }
{ "type": "usage",     "data": { /* incremental usage block */ } }
{ "type": "finish",    "data": { /* full ledger-grade tail; see streaming response */ } }
{ "type": "error",     "data": { "code": "…", "message": "…", "upstream_status": 401 } }
```

The `start` frame is one-shot per connection; opening multiple chats
requires multiple WS connections (keeps state simple — no multiplexing).

### LLM embeddings — request (`POST /v1/embeddings`)

```jsonc
{
  "provider": "openai",
  "model": "text-embedding-3-large",
  "input": [                               // string OR list of strings; max 2048 items per request
    "first chunk of text",
    "second chunk of text"
  ],
  "dimensions": 1024,                      // optional; provider-supported override
  "encoding_format": "float",              // float | base64
  "metadata": { "session_id": "sess_42" }
}
```

Response:

```jsonc
{
  "id": "req_01H...",
  "object": "embedding.batch",
  "tenant_id": "tenant_acme",
  "provider": "openai",
  "model": "text-embedding-3-large",
  "transport": "litellm",
  "data": [
    { "index": 0, "embedding": [0.0123, -0.0456, /* … */] },
    { "index": 1, "embedding": [/* … */] }
  ],
  "usage":     { "input_tokens": 42, "total_tokens": 42 },
  "cost":      { "computed_usd": 0.0001, "pricing_version": 7,
                 "provider_reported_usd": 0.0001, "currency": "USD" },
  "latency_ms":{ "total": 130, "guardrails_in": 4, "upstream": 124 },
  "cache":     { "our_cache_hit": false },
  "guardrails":{ "input_flags": [], "redactions_applied": [] },
  "policy":    { "rate_limit_remaining": 4520, "budget_remaining_usd": 234.55 }
}
```

### Voice — STT request (`POST /v1/voice/transcribe`)

Multipart upload (`multipart/form-data`):

```
--boundary
Content-Disposition: form-data; name="meta"
Content-Type: application/json

{
  "provider": "bhashini",                  // bhashini | sarvam | google
  "model": "bhashini-asr-hi",              // canonical id
  "language": "hi-IN",                     // BCP-47 tag; provider-specific normalisation in normaliser
  "diarization": false,
  "punctuation": true,
  "timestamps": "word",                    // none | word | segment
  "metadata": { "session_id": "sess_42" }
}
--boundary
Content-Disposition: form-data; name="audio"; filename="clip.wav"
Content-Type: audio/wav

<binary audio bytes>
--boundary--
```

Accepted audio formats: `audio/wav`, `audio/mpeg`, `audio/ogg`,
`audio/flac`, `audio/webm`. Hard cap 25 MB per request (configurable per
tenant policy).

Response:

```jsonc
{
  "id": "req_01H...",
  "object": "voice.transcription",
  "tenant_id": "tenant_acme",
  "provider": "bhashini",
  "model": "bhashini-asr-hi",
  "transport": "direct",
  "language": "hi-IN",
  "text": "नमस्ते, मैं ठीक हूँ।",
  "segments": [
    { "start_ms": 0, "end_ms": 1240, "text": "नमस्ते,", "speaker": null },
    { "start_ms": 1240, "end_ms": 2980, "text": "मैं ठीक हूँ।", "speaker": null }
  ],
  "usage":     { "audio_seconds": 2.98 },
  "cost":      { "computed_usd": 0.0009, "pricing_version": 7,
                 "provider_reported_usd": null, "currency": "USD" },
  "latency_ms":{ "total": 412, "upstream": 400 },
  "guardrails":{ "input_flags": [], "redactions_applied": [] },
  "policy":    { "rate_limit_remaining": 99, "budget_remaining_usd": 234.55 }
}
```

### Voice — TTS request (`POST /v1/voice/synthesize`)

```jsonc
{
  "provider": "sarvam",
  "model": "sarvam-tts-v1",
  "text": "नमस्ते, आपका स्वागत है।",
  "language": "hi-IN",
  "voice": "meera",                        // provider-specific voice id
  "format": "wav",                         // wav | mp3 | ogg
  "sample_rate_hz": 24000,
  "speed": 1.0,
  "metadata": { "session_id": "sess_42" }
}
```

Response is `application/octet-stream` (the audio bytes) with
ledger-grade metadata in headers, mirroring the response envelope:

```
Content-Type: audio/wav
X-Gritworks-Request-Id: req_01H...
X-Gritworks-Provider: sarvam
X-Gritworks-Model: sarvam-tts-v1
X-Gritworks-Transport: direct
X-Gritworks-Cost-Usd: 0.0017
X-Gritworks-Pricing-Version: 7
X-Gritworks-Audio-Seconds-Out: 3.12
X-Gritworks-Latency-Ms: 540
X-Gritworks-Cache-Hit: false
```

For callers that prefer JSON over headers, `Accept: application/json`
returns `{ "audio_b64": "...", /* full envelope */ }` instead.

### Voice — translate request (`POST /v1/voice/translate`)

Multipart upload identical to STT, with `meta.target_language` added.
Response shape matches STT plus `translated_text` and `source_language`.

### Voice — transliterate request (`POST /v1/voice/transliterate`)

Text-only JSON body:

```jsonc
{
  "provider": "bhashini",
  "model": "bhashini-xlit-hi",
  "text": "namaste",
  "source_script": "Latn",
  "target_script": "Deva",
  "language": "hi-IN"
}
```

Response: `{ "text": "नमस्ते", /* full envelope */ }`.

## API response shape

The chat-completion response shape below is the canonical envelope; the
embedding and voice responses above all follow the same `usage` /
`cost` / `latency_ms` / `cache` / `guardrails` / `policy` block layout.

### Non-streaming response (`POST /v1/chat`)

```jsonc
{
  "id": "req_01HZ7K9X3MNVQYZ0PJ5R8DTGAB",
  "object": "chat.completion",
  "created": 1730000000,
  "tenant_id": "tenant_acme",
  "provider": "anthropic",
  "model": "claude-sonnet-4-5",
  "transport": "litellm",          // or "direct"
  "region": "us-east-1",
  "choices": [
    {
      "index": 0,
      "message": {
        "role": "assistant",
        "content": "...",
        "tool_calls": [             // present when the model invoked tools
          { "id": "call_...", "name": "...", "arguments": { ... } }
        ]
      },
      "finish_reason": "stop"       // stop | length | tool_use | content_filter
    }
  ],
  "usage": {
    "input_tokens": 1234,
    "output_tokens": 567,
    "input_tokens_cache_write": 500,    // upstream prompt-cache write
    "input_tokens_cache_read": 1000,    // upstream prompt-cache read
    "total_tokens": 1801
  },
  "cost": {
    "computed_usd": 0.0123,             // from pricing YAML
    "pricing_version": 7,
    "provider_reported_usd": 0.0125,    // null if provider doesn't report
    "currency": "USD"
  },
  "latency_ms": {
    "total": 845,
    "guardrails_in": 12,
    "guardrails_out": 5,
    "upstream": 820,
    "time_to_first_token": null         // streams only
  },
  "cache": {
    "our_cache_hit": false,             // exact-match Redis cache
    "upstream_prompt_cache_hit": true   // provider-side prompt cache
  },
  "guardrails": {
    "input_flags": [],
    "output_flags": [],
    "redactions_applied": ["pii.email"]
  },
  "policy": {
    "rate_limit_remaining": 4521,
    "budget_remaining_usd": 234.56
  },
  "provider_raw": { /* untouched provider payload, for debugging/audit */ }
}
```

### Streaming response (REST SSE or WebSocket)

A single envelope shape across both transports, so client code is
identical. Each event has a `type` and a `data` payload:

```
event: token
data: {"index": 0, "delta": "Hello"}

event: token
data: {"index": 0, "delta": ", world"}

event: tool_use
data: {"index": 0, "id": "call_...", "name": "get_weather",
       "arguments_delta": "{\"city\":\"Pun"}

event: usage
data: {"input_tokens": 1234, "output_tokens": 12,
       "input_tokens_cache_read": 1000}

event: finish
data: {                                  // same shape as non-stream tail
  "id": "req_01H...",
  "finish_reason": "stop",
  "usage":     { /* final totals */ },
  "cost":      { /* computed + provider-reported */ },
  "latency_ms":{ "total": 845, "time_to_first_token": 142, ... },
  "cache":     { "our_cache_hit": false,
                 "upstream_prompt_cache_hit": true },
  "guardrails":{ ... },
  "policy":    { ... }
}

event: error
data: {"code": "tenant_key_rejected", "message": "...",
       "upstream_status": 401}
```

**Why a `finish` event with full metadata:** callers always get the same
ledger-grade information whether they consumed the stream or used the
non-streaming endpoint. No hidden state, no extra round-trip needed to
look up cost/latency/cache info after the fact.

## Phased delivery

Updated 2026-05-07 to reflect resolved decisions (see "Resolved decisions"
above): Phase 1 ships all 4 adapter files together, sync ledger insert (no
Arq), single-flight cache stampede protection deferred to Phase 2 with
`cache_segments` work, caller bearer-token auth lands in Phase 1.

1. **Foundations + full provider list** — skeleton, REST, **all 4 adapter
   files** (`litellm.py`, `openai_compatible.py`, `anthropic.py`,
   `bedrock.py`), ledger (dual cost + pricing YAML wired with cache rates,
   sync `asyncpg` insert), BYOK auth + `PostgresEncryptedBackend` + local-dev
   keys CLI, **caller→service bearer-token auth** (`Authorization: Bearer` +
   `X-Tenant-Id`, encrypted store in `shared/secrets/`), Langfuse, structlog,
   CI + Docker image, full API response envelope. One round-trip works
   end-to-end against any of the 7 routes with a ledger row that records
   computed cost, provider-reported usage, and prompt-cache outcomes.
2. **Streaming + prompt caching + cache** — WS streaming + REST SSE with the
   unified envelope; exact-match Redis cache with **single-flight SETNX lock**
   (stampede protection); `cache_segments` hint and `cache_policy=auto`
   wired through Anthropic (direct + Bedrock) and OpenAI; routing-table
   override flips behave correctly under load. LiteLLM kill-switch tested:
   force a LiteLLM exception (mock `litellm.acompletion` to raise on a
   chosen route), confirm the registry override flips that route to its
   direct adapter without code change.
3. **Policy + split-strategy guardrails + multi-region** — policy engine
   (rate/budget/allowlist); **Presidio per-chunk PII redaction + Llama-Guard
   on completion** (split strategy resolved 2026-05-07); multi-region
   failover with the routing table as the single source of truth (LiteLLM
   `fallbacks=[...]` per call is driven from the same routing entries — we
   don't maintain a separate LiteLLM config).
4. **Voice + batch consolidation + Arq** — Bhashini, Sarvam, Google STT/TTS
   wired through the same auth/ledger/observability stack; **Arq lands here**
   with `batch_submit` (multi-hour async jobs that need persistent
   scheduling); Arq-backed batch-API consolidator that promotes eligible
   async LLM requests onto OpenAI/Anthropic batch endpoints. Ledger flush
   may also flip to Arq if Phase 1-3 load tests show Postgres as the latency
   floor.
5. **Future** — MCP server + client transports; semantic cache.

## Verification

- **Spike before phase 1 lands:** install LiteLLM in a throwaway script,
  send a Llama chat-completion with a tool call via the SDK (both directly
  and via Bedrock-routed model strings). If LiteLLM handles tool_calls
  cleanly on the routes we care about, simplify the routing table; if not,
  confirm the direct-adapter override path. Also confirm Anthropic
  `cache_control` and OpenAI `cached_tokens` survive the SDK round-trip.
  Either way the answer is recorded, not assumed.
- **Contract tests** per provider (LLM and voice) with vcrpy-recorded
  fixtures — guarantees we don't drift from upstream response shapes.
- **E2E tests:** REST non-stream, REST SSE stream, WS stream; assert
  ledger row exists with both `our_cost_usd` and `provider_reported_usage`
  populated, and that token counts × pricing_version YAML reproduces
  `our_cost_usd`.
- **BYOK negative tests:** missing key → 422; bad key → 502 with upstream
  error surfaced; expired key → same path as bad key. No code path
  silently falls back to a Gritworks-default key.
- **Guardrails tests:** known-PII inputs are redacted before they hit the
  upstream; Llama-Guard flagged outputs are blocked or annotated per
  policy; both events show up in ledger `guardrail_flags`.
- **Multi-region failover test:** chaos-style — kill the primary region's
  endpoint mid-request and confirm the next region in the list serves
  the response, with the failover recorded in the ledger row.
- **Batch-consolidator test:** N eligible async requests within a window
  produce one upstream batch submission; ledger rows correctly attribute
  per-request cost from the batch result.
- **Load test (phase 2+):** Locust against a stub provider to confirm WS
  fan-out and ledger writes don't bottleneck under concurrency.
- **Prompt-cache test:** identical system prompt across two requests
  produces a cache write on the first and a cache read on the second;
  ledger rows show `input_tokens_cache_write` then
  `input_tokens_cache_read`, and `our_cost_usd` reflects the cheaper
  read rate on call two.

## Documentation deliverables

All docs live in `docs/`. The plan file becomes the project's
architecture spec on day one — not throwaway:

```
docs/
    architecture.md       # this file
    api.md                # REST + WS endpoints, request/response schemas,
                          # full response-envelope spec, error codes
    integration-layer.md  # LiteLLM vs direct routing table; how to add
                          # a new provider in three steps
    secrets-and-byok.md   # secret backend interface, local-dev CLI,
                          # production deployment per backend
    pricing-and-ledger.md # YAML format, dual cost recording, ledger
                          # schema, cache-rate columns, reconciliation
                          # query examples
    prompt-caching.md     # provider-by-provider matrix; cache_segments
                          # hint reference; auto vs explicit policy
    guardrails.md         # Presidio + Llama-Guard config, redaction
                          # policy, ledger flags
    voice.md              # Bhashini / Sarvam / Google integration notes
    deployment.md         # Docker, k8s helm, bare-metal/govt notes
    runbook.md            # on-call: failover triggers, common errors,
                          # ledger reconciliation, key rotation
```

The supplementary docs above are produced as each phase lands; this file
(`architecture.md`) is the anchor.

## Critical files (when implementation starts)

- `src/llm_service/providers/base.py` — LLM provider interface
- `src/llm_service/providers/registry.py` — routing table (hybrid LiteLLM / direct;
  keep it readable, not magic)
- `src/shared/auth.py` + `src/shared/secrets/` — BYOK enforcement; used by both
  `llm_service` and `voice`
- `src/llm_service/normaliser.py` — LLM request schema; must be MCP-ready
  (tool definitions first-class)
- `src/shared/db/models.py` — ledger schema; once shipped, migrations only

## Open questions for after approval

- **Pricing YAML update cadence**: weekly cron job that lints + emails a
  diff for review, vs purely manual updates with a CI check that fails
  the build if any model in `pricing/models.yaml` is older than N days.
  Recommend the latter — forces freshness without auto-merging untrusted
  scraped data.
- **Voice provider tenancy**: same BYOK model as LLM (recommended for
  consistency), or shared Gritworks accounts for the Indian govt
  providers (Bhashini etc.) where we negotiate volume? Affects voice key
  loading.

(The earlier "caller→service auth" question was resolved during the
2026-05-07 eng review — see "Resolved decisions" §4.)



## Failure scenarios (catalogued 2026-05-07)

Each scenario lists: detection, error handling, and what the caller sees.

- **Tenant has no key for provider** — detected at `shared/auth.py` resolve
  step. Returns `422 missing_tenant_key`. Caller sees explicit error code.
- **Tenant key rejected by upstream** — detected at adapter on first
  request. Returns `502 tenant_key_rejected` with upstream error surfaced
  verbatim. No retries to Gritworks-default key (hard invariant).
- **Bedrock STS credential expired** — detected by `boto3` client error
  type. Returns `502 tenant_key_rejected` with `code:
  aws_credentials_expired`. Caller can re-issue STS creds and retry.
- **Provider rate-limits tenant key (not Gritworks policy)** — detected at
  adapter on upstream 429. Returns `502 upstream_rate_limited` with
  `Retry-After` header passed through from upstream. Distinct from policy
  exceeded.
- **LiteLLM SDK raises mid-stream** (network blip, upstream disconnect,
  unhandled provider quirk) — detected by `try/except` around the async
  generator in `LiteLLMTransport.stream()`. Emits synthetic `error` event
  with `code: upstream_disconnected` on the WS/SSE stream. Ledger row
  records `status: partial_response` plus `error_code` from the LiteLLM
  exception class. Caller sees clean error event instead of hung connection.
- **Caller WS reads slowly, upstream stream backs up** — detected by
  per-connection bounded buffer (default ~256 chunks). Emits `error` event
  with `code: caller_too_slow`, disconnects. Documented in `docs/api.md`.
- **Pricing YAML missing model** — `PricingTable.compute_cost` raises
  `UnknownModelError`. Request fails before upstream call (no silent $0
  ledger row).
- **Static fallback to Gritworks-default key (must NOT exist)** — caught by
  static analyzer test `tests/unit/test_no_fallback_invariant.py` at CI
  time. AST-walks all transports, fails build if any code path can succeed
  with `key=None`.
