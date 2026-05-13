from __future__ import annotations

import asyncio
from typing import Any

from src.shared.guardrails.base import GuardrailsChecker, GuardrailsResult

_DEFAULT_ENTITIES = [
    "EMAIL_ADDRESS", "PHONE_NUMBER", "PERSON", "LOCATION",
    "CREDIT_CARD", "IBAN_CODE", "IP_ADDRESS", "US_SSN",
    "AADHAAR_NUMBER", "PAN_NUMBER",
    "IN_PASSPORT", "IN_DRIVING_LICENSE", "IN_IFSC_CODE", "IN_UPI_ID",
    "MAC_ADDRESS", "DATE_OF_BIRTH", "IN_PIN_CODE",
]

# Known UPI provider handles used in India
_UPI_HANDLES = (
    "oksbi|okhdfcbank|okicici|okaxis|paytm|ybl|upi|apl|ibl|axl|"
    "axisbank|hdfcbank|icici|sbi|kotak|indus|federal|pnb|bob|"
    "airtel|jio|phonepe|nsdl|barodampay|centralbank|idbi|rbl|"
    "equitas|esaf|ujjivan|aubank|idfcfirst"
)


class PresidioGuardrails(GuardrailsChecker):
    """PII detection and redaction using Microsoft Presidio."""

    def __init__(self) -> None:
        from presidio_analyzer import AnalyzerEngine, Pattern, PatternRecognizer
        from presidio_anonymizer import AnonymizerEngine

        self._analyzer: Any = AnalyzerEngine()
        self._anonymizer: Any = AnonymizerEngine()

        # Aadhaar: 12-digit number in groups of 4 separated by space or hyphen.
        # Separator required to avoid false positive with plain 12-digit bank account numbers.
        aadhaar = PatternRecognizer(
            supported_entity="AADHAAR_NUMBER",
            patterns=[Pattern("AADHAAR_NUMBER", r"\b\d{4}[ -]\d{4}[ -]\d{4}\b", 0.85)],
        )

        # PAN card: 5 uppercase letters + 4 digits + 1 uppercase letter
        pan = PatternRecognizer(
            supported_entity="PAN_NUMBER",
            patterns=[Pattern("PAN_NUMBER", r"\b[A-Z]{5}[0-9]{4}[A-Z]\b", 0.90)],
        )

        # Indian passport: 1 uppercase letter + 7 digits
        passport = PatternRecognizer(
            supported_entity="IN_PASSPORT",
            patterns=[Pattern("IN_PASSPORT", r"\b[A-Z][0-9]{7}\b", 0.70)],
        )

        # Indian driving license: state code (2 letters) + RTO (2 digits) +
        # year (4 digits) + serial (7 digits); separators optional
        driving_license = PatternRecognizer(
            supported_entity="IN_DRIVING_LICENSE",
            patterns=[Pattern(
                "IN_DRIVING_LICENSE",
                r"\b[A-Z]{2}[\s-]?\d{2}[\s-]?\d{4}[\s-]?\d{7}\b",
                0.80,
            )],
        )

        # IFSC code: 4 uppercase letters + 0 + 6 alphanumeric characters (always 11 chars)
        ifsc = PatternRecognizer(
            supported_entity="IN_IFSC_CODE",
            patterns=[Pattern("IN_IFSC_CODE", r"\b[A-Z]{4}0[A-Z0-9]{6}\b", 0.90)],
        )

        # UPI ID: handle@<known provider> — uses a fixed list of Indian UPI handles
        upi = PatternRecognizer(
            supported_entity="IN_UPI_ID",
            patterns=[Pattern(
                "IN_UPI_ID",
                rf"\b[\w.]+@(?:{_UPI_HANDLES})\b",
                0.90,
            )],
        )

        # MAC address: XX:XX:XX:XX:XX:XX or XX-XX-XX-XX-XX-XX
        mac = PatternRecognizer(
            supported_entity="MAC_ADDRESS",
            patterns=[Pattern(
                "MAC_ADDRESS",
                r"\b([0-9A-Fa-f]{2}[:-]){5}[0-9A-Fa-f]{2}\b",
                0.95,
            )],
        )

        # Date of birth: DD/MM/YYYY or DD-MM-YYYY
        dob = PatternRecognizer(
            supported_entity="DATE_OF_BIRTH",
            patterns=[Pattern(
                "DATE_OF_BIRTH",
                r"\b(0?[1-9]|[12][0-9]|3[01])[/\-](0?[1-9]|1[0-2])[/\-]([12][0-9]{3})\b",
                0.65,
            )],
        )

        # Indian PIN code: 6-digit postal code, first digit 1-9.
        # Requires a word boundary and either a preceding dash/space (address context)
        # or the literal word "PIN"/"pincode" to reduce false positives on plain 6-digit numbers.
        pin_code = PatternRecognizer(
            supported_entity="IN_PIN_CODE",
            patterns=[
                Pattern("IN_PIN_CODE_DASH", r"[-\s][1-9][0-9]{5}\b", 0.75),
                Pattern("IN_PIN_CODE_WORD", r"(?i)\bpin(?:\s*code)?[\s:]+[1-9][0-9]{5}\b", 0.90),
            ],
        )

        for recognizer in [aadhaar, pan, passport, driving_license, ifsc, upi, mac, dob, pin_code]:
            self._analyzer.registry.add_recognizer(recognizer)

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
