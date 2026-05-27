# Adding a Provider

This document explains how to wire a new LLM provider into the gateway. There are
two paths depending on how the provider authenticates and whether LiteLLM already
supports it cleanly.

---

## Which path to take

```
Does LiteLLM support this provider?
        │
        ├─ Yes ──────────────────────────────────────────────────────────┐
        │                                                                │
        │  Does the provider use a standard API key (bearer token)?     │
        │        │                                                       │
        │        ├─ Yes ──→ Path A: LiteLLM gravity well                │
        │        │          (add a key in the DB, nothing else)          │
        │        │                                                       │
        │        └─ No  ──→ Does it use AWS IAM or a custom endpoint?   │
        │                         │                                       │
        │                         └─ Yes ──→ Path A still               │
        │                            (existing key_formats cover it)    │
        │                                                                │
        └─ No / LiteLLM has known gaps ──→ Path B: Direct adapter      │
                                           (new file in providers/)     │
```

**Path A** is almost always the right choice. LiteLLM supports over 100 providers.
If a provider is in the [LiteLLM docs](https://docs.litellm.ai/docs/providers), use
Path A. No code changes are required for the gateway itself.

**Path B** is for providers where LiteLLM has a gap — incorrect streaming behaviour,
unsupported features, or a non-standard authentication flow — or where you need
precise, low-level control over the request (e.g. Bedrock's boto3 response shapes,
or a provider with a custom pagination protocol).

---

## Path A — LiteLLM gravity well

The gateway already handles any provider that LiteLLM supports. The only step is
storing the tenant's credential in the database.

### Step 1: Decide the credential format

Look at how the provider authenticates and pick one of the three existing
`key_format` values:

| `key_format` | `data` keys required | When to use |
|---|---|---|
| `api_key` | `api_key` | Any provider that takes a bearer token (OpenAI, Anthropic, Groq, Gemini, Mistral, Cohere, Together AI, …) |
| `api_key` + `api_base` | `api_key`, `api_base` | Self-hosted or dedicated endpoint with a bearer token (HuggingFace Endpoints, vLLM, Ollama, LM Studio) |
| `aws_credentials` | `access_key_id`, `secret_access_key`, `region` | AWS Bedrock (IAM auth) |
| `endpoint_pair` | `endpoint_url`, `token` | Custom endpoint with a separate token field |

If none of these fit — for example, a provider that uses OAuth 2.0 client credentials —
see "When a new key_format is needed" at the end of this document.

### Step 2: Store the tenant's key

Use the secrets CLI to encrypt and persist the key. The `provider` value here
becomes the `provider` field the calling service sends in every request:

```bash
# Groq (api_key)
uv run llm-service keys set \
    --tenant=tenant_acme \
    --provider=groq \
    --format=api_key \
    --data='{"api_key": "gsk_..."}'

# Google Gemini (api_key)
uv run llm-service keys set \
    --tenant=tenant_acme \
    --provider=gemini \
    --format=api_key \
    --data='{"api_key": "AIzaSy..."}'

# Self-hosted Ollama (api_key + api_base)
uv run llm-service keys set \
    --tenant=tenant_acme \
    --provider=ollama \
    --format=api_key \
    --data='{"api_key": "ollama", "api_base": "http://gpu-box:11434"}'
```

### Step 3: Send a request

The calling service sends its normal request body with the new `provider` value.
The gateway resolves to `LiteLLMTransport`, builds the model string, injects the
credential, and calls LiteLLM:

```http
POST /v1/chat
Authorization: Bearer svc_token
X-Tenant-Id: tenant_acme
Content-Type: application/json

{
  "provider": "groq",
  "model": "llama-3.1-70b-versatile",
  "messages": [{"role": "user", "content": "What is 2 + 2?"}],
  "params": {"max_tokens": 50}
}
```

LiteLLMTransport builds `"groq/llama-3.1-70b-versatile"` and passes
`api_key="gsk_..."` to `litellm.acompletion()`. No further code change is needed.

### Step 4: Check the model string (only if the name differs)

Most providers use their own name as the LiteLLM prefix (`groq`, `gemini`, `mistral`,
`together_ai`, `cohere`, etc.). If the provider's LiteLLM prefix differs from the
name you want calling services to use, add a remap entry in `litellm.py`:

```python
# src/llm_service/providers/litellm.py

_PROVIDER_REMAP: dict[str, str] = {
    "custom_endpoint": "openai",   # already here
    "my_provider":    "litellm_prefix",  # add here if needed
}
```

That is the only code change for Path A.

---

## Path B — Direct adapter

A direct adapter bypasses LiteLLM and calls the provider's SDK or HTTP API
directly. Use this when LiteLLM has a documented gap on your target route, or when
you need to own the full request/response cycle (exact streaming behaviour, provider
error classification, specific retry logic).

There are four steps.

### Step 1: Create the adapter file

Create `src/llm_service/providers/<name>.py`. The file must implement `BaseLLMProvider`:

```python
# src/llm_service/providers/my_provider.py
from __future__ import annotations

from collections.abc import AsyncIterator

from src.llm_service.providers.base import BaseLLMProvider, StreamEvent, TransportFinishData
from src.llm_service.schemas.chat import (
    CacheBlock, ChatResponse, Choice, ChoiceMessage, NormalisedLLMRequest, TokenData, UsageBlock,
)
from src.shared.db.enums import Transport
from src.shared.schemas.envelope import CostBlock, GuardrailsBlock, LatencyBlock, PolicyBlock
from src.shared.secrets.backend import TenantKeyPayload


class MyProviderTransport(BaseLLMProvider):

    async def chat(
        self, request: NormalisedLLMRequest, key: TenantKeyPayload
    ) -> ChatResponse:
        # 1. Extract the credential from key.data (key.key_format tells you the shape)
        api_key = key.data["api_key"]

        # 2. Call the provider SDK / HTTP API directly
        raw = await my_provider_sdk.complete(
            model=request.model,
            messages=self._serialize_messages(request.messages),
            api_key=api_key,          # always per-call, never global
            temperature=request.params.temperature if request.params else None,
            max_tokens=request.params.max_tokens if request.params else None,
        )

        # 3. Map the raw response to our schema
        usage = UsageBlock(
            input_tokens=raw.usage.input,
            output_tokens=raw.usage.output,
            total_tokens=raw.usage.total,
        )
        choices = [
            Choice(
                index=0,
                message=ChoiceMessage(role="assistant", content=raw.text),
                finish_reason=raw.stop_reason,
            )
        ]

        # 4. Return a partially-filled ChatResponse (handler fills id, tenant_id, latency, cost)
        return ChatResponse(
            id="",
            created=int(time.time()),
            tenant_id="",
            provider=request.provider,
            model=request.model,
            transport=Transport.DIRECT,
            choices=choices,
            usage=usage,
            cost=CostBlock(),
            latency_ms=LatencyBlock(),
            cache=CacheBlock(our_cache_hit=False),
            guardrails=GuardrailsBlock(),
            policy=PolicyBlock(),
            provider_raw=raw.to_dict(),
        )

    async def stream(
        self, request: NormalisedLLMRequest, key: TenantKeyPayload
    ) -> AsyncIterator[StreamEvent]:
        api_key = key.data["api_key"]

        async for chunk in my_provider_sdk.stream(model=request.model, ..., api_key=api_key):
            if chunk.delta:
                yield StreamEvent(type="token", data=TokenData(index=0, delta=chunk.delta))

        yield StreamEvent(
            type="finish",
            data=TransportFinishData(finish_reason="stop", usage=UsageBlock(...)),
        )
```

Three invariants every direct adapter must respect:

- **Never set the provider SDK's global key state** (`sdk.api_key = ...`). Always
  pass keys as per-call arguments. Under async concurrency, global state is shared
  across all concurrent requests on the worker — setting it once poisons other
  tenants' calls.

- **Always return a `ChatResponse` with stub values for handler-filled fields.**
  The handler fills `id`, `tenant_id`, `latency_ms`, `cost`, and `guardrails` after
  the transport returns. Return empty strings and zero-value blocks for those fields.

- **Yield exactly one `finish` event at the end of `stream()`.** The handler waits
  for this event to collect usage data before emitting the SSE `finish` event to the
  caller. Emitting it early (before all tokens) or not emitting it at all will break
  the streaming response.

### Step 2: Handle prompt caching if the provider supports it

Anthropic and Bedrock-Anthropic require explicit `cache_control` markers on content
blocks. If your adapter targets one of these providers, inject the marker when the
message has a `cache: "ephemeral"` hint:

```python
def _serialize_messages(self, messages: list[Any]) -> list[dict]:
    result = []
    for msg in messages:
        d = msg.model_dump(exclude={"cache"}, exclude_none=True)
        if msg.cache == "ephemeral" and isinstance(msg.content, str):
            d["content"] = [
                {
                    "type": "text",
                    "text": msg.content,
                    "cache_control": {"type": "ephemeral"},
                }
            ]
        result.append(d)
    return result
```

For providers that do not support prompt caching, just strip the `cache` field:

```python
def _serialize_messages(self, messages: list[Any]) -> list[dict]:
    return [m.model_dump(exclude={"cache"}, exclude_none=True) for m in messages]
```

### Step 3: Add the override to the registry

Open `src/llm_service/providers/registry.py` and import your adapter, then add it
to `_OVERRIDE`:

```python
# src/llm_service/providers/registry.py

from src.llm_service.providers.my_provider import MyProviderTransport

_OVERRIDE: dict[tuple[str, str, frozenset], type[BaseLLMProvider]] = {
    ("my_provider", "*", frozenset({"chat"})):   MyProviderTransport,
    ("my_provider", "*", frozenset({"stream"})): MyProviderTransport,
}
```

The wildcard `"*"` for the model field means all models under this provider route to
the adapter. If only specific models need the direct adapter and others should fall
through to LiteLLM, use an exact model string instead of `"*"`.

### Step 4: Store the tenant's key and test

Same as Path A Step 2 — store the credential via the CLI with the appropriate
`key_format`, then send a real request through the server to verify end to end.

---

## When a new key_format is needed

The three existing formats — `api_key`, `aws_credentials`, `endpoint_pair` — cover
the vast majority of providers. If a provider uses a fundamentally different
authentication shape (e.g. OAuth tokens with expiry, client certificate, or multi-key
setups), a new `key_format` value must be added. This is a two-file change:

**1. Add the enum value** in `src/shared/db/enums.py`:

```python
class KeyFormat(str, Enum):
    API_KEY          = "api_key"
    AWS_CREDENTIALS  = "aws_credentials"
    ENDPOINT_PAIR    = "endpoint_pair"
    MY_FORMAT        = "my_format"        # add here
```

**2. Handle it in the transport's `_credential_kwargs`** (or in your new adapter):

```python
if key.key_format == KeyFormat.MY_FORMAT:
    return {
        "client_id":     key.data["client_id"],
        "client_secret": key.data["client_secret"],
    }
```

No migration is needed for existing rows — existing `key_format` values in the
database are unaffected.

---

## Full worked example: adding Together AI

Together AI is an API-key provider that LiteLLM supports natively. Path A applies.

**Step 1: credential format** — `api_key` (Together AI uses a bearer token).

**Step 2: store the key**:

```bash
uv run llm-service keys set \
    --tenant=tenant_acme \
    --provider=together_ai \
    --format=api_key \
    --data='{"api_key": "...together-api-key..."}'
```

**Step 3: no code change needed.** `LiteLLMTransport._model_string("together_ai", "meta-llama/Llama-3-70b-chat-hf")`
returns `"together_ai/meta-llama/Llama-3-70b-chat-hf"`, which is the correct LiteLLM
model string for Together AI.

**Step 4: test**:

```bash
curl -X POST http://localhost:8000/v1/chat \
  -H "Authorization: Bearer svc_token" \
  -H "X-Tenant-Id: tenant_acme" \
  -H "Content-Type: application/json" \
  -d '{
    "provider": "together_ai",
    "model": "meta-llama/Llama-3-70b-chat-hf",
    "messages": [{"role": "user", "content": "What is 2 + 2?"}]
  }'
```

No files were changed. The response has `"transport": "litellm"` confirming it
routed through LiteLLMTransport.

---

## Checklist before opening a PR

- [ ] No SDK global key state set anywhere in the adapter (`sdk.api_key = ...` is banned)
- [ ] `chat()` returns a `ChatResponse` with empty stubs for `id`, `tenant_id`,
  `latency_ms`, `cost`, `guardrails`, `policy`
- [ ] `stream()` yields exactly one `finish` event as its last event
- [ ] `_serialize_messages()` strips the `cache` field from every message dict
- [ ] Registry entry added for both `{"chat"}` and `{"stream"}` feature sets
- [ ] `key_format` matches what the secrets CLI stores (or a new enum value is added)
- [ ] End-to-end test with a real upstream call confirmed locally

---

See [Provider Layer](providers.md) for how `BaseLLMProvider`, the registry, and
`LiteLLMTransport` work in detail.

See [Keys & Secrets CLI](keys-cli.md) for storing and rotating BYOK credentials.
