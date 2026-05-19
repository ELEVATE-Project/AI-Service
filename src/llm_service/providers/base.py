from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Literal, Optional, Union

from src.llm_service.schemas.chat import (
    ChatResponse, ErrorData, NormalisedLLMRequest, TokenData, ToolUseData, UsageBlock,
)
from src.shared.db.models import BatchJob
from src.shared.secrets.backend import TenantKeyPayload


class UpstreamTransportError(Exception):
    """Raised by transports after retries are exhausted or on non-transient upstream errors."""

    def __init__(
        self, code: str, message: str, http_status: int,
        retry_after: Optional[str] = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.http_status = http_status
        self.retry_after = retry_after


@dataclass
class TransportFinishData:
    finish_reason: str
    usage: UsageBlock


@dataclass
class StreamEvent:
    type: Literal["token", "tool_use", "finish", "error"]
    data: Union[TokenData, ToolUseData, TransportFinishData, ErrorData]


class BaseLLMProvider(ABC):
    @abstractmethod
    async def chat(
        self, request: NormalisedLLMRequest, key: TenantKeyPayload
    ) -> ChatResponse: ...

    @abstractmethod
    async def stream(
        self, request: NormalisedLLMRequest, key: TenantKeyPayload
    ) -> AsyncIterator[StreamEvent]: ...

    async def batch_submit(self, jobs: list[BatchJob], key: TenantKeyPayload) -> None:
        raise NotImplementedError(f"{type(self).__name__} does not support batch submission")

    async def batch_poll(
        self, upstream_batch_id: str, jobs: list[BatchJob], key: TenantKeyPayload
    ) -> None:
        raise NotImplementedError(f"{type(self).__name__} does not support batch polling")
