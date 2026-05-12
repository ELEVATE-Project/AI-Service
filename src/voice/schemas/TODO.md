# voice/schemas — voice Pydantic DTOs

API boundary types for the voice module. Import shared envelope sub-models from
`shared/schemas/envelope.py` rather than redefining them here.

## Files to create

- `transcribe.py`
  - `TranscribeRequest`: `audio` (bytes/upload), `language` (optional BCP-47), `provider` hint
  - `TranscribeResponse`: `text`, `language_detected`, `confidence` (optional),
    `usage: UsageBlock` (audio_duration_ms populated), `cost: CostBlock`,
    `latency_ms: LatencyBlock`, `guardrails: GuardrailsBlock`, `policy: PolicyBlock`

- `synthesize.py`
  - `SynthesizeRequest`: `text`, `voice_id`, `format` ("wav"|"mp3"|"ogg"), `speed` (optional), `provider` hint
  - `SynthesizeResponse`: `audio_url` or `audio_b64`, `format`,
    `usage: UsageBlock` (character_count populated), `cost: CostBlock`,
    `latency_ms: LatencyBlock`, `guardrails: GuardrailsBlock`, `policy: PolicyBlock`

- `translate.py`
  - `TranslateRequest`: `text`, `source_language`, `target_language`, `formality` (optional), `provider` hint
  - `TranslateResponse`: `translated_text`, `usage: UsageBlock` (character_count), `cost: CostBlock`, `latency_ms: LatencyBlock`, `guardrails: GuardrailsBlock`, `policy: PolicyBlock`

- `transliterate.py`
  - `TransliterateRequest`: `text`, `source_script`, `target_script`, `provider` hint
  - `TransliterateResponse`: `transliterated_text`, `usage: UsageBlock`, `cost: CostBlock`, `latency_ms: LatencyBlock`

## Phase

Phase 4
