# Guardrails

Guardrails inspect every message going into and every response coming out of an LLM, redacting PII and blocking unsafe content before either side ever sees it.

---

## Why guardrails exist

Without guardrails, a user could send a message containing a phone number or Aadhaar number, and that PII goes straight to an external LLM provider. Similarly, a model could return harmful content that your calling service then forwards to an end user.

Guardrails intercept at two points:

- **Input** — before the request is sent upstream. Runs after auth and policy, before cache lookup and the LLM call.
- **Output** — after the LLM responds. Runs before the response is returned to the caller.

If PII is found, it is redacted in place and the cleaned version goes upstream / back to the caller. If unsafe content is found, the request is blocked with `400 guardrails_blocked`.

---

## The three checkers

Three independent checkers exist. They are composed into a chain and run in this order:

| Order | Checker | What it does | Blocks? |
|-------|---------|--------------|---------|
| 1 | `SizeCapsGuardrails` | Rejects input that exceeds the configured character limit | Yes — input only |
| 2 | `PresidioGuardrails` | Detects and redacts PII using Microsoft Presidio | No — redacts and passes through |
| 3 | `LlamaGuardChecker` | Classifies content for safety using Meta Llama-Guard | Yes — on any S1–S13 violation |

Size caps run first because they are free (a single `sum()`). Presidio runs next because it is in-process and fast. Llama-Guard runs last because it is an LLM call and expensive.

Each checker is independent. You can enable any combination via config. If all three are disabled, a `StubGuardrails` pass-through is used and guardrail overhead is zero.

---

## What `GuardrailsResult` carries

Every checker returns a `GuardrailsResult`:

```python
@dataclass
class GuardrailsResult:
    blocked: bool = False
    flags: list[str] = field(default_factory=list)
    redactions: list[str] = field(default_factory=list)
    modified_messages: list[dict] | None = None
    modified_content: str | None = None
```

| Field | What it means |
|-------|--------------|
| `blocked` | `True` means stop the request immediately, return `400 guardrails_blocked` |
| `flags` | What was detected. Examples: `pii.email_address`, `pii.aadhaar_number`, `safety.hate` |
| `redactions` | What was removed. Usually matches `flags` when PII was actually anonymised |
| `modified_messages` | The full input message list with PII replaced. `None` if nothing changed |
| `modified_content` | The output string with PII replaced. `None` if nothing changed |

`modified_messages` and `modified_content` being `None` means "use the original". The chain only substitutes them when a checker actually changed something. This keeps a clean path for the common case where nothing is found.

---

## The chain

When more than one checker is enabled, they are wrapped in a `GuardrailsChain`. The chain passes results from one checker into the next.

For input:

```
messages (original)
      │
      ▼
 SizeCapsGuardrails.check_input()
      │ blocked → 400 guardrails_blocked
      │ pass ↓
 PresidioGuardrails.check_input()
      │ blocked → 400 guardrails_blocked (rare — Presidio redacts, not blocks)
      │ modified_messages → next checker sees the REDACTED messages, not originals
      │ pass ↓
 LlamaGuardChecker.check_input()
      │ blocked → 400 guardrails_blocked
      │ pass ↓
 normalised.messages = cleaned messages
 → sent to LLM
```

The key detail in the middle: if Presidio redacts PII from a message, Llama-Guard runs on the **already-redacted** version. Llama-Guard never sees raw PII.

For output, the same pattern applies but with a single string flowing through each `check_output()`.

---

## Checker 1: Size caps

Size caps exist to catch requests that are impractically large before anything else runs.

```python
# src/shared/guardrails/size_caps.py

async def check_input(self, messages):
    total = sum(len(msg.get("content") or "") for msg in messages)
    if total > self._max_input_chars:
        return GuardrailsResult(blocked=True, flags=["size.input_too_large"])
```

It sums the character lengths of all `content` fields across all messages. If the total exceeds `GUARDRAILS_SIZE_CAP_INPUT_CHARS`, the request is blocked before Presidio or Llama-Guard run.

For output, it flags but does not block:

```python
async def check_output(self, content):
    if len(content) > self._max_output_chars:
        return GuardrailsResult(flags=["size.output_too_large"])
```

The output is not blocked because the LLM has already run and the token cost is already incurred. Blocking it would throw away a completed response. The flag appears in the ledger so you can monitor it.

**Example — input blocked:**

Request with combined message content of 120,000 characters when the limit is 100,000:

```json
{
  "guardrails": {
    "input_flags": ["size.input_too_large"],
    "output_flags": [],
    "redactions_applied": []
  }
}
```

Actually, this never reaches the response — the request returns `400` with `"detail": "guardrails_blocked"`.

---

## Checker 2: Presidio (PII)

Presidio is Microsoft's open-source PII detection and anonymisation engine. It finds PII spans in text and replaces them with placeholder tokens like `<EMAIL_ADDRESS>` or `<PHONE_NUMBER>`.

### How Presidio works internally

Presidio uses two mechanisms:

1. **Regex pattern recognisers** — fast, for structured PII like email addresses, phone numbers, credit card numbers, IBANs, IP addresses. These are built into Presidio.
2. **spaCy NER (Named Entity Recognition)** — a neural NLP model that finds `PERSON` and `LOCATION` entities. This is why spaCy must be installed separately (`python -m spacy download en_core_web_sm`).

### Entities the gateway detects

| Entity | Examples |
|--------|---------|
| `EMAIL_ADDRESS` | `hello@example.com` |
| `PHONE_NUMBER` | `+91-9876543210`, `(022) 1234-5678` |
| `PERSON` | Names detected via spaCy NER |
| `LOCATION` | Addresses detected via spaCy NER |
| `CREDIT_CARD` | `4111 1111 1111 1111` |
| `IBAN_CODE` | `GB29 NWBK 6016 1331 9268 19` |
| `IP_ADDRESS` | `192.168.1.1` |
| `US_SSN` | `123-45-6789` |
| `AADHAAR_NUMBER` | `1234 5678 9012`, `123456789012` |
| `PAN_NUMBER` | `ABCDE1234F` |

The last two — Aadhaar and PAN — are Indian government ID formats and are added as custom pattern recognisers at startup:

```python
# src/shared/guardrails/presidio.py

aadhaar = PatternRecognizer(
    supported_entity="AADHAAR_NUMBER",
    patterns=[Pattern("AADHAAR_NUMBER", r"\b\d{4}\s?\d{4}\s?\d{4}\b", 0.85)],
)
pan = PatternRecognizer(
    supported_entity="PAN_NUMBER",
    patterns=[Pattern("PAN_NUMBER", r"\b[A-Z]{5}[0-9]{4}[A-Z]\b", 0.90)],
)
```

The score (0.85, 0.90) is a confidence threshold. Presidio only reports matches above 0.5 by default, so these are set conservatively high to avoid false positives.

### What redaction looks like

Original message:
```
"My email is kunal@example.com and my Aadhaar is 1234 5678 9012. Please help."
```

After Presidio:
```
"My email is <EMAIL_ADDRESS> and my Aadhaar is <AADHAAR_NUMBER>. Please help."
```

This redacted version is what the LLM sees. The caller receives the LLM's response to the cleaned input.

### Async execution

Presidio's analyzer is synchronous (spaCy runs sync inference). Calling it directly in an async handler would block the event loop and freeze other requests on the same worker. The gateway wraps each Presidio call in `asyncio.to_thread`:

```python
result = await asyncio.to_thread(self._check_text, content)
```

This offloads the CPU-bound work to a thread pool, keeping the event loop free.

### Input: per-message iteration

For input, Presidio runs on each message separately:

```python
for index, message in enumerate(messages):
    content = message.get("content")
    if not content:
        continue       # skip tool messages, None content, empty turns
    result = await asyncio.to_thread(self._check_text, content)
    if result.modified_content is not None:
        modified_messages[index] = {**message, "content": result.modified_content}
```

Messages without text content (tool call results, assistant messages that only contain `tool_calls`) are skipped. Only messages with a non-empty `content` string are scanned.

### Output: per-chunk in streaming

For streaming, `check_output_chunk` is called on each token chunk as it arrives. Presidio is fast enough (regex is microseconds) to run per-chunk without adding noticeable latency. This means PII is redacted in real time with no leak window — the caller never receives a chunk containing raw PII.

```python
async def check_output_chunk(self, chunk: str) -> GuardrailsResult:
    return await self.check_output(chunk)
```

### Example response with PII redaction

```json
{
  "choices": [
    {
      "message": {
        "role": "assistant",
        "content": "I found the record for <EMAIL_ADDRESS>. Their account is active."
      }
    }
  ],
  "guardrails": {
    "input_flags": ["pii.email_address"],
    "output_flags": ["pii.email_address"],
    "redactions_applied": ["pii.email_address", "pii.email_address"]
  }
}
```

`redactions_applied` can have duplicates when the same entity type appears on both input and output.

---

## Checker 3: Llama-Guard (safety)

Llama-Guard is a Meta language model fine-tuned specifically to classify conversations for safety. It is not a general-purpose LLM — its only output is `safe` or `unsafe` with category codes.

### Why a model and not rules

Rule-based safety filters are brittle. They catch explicit keywords but miss context. Llama-Guard understands nuance: a message about handling weapons in a historical context is different from a message requesting instructions for harm. A fine-tuned classifier generalises far better than a keyword list.

### The 13 safety categories

| Code | Category |
|------|----------|
| S1 | Violent Crimes |
| S2 | Non-Violent Crimes |
| S3 | Sex-Related Crimes |
| S4 | Child Sexual Exploitation |
| S5 | Defamation |
| S6 | Specialized Advice (unqualified legal, medical, financial) |
| S7 | Privacy |
| S8 | Intellectual Property |
| S9 | Indiscriminate Weapons (CBRN) |
| S10 | Hate |
| S11 | Suicide & Self-Harm |
| S12 | Sexual Content |
| S13 | Elections |

### How the prompt is built

Llama-Guard 3 expects a specific prompt format. The gateway constructs it from the conversation:

```
Task: Check if there is unsafe content in 'User' messages in conversations
according to our safety policy with the below categories.

<BEGIN UNSAFE CONTENT CATEGORIES>
S1: Violent Crimes.
S2: Non-Violent Crimes.
... (all 13)
<END UNSAFE CONTENT CATEGORIES>

<BEGIN CONVERSATION>

User: How do I make a bomb?

<END CONVERSATION>

Provide your safety assessment for ONLY THE LAST User turn in the above conversation:
 - First line must read 'safe' or 'unsafe'.
 - If unsafe, a second line must include a comma-separated list of violated categories.
```

For output checks, the same structure is used but with `Agent:` instead of `User:` and the response text.

### Parsing the model's response

```python
def _parse_response(response_text: str) -> GuardrailsResult:
    lines = response_text.strip().splitlines()
    first = lines[0].strip().lower()
    if first == "safe":
        return GuardrailsResult()
    raw_codes = lines[1].split(",") if len(lines) > 1 else []
    flags = [f"safety.{_CATEGORY_MAP.get(code.strip(), code.strip().lower())}" ...]
    return GuardrailsResult(blocked=bool(flags), flags=flags)
```

`safe` → empty result, passes through. `unsafe\nS1,S10` → `blocked=True`, `flags=["safety.violent_crimes", "safety.hate"]`.

### The service-level API key

Llama-Guard calls use a **service-level key** configured in settings (`GUARDRAILS_LLAMA_GUARD_API_KEY`), not the tenant's BYOK key. Tenant keys are for billable LLM requests on the tenant's behalf. Llama-Guard is infrastructure — it runs on the gateway's own credentials regardless of which tenant the request is for.

### Streaming: completion-only

For streaming responses, Llama-Guard does **not** run per-chunk. Running a full LLM inference call on every token chunk would add N inference calls to a stream of N tokens — a significant performance and cost cliff. Instead, `check_output_chunk` returns an empty result, and `check_output` is called once when the full response is assembled.

```python
async def check_output_chunk(self, chunk: str) -> GuardrailsResult:
    # Llama-Guard runs at completion only; per-chunk calls are a perf cliff.
    return GuardrailsResult()
```

This means there is a bounded safety leak window during streaming: unsafe content could be partially emitted before Llama-Guard fires at the end. Presidio (which does run per-chunk) covers the PII side in real time.

### Example — blocked for safety

A request where the user asks for instructions to cause harm:

The request never reaches the LLM. The gateway returns:

```
HTTP 400 Bad Request

{
  "detail": "guardrails_blocked"
}
```

The ledger row records `status: error`, `error_code: guardrails_blocked`, and `guardrail_flags: {"input_flags": ["safety.violent_crimes"]}`.

---

## Configuration

All guardrail behaviour is controlled by environment variables in `.env`. None have defaults — every variable must be explicitly set.

| Variable | Type | What it controls |
|----------|------|-----------------|
| `GUARDRAILS_PRESIDIO_ENABLED` | `bool` | Enable PII detection and redaction |
| `GUARDRAILS_LLAMA_GUARD_ENABLED` | `bool` | Enable safety classification |
| `GUARDRAILS_LLAMA_GUARD_MODEL` | `str` | Model identifier, e.g. `meta.llama-guard-3-8b-instruct-v1:0` for Bedrock |
| `GUARDRAILS_LLAMA_GUARD_API_KEY` | `str` or empty | Service-level API key for the Llama-Guard model. Leave blank for IAM-based auth (Bedrock) |
| `GUARDRAILS_LLAMA_GUARD_API_BASE` | `str` or empty | Custom endpoint URL. Leave blank for default provider base URL |
| `GUARDRAILS_SIZE_CAP_INPUT_CHARS` | `int` | Max total input characters across all messages. Set to `0` to disable |
| `GUARDRAILS_SIZE_CAP_OUTPUT_CHARS` | `int` | Max output characters to flag (not block) |

**Minimal dev setup (Presidio only, Llama-Guard off):**

```bash
GUARDRAILS_PRESIDIO_ENABLED=true
GUARDRAILS_LLAMA_GUARD_ENABLED=false
GUARDRAILS_LLAMA_GUARD_MODEL=meta.llama-guard-3-8b-instruct-v1:0
GUARDRAILS_LLAMA_GUARD_API_KEY=
GUARDRAILS_LLAMA_GUARD_API_BASE=
GUARDRAILS_SIZE_CAP_INPUT_CHARS=100000
GUARDRAILS_SIZE_CAP_OUTPUT_CHARS=50000
```

**Everything off (stub mode):**

```bash
GUARDRAILS_PRESIDIO_ENABLED=false
GUARDRAILS_LLAMA_GUARD_ENABLED=false
GUARDRAILS_LLAMA_GUARD_MODEL=none
GUARDRAILS_LLAMA_GUARD_API_KEY=
GUARDRAILS_LLAMA_GUARD_API_BASE=
GUARDRAILS_SIZE_CAP_INPUT_CHARS=0
GUARDRAILS_SIZE_CAP_OUTPUT_CHARS=0
```

When all three are effectively disabled (Presidio off, Llama-Guard off, size cap 0), the factory returns a `StubGuardrails` with zero overhead.

---

## How the factory assembles the chain

The factory in `src/llm_service/api/deps.py` reads settings at first request, builds the chain, and caches it for the lifetime of the process:

```python
@functools.lru_cache(maxsize=1)
def _build_guardrails() -> GuardrailsChecker:
    checkers = []
    if settings.guardrails_size_cap_input_chars > 0:
        checkers.append(SizeCapsGuardrails(...))    # cheapest first
    if settings.guardrails_presidio_enabled:
        checkers.append(PresidioGuardrails())       # loads spaCy here
    if settings.guardrails_llama_guard_enabled:
        checkers.append(LlamaGuardChecker(...))     # most expensive last
    if not checkers:
        return StubGuardrails()
    if len(checkers) == 1:
        return checkers[0]                          # no chain overhead when only one
    return GuardrailsChain(checkers)
```

`lru_cache(maxsize=1)` means this runs exactly once. spaCy is loaded once at cold start, not on every request. A second call just returns the already-built instance.

---

## Where guardrails sit in the request pipeline

```
POST /v1/chat
      │
      ▼
 Auth + Tenant resolution          ← 401/403 if token invalid
      │
      ▼
 Request normalisation
      │
      ▼
 Policy check                      ← 429 if over budget/rate/model limit
      │
      ▼
 ► Guardrails — check_input()      ← 400 if blocked; redacted messages substituted
      │
      ▼
 Cache lookup
      │
      ▼
 LLM call (upstream)
      │
      ▼
 ► Guardrails — check_output()     ← 400 if blocked; redacted content substituted
      │
      ▼
 Ledger write
      │
      ▼
 Return ChatResponse
```

Guardrails run **after** policy (no point scanning PII on a request you'd reject for budget anyway) and **before** the cache lookup (the cached value should be the clean version, not the original with PII).

---

## The guardrails block in every response

Every `ChatResponse` includes a `guardrails` block regardless of whether anything fired:

```json
{
  "guardrails": {
    "input_flags": [],
    "output_flags": [],
    "redactions_applied": []
  }
}
```

When PII was found and redacted:

```json
{
  "guardrails": {
    "input_flags": ["pii.email_address", "pii.aadhaar_number"],
    "output_flags": [],
    "redactions_applied": ["pii.email_address", "pii.aadhaar_number"]
  }
}
```

When a safety violation was found (this never reaches the response body — the request is blocked):

```json
{
  "detail": "guardrails_blocked"
}
```

The same `guardrails` block appears on streaming responses in the final `finish` event.

The ledger row records `guardrail_flags` as a JSONB column with the same structure, so you can query across all requests that triggered a specific flag.

---

## Adding a new checker

All checkers implement `GuardrailsChecker` from `src/shared/guardrails/base.py`:

```python
class GuardrailsChecker(ABC):
    async def check_input(self, messages: list[dict]) -> GuardrailsResult: ...
    async def check_output(self, content: str) -> GuardrailsResult: ...
    async def check_output_chunk(self, chunk: str) -> GuardrailsResult:
        return GuardrailsResult()   # default: no-op for streaming
```

To add a new checker:

1. Create `src/shared/guardrails/your_checker.py`, implement all three methods.
2. Export it from `src/shared/guardrails/__init__.py`.
3. Add it to `_build_guardrails()` in `src/llm_service/api/deps.py`.
4. Add any config fields it needs to `src/shared/config.py` (no defaults).

The chain picks it up automatically — no changes needed in the handler or anywhere else.

---

## Code layout

```
src/shared/guardrails/
    base.py          GuardrailsResult dataclass + GuardrailsChecker ABC
    stub.py          Pass-through — no flags, no blocks, zero overhead
    presidio.py      PII detection and redaction (Microsoft Presidio + spaCy)
    llama_guard.py   Safety classification (Meta Llama-Guard via LiteLLM)
    size_caps.py     Input character limit — blocks oversized requests early
    chain.py         Composes multiple checkers; stops at first block
    __init__.py      Public exports

src/llm_service/api/deps.py     get_guardrails() factory
src/llm_service/api/rest/chat.py  Steps 6 and 9 — input and output check
```

---

See [Auth & Tenants](auth-and-tenants.md) for how the tenant is resolved before guardrails run.
See [Keys & Secrets](keys-cli.md) for how tenant BYOK keys are managed (separate from the Llama-Guard service key).

---

Next: [Response Cache](cache.md) — how identical requests are served from Redis without hitting a provider.