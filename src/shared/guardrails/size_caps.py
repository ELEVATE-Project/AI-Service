from __future__ import annotations

from src.shared.guardrails.base import GuardrailsChecker, GuardrailsResult


class SizeCapsGuardrails(GuardrailsChecker):
    """Rejects inputs that exceed configured character limits before any upstream call."""

    def __init__(self, max_input_chars: int, max_output_chars: int) -> None:
        self._max_input_chars = max_input_chars
        self._max_output_chars = max_output_chars

    async def check_input(self, messages: list[dict]) -> GuardrailsResult:
        total = sum(
            len(msg.get("content") or "") for msg in messages if msg.get("role") != "system"
        )
        if total > self._max_input_chars:
            return GuardrailsResult(blocked=True, flags=["size.input_too_large"])
        return GuardrailsResult()

    async def check_output(self, content: str) -> GuardrailsResult:
        if len(content) > self._max_output_chars:
            # Flag only — response already generated; blocking here would discard a completed call.
            return GuardrailsResult(flags=["size.output_too_large"])
        return GuardrailsResult()

    async def check_output_chunk(self, chunk: str) -> GuardrailsResult:
        return GuardrailsResult()
