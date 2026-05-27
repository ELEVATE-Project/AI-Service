# Architecture

`llm-service` is a multi-provider LLM gateway. Calling services send a single normalised request and get a consistent response — the gateway handles provider routing, auth, policy, caching, guardrails, and cost recording.

---

## Request flow

```
  Calling Services
  ┌─────────────┐   ┌─────────────┐   ┌──────────────────┐
  │  SAATHI bot │   │  Mitra Bot  │   │ Any Future Svc   │
  └──────┬──────┘   └──────┬──────┘   └────────┬─────────┘
         │                 │                    │
         └─────────────────┴────────────────────┘
                           │
               Bearer token + X-Tenant-Id + JSON body
                           │
                           ▼
  ┌────────────────────────────────────────────────────┐
  │              llm-service  (FastAPI + uvicorn)      │
  │                                                    │
  │  ┌──────────────────────────────────────────────┐  │
  │  │                  API Layer                   │  │
  │  │   POST /v1/chat · POST /v1/chat/stream       │  │
  │  │          (WebSocket + MCP planned)           │  │
  │  └───────────────────┬──────────────────────────┘  │
  │                      │                             │
  │  ┌───────────────────▼──────────────────────────┐  │
  │  │           Auth + Tenant Resolver             │  │
  │  │  SHA-256(Bearer token) → calling_services    │  │
  │  │  X-Tenant-Id validation                      │  │◄──── auth lookup ────► Postgres
  │  └───────────────────┬──────────────────────────┘  │
  │                      │                             │
  │  ┌───────────────────▼──────────────────────────┐  │
  │  │            Request Normaliser                │  │
  │  │  ChatRequest → NormalisedLLMRequest          │  │
  │  │  Merge request fields · inject cache hints   │  │
  │  └───────────────────┬──────────────────────────┘  │
  │                      │                             │
  │  ┌───────────────────▼──────────────────────────┐  │
  │  │              Policy Engine                   │  │
  │  │  rate limit · monthly budget                 │  │
  │  │  model allow/deny · size cap                 │  │
  │  └───────────────────┬──────────────────────────┘  │
  │                      │                             │
  │  ┌───────────────────▼──────────────────────────┐  │
  │  │            Guardrails  (input)               │  │
  │  │  Presidio PII redact per-chunk               │  │
  │  │  Llama-Guard safety check at completion      │  │
  │  └───────────────────┬──────────────────────────┘  │
  │                      │                             │
  │  ┌───────────────────▼──────────────────────────┐  │
  │  │            Exact-Match Cache                 │  │
  │  │  Redis SETNX single-flight lock              │  │◄──── SETNX / GET / SET ──► Redis
  │  │  SHA-256 key per tenant                      │  │
  │  └───────────────────┬──────────────────────────┘  │
  │                      │  (cache miss)               │
  │  ┌───────────────────▼──────────────────────────┐  │
  │  │             Routing Registry                 │  │◄──── load BYOK key ──► Secret
  │  │  (provider, model, feature) → transport      │  │                        Backends
  │  │  override or LiteLLM default                 │  │
  │  └──────────┬────────────────────┬──────────────┘  │
  │             │                    │                  │
  │  ┌──────────▼──────┐  ┌──────────▼─────────────┐   │
  │  │ LiteLLMTransport│  │    Direct Adapters     │   │
  │  │  default        │  │  Anthropic · Bedrock   │   │──────────────────────────────────►
  │  │  in-process SDK │  │  (boto3)               │   │                       Upstream
  │  └──────────┬──────┘  └──────────┬─────────────┘   │                    LLM Providers
  │             └────────────────────┘                  │
  │                      │                             │
  │  ┌───────────────────▼──────────────────────────┐  │
  │  │              Usage Ledger                    │  │
  │  │  YAML-computed cost + provider-reported JSONB│  │──── write ledger row ──► Postgres
  │  │  asyncpg insert                              │  │
  │  └───────────────────┬──────────────────────────┘  │
  │                      │                             │
  │  ┌───────────────────▼──────────────────────────┐  │
  │  │         Langfuse self-hosted · structlog      │  │
  │  └──────────────────────────────────────────────┘  │
  │                                                    │
  └────────────────────────────────────────────────────┘
```

---

## Upstream LLM providers

| Provider | Integration | Notes |
|----------|-------------|-------|
| OpenAI | LiteLLM SDK | `api.openai.com` |
| Anthropic | LiteLLM SDK (chat/stream) · direct SDK (batch) | `api.anthropic.com` |
| AWS Bedrock | LiteLLM SDK (chat/stream) · direct boto3 (batch) | Anthropic + Llama models |
| HuggingFace | LiteLLM SDK | Endpoints / TGI / vLLM |
| Future providers | LiteLLM SDK (automatic) | Add a registry entry; LiteLLM handles the rest |

---

## Data stores

```
  ┌─────────────────────────────┐   ┌─────────────────────────────┐
  │          Postgres           │   │            Redis             │
  │                             │   │                             │
  │  tenant_keys                │   │  exact-match response cache  │
  │  calling_services           │   │  SETNX stampede lock         │
  │  ledger_entries             │   │                             │
  │  policies                   │   └─────────────────────────────┘
  │  batch_jobs                 │
  │                             │
  └─────────────────────────────┘
```

---

## Secret backends (pluggable)

The secret backend is swapped via config — application code is identical across all three.

```
  ┌────────────────────────────────────────────┐
  │          Secret Backends (pluggable)       │
  │                                            │
  │  PostgresEncryptedBackend                  │
  │    Fernet encryption · OS Keyring          │  ← local dev + on-prem
  │    master key never touches the database   │
  │                                            │
  │  VaultBackend                              │
  │    HashiCorp Vault                         │  ← self-hosted / govt
  │                                            │
  │  AWSSecretsManagerBackend                  │
  │    AWS Secrets Manager                     │  ← cloud deployments
  │                                            │
  └────────────────────────────────────────────┘
```

---

## Transport routing

The routing registry maps `(provider, model, feature)` to a transport. Anything without an explicit override falls through to `LiteLLMTransport`.

```
  (provider, model, feature)
          │
          ▼
  explicit override?
    ├─ yes → AnthropicTransport  (anthropic, *, batch)
    │         BedrockTransport   (bedrock,   *, batch)
    │
    └─ no  → LiteLLMTransport   (everything else: chat, stream, embed)
```

---

## Voice module (planned)

Shares the same auth, ledger, and guardrails infrastructure as the LLM module.

```
  ┌──────────────────────────────────────────────────────┐
  │                  Voice Module                        │
  │                                                      │
  │  POST /v1/voice/transcribe                           │
  │  POST /v1/voice/synthesize                           │    ┌─────────────────┐
  │  POST /v1/voice/translate       ───────────────────► │    │  Bhashini        │
  │  POST /v1/voice/transliterate                        │    │  Sarvam          │
  │                                                      │    │  Google STT/TTS  │
  │  same auth + ledger + guardrails infra               │    └─────────────────┘
  └──────────────────────────────────────────────────────┘
```

---

## Pipeline summary

| Step | Component | Failure response |
|------|-----------|-----------------|
| 1 | API Layer — parse + validate request | `422` on malformed body |
| 2 | Auth + Tenant Resolver | `401` bad token · `403` tenant not authorised |
| 3 | Request Normaliser | `400` missing provider or model |
| 4 | Policy Engine | `429 policy_exceeded` |
| 5 | Guardrails — input | `400 guardrails_blocked` |
| 6 | Exact-Match Cache | cache hit → return immediately |
| 7 | Routing Registry + BYOK key load | `422 missing_tenant_key` |
| 8 | Transport (LiteLLM or direct) | `502` / `504` upstream errors |
| 9 | Usage Ledger write | non-fatal — response still returned on failure |
| 10 | Guardrails — output | `400 guardrails_blocked` |
| 11 | Cache write + Langfuse trace | non-fatal |