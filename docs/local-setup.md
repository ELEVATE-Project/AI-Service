# Local Setup

This guide gets the project running on your machine from scratch.

---

## Prerequisites

* macOS
* Homebrew installed
* Python 3.10
* Git

---

## 1. Install Python 3.10

```bash
brew install python@3.10
```

Verify installation:

```bash
python3.10 --version
```

---

## 2. Install uv and Set Up Virtual Environment

### Step 1: Install uv

Install uv using the official installer. Follow the instructions at:
https://docs.astral.sh/uv/getting-started/installation/

```bash
curl -LsSf https://astral.sh/uv/install.sh | sh
```

Restart your shell or run `source $HOME/.local/bin/env` to make `uv` available.

### Step 2: Go to the project directory

```bash
cd /path/to/ai-service
```

### Step 3: Create the virtual environment and install dependencies

`uv sync` will automatically create a `.venv` directory and install all dependencies:

```bash
uv sync
```

### Step 4: Activate the virtual environment

```bash
source .venv/bin/activate
```

---

## Step 3 — Install the spaCy language model

The Presidio PII guardrail requires a spaCy model. This is a data download, not a Python package, so `uv sync` does not fetch it automatically. Run this once:

```bash
uv run python -m spacy download en_core_web_lg
```

---

## Step 4 — Set Up Local PostgreSQL Database

### 4.1 Install PostgreSQL

Using Homebrew:

```bash
brew install postgresql@17
```

Start PostgreSQL:

```bash
brew services start postgresql@17
```

Verify it's running:

```bash
psql --version
```

---

### 4.2 Create the Database

Login to Postgres:

```bash
psql -d postgres
```

Create the database:

```sql
CREATE DATABASE llm_service;
```

Exit psql:

```sql
\q
```

---

### 4.3 Common Issues

**Postgres not starting**

```bash
brew services restart postgresql@17
```

**Port conflict**

```bash
lsof -i :5432
```

---

## Step 5 — Configure environment variables

Create a `.env` file in the project root:

```env
DB_URL=postgresql+asyncpg://postgres:postgres@localhost:5432/llm_service
REDIS_URL=redis://localhost:6379
LOG_LEVEL=INFO
PRICING_STALENESS_DAYS=7
SECRET_BACKEND=postgres

# Guardrails — all three disabled for minimal local dev
GUARDRAILS_PRESIDIO_ENABLED=false
GUARDRAILS_LLAMA_GUARD_ENABLED=false
GUARDRAILS_LLAMA_GUARD_MODEL=none
GUARDRAILS_LLAMA_GUARD_API_KEY=
GUARDRAILS_LLAMA_GUARD_API_BASE=
GUARDRAILS_SIZE_CAP_INPUT_CHARS=100000
GUARDRAILS_SIZE_CAP_OUTPUT_CHARS=50000

# Cache — set to 0 to disable caching during development
CACHE_TTL_SECONDS=0

# Retry behaviour
LLM_RETRY_MAX_ATTEMPTS=3
LLM_RETRY_BACKOFF_BASE_S=2.0

# Batch
BATCH_MAX_SUBMIT_ATTEMPTS=3

# OpenRouter app attribution (optional) — sent as HTTP-Referer / X-Title so
# usage is credited to your app in the OpenRouter dashboard. Per-request
# provider_options.referer / provider_options.title override these.
OPENROUTER_APP_URL=
OPENROUTER_APP_TITLE=
```

Every variable must be present — the app fails fast on any missing config. The two `OPENROUTER_*` variables are optional and may be omitted entirely.

---

## Step 6 — Run database migrations

```bash
alembic upgrade head
```

This applies all migration files from `src/shared/db/migrations/versions/` and creates the tables in Postgres.

---

## Step 7 — Register a tenant and provider key

Run the interactive setup script:

```bash
uv run python scripts/add_tenant_key.py
```

The script walks you through everything: creating a tenant, registering a calling service, and storing a provider API key. Here is what it asks and what to enter.

---

### Anthropic example

```
Tenant ID: saathi
Tenant display name [saathi]: Saathi

Grant a calling service access to 'saathi'? (y/n): y

  Service name (press Enter to create new): [press Enter]
  New service name: saathi_service

  !! Save this token now — it will not be shown again:
     svc_<generated-token>

  ✓ Access granted: saathi_service → saathi

Grant another calling service access to 'saathi'? (y/n): n

Add a provider key for 'saathi'? (y/n): y

  Provider (openai / azure / anthropic / bedrock / vertex_ai / groq / custom_endpoint): anthropic
  API key: <paste your Anthropic API key here>

  ✓ Key (anthropic / api_key) written.

Add another key for 'saathi'? (y/n): n
Add another tenant? (y/n): n
```

**Copy the bearer token** printed by the script and save it in your calling service's `.env` — it is shown exactly once and never stored in plain text.

---

### Bedrock example

```
Tenant ID: saathi
Tenant display name [saathi]: Saathi

Grant a calling service access to 'saathi'? (y/n): y

  Service name (press Enter to create new): [press Enter]
  New service name: saathi_service

  !! Save this token now — it will not be shown again:
     svc_<generated-token>

Grant another calling service access to 'saathi'? (y/n): n

Add a provider key for 'saathi'? (y/n): y

  Provider (openai / azure / anthropic / bedrock / vertex_ai / groq / custom_endpoint): bedrock
  AWS Access Key ID: <your-aws-access-key-id>
  AWS Secret Access Key: <your-aws-secret-access-key>
  AWS Region [us-east-1]: us-west-2
  AWS Session Token (press Enter to skip): [press Enter]
  AWS Role Name (press Enter to skip): [press Enter]
  S3 bucket name for batch inference (press Enter to skip): [press Enter]
  IAM Role ARN for batch inference (press Enter to skip): [press Enter]

  ✓ Key (bedrock / aws_credentials) written.

Add another key for 'saathi'? (y/n): n
Add another tenant? (y/n): n
```

You can skip all the optional Bedrock fields (session token, role name, S3 bucket, IAM role ARN) for regular chat and streaming. They are only needed if you plan to use the [Batch API](batch-api.md).

If you add both Anthropic and Bedrock keys for the same tenant, run the script once and answer `y` to "Add another key" after the first one.

---

### OpenRouter example

To add an OpenRouter key for a tenant, run:

```bash
uv run llm-service keys set --tenant=<tenant_id> --provider=openrouter --format=api_key --data='{"api_key":"<api_key>"}'
```

Replace `<tenant_id>` with your tenant ID (e.g. `saathi`) and `<api_key>` with your OpenRouter API key.

---

## Step 8 — Start the server

```bash
uv run uvicorn main:app --reload --port 8000
```

The server starts on `http://localhost:8000`. The `--reload` flag restarts automatically when you change code.

You should see:

```
INFO:     Uvicorn running on http://127.0.0.1:8000 (Press CTRL+C to quit)
INFO:     Started reloader process
```

---

## Step 9 — Verify everything works

Send a test request using the bearer token and tenant ID from Step 7:

Replace `<bearer-token>` with the token printed by the script, and `saathi` with your tenant ID:

```bash
curl --location 'http://localhost:8000/v1/chat/' \
  --header 'Authorization: Bearer <bearer-token>' \
  --header 'X-Tenant-Id: saathi' \
  --header 'Content-Type: application/json' \
  --data-raw '{
    "provider": "anthropic",
    "model": "claude-sonnet-4-5",
    "messages": [
        {
            "role": "system",
            "content": "You are a helpful assistant."
        },
        {
            "role": "user",
            "content": "Hi my name is Kunal and my email is kunal@example.com. Who is the president of India?"
        }
    ],
    "params": {
        "max_tokens": 50
    }
}'
```

A successful response looks like:

```json
{
  "id": "req_...",
  "object": "chat.completion",
  "tenant_id": "saathi",
  "provider": "anthropic",
  "model": "claude-sonnet-4-5",
  "choices": [{ "message": { "role": "assistant", "content": "..." }, "finish_reason": "stop" }],
  "usage": { "input_tokens": 80, "output_tokens": 50 },
  "cost": { "computed_usd": 0.0003 },
  "cache": { "our_cache_hit": false }
}
```

If you get `401` — check your bearer token matches what the script printed.
If you get `403` — check `X-Tenant-Id` matches the tenant ID you created.
If you get `422 missing_tenant_key` — re-run the script and add an Anthropic key for that tenant.

---

## Set Up Redis (Local, optional)

Redis is used for response caching. It is disabled by default (`CACHE_TTL_SECONDS=0`) so this step is only needed if you want to enable caching.

### Install Redis

```bash
brew install redis
```

### Start Redis Server

```bash
brew services start redis
```

### Verify Redis Is Running

```bash
redis-cli ping
```

Expected output:

```text
PONG
```

### Common Redis Issues

**Redis not running**

```bash
brew services restart redis
```

**Port already in use**

```bash
lsof -i :6379
```

---

## Useful commands

```bash
# Apply all pending migrations
alembic upgrade head

# Create a new migration
alembic revision --autogenerate -m "your migration message"

# Start the Arq background worker (needed for batch jobs)
uv run arq src.shared.queue.worker.WorkerSettings
```

---

Next: [Usage Guide](usage.md) — how to call non-streaming, streaming, and batch endpoints.
