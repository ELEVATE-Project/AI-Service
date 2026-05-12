# llm_service/cache — LLM response exact-match cache

LLM-specific cache. Voice has its own cache strategy (if needed) in `voice/cache/`.
Both share the same Redis connection from `shared/config.py`.

## Files to create

- `redis.py` — `LLMResponseCache`:
  - Cache key: `sha256(tenant_id + provider + model + canonical_json(messages + params))`
  - Hit → deserialize and return cached `ChatResponse` with `cache.our_cache_hit: true`; upstream is never called
  - Miss → acquire single-flight Redis lock on the cache key (resolved during eng review 2026-05-07: stampede protection is required, not optional)
    - Lock acquired (first miss): caller proceeds upstream; on response, stores in cache + releases lock
    - Lock held by another request: wait up to ~10s for the first request to fill the cache, then read; on timeout, proceed upstream independently (degraded mode — log + metric)
  - Lock TTL: ~10s (longer than P99 upstream latency, shorter than cache TTL)
  - Use `redis-py` `redis.lock(blocking_timeout=10)` — standard pattern, no custom logic
  - Stores the **full response envelope** (including cost/usage metadata) so callers get identical shape on hit
  - TTL configurable per provider in `shared/config.py` (default: 24h)

- `semantic.py` — **stub only** (Phase 5):
  - Interface matching `LLMResponseCache`
  - Body: `raise NotImplementedError("semantic cache deferred to Phase 5")`
  - Embedding-similarity cache is out of v1 scope

## Cache position in pipeline

Lookup: after guardrails input filter, before normaliser + routing.
Write: after ledger enqueued (we store the complete final envelope).

## Phase

Phase 2 (exact-match), Phase 5 (semantic)
