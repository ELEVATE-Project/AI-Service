# Implementation Guide — How to Build This Step by Step

This doc walks you through the full picture: what each piece does, what order to build
it, and what a real request looks like as it travels through every layer. Written for
someone about to start coding Phase 1 from scratch.

---

## The Big Picture (One Paragraph)

A calling service (say, a tax chatbot) sends an HTTP request to `llm-service` asking it
to talk to OpenAI on their behalf. `llm-service` checks who they are, loads their OpenAI
key from the database, calls OpenAI, and sends back a response with full cost + token
data attached. The calling service never touches the OpenAI SDK — that's our job.

The service is a **FastAPI app**. Every request goes through a fixed pipeline of steps.
You build each step as its own module in `src/`. They plug together in the API handler.

---

## Walk Through One Request — Detailed

Let's say a calling service sends this:

```http
POST /v1/chat
Authorization: Bearer svc_token_abc123
X-Tenant-Id: tenant_acme
Content-Type: application/json

{
  "provider": "openai",
  "model": "gpt-4o",
  "messages": [
    { "role": "user", "content": "What is 2 + 2?" }
  ],
  "params": { "temperature": 0.2, "max_tokens": 100 },
  "stream": false
}
```

Here is what happens, in order, line by line.

---

### Step 1 — FastAPI receives the request (`api/rest/chat.py`)

FastAPI matches the route `POST /v1/chat`. Before your handler function even runs,
FastAPI calls the **dependency functions** you declared (more on deps below). Your
handler gets called with the already-resolved objects those deps returned.

```python
@router.post("/v1/chat")
async def chat(
    body: ChatRequest,           # Pydantic parsed + validated the JSON body
    tenant: Tenant = Depends(get_tenant),      # from shared/auth.py
    db: AsyncSession = Depends(get_db),        # from shared/db
    ...
):
    ...
```

**What `ChatRequest` is:** a Pydantic model in `src/llm_service/schemas/chat.py`. It
defines exactly what fields are allowed in the body, their types, and which are required.
If the JSON body is malformed or missing a required field, FastAPI returns a `422` before
your handler runs — you don't have to write that check yourself.

---

### Step 2 — Auth + Tenant Resolution (`shared/auth.py`)

This is the `get_tenant` dependency. It does two things:

**1. Verify the caller (Auth Token)**

Reads `Authorization: Bearer svc_token_abc123`, hashes it with SHA-256, and looks up
the hash in the `calling_services` table (`bearer_token_hash` column). The raw token is
never stored — only its hash is. This means:
- If the DB leaks, attackers get useless hashes, not live tokens
- To rotate a token, just generate a new one, hash it, update the row — done

If no match is found → raises `HTTPException(401)`.

**2. Resolve the tenant (Tenant ID)**

Reads `X-Tenant-Id: tenant_acme` and checks that the matched `CallingService` row
actually lists `tenant_acme` in its `allowed_tenant_ids`. A calling service may be
registered for some tenants but not others.

If the tenant is not in the allowed list → raises `HTTPException(403)`.

**What it returns:** a `Tenant` object (a Pydantic model / dataclass) containing
`tenant_id`, `name`, etc. Your handler gets this via `Depends(get_tenant)`.

**Why this matters for BYOK:** `auth.py` doesn't load the LLM key itself — that happens
later in the provider layer. `auth.py` just answers two questions:
- *"Who are you?"* — from the auth token
- *"Who are you acting for?"* — from the tenant ID

**Example:**

```
CallingService row:
  name: "taxbot-inc"
  bearer_token_hash: sha256("svc_token_abc123") → "9f86d08..."
  allowed_tenant_ids: ["tenant_acme", "tenant_globex"]

Incoming request: Bearer svc_token_abc123 + X-Tenant-Id: tenant_acme
→ hash token, find row ✅, tenant in allowed list ✅ → returns Tenant(id="tenant_acme")

Incoming request: Bearer svc_token_abc123 + X-Tenant-Id: tenant_wayne_corp
→ hash token, find row ✅, tenant NOT in allowed list ❌ → 403

Incoming request: Bearer bad_token + X-Tenant-Id: tenant_acme
→ hash token, no row found ❌ → 401
```

---

### Step 3 — Load the Tenant's Key (`shared/secrets/`)

Not a separate pipeline step — the provider adapter does this at call time. But here's
how it works:

`src/shared/secrets/` defines a `SecretBackend` interface:

```python
class SecretBackend:
    async def get_key(self, tenant_id: str, provider: str) -> TenantKeyPayload: ...
    async def set_key(self, tenant_id: str, provider: str, payload: dict) -> None: ...
```

There is one concrete implementation for Phase 1: `PostgresEncryptedBackend`. It reads
from a `tenant_keys` table in Postgres where the key is stored encrypted. The master
encryption key lives in the OS keyring locally, and in Vault / AWS Secrets Manager in
production.

When the provider adapter wants the OpenAI key for `tenant_acme`, it calls:

```python
key_payload = await secret_backend.get_key("tenant_acme", "openai")
# returns: TenantKeyPayload(key_format="api_key", data={"api_key": "sk-..."})
```

If no key exists → raises `MissingTenantKeyError` → handler returns `422 missing_tenant_key`.

---

### Step 4 — Normalise the Request (`llm_service/normaliser.py`)

The raw `ChatRequest` that came in has fields like `provider`, `model`, `messages`,
`tools`, `params`. The normaliser converts this into a **provider-agnostic internal
object** called `NormalisedLLMRequest`.

Why do this? Each provider has slightly different field names. OpenAI uses `max_tokens`,
Anthropic uses `max_tokens` too but different structure for messages, Bedrock uses its
own format. The normaliser is where you do that translation once — the provider adapters
then receive a clean, consistent object.

**What the normaliser also does:**
- Validates that `provider` + `model` are present (missing → `400`)
- Injects `cache_control` markers on system prompts/tool defs if `cache_policy=auto`
  (Phase 2 detail — skip for now)
- Passes through `metadata` untouched (it goes to Langfuse, not upstream)

```python
# normaliser.py
def normalise(request: ChatRequest) -> NormalisedLLMRequest:
    return NormalisedLLMRequest(
        provider=request.provider,
        model=request.model,
        messages=request.messages,  # may transform shapes per provider later
        tools=request.tools,
        params=request.params,
        stream=request.stream,
        metadata=request.metadata,
    )
```

For Phase 1 this is mostly a pass-through. It becomes more important in Phase 2 when
cache hints get injected.

---

### Step 5 — Policy Check (`shared/policy/`)

Before spending money on an upstream call, check if the tenant is allowed to:

- **Rate limit**: have they made too many requests in the last minute/hour?
- **Budget**: have they already spent their cost budget this month?
- **Model allowlist**: is `gpt-4o` in the list of models this tenant can use?
- **Size cap**: is `max_tokens: 100` within their allowed limit?

This reads from a `policies` table in Postgres (or from an in-memory config keyed by
tenant). It raises `PolicyExceededError` → handler returns `429 policy_exceeded`.

**Phase 1 approach:** simple per-tenant config in the database. No Redis counters yet
(that lands in Phase 2 with the cache).

---

### Step 6 — Guardrails: Input (`shared/guardrails/`)

Before the request hits the LLM, check the user's message for:

- **PII (Presidio)**: email addresses, phone numbers, ID numbers in the message text
- **Safety (Llama-Guard)**: harmful/illegal content

If something bad is found → either redact it (PII) or block the request entirely
(safety violation).

**Phase 1:** stub this out. Return the input unchanged, record no flags. Wire it properly
in Phase 3. The important thing is that your pipeline has a `guardrails_in` hook point
from day one, even if it does nothing yet.

---

### Step 7 — Cache Lookup (`llm_service/cache/`)

Check Redis: has this exact request (same tenant + provider + model + messages) been
answered before?

The cache key is a hash of `(tenant_id, provider, model, messages_json, params_json)`.
If a cache hit → return the cached `ChatResponse` immediately, skip Steps 8–10, go
straight to Step 11.

**Phase 1:** stub this out as a cache miss always. Wire Redis properly in Phase 2. Again,
the hook point should be there from day one.

---

### Step 8 — Route to Provider (`llm_service/providers/registry.py`)

The registry is a lookup table that says: given `(provider, model, feature_set)`, which
transport class handles this call?

```python
# The table in registry.py
overrides = {
    ("openai",         "*", frozenset({"chat"})): OpenAICompatibleTransport,
    ("anthropic",      "*", frozenset({"chat"})): AnthropicTransport,
    ("bedrock",        "*", frozenset({"chat"})): BedrockTransport,
    ("hf_endpoint",    "*", frozenset({"chat"})): OpenAICompatibleTransport,
    ("hf_self_hosted", "*", frozenset({"chat"})): OpenAICompatibleTransport,
}
# Default (anything not in overrides): LiteLLMTransport
```

For our `openai / gpt-4o / chat` request → resolves to `OpenAICompatibleTransport`.

If you add a new provider tomorrow and don't add an override → it automatically falls
through to `LiteLLMTransport` (the gravity well default). This is why LiteLLM is a
safety net for future providers, not a dependency for current ones.

---

### Step 9 — The Actual LLM Call (`llm_service/providers/*.py`)

The resolved transport is instantiated and called:

```python
transport = registry.resolve("openai", "gpt-4o", {"chat"})
response = await transport.chat(normalised_request, key_payload)
```

Every transport implements the same `BaseLLMProvider` interface (`src/llm_service/providers/base.py`):

```python
class BaseLLMProvider:
    async def chat(self, request: NormalisedLLMRequest, key: TenantKeyPayload) -> ChatResponse: ...
    async def stream(self, request: NormalisedLLMRequest, key: TenantKeyPayload) -> AsyncIterator[StreamEvent]: ...
    async def embed(self, request: NormalisedEmbedRequest, key: TenantKeyPayload) -> EmbedResponse: ...
```

**For `OpenAICompatibleTransport` (`openai_compatible.py`):**
It uses the `openai` Python SDK. The key from `key_payload.data["api_key"]` is passed
directly to the SDK client — it is never stored anywhere globally.

```python
client = AsyncOpenAI(api_key=key_payload.data["api_key"])
raw = await client.chat.completions.create(
    model=request.model,
    messages=request.messages,
    temperature=request.params.temperature,
    max_tokens=request.params.max_tokens,
)
```

The raw OpenAI response is then mapped into the standard `ChatResponse` envelope.

**For `LiteLLMTransport` (`litellm.py`):**
LiteLLM runs in-process (no separate container). Same idea, but using the LiteLLM SDK
which can talk to dozens of providers using the same OpenAI-compatible interface:

```python
raw = await litellm.acompletion(
    model=f"{request.provider}/{request.model}",
    messages=request.messages,
    api_key=key_payload.data["api_key"],
    api_base=key_payload.data.get("api_base"),  # for self-hosted
)
```

The key is injected per-call as a kwarg — `litellm.api_key` is **never set globally**.

**What comes back:** the raw provider response object. The transport normalises this into
a `ChatResponse` with the full envelope: choices, usage block, etc.

---

### Step 10 — Ledger Write (`shared/ledger/`)

After the upstream call completes (whether it succeeded or failed), a ledger row is
written to Postgres. This records everything that happened:

```
tenant_id, provider, model, transport, region,
tokens_in, tokens_out,
our_cost_usd (computed from pricing/models.yaml),
pricing_version,
provider_reported_usage (raw JSON from the response),
latency_ms, status, error_code,
our_cache_hit, guardrail_flags
```

**Two costs are always recorded:**
- `our_cost_usd` — calculated by us using `pricing/models.yaml` rates
- `provider_reported_usage` — the raw usage block from the provider response, stored as JSONB

This is non-negotiable. If the upstream call succeeded but the ledger write failed, the
request still returns success — the ledger error is logged so the row can be
reconstructed.

**Phase 1:** sync `asyncpg` insert directly in the request handler. Simple. No queue.

---

### Step 11 — Guardrails: Output (`shared/guardrails/`)

Same as Step 6 but on the response text. PII redaction, safety check. Phase 1: stub.

---

### Step 12 — Return the Response

The handler builds the final `ChatResponse` and returns it. FastAPI serialises it to JSON.

```json
{
  "id": "req_01H...",
  "object": "chat.completion",
  "tenant_id": "tenant_acme",
  "provider": "openai",
  "model": "gpt-4o",
  "transport": "direct",
  "choices": [
    {
      "index": 0,
      "message": { "role": "assistant", "content": "4" },
      "finish_reason": "stop"
    }
  ],
  "usage": { "input_tokens": 20, "output_tokens": 2, "total_tokens": 22 },
  "cost": { "computed_usd": 0.00004, "pricing_version": 1, "provider_reported_usd": 0.00004 },
  "latency_ms": { "total": 320, "upstream": 315 },
  "cache": { "our_cache_hit": false },
  "guardrails": { "input_flags": [], "output_flags": [], "redactions_applied": [] },
  "policy": { "rate_limit_remaining": 4999 }
}
```

---

## For Streaming (SSE)

When `stream: true` comes in, Steps 1–8 are identical. Step 9 calls `transport.stream()`
instead of `transport.chat()`. That returns an async generator. The API handler wraps it
in a FastAPI `StreamingResponse` with `media_type="text/event-stream"` and yields SSE
events as they arrive:

```
event: token
data: {"index": 0, "delta": "4"}

event: finish
data: { ...full ledger tail... }
```

The ledger write (Step 10) happens after the stream ends, inside the generator's
cleanup.

---

## What to Build and In What Order

The order below lets you test each layer as you go. Each step is buildable independently.

### Batch 1 — App boots, route exists, returns stub

**Goal:** `uvicorn` starts, `POST /v1/chat` returns a hardcoded response.

Files to create:
- `src/shared/config.py` — `Settings` class with Pydantic-settings. Fields for `db_url`,
  `redis_url`, `log_level`. Nothing fancy yet.
- `src/llm_service/schemas/chat.py` — `ChatRequest`, `ChatResponse`. Match the request
  and response shapes in `docs/architecture.md` exactly.
- `src/shared/schemas/envelope.py` — `UsageBlock`, `CostBlock`, `LatencyBlock`,
  `CacheBlock`, `GuardrailsBlock`, `PolicyBlock`. These are the sub-objects that appear
  in every response.
- `src/llm_service/api/rest/chat.py` — `POST /v1/chat` handler that parses `ChatRequest`
  and returns a hardcoded `ChatResponse`. No auth, no LLM call yet.
- `main.py` (at the root or `src/`) — creates the `FastAPI()` app, includes the router,
  runs with `uvicorn`.

**Test:** `curl -X POST http://localhost:8000/v1/chat -d '{"provider":"openai","model":"gpt-4o","messages":[{"role":"user","content":"hi"}]}'`
returns a stub response.

---

### Batch 2 — Database models and migrations

**Goal:** Postgres tables exist. The app can connect.

Files to create:
- `src/shared/db/models.py` — SQLAlchemy ORM models:
  - `Tenant` — `id, name, created_at`
  - `CallingService` — `id, name, bearer_token_hash, allowed_tenant_ids[]`
  - `TenantKey` — `id, tenant_id, provider, key_format, encrypted_payload, created_at`
  - `LedgerEntry` — all the columns listed in `docs/architecture.md` under "Ledger row shape"
  - `Policy` — `tenant_id, max_tokens_per_request, rate_limit_rpm, budget_usd_monthly, ...`
- `src/shared/db/__init__.py` — creates the `async_engine` + `AsyncSessionLocal` using
  `settings.db_url`.
- `deploy/docker/docker-compose.yml` — Postgres + Redis containers for local dev.
- Alembic setup: `alembic init src/shared/db/migrations`, configure `env.py` to use
  async engine + your models.

**Test:** `alembic upgrade head` creates all tables. `docker-compose up` brings up
Postgres and Redis.

---

### Batch 3 — Auth + Secrets

**Goal:** the API rejects requests with bad/missing tokens and resolves the tenant.

Files to create:
- `src/shared/secrets/backend.py` — abstract `SecretBackend` class:
  ```python
  class SecretBackend(ABC):
      async def get_key(self, tenant_id: str, provider: str) -> TenantKeyPayload: ...
      async def set_key(self, tenant_id: str, provider: str, payload: dict) -> None: ...
  ```
  `TenantKeyPayload` is a dataclass with `key_format: str` and `data: dict`.

- `src/shared/secrets/postgres_encrypted.py` — `PostgresEncryptedBackend`:
  - Reads from `tenant_keys` table
  - Decrypts with Fernet using master key from `keyring.get_password("ai-service", "master-key")`
  - Raises `MissingTenantKeyError` if no row found

- `src/shared/secrets/cli.py` — the `uv run llm-service keys` CLI (Click):
  - `keys init` — generates master key, stores in OS keyring
  - `keys set --tenant --provider --key` — encrypts and writes to DB
  - `keys list --tenant` — lists with masked values

- `src/shared/auth.py`:
  - `async def get_tenant(request: Request, db: AsyncSession) -> Tenant` — FastAPI dependency
  - Reads `Authorization: Bearer <token>` header
  - Hashes it, looks up `CallingService` by hash in DB
  - Reads `X-Tenant-Id` header, verifies this service can act on that tenant
  - Returns `Tenant` object or raises `HTTPException(401)` / `HTTPException(403)`

- `src/llm_service/api/deps.py` — collects all FastAPI dependency functions in one place:
  ```python
  async def get_tenant(...): ...   # from shared/auth
  async def get_db(...): ...       # yields AsyncSession
  async def get_secret_backend(...): ...   # yields SecretBackend instance
  ```

**Test:** Wire `get_tenant` into the stub handler. Send a request without a token → 401.
Insert a test `CallingService` row in the DB, send the right token → handler runs.

---

### Batch 4 — Pricing YAML *(prepares Step 10 cost computation)*

**Goal:** we can compute `our_cost_usd` for any model.

Files to create:
- `pricing/models.yaml` — start with just OpenAI models:
  ```yaml
  pricing_version: 1
  models:
    openai/gpt-4o:
      input_rate_per_1k: 0.005
      output_rate_per_1k: 0.015
      cache_write_rate_per_1k: 0.00625
      cache_read_rate_per_1k: 0.00125
      last_updated: "2026-05-11"
      source_url: "https://openai.com/pricing"
  ```

- `src/shared/ledger/pricing.py` — `PricingTable` class:
  - Loads YAML at startup
  - `compute_cost(provider, model, tokens_in, tokens_out, ...) -> float`
  - Raises `UnknownModelError` if model not in YAML (blocks the request — no silent $0 rows)
  - CI check: fails if any model entry is older than `settings.pricing_staleness_days`

**Test:** unit test that `compute_cost("openai", "gpt-4o", 100, 50)` returns the right
number given the YAML rates.

---

### Batch 5 — Load Tenant BYOK Key + Normalise Request (Steps 3 & 4)

**Goal:** before any request reaches an LLM, we can load the right tenant key and
produce a clean provider-agnostic request object.

Files to create:
- `src/llm_service/normaliser.py` — `normalise(request: ChatRequest) -> NormalisedLLMRequest`:
  - Validates `provider` and `model` are present (missing → `400`)
  - For Phase 1 this is mostly a pass-through; cache hint injection lands in Phase 2
  ```python
  def normalise(request: ChatRequest) -> NormalisedLLMRequest:
      if not request.provider or not request.model:
          raise HTTPException(status_code=400, detail="provider and model are required")
      return NormalisedLLMRequest(
          provider=request.provider,
          model=request.model,
          messages=request.messages,
          tools=request.tools,
          params=request.params,
          stream=request.stream,
          metadata=request.metadata,
      )
  ```

- `src/llm_service/api/deps.py` — add `get_secret_backend` dependency:
  ```python
  async def get_secret_backend() -> SecretBackend:
      return PostgresEncryptedBackend(...)
  ```

Wire into `chat.py` (Steps 3 & 4):
```python
normalised = normalise(body)                                  # Step 4
key = await secret_backend.get_key(tenant.id, body.provider) # Step 3
# MissingTenantKeyError → 422 missing_tenant_key
```

**Test:**
- Call `normalise(...)` with a missing `provider` field → `400`.
- Call `secret_backend.get_key(...)` for a tenant that has no key in the DB → `MissingTenantKeyError`.
- With a valid key inserted via `keys set`, `get_key` returns a `TenantKeyPayload` with the decrypted API key.

---

### Batch 6 — Policy Check (Step 5)

**Goal:** requests that exceed a tenant's configured limits are rejected before any
upstream call is made.

Files to create:
- `src/shared/policy/checker.py` — `PolicyChecker`:
  ```python
  class PolicyChecker:
      async def check(self, tenant_id: str, request: NormalisedLLMRequest) -> None:
          # reads Policy row for tenant from DB
          # raises PolicyExceededError on: model not in allowlist,
          #   max_tokens exceeded, budget exceeded, rate limit exceeded
  ```
  Raises `PolicyExceededError` → handler returns `429 policy_exceeded`.

- `src/shared/policy/__init__.py` — exports `PolicyChecker`, `PolicyExceededError`.

- `src/llm_service/api/deps.py` — add `get_policy_checker` dependency.

Wire into `chat.py` (Step 5):
```python
await policy_checker.check(tenant.id, normalised)  # Step 5
```

**Phase 1 scope:** reads directly from the `policies` DB table. No Redis counters yet
(rate-limit sliding window lands in Phase 2 with the cache layer).

**Test:**
- Insert a `Policy` row with `model_allowlist=["gpt-4o-mini"]`. Send a request for
  `gpt-4o` → `429`. Send for `gpt-4o-mini` → passes through.
- Set `max_tokens_per_request=10`, send `params.max_tokens=100` → `429`.

---

### Batch 7 — Guardrails Input Stub (Step 6)

**Goal:** the pipeline has a real hook point for input guardrails; Phase 1 passes
through unchanged. Presidio + Llama-Guard wire up in Phase 3.

Files to create:
- `src/shared/guardrails/base.py` — `GuardrailsChecker` ABC:
  ```python
  class GuardrailsChecker(ABC):
      async def check_input(self, messages: list[dict]) -> GuardrailsResult: ...
      async def check_output(self, content: str) -> GuardrailsResult: ...
  ```
  `GuardrailsResult` carries `flags: list[str]` and `redactions: list[str]`.

- `src/shared/guardrails/stub.py` — `StubGuardrails(GuardrailsChecker)`:
  Both methods return an empty `GuardrailsResult` with no flags or redactions.

- `src/llm_service/api/deps.py` — add `get_guardrails` dependency (returns `StubGuardrails`).

Wire into `chat.py` (Step 6):
```python
input_result = await guardrails.check_input(normalised.messages)  # Step 6
# if input_result.blocked: raise HTTPException(400, "guardrails_blocked")
```

**Test:** with the stub, any input passes through with empty flags. Verify the
`guardrails.input_flags` field in the response is `[]`.

---

### Batch 8 — Cache Lookup Stub (Step 7)

**Goal:** the pipeline has a real cache hook; Phase 1 always misses. Redis wires up
in Phase 2.

Files to create:
- `src/llm_service/cache/base.py` — `CacheBackend` ABC:
  ```python
  class CacheBackend(ABC):
      async def get(self, key: str) -> ChatResponse | None: ...
      async def set(self, key: str, response: ChatResponse, ttl_s: int) -> None: ...
  ```

- `src/llm_service/cache/stub.py` — `StubCache(CacheBackend)`:
  `get` always returns `None`; `set` is a no-op.

- `src/llm_service/cache/keys.py` — `make_cache_key(tenant_id, provider, model, messages, params) -> str`:
  SHA-256 of the serialised inputs. Implement now so Phase 2 only needs to swap the backend.

- `src/llm_service/api/deps.py` — add `get_cache` dependency (returns `StubCache`).

Wire into `chat.py` (Step 7):
```python
cache_key = make_cache_key(tenant.id, normalised)
cached = await cache.get(cache_key)          # Step 7
if cached:
    return cached                            # skip Steps 8–11
```

**Test:** with the stub, every request is a miss (`cache.our_cache_hit=False` in
the response). Confirm the `cache` block in the response envelope has `our_cache_hit: false`.

---

### Batch 9 — Provider Layer + Upstream Call (Step 8)

**Goal:** the handler actually calls LiteLLM and gets a real LLM response.

Files to create:
- `src/llm_service/providers/base.py` — `BaseLLMProvider` ABC:
  ```python
  class BaseLLMProvider(ABC):
      async def chat(self, request: NormalisedLLMRequest, key: TenantKeyPayload) -> ChatResponse: ...
      async def stream(self, request: NormalisedLLMRequest, key: TenantKeyPayload) -> AsyncIterator[StreamEvent]: ...
      async def embed(self, request: NormalisedEmbedRequest, key: TenantKeyPayload) -> EmbedResponse: ...
  ```

- `src/llm_service/providers/litellm.py` — `LiteLLMTransport(BaseLLMProvider)`:
  - `async def chat(...)`:
    - Calls `litellm.acompletion(model=..., messages=..., api_key=key.data["api_key"])`
    - Maps raw LiteLLM response to `ChatResponse` envelope
    - Reads `usage.prompt_tokens`, `usage.completion_tokens` from response
  - `async def stream(...)`:
    - Calls `litellm.acompletion(..., stream=True)` → async generator
    - Yields `StreamEvent` objects: `token`, `tool_use`, `finish`

- `src/llm_service/providers/registry.py` — `resolve(provider, model, features) -> BaseLLMProvider`:
  - Start with everything going to `LiteLLMTransport`
  - Add the overrides table (OpenAI-compatible, Anthropic, Bedrock) after LiteLLM is verified

Wire into `chat.py` (Step 8):
```python
transport = registry.resolve(normalised.provider, normalised.model, {"chat"})  # Step 8
response = await transport.chat(normalised, key)
```

**Test:** hardcode a tenant key in a test, call `LiteLLMTransport.chat(...)` directly
with a real OpenAI key → you get a real response back.

---

### Batch 10 — Guardrails Output Stub (Step 9)

**Goal:** same hook pattern as Batch 7 but applied to the LLM's response text before
returning it to the caller.

No new files needed — extend `StubGuardrails` from Batch 7 (already has `check_output`).

Wire into `chat.py` (Step 9):
```python
output_result = await guardrails.check_output(response.choices[0].message.content)  # Step 9
# merge output_result.flags into response.guardrails.output_flags
# apply any redactions to response.choices[0].message.content
```

**Test:** with the stub, the response content passes through unchanged and
`guardrails.output_flags` is `[]`.

---

### Batch 11 — Ledger Write (Step 10)

**Goal:** after every upstream call a `ledger_entries` row is written with both cost
numbers. This is non-negotiable — both must be present before the request is complete.

Files to create:
- `src/shared/ledger/writer.py` — `LedgerWriter`:
  ```python
  class LedgerWriter:
      async def write(self, entry: LedgerEntry) -> None:
          # asyncpg INSERT INTO ledger_entries ... ON CONFLICT (request_id) DO NOTHING
  ```

- `src/shared/ledger/__init__.py` — exports `LedgerWriter`, `PricingTable`.

Wire into `chat.py` (Step 10):
```python
our_cost = pricing_table.compute_cost(
    normalised.provider, normalised.model,
    response.usage.input_tokens, response.usage.output_tokens,
)
await ledger.write(LedgerEntry(
    tenant_id=tenant.id,
    provider=normalised.provider,
    model=normalised.model,
    transport="litellm",
    tokens_in=response.usage.input_tokens,
    tokens_out=response.usage.output_tokens,
    our_cost_usd=our_cost,
    pricing_version=pricing_table.version,
    provider_reported_usage=response.usage.model_dump(),
    latency_ms=elapsed_ms,
    status="success",
    guardrail_flags=input_result.flags + output_result.flags,
))  # Step 10
```

If the ledger write fails, log the error but still return the response — the row can be
reconstructed from logs; losing the caller's response is worse.

**Test:** send a real request, check that a row appeared in `ledger_entries` with both
`our_cost_usd` and `provider_reported_usage` populated.

---

### Batch 12 — Wire the Full Pipeline End to End

**Goal:** `chat.py` runs all steps in order with no stubs left in the critical path.
Steps 6, 7, 9 still use stub implementations but the hook points are real.

Final `chat.py` handler sequence:
1. `get_tenant` — real (Batch 3)
2. `normalise` — real (Batch 5) — Step 4
3. `get_key` — real (Batch 5) — Step 3
4. `policy_checker.check` — real (Batch 6) — Step 5
5. `guardrails.check_input` — stub (Batch 7) — Step 6
6. `cache.get` — stub (Batch 8) — Step 7
7. `registry.resolve` + `transport.chat` — real (Batch 9) — Step 8
8. `guardrails.check_output` — stub (Batch 10) — Step 9
9. `ledger.write` — real (Batch 11) — Step 10
10. return response

**Test (the main Phase 1 test):**
```bash
# 1. Insert a calling service and tenant key in local DB
# 2. Run the server
uv run uvicorn main:app --reload

# 3. Send a real request
curl -X POST http://localhost:8000/v1/chat \
  -H "Authorization: Bearer your-dev-token" \
  -H "X-Tenant-Id: tenant_dev" \
  -H "Content-Type: application/json" \
  -d '{
    "provider": "openai",
    "model": "gpt-4o-mini",
    "messages": [{"role": "user", "content": "What is 2+2?"}],
    "params": {"max_tokens": 50}
  }'

# 4. Verify response has full envelope (cost, usage, latency, guardrails, policy, cache)
# 5. Verify ledger_entries row exists in DB with both cost fields populated
```

---

### Batch 13 — SSE Streaming

**Goal:** `POST /v1/chat/stream` returns a proper SSE stream running the same full
pipeline (Steps 3–10) as the non-streaming path.

Add to `chat.py`:
```python
from fastapi.responses import StreamingResponse

@router.post("/v1/chat/stream")
async def chat_stream(body: ChatRequest, ...):
    normalised = normalise(body)
    key = await secret_backend.get_key(tenant.id, body.provider)
    await policy_checker.check(tenant.id, normalised)
    input_result = await guardrails.check_input(normalised.messages)
    cached = await cache.get(make_cache_key(tenant.id, normalised))
    if cached:
        # re-emit cached response as a single finish event
        ...
    transport = registry.resolve(normalised.provider, normalised.model, {"stream"})

    async def event_generator():
        async for event in transport.stream(normalised, key):
            yield f"event: {event.type}\ndata: {event.data.model_dump_json()}\n\n"
        # ledger write happens here after stream ends

    return StreamingResponse(event_generator(), media_type="text/event-stream")
```

The `stream` method in `LiteLLMTransport`:
```python
async def stream(self, request, key):
    async for chunk in await litellm.acompletion(..., stream=True):
        delta = chunk.choices[0].delta.content or ""
        if delta:
            yield StreamEvent(type="token", data=TokenData(delta=delta))
    yield StreamEvent(type="finish", data=finish_data)
```

**Test:**
```bash
curl -N -X POST http://localhost:8000/v1/chat/stream \
  -H "Authorization: Bearer your-dev-token" \
  -H "X-Tenant-Id: tenant_dev" \
  -H "Accept: text/event-stream" \
  -d '{"provider":"openai","model":"gpt-4o-mini","messages":[{"role":"user","content":"Count to 5"}],"stream":true}'
```

Tokens should arrive one by one, then a `finish` event with the full metadata.

---

## Summary — What "Done" Looks Like for Phase 1

You can:

1. Start the server with `uv run uvicorn main:app --reload`
2. Send a `POST /v1/chat` with a valid caller token + tenant ID
3. Get a real LLM response back with the full envelope (choices + usage + cost + latency)
4. Find a row in `ledger_entries` with both `our_cost_usd` and `provider_reported_usage`
5. A bad/missing token returns `401` before touching any LLM
6. A missing tenant key returns `422 missing_tenant_key`
7. A policy violation (bad model, over-budget, over-limit) returns `429 policy_exceeded`
8. Same request via `/v1/chat/stream` streams SSE tokens with a `finish` tail event

Guardrails (Steps 6 & 9) and Redis cache (Step 7) have real hook points but use stub
implementations — Presidio/Llama-Guard wire up in Phase 3, Redis in Phase 2.

---

## File Creation Checklist (Phase 1)

```
src/shared/
  config.py                        ← Settings (db_url, redis_url, log_level, ...)
  auth.py                          ← get_tenant dependency
  db/
    __init__.py                    ← engine + AsyncSessionLocal
    models.py                      ← Tenant, CallingService, TenantKey, LedgerEntry, Policy
    migrations/                    ← Alembic setup
  secrets/
    backend.py                     ← SecretBackend ABC + TenantKeyPayload
    postgres_encrypted.py          ← PostgresEncryptedBackend
    cli.py                         ← keys init / set / list CLI
  policy/
    checker.py                     ← PolicyChecker, PolicyExceededError
    __init__.py
  guardrails/
    base.py                        ← GuardrailsChecker ABC + GuardrailsResult
    stub.py                        ← StubGuardrails (pass-through)
    __init__.py
  ledger/
    pricing.py                     ← PricingTable (loads YAML, computes cost)
    writer.py                      ← LedgerWriter (asyncpg insert)
    __init__.py
  schemas/
    envelope.py                    ← UsageBlock, CostBlock, LatencyBlock, CacheBlock, ...

src/llm_service/
  schemas/
    chat.py                        ← ChatRequest, ChatResponse, NormalisedLLMRequest
    embed.py                       ← EmbedRequest, EmbedResponse
  normaliser.py                    ← normalise(ChatRequest) → NormalisedLLMRequest
  cache/
    base.py                        ← CacheBackend ABC
    stub.py                        ← StubCache (always miss)
    keys.py                        ← make_cache_key(...)
    __init__.py
  providers/
    base.py                        ← BaseLLMProvider ABC
    registry.py                    ← resolve(provider, model, features) → transport
    litellm.py                     ← LiteLLMTransport
    openai_compatible.py           ← OpenAICompatibleTransport
    anthropic.py                   ← AnthropicTransport
    bedrock.py                     ← BedrockTransport
  api/
    deps.py                        ← get_tenant, get_db, get_secret_backend,
                                      get_policy_checker, get_guardrails,
                                      get_cache, get_ledger
    rest/
      chat.py                      ← POST /v1/chat (non-stream + SSE stream)
      embed.py                     ← POST /v1/embeddings
      models.py                    ← GET /v1/models

main.py                            ← FastAPI app, router includes, uvicorn entry

pricing/
  models.yaml                      ← per-model rates

deploy/docker/
  docker-compose.yml               ← postgres + redis
```

---

## One Thing to Avoid

Never set `litellm.api_key = "..."` or `openai.api_key = "..."` at the module level.
These are global and would mean tenant A's key could be used for tenant B's request if
timing is bad. Always pass the key as a per-call argument to the client or SDK function.
This is the BYOK invariant and it must be true in every transport file.
