from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field


@dataclass
class GuardrailsResult:
    blocked: bool = False
    flags: list[str] = field(default_factory=list)
    redactions: list[str] = field(default_factory=list)
    modified_messages: list[dict] | None = None
    modified_content: str | None = None


class GuardrailsChecker(ABC):
    @abstractmethod
    async def check_input(self, messages: list[dict]) -> GuardrailsResult: ...

    @abstractmethod
    async def check_output(self, content: str) -> GuardrailsResult: ...

    async def check_output_chunk(self, chunk: str) -> GuardrailsResult:
        # Default no-op; Presidio overrides for per-chunk PII redaction.
        # Llama-Guard runs at completion only — not per-chunk.
        return GuardrailsResult()
