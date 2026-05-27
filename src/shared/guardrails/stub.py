from __future__ import annotations

from src.shared.guardrails.base import GuardrailsChecker, GuardrailsResult


class StubGuardrails(GuardrailsChecker):
    """Pass-through; used when all guardrail backends are disabled."""

    async def check_input(self, messages: list[dict]) -> GuardrailsResult:
        return GuardrailsResult()

    async def check_output(self, content: str) -> GuardrailsResult:
        return GuardrailsResult()

    async def check_output_chunk(self, chunk: str) -> GuardrailsResult:
        return GuardrailsResult()
