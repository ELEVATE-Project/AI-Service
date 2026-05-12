# voice/providers — voice provider adapters

## Files to create

- `base.py` — `BaseVoiceProvider` interface:
  ```python
  async def transcribe(audio: bytes, language: str | None, key: str) -> TranscribeResult: ...
  async def synthesize(text: str, voice_id: str, format: str, key: str) -> SynthesizeResult: ...
  async def translate(text: str, source_lang: str, target_lang: str, key: str) -> TranslateResult: ...
  async def transliterate(text: str, source_script: str, target_script: str, key: str) -> TransliterateResult: ...
  ```
  Not every provider supports all methods — raise `UnsupportedCapabilityError` if not.

- `registry.py` — voice routing table:
  ```python
  RoutingKey = tuple[str, str]  # (provider, capability)
  # e.g. ("bhashini", "transcribe"), ("google", "synthesize")
  ```
  Maps to a provider class. Default per capability configurable in `shared/config.py`.

- `bhashini.py` — `BhashiniProvider`:
  - Indian govt Bhashini API (22 scheduled Indian languages)
  - STT, TTS, translation, transliteration
  - Auth: per-tenant key via `shared/secrets` (or shared Gritworks key — TBD)

- `sarvam.py` — `SarvamProvider`:
  - Sarvam AI — high-quality Indic language STT/TTS
  - Auth: per-tenant key via `shared/secrets`

- `google.py` — `GoogleVoiceProvider`:
  - Google Cloud STT (`speech_v1`) + TTS (`texttospeech_v1`)
  - Auth: service account key via `shared/secrets`

## Hard invariant

New voice provider = **one file here + one registry entry**. Same rule as `llm_service/providers/`.

## Phase

Phase 4
