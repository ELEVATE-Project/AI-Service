# tests

Three test layers matching the architecture's verification requirements.

| Suite | Dir | Tool | Phase |
|-------|-----|------|-------|
| Unit | `unit/` | pytest | all |
| Contract (per-provider) | `contract/` | pytest + vcrpy | 1+ |
| End-to-end | `e2e/` | pytest + running service | 1+ |

## Shared fixtures (add `conftest.py` here)

- Async event loop setup (`pytest-asyncio`)
- Test DB (Postgres via testcontainers or docker-compose)
- Test Redis
- Mock `SecretBackend` that returns a fixed test key

## Phase

Grows with each phase.
