from __future__ import annotations

from collections.abc import Awaitable, Callable

from src.shared.guardrails.base import GuardrailsChecker, GuardrailsResult


class GuardrailsChain(GuardrailsChecker):
    """Runs a list of checkers in order; stops at the first block."""

    def __init__(self, checkers: list[GuardrailsChecker]) -> None:
        self._checkers = checkers

    async def check_input(self, messages: list[dict]) -> GuardrailsResult:
        combined = GuardrailsResult()
        current_messages = messages
        for checker in self._checkers:
            result = await checker.check_input(current_messages)
            combined.flags.extend(result.flags)
            combined.redactions.extend(result.redactions)
            if result.modified_messages is not None:
                combined.modified_messages = result.modified_messages
                current_messages = result.modified_messages
            if result.blocked:
                combined.blocked = True
                return combined
        return combined

    async def _chain_string(
        self, content: str,
        call: Callable[[GuardrailsChecker, str], Awaitable[GuardrailsResult]],
    ) -> GuardrailsResult:
        combined = GuardrailsResult()
        current = content
        for checker in self._checkers:
            result = await call(checker, current)
            combined.flags.extend(result.flags)
            combined.redactions.extend(result.redactions)
            if result.modified_content is not None:
                combined.modified_content = result.modified_content
                current = result.modified_content
            if result.blocked:
                combined.blocked = True
                return combined
        return combined

    async def check_output(self, content: str) -> GuardrailsResult:
        return await self._chain_string(content, lambda c, s: c.check_output(s))

    async def check_output_chunk(self, chunk: str) -> GuardrailsResult:
        return await self._chain_string(chunk, lambda c, s: c.check_output_chunk(s))
