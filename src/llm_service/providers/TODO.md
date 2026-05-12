# llm_service/providers — LLM transport layer

## Files to create

Resolved during eng review (2026-05-07): Phase 1 ships **4 adapter files** covering 7 routes. The OpenAI-compatible transport handles OpenAI proper plus both HF flavors via `base_url` configuration — same wire format, different endpoint sources.

- `base.py` — `BaseLLMProvider` interface:
  ```python
  async def chat(request: NormalisedLLMRequest, key: TenantKeyPayload) -> ChatResponse: ...
  async def stream(request: NormalisedLLMRequest, key: TenantKeyPayload) -> AsyncIterator[StreamEvent]: ...
  async def embed(request: NormalisedEmbedRequest, key: TenantKeyPayload) -> EmbedResponse: ...
  ```
  `TenantKeyPayload` is the decoded blob from `shared/db.TenantKey.encrypted_payload` — shape varies by `key_format` (api_key / aws_credentials / endpoint_pair).

- `registry.py` — routing table (keep readable, not magic):
  ```python
  RoutingKey = tuple[str, str, frozenset[str]]  # (provider, model, feature_set)

  # Default: every (provider, model, *) → LiteLLMTransport (the gravity well)
  # Overrides flip specific routes to direct adapters.
  overrides = {
      ("openai",         "*", "*"): OpenAICompatibleTransport,  # direct OpenAI
      ("anthropic",      "*", "*"): AnthropicTransport,         # direct api.anthropic.com
      ("bedrock",        "*", "*"): BedrockTransport,           # Anthropic + Llama on Bedrock
      ("hf_endpoint",    "*", "*"): OpenAICompatibleTransport,  # tenant-supplied URL+token
      ("hf_self_hosted", "*", "*"): OpenAICompatibleTransport,  # gritworks GPU infra
  }
  def resolve(provider, model, features) -> type[BaseLLMProvider]: ...
  ```
  Model glob `"*"` matches any model string for that provider.

  Kill switch: if LiteLLM has a known gap on a specific (provider, model, feature) route, the override table flips that route to its direct adapter without code change — `BaseLLMProvider` interface is identical across LiteLLM and direct paths.

- `litellm.py` — `LiteLLMTransport` (Phase 1):
  - The gravity well: any future provider added without an override falls back to LiteLLM
  - Wraps the LiteLLM Python SDK in-process: `litellm.acompletion`, `litellm.aembedding`, async streaming generators
  - Tenant BYOK key + `api_base` (and AWS creds where relevant) passed as per-call kwargs — never sets `litellm.api_key` globally (would defeat BYOK)
  - Forwards `cache_control` blocks (Anthropic) and reads OpenAI `usage.prompt_tokens_details.cached_tokens` from the SDK response
  - Translates routing-table region lists into per-call `fallbacks=[...]`
  - **Spike before Phase 1 lands**: send a Llama-with-tool-calls request through the SDK (both directly and via Bedrock-routed model strings); verify Anthropic `cache_control` and OpenAI `cached_tokens` survive the SDK round-trip. Phase 1 adapter list does not depend on LiteLLM for any specific route — it's the safety net for future providers.

- `openai_compatible.py` — `OpenAICompatibleTransport` (Phase 1):
  - Single adapter, three registry entries — handles **OpenAI proper, HF Inference Endpoints, HF Self-hosted TGI/vLLM**
  - Reads `base_url` and token-source from per-route config:
    - `openai/*`: `base_url=https://api.openai.com/v1`, token from `key_format=api_key` payload
    - `hf_endpoint/*`: `base_url` and token from `key_format=endpoint_pair` payload (per-tenant)
    - `hf_self_hosted/*`: `base_url=settings.hf_internal_url`, no per-tenant token (Gritworks GPU infra is internal-network only)
  - All three flavors are OpenAI wire-format compatible (TGI/vLLM expose `/v1/chat/completions`)
  - Adding Groq/Together/Fireworks later = one registry line, no new file

- `anthropic.py` — `AnthropicTransport` (Phase 1):
  - Direct `api.anthropic.com` via the official `anthropic` SDK
  - Injects `cache_control: {"type": "ephemeral"}` on system prompt + tool defs when cache threshold met (1024 tokens for Sonnet/Haiku, 2048 for Opus)
  - For tenants who prefer api.anthropic.com over Bedrock; `key_format=api_key`

- `bedrock.py` — `BedrockTransport` (Phase 1):
  - AWS Bedrock via `boto3` — handles **Anthropic Claude models AND Llama models** (no LiteLLM gap on tool_calls because Bedrock handles Llama natively)
  - `key_format=aws_credentials`; supports STS-issued credentials
  - Failure paths: STS expiry → `502 tenant_key_rejected` with `code: aws_credentials_expired`; provider rate-limit → `502 upstream_rate_limited` with `Retry-After` passthrough
  - Same `cache_control` pass-through for Anthropic models on Bedrock as direct Anthropic

## Hard invariant

New LLM provider = **one file here + one registry entry** (or, for OpenAI-compatible providers, just one registry entry pointing at the existing `OpenAICompatibleTransport`). No provider logic anywhere else.

## Phase

Phase 1: all 4 adapter files + `base.py` + `registry.py`. LiteLLM, OpenAI-compatible, Anthropic direct, Bedrock direct land together.
