# shared/queue — Arq async task worker

Single Arq worker process handles background tasks for all modules — LLM and voice.

## Files to create

- `worker.py` — Arq worker entrypoint:
  ```python
  class WorkerSettings:
      redis_settings = RedisSettings.from_dsn(settings.redis_url)
      functions = [ledger_flush, retry_failed, batch_submit]
      max_jobs = 50
      job_timeout = 30
  ```
  Start with: `uv run arq shared.queue.worker.WorkerSettings`

- `tasks.py` — task functions:

  - `ledger_flush(ctx, payload: LedgerEntryPayload)` **(Phase 1)**
    - Inserts one row into `shared/db` → `ledger_entries`
    - Idempotent: `INSERT … ON CONFLICT (request_id) DO NOTHING`
    - Called from both `llm_service` and `voice` hot paths

  - `retry_failed(ctx, request_id: str)` **(Phase 2)**
    - Re-enqueues failed upstream calls with exponential backoff
    - Max retries configurable; after max → mark `status = permanent_failure`

  - `batch_submit(ctx, batch_id: str)` **(Phase 4)**
    - Collects buffered async LLM requests → submits as one OpenAI/Anthropic Batch API call (~50% cheaper)
    - On completion: parses results, writes per-request ledger rows via `ledger_flush`
    - Voice calls are not batchable in v1

## Phase

**Phase 4** (queue is `batch_submit`-only).

Resolved during eng review (2026-05-07): ledger flush is sync `asyncpg` insert from the request handler — no Arq. `retry_failed` (Phase 2) uses FastAPI BackgroundTasks in-process. The Arq worker process and Redis-as-required-dependency only land in Phase 4 when `batch_submit` needs persistent scheduling for OpenAI/Anthropic batch endpoints (multi-hour async jobs that survive restarts).

Until Phase 4: this directory stays scaffolded with `__init__.py` only. Don't land Arq early.
