from __future__ import annotations
from typing import Any, Literal, Optional
from pydantic import BaseModel
from src.shared.schemas.envelope import CostBlock, GuardrailsBlock, LatencyBlock, PolicyBlock


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
    type: str = "function"
    function: ToolFunction


class ChatParams(BaseModel):
    temperature: Optional[float] = None
    max_tokens: Optional[int] = None
    top_p: Optional[float] = None
    stop: Optional[list[str]] = None
    seed: Optional[int] = None
    connect_timeout: Optional[float] = None
    read_timeout: Optional[float] = None


class ChatRequest(BaseModel):
    provider: str
    model: str
    messages: list[MessageParam]
    tools: Optional[list[Tool]] = None
    tool_choice: Optional[Any] = None
    params: Optional[ChatParams] = None
    cache_policy: Literal["auto", "explicit", "off"] = "auto"
    metadata: Optional[dict[str, Any]] = None


class NormalisedLLMRequest(BaseModel):
    provider: str
    model: str
    messages: list[MessageParam]
    tools: Optional[list[Tool]] = None
    tool_choice: Optional[Any] = None
    params: Optional[ChatParams] = None
    metadata: Optional[dict[str, Any]] = None


class ChoiceMessage(BaseModel):
    role: str
    content: Optional[str] = None
    tool_calls: Optional[list[dict[str, Any]]] = None


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


class ErrorData(BaseModel):
    code: str
    message: str
    upstream_status: Optional[int] = None
    retry_after: Optional[str] = None
