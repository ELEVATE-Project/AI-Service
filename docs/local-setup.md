# Local Setup

This guide gets the project running on your machine from scratch. No experience with the codebase needed.

---

## What you need before starting

| Tool | Why |
|------|-----|
| Python 3.10+ | The project runs on it |
| [uv](https://docs.astral.sh/uv/getting-started/installation/) | Package manager — replaces pip + venv |
| Docker Desktop | Runs Postgres and Redis locally |

---

## Step 1 — Install dependencies

From the project root:

```bash
uv sync
```

This creates a `.venv` folder and installs everything listed in `pyproject.toml`.

---

## Step 2 — Start Postgres and Redis

```bash
docker compose -f deploy/docker/docker-compose.yml up -d
```

This starts two containers:

- **Postgres** on port `5432` — stores tenants, keys, ledger entries, policies
- **Redis** on port `6379` — used for caching (wired in later)

To check they're running:

```bash
docker compose -f deploy/docker/docker-compose.yml ps
```

Both should show `healthy`.

---

## Step 3 — Run database migrations

```bash
alembic upgrade head
```

This applies all existing migration files from `src/shared/db/migrations/versions/` and creates the tables in Postgres.

> **If you're changing models:** run `alembic revision --autogenerate -m "description"` to generate a new migration file, then `alembic upgrade head` to apply it.

---

## Step 4 — Create the master encryption key

```bash
uv run llm-service keys init
```

This generates a secret key and saves it in your OS keyring (macOS Keychain on Mac). It's used to encrypt all the API keys stored in the database. Run this once. If you run it again it'll tell you one already exists.

---

## Step 5 — Register a calling service

A "calling service" is any app that talks to this gateway (your chatbot, your backend, etc.). You register it once and get a bearer token to use in requests.

```bash
uv run llm-service services add \
  --name dev-service \
  --token my-dev-token-123 \
  --tenants tenant_dev
```

- `--name` — a label for this service, just for your reference
- `--token` — the token you'll put in the `Authorization: Bearer` header. Pick anything for local dev.
- `--tenants` — which tenants this service is allowed to act on behalf of. Comma-separated for multiple.

The token is hashed with SHA-256 before storing — the raw value is never saved anywhere. Copy it somewhere safe now. You can always create a new one but can't retrieve this one later.

---

## Step 6 — Store a provider API key

This stores the actual API key (e.g. your OpenAI key) for a tenant, encrypted in Postgres.

```bash
uv run llm-service keys set \
  --tenant tenant_dev \
  --provider openai \
  --key-format api_key \
  --payload '{"api_key": "sk-your-openai-key-here"}'
```

- `--tenant` — the tenant ID you used in Step 5
- `--provider` — `openai`, `anthropic`, `bedrock`, etc.
- `--key-format` — `api_key` for most providers, `aws_credentials` for Bedrock, `endpoint_pair` for self-hosted
- `--payload` — JSON with the actual credential

To check what keys are registered:

```bash
uv run llm-service keys list --tenant tenant_dev
```

Values are masked — you'll see the provider and format but not the actual key.

---

## Step 7 — Start the server

```bash
uv run uvicorn main:app --reload
```

The server starts on `http://localhost:8000`. The `--reload` flag restarts it automatically when you change code.

You should see something like:

```
INFO:     Uvicorn running on http://127.0.0.1:8000 (Press CTRL+C to quit)
INFO:     Started reloader process
```

---

## Step 8 — Verify everything works

```bash
curl -s -X POST http://localhost:8000/v1/chat \
  -H "Authorization: Bearer my-dev-token-123" \
  -H "X-Tenant-Id: tenant_dev" \
  -H "Content-Type: application/json" \
  -d '{"provider":"openai","model":"gpt-4o","messages":[{"role":"user","content":"hello"}]}' \
  | python -m json.tool
```

If you get a JSON response back, everything is wired correctly.

If you get `401` — check the token matches what you used in Step 5.  
If you get `403` — check the tenant ID matches what you used in Step 5.

---

## Environment variables

The defaults work for local dev with no extra config. If you need to override anything, create a `.env` file in the project root:

```env
DB_URL=postgresql+asyncpg://postgres:postgres@localhost:5432/llm_service
REDIS_URL=redis://localhost:6379
LOG_LEVEL=INFO
```

---

## Useful commands

```bash
# See all CLI commands
uv run llm-service --help
uv run llm-service keys --help
uv run llm-service services --help

# Stop Docker services
docker compose -f deploy/docker/docker-compose.yml down

# Wipe the database and start fresh
docker compose -f deploy/docker/docker-compose.yml down -v
docker compose -f deploy/docker/docker-compose.yml up -d
alembic upgrade head
```

---

Ready to make your first real request? See the [Usage Guide](usage.md).