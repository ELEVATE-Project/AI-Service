# shared/guardrails — input/output filter chain

Applies to text crossing the AI boundary in **both modules**:
- **LLM**: user messages in, model responses out
- **Voice (STT)**: transcribed text out (PII could be in transcribed speech)
- **Voice (TTS/translation)**: text being sent for synthesis or translation

## Files to create

- `base.py` — `GuardrailFilter` interface:
  ```python
  async def filter_input(texts: list[str]) -> FilterResult: ...
  async def filter_output(text: str) -> FilterResult: ...  # called per chunk for streaming
  ```
  `FilterResult`: `blocked: bool`, `redactions: list[str]`, `flags: list[str]`

- `presidio.py` — `PresidioFilter` (Microsoft Presidio):
  - PII detection + redaction on input and output text
  - Configurable entity types: email, phone, name, Aadhaar number, PAN card, etc.
  - Indian PII recognisers are a priority given the govt customer base
  - Redacted entities recorded as `pii.<entity_type>` in `guardrail_flags`

- `llama_guard.py` — `LlamaGuardFilter` (Meta Llama-Guard):
  - Safety classification on input and output text
  - Configurable per category: block vs annotate
  - For voice: runs on transcribed text (STT output) and text being synthesized (TTS input)
  - Classifications recorded in `guardrail_flags`

- `size_caps.py` — `SizeCapFilter`:
  - LLM: reject if input exceeds max token cap before hitting upstream
  - Voice: reject if audio exceeds max duration, or text exceeds max character cap
  - Separate caps for system prompt, user turn, total context (LLM-specific)

## Ledger integration

All fired guardrail results → `ledger_entries.guardrail_flags` (JSONB) + response `guardrails` block:
```json
{"input_flags": [], "output_flags": [], "redactions_applied": ["pii.phone"]}
```

## Phase

Phase 3
