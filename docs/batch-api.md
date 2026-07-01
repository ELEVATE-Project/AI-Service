# Batch API

The batch API lets you submit requests asynchronously and collect results later. Providers process them in bulk — OpenAI and Anthropic charge roughly half the normal price for batch jobs.

---

## When to use it

Use batch when you don't need an immediate response:

- Summarising a large set of documents overnight
- Running evals across hundreds of test cases
- Generating content for a queue that workers process asynchronously

Don't use batch when the user is waiting for a reply — use the regular chat or streaming endpoint instead.

---

## Eligible providers

| Provider | Supported |
|----------|-----------|
| `anthropic` | Yes |
| `bedrock` | Yes |
| `openai` | Yes (via LiteLLM) |
| Others | No — returns `422 provider_not_batch_eligible` |

---

## Step 1 — Submit a batch request

Add `"metadata": {"batch": true}` to any standard chat request:

```bash
curl -s -X POST http://localhost:8000/v1/chat \
  -H "Authorization: Bearer <bearer-token>" \
  -H "X-Tenant-Id: saathi" \
  -H "Content-Type: application/json" \
  -d '{
    "provider": "anthropic",
    "model": "claude-sonnet-4-5",
    "messages": [
      { "role": "user", "content": "Summarise the key points of the attached document." }
    ],
    "params": { "max_tokens": 500 },
    "metadata": { "batch": true }
  }'
```

The response is `202 Accepted` — not a `ChatResponse`:

```json
{
  "job_id": "xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx",
  "request_id": "req_abc123...",
  "status": "pending"
}
```

Save the `job_id`. You'll use it to poll for the result.

---

## Step 2 — Poll for the result

```bash
curl -s http://localhost:8000/v1/chat/batch/<job_id> \
  -H "Authorization: Bearer <bearer-token>" \
  -H "X-Tenant-Id: saathi"
```

**While the job is pending or submitted:**

```json
{
  "id": "xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx",
  "request_id": "req_abc123...",
  "status": "pending",
  "provider": "anthropic",
  "model": "claude-sonnet-4-5",
  "created_at": "2026-05-26T10:00:00Z",
  "completed_at": null,
  "result": null,
  "error_code": null
}
```

**When the job is complete:**

```json
{
  "id": "xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx",
  "request_id": "req_abc123...",
  "status": "complete",
  "provider": "anthropic",
  "model": "claude-sonnet-4-5",
  "created_at": "2026-05-26T10:00:00Z",
  "completed_at": "2026-05-26T11:23:45Z",
  "result": {
    "id": "req_abc123...",
    "choices": [
      {
        "index": 0,
        "message": {
          "role": "assistant",
          "content": "The document covers three key areas: ..."
        },
        "finish_reason": "stop"
      }
    ],
    "usage": { "input_tokens": 1200, "output_tokens": 150 },
    "cost": { "computed_usd": 0.0009 }
  },
  "error_code": null
}
```

The `result` field is a full `ChatResponse` — same shape as a non-streaming response.

**If the job failed:**

```json
{
  "status": "failed",
  "result": null,
  "error_code": "batch_failed"
}
```

---

## Job lifecycle

```
POST /v1/chat {batch: true}
        │
        ▼
  BatchJob created (status: pending)
        │
        ▼  ── up to 5 minutes ──
  Arq worker: batch_submit runs
        │
        ▼
  Jobs grouped by (tenant, provider)
  API key loaded from secrets store
  JSONL built and submitted to provider
        │
        ▼
  BatchJob updated (status: submitted, upstream_batch_id set)
        │
        ▼  ── minutes to hours ──
  Arq worker: batch_poll runs
        │
        ▼
  Provider batch status checked
  Results downloaded and parsed
        │
        ▼
  BatchJob updated (status: complete, result stored as JSONB)
        │
        ▼
  GET /v1/chat/batch/<job_id> returns full ChatResponse
```

---

## Background worker

Batch submission and polling are handled by an Arq background worker. Start it with:

```bash
uv run arq src.shared.queue.worker.WorkerSettings
```

The worker runs two cron jobs:
- `batch_submit` — every 5 minutes (`:00`, `:05`, `:10`, ...)
- `batch_poll` — every 5 minutes, offset by 1 minute (`:01`, `:06`, `:11`, ...)

The offset prevents polling from running before submission has updated the `upstream_batch_id`.

---

## Provider-specific behaviour

### Anthropic

- No file upload step — requests are sent directly to `client.messages.batches.create()`.
- Results are streamed when `processing_status == "ended"`.
- Typical completion time: 1 minute to 24 hours.
- `max_tokens` defaults to `1024` if not specified (Anthropic requires it).

### Bedrock

- Input is uploaded as a JSONL file to S3, then a model invocation job is created.
- Results are downloaded from S3 when the job status is `Completed`.
- Requires an IAM role ARN and S3 bucket in the tenant key (configured via `scripts/add_tenant_key.py`).
- Supports `anthropic.*`, `meta.llama*`, and `amazon.titan*` model families.

### OpenAI (via LiteLLM)

- Input is uploaded as a file via the OpenAI Files API, then a batch is created.
- Results are downloaded from the output file when `batch.status == "completed"`.
- Completion window is 24 hours.

---

## What happens before submission

Before a batch job is queued, the request goes through the same pre-flight as a regular request:

1. Auth + tenant resolution
2. Request normalisation (provider + model validation)
3. Policy check (model allowlist, budget)
4. Guardrails — input check (PII redaction, safety)
5. Cache lookup — if cached, returns immediately with no job created
6. Batch detection → creates `BatchJob` row → returns `202`

The normalised (guardrail-cleaned) request is stored in the `BatchJob` row. The worker submits exactly what the guardrails approved.

---

## Tenant isolation

The polling endpoint (`GET /v1/chat/batch/<job_id>`) checks that the resolved tenant owns the job. If a job with that ID exists but belongs to a different tenant, the response is `404` — not `403`. This prevents leaking information about whether a job ID exists.

---

See [Database Models](models.md) for the full `batch_jobs` table schema.
