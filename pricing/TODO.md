# pricing — provider model pricing data

## File to create

`models.yaml` — YAML keyed by `provider/model`:

```yaml
pricing_version: 1   # bump this integer on every change

models:
  openai/gpt-4o:
    input_rate_per_1k_tokens: 0.0025
    output_rate_per_1k_tokens: 0.0100
    cache_write_rate_per_1k_tokens: 0.0031   # ~25% premium
    cache_read_rate_per_1k_tokens: 0.00125   # ~50% discount
    last_updated: "2025-05-07"
    source_url: "https://openai.com/api/pricing"

  anthropic/claude-sonnet-4-6:
    input_rate_per_1k_tokens: 0.003
    output_rate_per_1k_tokens: 0.015
    cache_write_rate_per_1k_tokens: 0.00375
    cache_read_rate_per_1k_tokens: 0.0003
    last_updated: "2025-05-07"
    source_url: "https://www.anthropic.com/pricing"

  # ... add all models for: openai, anthropic, bedrock/* anthropic models,
  #     groq/*, llama/* (self-hosted: set rates to 0 or infra cost)
```

## Models to cover (minimum for Phase 1)

- OpenAI: `gpt-4o`, `gpt-4o-mini`, `o1`, `o3-mini`, `text-embedding-3-small`, `text-embedding-3-large`
- Anthropic: `claude-opus-4-7`, `claude-sonnet-4-6`, `claude-haiku-4-5`
- Bedrock (Anthropic): `anthropic.claude-sonnet-4-6`, `anthropic.claude-haiku-4-5`
- Groq: `llama-3.3-70b-versatile`, `mixtral-8x7b-32768`
- Llama (self-hosted): rates = 0 (or infra cost per token)

## CI check (Phase 1)

Add a CI step that fails the build if any model's `last_updated` is older than the
configured staleness window (recommended: 30 days). Script lives in `scripts/check_pricing_freshness.py`.

## Hard invariant

Every change to this file **must** bump `pricing_version`. The version is recorded in every
ledger row so historical costs can be recomputed when rates change.
