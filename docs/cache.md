# Response Cache

The gateway caches LLM responses in Redis so that identical requests return instantly without spending tokens or hitting an upstream provider.

---

## Why the cache exists

Every upstream LLM call costs money and adds latency. Many real-world workloads repeat the same prompt — dashboards that regenerate on every page load, test suites that run the same prompt dozens of times, batch jobs that process overlapping inputs. Without a cache, each of those pays the full cost every time.

The cache is an exact-match lookup keyed per tenant. If the same tenant sends the same request twice, the second response is returned from Redis in milliseconds with zero upstream spend.

---

## What counts as "the same request"

The cache key is a SHA-256 hash of these fields:

| Field | Included |
|-------|----------|
| `tenant_id` | Yes — tenant A never sees tenant B's cached response |
| `provider` | Yes |
| `model` | Yes |
| `messages` | Yes — full content including role, cache hints, and tool calls |
| `tools` | Yes — tool definitions affect the model's output |
| `params` | Yes — temperature, max_tokens, top_p, stop, seed |
| `stream` | No — streaming vs non-streaming is a transport choice, not a content difference |
| `metadata` | No — goes to observability only, does not affect the LLM output |

Changing any included field produces a different key and a cache miss.

The key is computed **after** input guardrails run. If Presidio redacted PII from a message, the cache key is based on the redacted version — so the stored value is always the clean copy, never the original with PII.

---

## The single-flight lock

Without coordination, a burst of identical concurrent requests would all miss the cache simultaneously and each trigger an upstream call. This is a cache stampede.

The gateway prevents it with a SETNX-based single-flight lock:

```
Request A (arrives first)
  → GET cache_key       → miss
  → SET lock:{key} NX   → acquired
  → return None         → proceeds to upstream call
  → upstream responds
  → SET cache_key (data, TTL)
  → DEL lock:{key}

Request B (arrives during A's upstream call)
  → GET cache_key       → miss
  → SET lock:{key} NX   → not acquired (A holds it)
  → poll GET cache_key every 200ms, up to 10s
  → A finishes → data key appears
  → returns cached response ✓

Request C (arrives after A completes)
  → GET cache_key       → hit → returns immediately ✓
```

One request wins the lock and pays the upstream cost. Concurrent duplicates wait up to 10 seconds for the data to appear. If the upstream call takes longer than 10 seconds and the lock expires, waiting requests fall through and make their own upstream call — this is acceptable and self-corrects once the first response is stored.

---

## Redis failure is transparent

Every Redis call is wrapped in a `try/except`. If Redis is down, unreachable, or returns an unexpected error:

- `get` returns `None` — treated as a cache miss, the request proceeds normally
- `set` is a no-op — the response is returned to the caller without being stored

A Redis outage degrades cache hit rate but never breaks a live request. There are no retries, no fallback queues, and no alerts from within the gateway — monitoring Redis availability is an infrastructure concern.

---

## Cache hit in the response

Every response includes a `cache` block:

```json
{
  "cache": {
    "our_cache_hit": false
  }
}
```

On a cache hit, `our_cache_hit` is `true`:

```json
{
  "cache": {
    "our_cache_hit": true
  }
}
```

The ledger row for a cache hit records `our_cost_usd = 0` — no upstream call was made, so no cost is incurred.

---

## Cache hits in streaming responses

When `POST /v1/chat/stream` hits the cache, the gateway re-emits the cached response as a single `finish` SSE event rather than replaying individual token chunks:

```
event: finish
data: {"id":"req_...","finish_reason":"stop","usage":{...},"cache":{"our_cache_hit":true},...}
```

The client receives the full metadata in one event, just as it would at the end of a real stream.

---

## Configuration

Two environment variables control the cache. Neither has a default — both must be set explicitly in `.env`.

| Variable | Type | What it controls |
|----------|------|-----------------|
| `REDIS_URL` | `str` | Connection URL, e.g. `redis://localhost:6379/0` |
| `CACHE_TTL_SECONDS` | `int` | How long a cached response lives before expiry |

A TTL of `300` (5 minutes) is a reasonable starting point for most workloads. Set it to a short value (e.g. `10`) in local dev to avoid stale responses during testing.

There is no per-tenant or per-model TTL — all responses share the same TTL. Disabling the cache entirely is not supported via config; bring Redis down or point `REDIS_URL` at a non-existent instance and the cache silently degrades to a pass-through.

---

## Where the cache sits in the request pipeline

```
POST /v1/chat
      │
      ▼
 Auth + Tenant resolution
      │
      ▼
 Request normalisation
      │
      ▼
 Policy check
      │
      ▼
 Guardrails — check_input()       ← PII redacted here before key is computed
      │
      ▼
 ► Cache lookup (make_cache_key → RedisCache.get)
      │
      ├─ hit  → return cached response (our_cache_hit: true)
      │
      └─ miss → LLM call
                      │
                      ▼
               Guardrails — check_output()
                      │
                      ▼
               Ledger write
                      │
                      ▼
               ► Cache set (RedisCache.set)
                      │
                      ▼
               Return ChatResponse
```

The cache lookup happens after input guardrails so the stored value is always the PII-clean version. It happens before the LLM call so a hit skips all upstream cost.

---

## Code layout

```
src/llm_service/cache/
    base.py         CacheBackend ABC — get / set interface
    keys.py         make_cache_key() — SHA-256 of canonical inputs
    redis_cache.py  RedisCache — SETNX lock, poll loop, fault-tolerant wrappers
    __init__.py     Public exports

src/llm_service/api/deps.py     get_cache() factory (singleton via lru_cache)
src/llm_service/api/rest/chat.py  Step 7 — cache lookup and post-call set
```

---

See [Guardrails](guardrails.md) for the PII redaction that runs before the cache key is computed.
See [Auth & Tenants](auth-and-tenants.md) for how `tenant_id` is resolved before the key is hashed.