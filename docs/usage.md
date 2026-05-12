# Usage Guide

This guide shows how to call the API once you have the server running locally. If you haven't done that yet, start with [Local Setup](local-setup.md).

---

## Before every request

Every request needs two headers:

| Header | What to put | Example |
|--------|-------------|---------|
| `Authorization` | `Bearer <token>` — the token you created in setup | `Bearer my-dev-token-123` |
| `X-Tenant-Id` | The tenant ID | `tenant_dev` |
| `Content-Type` | Always `application/json` | `application/json` |

---

## Chat — non-streaming

**`POST /v1/chat`**

Use this when you want the full response in one go.

```bash
curl -s -X POST http://localhost:8000/v1/chat \
  -H "Authorization: Bearer my-dev-token-123" \
  -H "X-Tenant-Id: tenant_dev" \
  -H "Content-Type: application/json" \
  -d '{
    "provider": "openai",
    "model": "gpt-4o",
    "messages": [
      { "role": "user", "content": "What is 2 + 2?" }
    ],
    "params": {
      "temperature": 0.2,
      "max_tokens": 100
    }
  }'
```

**Sample response:**

```json
{
  "id": "req_a3f1c2d4e5b6789012345678",
  "object": "chat.completion",
  "created": 1746000000,
  "tenant_id": "tenant_dev",
  "provider": "openai",
  "model": "gpt-4o",
  "transport": "stub",
  "region": null,
  "choices": [
    {
      "index": 0,
      "message": {
        "role": "assistant",
        "content": "[stub] Real LLM not wired yet.",
        "tool_calls": null
      },
      "finish_reason": "stop"
    }
  ],
  "usage": {
    "input_tokens": 10,
    "output_tokens": 7,
    "input_tokens_cache_write": null,
    "input_tokens_cache_read": null,
    "total_tokens": 17
  },
  "cost": {
    "computed_usd": 0.0,
    "pricing_version": 0,
    "provider_reported_usd": null,
    "currency": "USD"
  },
  "latency_ms": {
    "total": 3,
    "guardrails_in": null,
    "guardrails_out": null,
    "upstream": 3,
    "time_to_first_token": null
  },
  "cache": {
    "our_cache_hit": false,
    "upstream_prompt_cache_hit": null
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

> **Note:** The response currently returns a stub (`"transport": "stub"`, content says "Real LLM not wired yet"). This is expected — the provider layer (the part that actually calls OpenAI/Anthropic) gets added next. The auth, tenant resolution, and full response envelope are real and working.

---

## Chat — streaming (SSE)

**`POST /v1/chat/stream`**

Use this when you want tokens to stream back as they're generated. The response is [Server-Sent Events](https://developer.mozilla.org/en-US/docs/Web/API/Server-sent_events).

```bash
curl -N -s -X POST http://localhost:8000/v1/chat/stream \
  -H "Authorization: Bearer my-dev-token-123" \
  -H "X-Tenant-Id: tenant_dev" \
  -H "Content-Type: application/json" \
  -H "Accept: text/event-stream" \
  -d '{
    "provider": "openai",
    "model": "gpt-4o",
    "messages": [
      { "role": "user", "content": "Count to 5 slowly" }
    ],
    "stream": true
  }'
```

The `-N` flag tells curl not to buffer — you'll see each event arrive as it's sent.

**Sample output:**

```
event: token
data: {"index":0,"delta":"[stub] "}

event: token
data: {"index":0,"delta":"Real "}

event: token
data: {"index":0,"delta":"LLM "}

event: token
data: {"index":0,"delta":"not "}

event: token
data: {"index":0,"delta":"wired "}

event: token
data: {"index":0,"delta":"yet."}

event: finish
data: {"id":"req_a3f1c2d4e5b6789012345678","finish_reason":"stop","usage":{"input_tokens":10,"output_tokens":6,"input_tokens_cache_write":null,"input_tokens_cache_read":null,"total_tokens":16},"cost":{"computed_usd":0.0,"pricing_version":0,"provider_reported_usd":null,"currency":"USD"},"latency_ms":{"total":498,"guardrails_in":null,"guardrails_out":null,"upstream":498,"time_to_first_token":82},"cache":{"our_cache_hit":false,"upstream_prompt_cache_hit":null},"guardrails":{"input_flags":[],"output_flags":[],"redactions_applied":[]},"policy":{"rate_limit_remaining":null,"budget_remaining_usd":null}}
```

Every event has a `type` and a `data` field:

| Event type | When it fires | What's in `data` |
|------------|---------------|-----------------|
| `token` | One per generated token | `index` + `delta` (the text chunk) |
| `tool_use` | When the model calls a function | Tool name, ID, and argument chunks |
| `usage` | Mid-stream update | Incremental token counts |
| `finish` | End of stream | Full metadata — same fields as the non-streaming response |
| `error` | Something went wrong | Error code + message |

---

## Multi-turn conversations

Pass the full conversation history in `messages`:

```bash
curl -s -X POST http://localhost:8000/v1/chat \
  -H "Authorization: Bearer my-dev-token-123" \
  -H "X-Tenant-Id: tenant_dev" \
  -H "Content-Type: application/json" \
  -d '{
    "provider": "openai",
    "model": "gpt-4o",
    "messages": [
      { "role": "user", "content": "My name is Kunal." },
      { "role": "assistant", "content": "Nice to meet you, Kunal!" },
      { "role": "user", "content": "What is my name?" }
    ]
  }'
```

---

## With tool definitions

```bash
curl -s -X POST http://localhost:8000/v1/chat \
  -H "Authorization: Bearer my-dev-token-123" \
  -H "X-Tenant-Id: tenant_dev" \
  -H "Content-Type: application/json" \
  -d '{
    "provider": "openai",
    "model": "gpt-4o",
    "messages": [
      { "role": "user", "content": "What is the weather in Mumbai?" }
    ],
    "tools": [
      {
        "type": "function",
        "function": {
          "name": "get_weather",
          "description": "Get current weather for a city",
          "parameters": {
            "type": "object",
            "properties": {
              "city": { "type": "string" }
            },
            "required": ["city"]
          }
        }
      }
    ],
    "tool_choice": "auto"
  }'
```

---

## Request body fields

| Field | Required | What it does |
|-------|----------|-------------|
| `provider` | Yes | Which LLM provider to use: `openai`, `anthropic`, `bedrock`, `hf_endpoint`, `hf_self_hosted` |
| `model` | Yes | Model ID, e.g. `gpt-4o`, `claude-sonnet-4-5` |
| `messages` | Yes | List of messages. Each has `role` (`user`, `assistant`, `system`, `tool`) and `content` |
| `params.temperature` | No | 0.0–2.0. Lower = more deterministic |
| `params.max_tokens` | No | Max tokens to generate |
| `params.top_p` | No | Nucleus sampling |
| `stream` | No | `true` to get SSE stream. Default `false` |
| `tools` | No | OpenAI-style function definitions |
| `tool_choice` | No | `auto`, `none`, `required`, or a specific function |
| `cache_policy` | No | `auto` (default), `explicit`, or `off` — controls prompt caching |
| `metadata` | No | Free-form JSON, passed to tracing. Not sent to the provider |

---

## Error responses

All errors use the same shape:

```json
{
  "detail": "Invalid bearer token."
}
```

| HTTP code | When you see it |
|-----------|----------------|
| `400` | Missing `X-Tenant-Id` header, or `provider`/`model` not provided |
| `401` | Missing or wrong `Authorization: Bearer` token |
| `403` | Token is valid but not allowed to act on the requested tenant |
| `422` | Request body failed validation (missing required field, wrong type) |
| `429` | Tenant hit their rate limit or monthly budget cap |
| `502` | The upstream provider returned an error (e.g. bad API key, rate limited by provider) |

---

## Passing a custom request ID

If you want to track a specific request across logs and the ledger, pass your own ID:

```bash
curl -s -X POST http://localhost:8000/v1/chat \
  -H "Authorization: Bearer my-dev-token-123" \
  -H "X-Tenant-Id: tenant_dev" \
  -H "X-Request-Id: my-trace-id-001" \
  -H "Content-Type: application/json" \
  -d '{ ... }'
```

The same ID will appear in the response `id` field and in the ledger row. If you don't pass one, the gateway generates one for you.