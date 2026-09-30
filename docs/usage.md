# Usage Guide

This guide covers every field exposed by the API — request parameters, response shapes, and streaming events. If you haven't set up yet, start with [Local Setup](local-setup.md).

---

## Required headers

All endpoints require:

| Header | Value |
|--------|-------|
| `Authorization` | `Bearer <token>` — from `scripts/add_tenant_key.py` |
| `X-Tenant-Id` | Your tenant ID, e.g. `saathi` |
| `Content-Type` | `application/json` |

---

## POST /v1/chat

Returns the full response in one go.

### Request

```json
{
  "provider": "anthropic",
  "model": "claude-sonnet-4-5",
  "messages": [
    { "role": "system", "content": "You are a helpful assistant.", "cache": "ephemeral" },
    { "role": "user", "content": "What is 2 + 2?" }
  ],
  "tools": [],
  "tool_choice": "auto",
  "params": {
    "temperature": 0.7,
    "max_tokens": 1024,
    "top_p": 1.0,
    "stop": null,
    "seed": null,
    "connect_timeout": null,
    "read_timeout": null,
    "web_search_options": null,
    "cache_options": null,
    "retry": null
  },
  "metadata": {},
  "use_defaults": false
}
```

**Top-level fields**

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `provider` | `string` | Conditionally | `anthropic`, `openai`, `bedrock`, `groq`, `openrouter`, `custom_endpoint`. Omit only when `use_defaults: true` and the tenant has a `default_provider` configured — otherwise required. |
| `model` | `string` | Conditionally | Model ID as the provider uses it, e.g. `claude-sonnet-4-5`. For `openrouter`, use the OpenRouter slug, e.g. `openai/gpt-4o` or `anthropic/claude-3.5-sonnet`. Omit only when `use_defaults: true` and the tenant has a `default_model` configured — otherwise required. |
| `messages` | `Message[]` | Yes | Conversation history. See message fields below. |
| `tools` | `Tool[]` | No | Function definitions the model can call |
| `tool_choice` | `string \| object` | No | `"auto"`, `"none"`, `"required"`, or `{"type":"function","function":{"name":"..."}}` |
| `params` | `ChatParams` | No | Inference parameters. All fields optional. |
| `metadata` | `object` | No | Free-form. Pass `{"batch": true}` to submit asynchronously. Not sent upstream. |
| `provider_options` | `object` | No | Provider-specific passthrough. Consumed only for `provider: "openrouter"` — keys: `provider` (routing prefs), `models` (fallback list), `plugins` (e.g. web search), `referer` / `title` (app attribution). See [Provider Layer → OpenRouter](providers.md#openrouter). |
| `use_defaults` | `bool` | No | `false`/omitted (default): `provider`/`model` are required as usual — today's behavior, unchanged. `true`: opts into filling, field-by-field, any of `provider`, `model`, or individual `params` fields the request left unset from the tenant's `tenant_defaults` row, if one exists. Defaults only fill gaps — any field the request *does* set (including a single `params` field like `max_tokens` while leaving `temperature` unset) is never overridden. See [Tenants → Tenant Defaults](auth-and-tenants.md) for the merge rule and a worked example. |

**Message fields**

| Field | Type | Description |
|-------|------|-------------|
| `role` | `string` | `"system"`, `"user"`, `"assistant"`, or `"tool"` |
| `content` | `string?` | Message text. Null for assistant messages that contain only `tool_calls`. |
| `cache` | `string?` | `"ephemeral"` — requests provider-side prompt caching for this message. Works with Anthropic, Bedrock, and OpenRouter (Claude/Gemini/etc. models). Always takes precedence over `params.cache_options.enabled` for this message. See [Provider Layer → Prompt caching](providers.md#prompt-caching). |
| `tool_calls` | `object[]?` | Tool calls the assistant issued. Present on `role: "assistant"` turns that invoked a tool. |
| `tool_call_id` | `string?` | ID of the tool call this message is responding to. Present on `role: "tool"` turns. |

**`params` fields** (all optional)

| Field | Type | Description |
|-------|------|-------------|
| `temperature` | `float` | Sampling temperature. Higher = more random. |
| `max_tokens` | `int` | Maximum output tokens. Subject to tenant policy cap. |
| `top_p` | `float` | Nucleus sampling. Use `temperature` or `top_p`, not both. |
| `stop` | `string[]` | Stop sequences — generation halts when any is produced. |
| `seed` | `int` | Fixed seed for deterministic sampling (provider support varies). |
| `connect_timeout` | `float` | Seconds to wait for the upstream connection to establish. |
| `read_timeout` | `float` | Seconds to wait for the upstream to return data after connecting. |
| `web_search_options` | `object` | Enable web search. Fields: `search_context_size` (`string`), `user_location` (`object`). Provider-specific. |
| `cache_options` | `object` | Opt-in automatic provider-side prompt caching. Fields: `enabled` (`bool`), `ttl` (`string`, `"5m"` or `"1h"`), `targets` (`string[]`, `"prompt"` and/or `"tools"`). See [Provider Layer → Prompt caching](providers.md#prompt-caching); current supported values are also served live at `GET /v1/cache/options`. |
| `retry` | `object` | Per-request override of the service-wide retry defaults. Fields: `enabled` (`bool` — `false` forces a single attempt, no retry), `max_attempts` (`int`, `1`–`10`), `backoff_base_s` (`float`, seconds, `0`–`60`) — out-of-range values are rejected with `422`, not clamped. Only applies to the fixed set of retryable upstream error types (timeouts, rate limits, 5xx) — a non-retryable error (e.g. an invalid model ID) still fails on the first attempt regardless of these values. See [Provider Layer → LiteLLMTransport](providers.md#litellmtransport). |

**Tool definition fields**

| Field | Type | Description |
|-------|------|-------------|
| `type` | `string` | `"function"` |
| `function.name` | `string` | Function name the model will use when calling it |
| `function.description` | `string?` | What the function does |
| `function.parameters` | `object?` | JSON Schema describing the function's arguments |

### Response

```json
{
  "id": "req_a3f1c2d4e5b6789012345678",
  "object": "chat.completion",
  "created": 1746000000,
  "tenant_id": "saathi",
  "provider": "anthropic",
  "model": "claude-sonnet-4-5",
  "transport": "LiteLLMTransport",
  "region": null,
  "choices": [
    {
      "index": 0,
      "message": {
        "role": "assistant",
        "content": "2 + 2 equals 4.",
        "tool_calls": null,
        "citations": null
      },
      "finish_reason": "stop"
    }
  ],
  "usage": {
    "input_tokens": 28,
    "output_tokens": 11,
    "total_tokens": 39,
    "input_tokens_cache_read": null,
    "input_tokens_cache_write": null
  },
  "cost": {
    "computed_usd": 0.0003,
    "pricing_version": 1,
    "provider_reported_usd": null,
    "currency": "USD"
  },
  "latency_ms": {
    "total": 842,
    "upstream": 820,
    "guardrails_in": null,
    "guardrails_out": null,
    "time_to_first_token": null
  },
  "cache": {
    "our_cache_hit": false,
    "upstream_prompt_cache_hit": false
  },
  "guardrails": {
    "input_flags": [],
    "output_flags": [],
    "redactions_applied": []
  },
  "policy": {
    "rate_limit_remaining": null,
    "budget_remaining_usd": null
  },
  "provider_raw": null
}
```

**Response fields**

| Field | Description |
|-------|-------------|
| `id` | Gateway request ID. Matches `X-Request-Id` header if supplied, otherwise generated. |
| `object` | Always `"chat.completion"` |
| `created` | Unix timestamp of request creation |
| `tenant_id` | Tenant this request was attributed to |
| `provider` | Provider that handled the call |
| `model` | Model ID echoed from the request |
| `transport` | Adapter used: `LiteLLMTransport`, `AnthropicTransport`, or `BedrockTransport` |
| `region` | Cloud region used, if applicable |
| `choices[].index` | Choice index (always `0` for single-choice responses) |
| `choices[].message.role` | Always `"assistant"` |
| `choices[].message.content` | Response text. `null` when model issued tool calls instead. |
| `choices[].message.tool_calls` | Array of tool calls the model made. `null` if none. |
| `choices[].message.citations` | Web search citations. `null` if web search was not used. |
| `choices[].finish_reason` | `"stop"`, `"length"`, `"tool_calls"`, or `"content_filter"` |
| `usage.input_tokens` | Prompt tokens billed |
| `usage.output_tokens` | Completion tokens billed |
| `usage.total_tokens` | Sum of input and output tokens |
| `usage.input_tokens_cache_read` | Tokens served from provider prompt cache (cheaper rate). `null` if not applicable. |
| `usage.input_tokens_cache_write` | Tokens written to provider prompt cache. `null` if not applicable. |
| `cost.computed_usd` | Cost computed from `pricing/models.yaml` |
| `cost.pricing_version` | YAML version used to compute cost |
| `cost.provider_reported_usd` | Cost reported by the provider, where available |
| `cost.currency` | Always `"USD"` |
| `latency_ms.total` | Total wall-clock time from request receipt to response sent (ms) |
| `latency_ms.upstream` | Time spent waiting for the upstream provider (ms) |
| `latency_ms.guardrails_in` | Time spent on input guardrails (ms). `null` if guardrails disabled. |
| `latency_ms.guardrails_out` | Time spent on output guardrails (ms). `null` if guardrails disabled. |
| `latency_ms.time_to_first_token` | `null` on non-streaming responses |
| `cache.our_cache_hit` | `true` if served from Redis — no upstream call was made |
| `cache.upstream_prompt_cache_hit` | `true` if the provider reused a cached prompt prefix |
| `guardrails.input_flags` | Guardrail flags fired on the input, e.g. `["pii.email_address"]` |
| `guardrails.output_flags` | Guardrail flags fired on the output |
| `guardrails.redactions_applied` | PII types that were redacted before going upstream |
| `policy.rate_limit_remaining` | Requests remaining in the current rate-limit window |
| `policy.budget_remaining_usd` | Monthly budget remaining in USD |
| `provider_raw` | Raw upstream provider response, for debugging. `null` in normal operation. |

---

## POST /v1/chat/stream

Same request body as `POST /v1/chat`. Returns a Server-Sent Events stream.

```bash
curl -N -X POST http://localhost:8000/v1/chat/stream \
  -H "Authorization: Bearer <bearer-token>" \
  -H "X-Tenant-Id: saathi" \
  -H "Content-Type: application/json" \
  -d '{"provider":"anthropic","model":"claude-sonnet-4-5","messages":[{"role":"user","content":"Count to 3."}],"params":{"max_tokens":50}}'
```

### Stream events

Each SSE event has a `type` and a `data` payload.

---

**`token`** — one text chunk

```
event: token
data: {"index": 0, "delta": "1, 2"}
```

| Field | Description |
|-------|-------------|
| `index` | Choice index (always `0`) |
| `delta` | The text fragment for this chunk |

---

**`tool_use`** — one fragment of a tool call argument

```
event: tool_use
data: {"index": 0, "id": "call_abc", "name": "get_weather", "arguments_delta": "{\"city\":\"Mum"}
```

| Field | Description |
|-------|-------------|
| `index` | Tool call index within the response |
| `id` | Tool call ID — stable across fragments for the same call |
| `name` | Function name — present on the first fragment, empty string on subsequent ones |
| `arguments_delta` | Partial JSON string of the function arguments |

---

**`finish`** — final event, always present

```
event: finish
data: {
  "id": "req_...",
  "finish_reason": "stop",
  "usage": { "input_tokens": 28, "output_tokens": 12, "total_tokens": 40, "input_tokens_cache_read": null, "input_tokens_cache_write": null },
  "cost": { "computed_usd": 0.0003, "pricing_version": 1, "provider_reported_usd": null, "currency": "USD" },
  "latency_ms": { "total": 1200, "upstream": 1180, "guardrails_in": null, "guardrails_out": null, "time_to_first_token": 320 },
  "cache": { "our_cache_hit": false, "upstream_prompt_cache_hit": false },
  "guardrails": { "input_flags": [], "output_flags": [], "redactions_applied": [] },
  "policy": { "rate_limit_remaining": null, "budget_remaining_usd": null },
  "citations": null
}
```

| Field | Description |
|-------|-------------|
| `id` | Request ID |
| `finish_reason` | `"stop"`, `"length"`, `"tool_calls"`, or `"content_filter"` |
| `usage` | Final token counts (same fields as non-streaming `usage`) |
| `cost` | Computed cost (same fields as non-streaming `cost`) |
| `latency_ms` | Same as non-streaming, plus `time_to_first_token` is populated |
| `cache` | Same as non-streaming |
| `guardrails` | Same as non-streaming |
| `policy` | Same as non-streaming |
| `citations` | Web search citations if web search was used, otherwise `null` |

---

**`error`** — emitted instead of `finish` when something goes wrong

```
event: error
data: {"code": "upstream_disconnected", "message": "...", "upstream_status": null, "retry_after": null}
```

| Field | Description |
|-------|-------------|
| `code` | Structured error code — same values as the HTTP error table below |
| `message` | Human-readable detail |
| `upstream_status` | HTTP status from the upstream provider, if applicable |
| `retry_after` | Seconds to wait before retrying, forwarded from the provider's `Retry-After` header |

---

## POST /v1/chat — batch submit

Same request body as `POST /v1/chat`, with `"metadata": {"batch": true}` added. Returns `202 Accepted` immediately instead of waiting for the LLM.

Supported providers: `anthropic`, `bedrock`, `openai`.

```json
{
  "provider": "anthropic",
  "model": "claude-sonnet-4-5",
  "messages": [{ "role": "user", "content": "Summarise this." }],
  "params": { "max_tokens": 500 },
  "metadata": { "batch": true }
}
```

### Accepted response (`202`)

```json
{
  "job_id": "xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx",
  "request_id": "req_abc123",
  "status": "pending"
}
```

| Field | Description |
|-------|-------------|
| `job_id` | UUID to use when polling `GET /v1/chat/batch/{job_id}` |
| `request_id` | Gateway request ID |
| `status` | Always `"pending"` on initial submission |

---

## GET /v1/chat/batch/{job_id}

Poll for the result of a batch job. The `job_id` comes from the `202` response above.

```bash
curl http://localhost:8000/v1/chat/batch/<job_id> \
  -H "Authorization: Bearer <bearer-token>" \
  -H "X-Tenant-Id: saathi"
```

### Poll response

```json
{
  "id": "xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx",
  "request_id": "req_abc123",
  "status": "complete",
  "provider": "anthropic",
  "model": "claude-sonnet-4-5",
  "created_at": "2026-05-26T10:00:00Z",
  "completed_at": "2026-05-26T11:23:45Z",
  "result": { },
  "error_code": null
}
```

| Field | Description |
|-------|-------------|
| `id` | The `job_id` |
| `request_id` | Gateway request ID |
| `status` | `"pending"`, `"submitted"`, `"complete"`, or `"failed"` |
| `provider` | Provider handling the batch |
| `model` | Model ID |
| `created_at` | When the `202` was issued |
| `completed_at` | When the result was retrieved from the provider. `null` until complete. |
| `result` | Full `ChatResponse` object when `status == "complete"`. `null` otherwise. Same shape as `POST /v1/chat`. |
| `error_code` | Reason for failure when `status == "failed"`. `null` otherwise. |

Keep polling until `status` is `"complete"` or `"failed"`. There is no webhook — the caller polls.

---

## GET /v1/cache/options

Supported values for `params.cache_options` — poll this instead of hardcoding `ttl`/`targets` values, so a calling service always gets the current supported set.

```bash
curl http://localhost:8000/v1/cache/options \
  -H "Authorization: Bearer <bearer-token>" \
  -H "X-Tenant-Id: saathi"
```

```json
{
  "data": {
    "providers": ["anthropic", "bedrock", "openrouter"],
    "ttl_values": ["5m", "1h"],
    "ttl_default": null,
    "target_values": ["prompt", "tools"],
    "target_default": ["prompt", "tools"]
  }
}
```

See [Provider Layer → Prompt caching](providers.md#prompt-caching) for the full behavior.

---

## Error codes

| HTTP status | `detail` | Meaning |
|-------------|----------|---------|
| 400 | `guardrails_blocked` | Input or output failed a guardrail check |
| 401 | `Missing or malformed Authorization header.` | Missing or malformed `Authorization` header |
| 401 | `Invalid bearer token.` | Token not found in the database |
| 403 | `Service is not authorised to act on behalf of tenant '...'` | Token valid but this service cannot access the requested tenant |
| 400 | `Missing X-Tenant-Id header.` | `X-Tenant-Id` header absent |
| 422 | `missing_tenant_key` | No provider key stored for this tenant |
| 422 | `provider_not_batch_eligible` | Provider does not support the batch API |
| 429 | `policy_exceeded` | Model denied, monthly budget exceeded, or `max_tokens` over the tenant cap |
| 502 | `upstream_error` | Provider returned an unrecognised error |
| 502 | `tenant_key_rejected` | Provider rejected the API key |
| 502 | `upstream_rate_limited` | Provider rate-limited this key. `Retry-After` header forwarded when available. |
| 504 | `upstream_timeout` | Provider did not respond within the configured timeout |

---

See [Auth & Tenants](auth-and-tenants.md) for how bearer tokens and tenant IDs work.
