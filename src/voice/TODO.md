# voice — voice AI module

Peer module to `llm_service`. Handles STT (speech-to-text), TTS (text-to-speech),
translation, and transliteration via third-party voice AI providers.

Callers hit `/v1/voice/*` endpoints and get the same metadata envelope as LLM calls:
cost, latency, guardrails, policy — all from `shared/`.

## Imports pattern

```python
from shared.config import settings
from shared.auth import get_tenant
from shared.secrets import SecretBackend
from shared.ledger import LedgerWriter
from shared.policy import PolicyRegistry
from shared.guardrails import GuardrailChain
from shared.schemas.envelope import CostBlock, UsageBlock, ...
```

## Structure

| Module | Purpose |
|--------|---------|
| `api/` | FastAPI routers for voice endpoints (STT, TTS, translate, transliterate) |
| `normaliser.py` | Converts incoming voice request → provider-agnostic internal schema (language codes, audio formats, etc.) |
| `providers/` | `BaseVoiceProvider` interface + Bhashini, Sarvam, Google adapters |
| `schemas/` | Voice-specific Pydantic DTOs: `TranscribeRequest/Response`, `SynthesizeRequest/Response`, etc. |

## Request pipeline (every voice call)

```
API (schemas/TranscribeRequest or SynthesizeRequest)
  → shared/auth          resolve tenant
  → shared/policy        check rate limit + budget
  → shared/guardrails    filter input text (TTS/translation: PII, safety)
  → voice/normaliser     normalise to provider-agnostic voice schema
  → voice/providers      route to Bhashini / Sarvam / Google
  → shared/guardrails    filter output text (STT: PII redaction on transcription)
  → shared/ledger        write ledger row (duration_ms, character_count, cost)
  → API response
```

## Open question (resolve before Phase 4)

Per-tenant BYOK vs shared Gritworks-negotiated keys for Bhashini/Sarvam?
Govt customers likely require their own keys; commercial may prefer shared.
See `docs/architecture.md` → Open questions.

## Phase

Phase 4
