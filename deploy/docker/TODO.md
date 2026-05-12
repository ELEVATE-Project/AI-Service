# deploy/docker — Docker + docker-compose

## Files to create

- `Dockerfile` — multi-stage build:
  ```dockerfile
  # Stage 1: dependency install
  FROM python:3.12-slim AS builder
  COPY --from=ghcr.io/astral-sh/uv:latest /uv /bin/uv
  WORKDIR /app
  COPY pyproject.toml uv.lock ./
  RUN uv sync --frozen --no-dev

  # Stage 2: production image
  FROM python:3.12-slim
  COPY --from=builder /app/.venv /app/.venv
  COPY src/ /app/src/
  ENV PATH="/app/.venv/bin:$PATH"
  CMD ["uvicorn", "llm_service.api.app:app", "--host", "0.0.0.0", "--port", "8000"]
  ```

- `docker-compose.yml` — local dev stack:
  ```yaml
  services:
    postgres:   # standard postgres image, persist volume
    redis:      # standard redis image
    langfuse:   # langfuse/langfuse self-hosted image
    llm-service: # built from ./Dockerfile — LiteLLM runs in-process, no extra container
  ```
  - All services have healthcheck + `depends_on: condition: service_healthy`
  - No secrets in compose file — llm-service reads from OS keyring or env injection

## Phase

Phase 1
