from __future__ import annotations

import asyncio
from typing import Any

from src.shared.guardrails.base import GuardrailsChecker, GuardrailsResult

_DEFAULT_ENTITIES = [
    "EMAIL_ADDRESS", "PHONE_NUMBER", "PERSON", "LOCATION", "IN_PIN_CODE",
]


class PresidioGuardrails(GuardrailsChecker):
    """PII detection and redaction using Microsoft Presidio."""

    def __init__(self) -> None:
        from presidio_analyzer import AnalyzerEngine, Pattern, PatternRecognizer
        from presidio_anonymizer import AnonymizerEngine

        self._analyzer: Any = AnalyzerEngine()
        self._anonymizer: Any = AnonymizerEngine()

        # Indian PIN code: 6-digit postal code, first digit 1-9.
        # Requires either a preceding dash/space (address context) or the literal
        # word "PIN"/"pincode" to reduce false positives on plain 6-digit numbers.
        pin_code = PatternRecognizer(
            supported_entity="IN_PIN_CODE",
            patterns=[
                Pattern("IN_PIN_CODE_DASH", r"[-\s][1-9][0-9]{5}\b", 0.75),
                Pattern("IN_PIN_CODE_WORD", r"(?i)\bpin(?:\s*code)?[\s:]+[1-9][0-9]{5}\b", 0.90),
            ],
        )

        self._analyzer.registry.add_recognizer(pin_code)

    def _check_text(self, text: str) -> GuardrailsResult:
        results = self._analyzer.analyze(text=text, language="en", entities=_DEFAULT_ENTITIES)
        if not results:
            return GuardrailsResult()
        anonymized = self._anonymizer.anonymize(text=text, analyzer_results=results)
        flags = sorted({f"pii.{r.entity_type.lower()}" for r in results})
        modified = anonymized.text if anonymized.text != text else None
        return GuardrailsResult(flags=flags, redactions=flags, modified_content=modified)

    async def check_input(self, messages: list[dict]) -> GuardrailsResult:
        all_flags: set[str] = set()
        modified_messages = list(messages)
        any_modified = False

        for index, message in enumerate(messages):
            content = message.get("content")
            if not content:
                continue
            result = await asyncio.to_thread(self._check_text, content)
            all_flags.update(result.flags)
            if result.modified_content is not None:
                modified_messages[index] = {**message, "content": result.modified_content}
                any_modified = True

        flags = sorted(all_flags)
        return GuardrailsResult(
            flags=flags,
            redactions=flags,
            modified_messages=modified_messages if any_modified else None,
        )

    async def check_output(self, content: str) -> GuardrailsResult:
        result = await asyncio.to_thread(self._check_text, content)
        return result

    async def check_output_chunk(self, chunk: str) -> GuardrailsResult:
        return await self.check_output(chunk)
