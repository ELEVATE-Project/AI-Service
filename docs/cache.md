# Response Cache

The gateway caches LLM responses in Redis so that identical requests return instantly without spending tokens or hitting a provider.

---

## Why the cache exists

Every upstream LLM call costs money and adds latency. Many real-world workloads repeat the same prompt — dashboards that regenerate on page load, test suites that run the same prompt dozens of times, batch jobs processing overlapping inputs. Without a cache, each of those pays the full cost every time.

The cache is an exact-match lookup keyed per tenant. If the same tenant sends the same request twice, the second response is returned from Redis in milliseconds with zero upstream spend.

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

| Field | Included |
|-------|----------|
| `tenant_id` | Yes — tenant A never sees tenant B's cache |
| `provider` | Yes |
| `model` | Yes |
| `messages` | Yes — full content, role, and `cache` hint |
| `tools` | Yes — tool definitions affect the response |
| `params` | Yes — temperature, max_tokens, top_p, stop, seed |

Changing any of these fields produces a different cache key and a cache miss.

Fields **not** included: `metadata` (Langfuse session tracking, not sent upstream), `stream` (the same underlying response is used for both streaming and non-streaming cache replays), `tool_choice` is included in the `params` hash via the normalised request.

---

## Stampede protection

When many identical requests arrive simultaneously (a burst of concurrent users hitting the same prompt), only one should call the upstream LLM. The others should wait and use that result.

This is handled with a Redis distributed lock:

```
Request 1  → GET key → miss
           → SET lock:key NX EX 10  → acquired lock
           → proceeds to LLM call

Request 2  → GET key → miss
           → SET lock:key NX EX 10  → lock already held
           → polls GET key every 200ms until data appears or 10s timeout

Request 1  → gets LLM response
           → SET key <response> EX <ttl>
           → DEL lock:key          ← releases lock

Request 2  → sees data on next poll → returns cached response
```

If the lock holder's LLM call fails or times out, the lock expires after 10 seconds and the next waiting request acquires it and tries the upstream call itself.

---

## Redis failure handling

All Redis operations are wrapped in `try/except`. If Redis is down or unreachable:

- `get()` returns `None` — treated as a cache miss, the request proceeds normally
- `set()` is a no-op — the response is returned to the caller but not cached

Redis failure never crashes the request. The gateway degrades gracefully to pass-through mode.

---

## Cache TTL

Controlled by `CACHE_TTL_SECONDS` in `.env`. The default in production is `300` (5 minutes). Set to `0` to disable caching entirely during development.

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

- On a **cache miss**: the stream flows live from the upstream, token by token. After the stream ends, the completed response is stored in Redis for future requests.
- On a **cache hit**: the cached response is replayed as a synthetic stream — a `token` event for the content, then a `finish` event with the full metadata. The caller sees the same SSE event sequence as a live stream.

---

See [Provider Layer](providers.md) for how the upstream LLM call works on a cache miss.
