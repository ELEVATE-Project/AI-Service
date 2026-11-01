# Response Cache

The gateway has an optional cache of complete LLM responses in Redis. Eligible identical requests can reuse a stored answer without calling a provider.

> This page is about the gateway's own Redis response cache (`our_cache_hit`). For provider-side prompt caching (Anthropic's `cache_control`, `params.cache_options`, `upstream_prompt_cache_hit`) — a separate, orthogonal mechanism — see [Provider Layer → Prompt caching](providers.md#prompt-caching).

---

## Why the cache exists

This cache can save provider calls for repeatable requests that can safely reuse an answer. It is not suitable for every chatbot turn: live information, reasoning and sampling can require a fresh response. Saathi's reasoning-enabled teacher bots bypass this cache. Keeping the cache enabled for other workloads should be justified by useful hits.

The cache is an exact-match lookup keyed per tenant. If the same tenant sends an eligible request twice before expiry, the second response can come from Redis with zero upstream spend. Batch admission bypasses this response cache.

## Which responses can be reused

`CACHE_TTL_SECONDS <= 0` bypasses cache reads, writes and locks entirely. With a positive expiry, neither cache reads nor writes occur for requests with:

- Positive `params.temperature`.
- Enabled `params.thinking`, or reasoning effort in `params.output_config`.
- `params.web_search_options`, an Anthropic `web_search*` tool, an `openrouter:web_search` tool, or an OpenRouter `web` plugin.

Responses containing citations, reasoning blocks or a positive web-search usage count are not stored or replayed. This also protects against a provider returning these without an explicit request flag. Provider prompt caching remains independent of these rules.

---

## Where it sits in the pipeline

```
Request pipeline
  │
  ├─ Auth + tenant resolution
  ├─ Request normalisation
  ├─ Policy check
  ├─ Guardrails — input
  ► Cache lookup                  ← cache hit → return immediately (you are here)
  ├─ LLM call
  └─ Cache write (after response)
```

A cache hit skips the LLM call entirely. The response is returned with `"our_cache_hit": true` in the `cache` block.

---

## What counts as "the same request"

The cache key is a SHA-256 hash of these fields, canonically serialised:

- `tenant_id`, provider and model.
- Full messages and tool definitions, including opaque provider fields.
- `tool_choice`, including a structured choice of a specific tool.
- Answer parameters: temperature, maximum tokens, top-p, stop, seed, search options, provider prompt-cache options, thinking and output configuration.
- `provider_options`, including provider routing preferences and plugins.

Changing any of these fields produces a different cache key and a cache miss.

Timeouts, retries and tracking metadata do not change the key. Streaming and normal chat share eligible responses. The key includes a version so entries written before the tool-choice fix cannot be replayed.

---

## Cache population locks

Cache lookup never polls for another request's answer. A request that misses the cache tries once to reserve population with a Redis lock. If another request already owns it, this request calls the provider without waiting and does not populate the cache. Concurrent misses can therefore make separate provider calls; the lock protects population rather than guaranteeing a single provider call.

The reservation has a unique owner token and a 10-second expiry:

```
Request 1  → GET key → miss
           → SET lock:key <token-A> NX PX 10000 → acquired lock
           → proceeds to LLM call

Request 2  → GET key → miss
           → reservation already held
           → calls provider without waiting; does not populate cache

Request 1  → gets LLM response
           → SET key <response> EX <ttl>
           → atomic compare-and-delete of lock:key if still owned by token-A
```

The owner releases its lock in `finally` after success, failure or stream cancellation. An atomic Lua comparison prevents an expired owner from deleting a newer owner's lock. Streaming acquires the lock only when the response body starts being consumed. If cleanup cannot reach Redis, the expiry remains the fallback.

---

## Redis failure handling

All Redis operations are wrapped in `try/except`. If Redis is down or unreachable:

- `get()` returns `None` — treated as a cache miss, the request proceeds normally
- `set()` is a no-op — the response is returned to the caller but not cached
- Failed lock acquisition also bypasses population; failed release falls back to expiry.

Redis failure never crashes the request. The gateway degrades gracefully to pass-through mode.

---

## Cache TTL

Controlled by `CACHE_TTL_SECONDS` in the service configuration. A value of `300` keeps an eligible response for 5 minutes. Set it to `0` to disable this cache entirely, including Redis reads and locks. Restart the service after changing the setting.

```env
CACHE_TTL_SECONDS=300
```

---

## The `cache` block in responses

Every response includes a `cache` block:

```json
{
  "cache": {
    "our_cache_hit": false,
    "upstream_prompt_cache_hit": true
  }
}
```

| Field | Meaning |
|-------|---------|
| `our_cache_hit` | `true` if served from Redis — no upstream call was made |
| `upstream_prompt_cache_hit` | `true` if the provider served some tokens from its own prompt cache (Anthropic, OpenAI) |

These are independent. A request can have `our_cache_hit: false` (first time we've seen it) but `upstream_prompt_cache_hit: true` (the provider reused a cached prefix on its side).

---

## Streaming and the cache

For streaming responses (`POST /v1/chat/stream`):

- On a **cache miss**: the stream flows live from the upstream, token by token. After completion, an eligible response is stored only if this request acquired the population lock.
- On a **cache hit**: the cached response is replayed as a synthetic stream — a `token` event for the content, then a `finish` event with the full metadata. The caller sees the same SSE event sequence as a live stream.

---

See [Provider Layer](providers.md) for how the upstream LLM call works on a cache miss.
