from __future__ import annotations
from typing import Any, Literal, Optional
from pydantic import BaseModel, ConfigDict, Field, field_validator
from src.shared.schemas.envelope import CostBlock, GuardrailsBlock, LatencyBlock, PolicyBlock

# Single source of truth for provider-side prompt-cache capability, consumed by
# the serialization logic (providers/litellm.py), schema validation below, and
# the GET /v1/cache/options endpoint — so calling services always read the
# current supported values instead of hardcoding them.
CACHE_CAPABLE_PROVIDERS: tuple[str, ...] = ("anthropic", "bedrock", "openrouter")
CACHE_TTL_VALUES: tuple[str, ...] = ("5m", "1h")
CACHE_TARGET_VALUES: tuple[str, ...] = ("prompt", "tools")
CACHE_TARGET_DEFAULT: tuple[str, ...] = ("prompt", "tools")


class UsageBlock(BaseModel):
    input_tokens: int = 0
    output_tokens: int = 0
    input_tokens_cache_write: Optional[int] = None
    input_tokens_cache_read: Optional[int] = None
    total_tokens: int = 0


class CacheBlock(BaseModel):
    our_cache_hit: bool = False
    upstream_prompt_cache_hit: Optional[bool] = None


class MessageParam(BaseModel):
    role: Literal["system", "user", "assistant", "tool"]
    content: Optional[str] = None
    cache: Optional[str] = None
    tool_calls: Optional[list[dict[str, Any]]] = None
    tool_call_id: Optional[str] = None


class ToolFunction(BaseModel):
    name: str
    description: Optional[str] = None
    parameters: Optional[dict[str, Any]] = None


class Tool(BaseModel):
    model_config = ConfigDict(extra="allow")

    type: str = "function"
    name: Optional[str] = None
    function: Optional[ToolFunction] = None


class WebSearchOptions(BaseModel):
    search_context_size: Optional[str] = None
    user_location: Optional[dict[str, Any]] = None


class CacheOptions(BaseModel):
    """Opt-in provider-side prompt caching, applied automatically so calling
    services don't have to mark individual messages/tools themselves.

    An explicit ``"cache": "ephemeral"`` on a message always overrides
    ``enabled`` for that message. Supported values come from
    ``CACHE_TTL_VALUES`` / ``CACHE_TARGET_VALUES`` above — also served live
    via GET /v1/cache/options.
    """
    enabled: Optional[bool] = None
    ttl: Optional[str] = None
    targets: Optional[list[str]] = None

    @field_validator("ttl")
    @classmethod
    def _validate_ttl(cls, value: Optional[str]) -> Optional[str]:
        if value is not None and value not in CACHE_TTL_VALUES:
            raise ValueError(f"ttl must be one of {CACHE_TTL_VALUES}")
        return value

    @field_validator("targets")
    @classmethod
    def _validate_targets(cls, value: Optional[list[str]]) -> Optional[list[str]]:
        if value is not None:
            invalid = set(value) - set(CACHE_TARGET_VALUES)
            if invalid:
                raise ValueError(f"targets contains unsupported values {sorted(invalid)}; must be one of {CACHE_TARGET_VALUES}")
        return value


RETRY_MAX_ATTEMPTS_CEILING = 10
RETRY_BACKOFF_BASE_S_CEILING = 60.0


class RetryOptions(BaseModel):
    """Per-request override of the service-wide retry defaults (Settings.llm_retry_*).

    max_attempts=0 (or negative) would skip the upstream call in LiteLLMTransport's
    retry loop entirely, crashing further down instead of erroring cleanly — 1 is
    the floor. Ceilings cap how long a request can hold a worker retrying.
    """

    enabled: Optional[bool] = None
    max_attempts: Optional[int] = Field(default=None, ge=1, le=RETRY_MAX_ATTEMPTS_CEILING)
    backoff_base_s: Optional[float] = Field(default=None, ge=0.0, le=RETRY_BACKOFF_BASE_S_CEILING)


class ChatParams(BaseModel):
    temperature: Optional[float] = None
    max_tokens: Optional[int] = None
    top_p: Optional[float] = None
    stop: Optional[list[str]] = None
    seed: Optional[int] = None
    connect_timeout: Optional[float] = None
    read_timeout: Optional[float] = None
    web_search_options: Optional[WebSearchOptions] = None
    cache_options: Optional[CacheOptions] = None
    retry: Optional[RetryOptions] = None


class ChatRequest(BaseModel):
    provider: Optional[str] = None
    model: Optional[str] = None
    messages: list[MessageParam]
    tools: Optional[list[Tool]] = None
    tool_choice: Optional[Any] = None
    params: Optional[ChatParams] = None
    metadata: Optional[dict[str, Any]] = None
    # Provider-specific passthrough. Currently consumed only for provider="openrouter":
    #   provider — OpenRouter routing prefs (order, allow_fallbacks, data_collection, ...)
    #   models   — OpenRouter model fallback list
    #   plugins  — OpenRouter plugins (e.g. web search)
    #   referer / title — per-request app-attribution overrides
    provider_options: Optional[dict[str, Any]] = None
    # Opt-in only: omitted/False leaves provider/model required as before. True makes the
    # normaliser look up the tenant's TenantDefaults row and fill in whatever the request omitted.
    use_defaults: Optional[bool] = None


class NormalisedLLMRequest(BaseModel):
    provider: str
    model: str
    messages: list[MessageParam]
    tools: Optional[list[Tool]] = None
    tool_choice: Optional[Any] = None
    params: Optional[ChatParams] = None
    metadata: Optional[dict[str, Any]] = None
    provider_options: Optional[dict[str, Any]] = None


class ChoiceMessage(BaseModel):
    role: str
    content: Optional[str] = None
    tool_calls: Optional[list[dict[str, Any]]] = None
    citations: Optional[list[Any]] = None


class Choice(BaseModel):
    index: int
    message: ChoiceMessage
    finish_reason: str


class ChatResponse(BaseModel):
    id: str
    object: str = "chat.completion"
    created: int
    tenant_id: str
    provider: str
    model: str
    transport: str
    region: Optional[str] = None
    choices: list[Choice]
    usage: UsageBlock
    cost: CostBlock
    latency_ms: LatencyBlock
    cache: CacheBlock
    guardrails: GuardrailsBlock
    policy: PolicyBlock
    provider_raw: Optional[dict[str, Any]] = None


# ── streaming event payloads ──────────────────────────────────────────────────

class TokenData(BaseModel):
    index: int
    delta: str


class ToolUseData(BaseModel):
    index: int
    id: str
    name: str
    arguments_delta: str


class UsageData(BaseModel):
    input_tokens: int
    output_tokens: int
    input_tokens_cache_read: Optional[int] = None


class FinishData(BaseModel):
    id: str
    finish_reason: str
    usage: UsageBlock
    cost: CostBlock
    latency_ms: LatencyBlock
    cache: CacheBlock
    guardrails: GuardrailsBlock
    policy: PolicyBlock
    citations: Optional[list[Any]] = None


class ErrorData(BaseModel):
    code: str
    message: str
    upstream_status: Optional[int] = None
    retry_after: Optional[str] = None
