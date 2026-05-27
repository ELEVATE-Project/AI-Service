# Provider Layer

The provider layer is the gateway's interface to every upstream LLM. It abstracts the differences between providers — authentication shapes, model string formats, streaming protocols — into a single interface that the rest of the pipeline never needs to think about.

---

## Why the provider layer exists

Every LLM provider has its own SDK, its own credential format, and its own streaming protocol. Without abstraction, every new provider would require changes across the handler, cache, ledger, and guardrails. Instead, the pipeline calls one interface — `BaseLLMProvider` — and the provider layer handles everything underneath.

The handler only ever sees:

```python
transport = registry.resolve(provider, model, feature)
response  = await transport.chat(normalised_request, tenant_key)
```

Nothing in `chat.py`, `cache/`, `guardrails/`, or `ledger/` knows which provider is underneath.

---

## The interface

`src/llm_service/providers/base.py` defines the contract every transport must implement:

```python
class BaseLLMProvider(ABC):
    async def chat(self, request: NormalisedLLMRequest, key: TenantKeyPayload) -> ChatResponse: ...
    async def stream(self, request: NormalisedLLMRequest, key: TenantKeyPayload) -> AsyncIterator[StreamEvent]: ...
    async def batch_submit(self, jobs: list[BatchJob], key: TenantKeyPayload) -> None: ...
    async def batch_poll(self, upstream_batch_id: str, jobs: list[BatchJob], key: TenantKeyPayload) -> None: ...
```

`batch_submit` and `batch_poll` are optional — the base class raises `NotImplementedError` by default. A transport only implements what it supports.

---

## The routing registry

`src/llm_service/providers/registry.py` is the routing table. Given `(provider, model, feature)`, it returns the right transport instance.

```python
_OVERRIDE: dict[tuple[str, str, str], RoutingEntry] = {
    ("anthropic", "*", "batch"): RoutingEntry(AnthropicTransport),
    ("bedrock",   "*", "batch"): RoutingEntry(BedrockTransport),
}

def resolve(provider: str, model: str, feature: str) -> BaseLLMProvider:
    entry = (
        _OVERRIDE.get((provider, model, feature))
        or _OVERRIDE.get((provider, "*", feature))
        or RoutingEntry(transport_cls=LiteLLMTransport)
    )
    return entry.transport_cls(regions=entry.regions)
```

The lookup tries exact match first, then wildcard model (`"*"`), then falls through to `LiteLLMTransport` as the default.

**Current routing table:**

| Provider | Feature | Transport |
|----------|---------|-----------|
| `anthropic` | `batch` | `AnthropicTransport` (direct Anthropic SDK) |
| `bedrock` | `batch` | `BedrockTransport` (direct boto3) |
| anything else | `chat` / `stream` / `embed` | `LiteLLMTransport` (default) |

This means all live chat and streaming — including Anthropic and Bedrock — flows through LiteLLM. The direct adapters exist only for batch operations where LiteLLM's batch API support is limited.

---

## LiteLLMTransport

`src/llm_service/providers/litellm.py` — the gravity-well default. Uses the LiteLLM Python SDK in-process.

**Model string format:** `provider/model`, e.g. `anthropic/claude-sonnet-4-5`, `bedrock/anthropic.claude-sonnet-4-5-20241022-v2:0`.

**Key injection:** credentials are passed as per-call kwargs — never set globally. `litellm.api_key` is never assigned.

```python
raw = await litellm.acompletion(
    model="anthropic/claude-sonnet-4-5",
    messages=[...],
    api_key=key.data["api_key"],      # Anthropic
    # or
    aws_access_key_id=...,            # Bedrock
    aws_secret_access_key=...,
    aws_region_name=...,
)
```

**Retry logic:** retries on `Timeout`, `ServiceUnavailableError`, `APIConnectionError`, `InternalServerError`, and `RateLimitError`. Configurable via `LLM_RETRY_MAX_ATTEMPTS` and `LLM_RETRY_BACKOFF_BASE_S`. For rate limits, respects the upstream `Retry-After` header if present.

**Error wrapping:** LiteLLM exceptions are caught and converted to `UpstreamTransportError` with a structured `code`:

| LiteLLM exception | `code` | HTTP status |
|-------------------|--------|------------|
| `Timeout` | `upstream_timeout` | 504 |
| `RateLimitError` | `upstream_rate_limited` | 502 |
| `AuthenticationError` | `tenant_key_rejected` | 502 |
| anything else | `upstream_error` | 502 |

**Streaming:** calls `litellm.acompletion(..., stream=True)` and yields `StreamEvent` objects — `token` for text deltas, `tool_use` for tool call argument fragments, `finish` when the stream ends. Usage data comes from the final chunk (via `stream_options={"include_usage": True}`).

**Multi-region failover:** if the registry entry has multiple regions, LiteLLM's `fallbacks=[...]` mechanism is used. The primary region is tried first; on failure, subsequent regions are tried automatically by the SDK.

**Prompt caching:** for Anthropic and Bedrock, messages marked with `cache: "ephemeral"` get `cache_control` blocks injected before sending. OpenAI prompt caching is automatic (no explicit markers needed).

---

## AnthropicTransport

`src/llm_service/providers/anthropic.py` — direct Anthropic SDK, used only for batch operations.

- `chat()` and `stream()` raise `NotImplementedError` — these routes go through LiteLLM.
- `batch_submit()` — calls `client.messages.batches.create()` with all jobs in one API call.
- `batch_poll()` — calls `client.messages.batches.retrieve()` and streams results when `processing_status == "ended"`.

---

## BedrockTransport

`src/llm_service/providers/bedrock.py` — direct boto3, used only for batch operations.

- `chat()` and `stream()` raise `NotImplementedError` — these routes go through LiteLLM.
- `batch_submit()` — uploads a JSONL file to S3, then calls `bedrock.create_model_invocation_job()`.
- `batch_poll()` — polls job status and reads results from S3 when the job is `Completed`.

Supports `anthropic.*`, `meta.llama*`, and `amazon.titan*` model families, each with provider-specific payload shapes.

---

## Key format handling

Each transport reads `key.key_format` to know how to extract credentials:

| `key_format` | Used by | Fields |
|---|---|---|
| `api_key` | LiteLLM (Anthropic, OpenAI, Groq), AnthropicTransport | `api_key` |
| `aws_credentials` | LiteLLM (Bedrock), BedrockTransport | `access_key_id`, `secret_access_key`, `region` |
| `endpoint_pair` | LiteLLM (custom endpoints) | `endpoint_url`, `token` |

No transport ever falls back to a default key if the tenant key is missing or the format is unsupported. An unsupported format raises `ValueError` immediately.

---

See [Batch API](batch-api.md) for how batch submission and polling work end-to-end.
