# llm_service — LLM inference module

LLM-specific code only. All shared infrastructure (auth, config, secrets, ledger, policy,
guardrails, db, queue, observability) lives in `shared/` and is imported from there.

## Imports pattern

```python
from shared.config import settings
from shared.auth import get_tenant
from shared.secrets import SecretBackend
from shared.ledger import LedgerWriter
from shared.policy import PolicyRegistry
from shared.guardrails import GuardrailChain
from shared.schemas.envelope import CostBlock, UsageBlock, ...
```

## Structure

| Module | Purpose |
|--------|---------|
| `api/` | FastAPI routers (REST + WS + MCP) and shared FastAPI dependencies |
| `normaliser.py` | Converts incoming `ChatRequest` → provider-agnostic internal schema; injects cache markers |
| `providers/` | `BaseLLMProvider` interface, routing registry, LiteLLM (in-process SDK) + direct provider adapters |
| `cache/` | LLM response exact-match cache (Redis, keyed on request hash) |
| `schemas/` | LLM-specific Pydantic DTOs: `ChatRequest`, `ChatResponse`, `StreamToken`, etc. |

## Request pipeline (every LLM call)

```
API (schemas/ChatRequest)
  → shared/auth.py         resolve tenant
  → shared/policy/         check rate limit + budget
  → shared/guardrails/     filter input (PII, safety)
  → llm_service/cache/     exact-match lookup (skip upstream on hit)
  → llm_service/normaliser normalise to provider-agnostic schema
  → llm_service/providers/ route → LiteLLM SDK or direct adapter
  → shared/guardrails/     filter output
  → shared/ledger/         write ledger row (via queue)
  → API (schemas/ChatResponse)
```

## Phase

Phase 1 (REST, all 4 adapters incl. LiteLLM, ledger), Phase 2 (WS streaming, cache),
Phase 3 (policy + guardrails wired in), Phase 5 (MCP)
