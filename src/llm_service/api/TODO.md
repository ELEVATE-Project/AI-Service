# llm_service/api — HTTP surface layer

FastAPI app entry point and shared LLM request dependencies.

## Files to create here

- `app.py` — `FastAPI()` instance, mount routers, register lifespan (startup: connect DB/Redis, shutdown: flush queue)
- `deps.py` — FastAPI dependency functions wiring shared infrastructure into routes:
  ```python
  async def get_tenant(request: Request) -> Tenant:
      return await shared.auth.get_tenant(request)   # resolves caller → tenant_id

  async def get_db() -> AsyncSession: ...            # SQLAlchemy session from shared/db

  async def get_policy(tenant: Tenant) -> PolicyRegistry: ...   # from shared/policy

  async def get_guardrails() -> GuardrailChain: ...             # from shared/guardrails

  async def get_cache() -> RedisCache: ...                      # from llm_service/cache
  ```

## Submodules

| Module | Phase | Purpose |
|--------|-------|---------|
| `rest/` | 1–2 | HTTP REST endpoints: `/v1/chat`, `/v1/embed`, `/v1/models`, admin |
| `ws/` | 2 | WebSocket streaming: `WS /v1/ws/chat` |
| `mcp/` | 5 | MCP transport — placeholder, deferred |

## Phase

Phase 1 (deps.py + REST), Phase 2 (WebSocket)
