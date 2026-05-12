# voice/api — voice HTTP endpoints

## Files to create

- `deps.py` — FastAPI dependencies wiring shared infrastructure into voice routes:
  ```python
  async def get_tenant(request: Request) -> Tenant: ...
  async def get_policy(tenant: Tenant) -> PolicyRegistry: ...
  async def get_guardrails() -> GuardrailChain: ...
  async def get_db() -> AsyncSession: ...
  ```

- `transcribe.py` — `POST /v1/voice/transcribe`:
  - Request: multipart audio file + `language` (optional) + `provider` hint (optional)
  - Response: `TranscribeResponse` — text, detected language, `usage`, `cost`, `latency_ms`, `guardrails`, `policy`
  - Guardrails: PII redaction on transcribed text output

- `synthesize.py` — `POST /v1/voice/synthesize`:
  - Request: `text`, `voice_id`, `format` (wav/mp3/ogg), `provider` hint (optional)
  - Response: binary audio stream + `cost`, `latency_ms`, `guardrails`, `policy` in headers or SSE finish event
  - Guardrails: safety + PII check on input text before sending to provider

- `translate.py` — `POST /v1/voice/translate`:
  - Request: `text`, `source_language`, `target_language`, `provider` hint
  - Response: translated text + full metadata envelope

- `transliterate.py` — `POST /v1/voice/transliterate`:
  - Request: `text`, `source_script`, `target_script`, `provider` hint
  - Response: transliterated text + full metadata envelope

## Phase

Phase 4
