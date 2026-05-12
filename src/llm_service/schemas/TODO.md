# llm_service/schemas — LLM Pydantic DTOs

API boundary types for the LLM module. Import shared envelope sub-models from `shared/schemas/envelope.py`
rather than redefining them here.

## Files to create

- `chat.py`
  - `ChatRequest`: `model`, `messages[]`, `tools[]` (optional), `stream` (bool),
    `cache_policy` ("auto"|"explicit"|"off"), `cache_segments` hint
  - `Message`: `role`, `content`, `cache` hint (optional)
  - `ChatResponse`: `id`, `object`, `created`, `tenant_id`, `provider`, `model`, `transport`, `region`,
    `choices[]`, `usage: UsageBlock`, `cost: CostBlock`, `latency_ms: LatencyBlock`,
    `cache: CacheBlock`, `guardrails: GuardrailsBlock`, `policy: PolicyBlock`, `provider_raw`
    (all from `shared/schemas/envelope.py`)

- `stream.py`
  - `StreamToken`: `{"type": "token", "data": {"index": int, "delta": str}}`
  - `StreamToolUse`: `{"type": "tool_use", "data": {"id", "name", "arguments_delta"}}`
  - `StreamUsage`: `{"type": "usage", "data": UsageBlock}`
  - `StreamFinish`: `{"type": "finish", "data": <same tail as ChatResponse minus choices>}`
  - `StreamError`: `{"type": "error", "data": {"code", "message", "upstream_status"}}`

- `embed.py`
  - `EmbedRequest`: `model`, `input` (str or list[str]), `encoding_format`
  - `EmbedResponse`: `data[]` (index + embedding vector), `usage: UsageBlock`, `cost: CostBlock`, `latency_ms: LatencyBlock`

## Hard invariant

All fields in `ChatResponse` and `StreamFinish` are **required**.
Never make them `Optional` to "simplify" — callers depend on the complete envelope.

## Phase

Phase 1
