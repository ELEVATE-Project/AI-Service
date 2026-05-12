# shared/observability — tracing + structured logging

Used by all modules (`llm_service`, `voice`). One Langfuse project, one log stream.

## Files to create

- `langfuse.py` — Langfuse self-hosted trace client:
  - Initialize `langfuse.Langfuse` from `shared/config.py` (host, public/secret key)
  - `trace_request(request_id, tenant_id, module: str, ...)` context manager
  - Spans: auth, policy, guardrails_in, upstream, guardrails_out, ledger_write
  - OTel-compatible — exportable to any OTel collector without code changes
  - Self-hostable: required for govt customers who cannot send traces to cloud SaaS

- `logging.py` — structlog configuration:
  - JSON output in production; pretty `ConsoleRenderer` in dev (driven by `config.log_level`)
  - Standard processors: timestamp, log level, `request_id`, `tenant_id`, `module` (llm/voice)
  - Correlation ID set as a context var at request start in `api/deps.py` of each module

## Phase

Phase 1
