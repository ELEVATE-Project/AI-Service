from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Literal, Union

from src.llm_service.schemas.chat import (
    ChatResponse, NormalisedLLMRequest, TokenData, ToolUseData, UsageBlock,
)
from src.shared.secrets.backend import TenantKeyPayload


@dataclass
class TransportFinishData:
    finish_reason: str
    usage: UsageBlock


@dataclass
class StreamEvent:
    type: Literal["token", "tool_use", "finish", "error"]
    data: Union[TokenData, ToolUseData, TransportFinishData, str]


class BaseLLMProvider(ABC):
    @abstractmethod
    async def chat(
        self, request: NormalisedLLMRequest, key: TenantKeyPayload
    ) -> ChatResponse: ...

    @abstractmethod
    async def stream(
        self, request: NormalisedLLMRequest, key: TenantKeyPayload
    ) -> AsyncIterator[StreamEvent]: ...