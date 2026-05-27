from __future__ import annotations

from typing import Optional

from src.shared.guardrails.base import GuardrailsChecker, GuardrailsResult

_CATEGORY_MAP: dict[str, str] = {
    "S1": "violent_crimes", "S2": "non_violent_crimes", "S3": "sex_crimes",
    "S4": "child_exploitation", "S5": "defamation", "S6": "specialized_advice",
    "S7": "privacy", "S8": "intellectual_property", "S9": "indiscriminate_weapons",
    "S10": "hate", "S11": "suicide_self_harm", "S12": "sexual_content", "S13": "elections",
}

_SAFETY_CATEGORIES = """S1: Violent Crimes.
S2: Non-Violent Crimes.
S3: Sex-Related Crimes.
S4: Child Sexual Exploitation.
S5: Defamation.
S6: Specialized Advice.
S7: Privacy.
S8: Intellectual Property.
S9: Indiscriminate Weapons.
S10: Hate.
S11: Suicide & Self-Harm.
S12: Sexual Content.
S13: Elections."""


def _categories_block() -> str:
    return f"<BEGIN UNSAFE CONTENT CATEGORIES>\n{_SAFETY_CATEGORIES}\n<END UNSAFE CONTENT CATEGORIES>"


def _build_input_prompt(messages: list[dict]) -> str:
    conversation_lines: list[str] = []
    for msg in messages:
        role = msg.get("role", "")
        content = msg.get("content") or ""
        if role == "user":
            conversation_lines.append(f"User: {content}")
        elif role == "assistant":
            conversation_lines.append(f"Agent: {content}")

    conversation = "\n\n".join(conversation_lines)
    return (
        "Task: Check if there is unsafe content in 'User' messages in conversations "
        "according to our safety policy with the below categories.\n\n"
        f"{_categories_block()}\n\n"
        "<BEGIN CONVERSATION>\n\n"
        f"{conversation}\n\n"
        "<END CONVERSATION>\n\n"
        "Provide your safety assessment for ONLY THE LAST User turn in the above conversation:\n"
        " - First line must read 'safe' or 'unsafe'.\n"
        " - If unsafe, a second line must include a comma-separated list of violated categories."
    )


def _build_output_prompt(content: str) -> str:
    return (
        "Task: Check if there is unsafe content in 'Agent' messages according to our "
        "safety policy with the below categories.\n\n"
        f"{_categories_block()}\n\n"
        "<BEGIN CONVERSATION>\n\n"
        f"Agent: {content}\n\n"
        "<END CONVERSATION>\n\n"
        "Provide your safety assessment for ONLY THE LAST Agent turn in the above conversation:\n"
        " - First line must read 'safe' or 'unsafe'.\n"
        " - If unsafe, a second line must include a comma-separated list of violated categories."
    )


def _parse_response(response_text: str) -> GuardrailsResult:
    lines = response_text.strip().splitlines()
    first = lines[0].strip().lower() if lines else ""
    if first == "safe" or not first:
        return GuardrailsResult()
    raw_codes = lines[1].split(",") if len(lines) > 1 else []
    flags = [
        f"safety.{_CATEGORY_MAP.get(code.strip(), code.strip().lower())}"
        for code in raw_codes if code.strip()
    ]
    return GuardrailsResult(blocked=bool(flags), flags=flags)


class LlamaGuardChecker(GuardrailsChecker):
    """Safety classification using Meta Llama-Guard via LiteLLM."""

    def __init__(self, model: str, api_key: Optional[str], api_base: Optional[str]) -> None:
        import litellm  # noqa: PLC0415
        self._litellm = litellm
        self._model = model
        self._api_key = api_key or None
        self._api_base = api_base or None

    async def _classify(self, prompt: str) -> GuardrailsResult:
        kwargs: dict = dict(
            model=self._model,
            messages=[{"role": "user", "content": prompt}],
            max_tokens=50,
            temperature=0,
        )
        if self._api_key:
            kwargs["api_key"] = self._api_key
        if self._api_base:
            kwargs["api_base"] = self._api_base

        raw = await self._litellm.acompletion(**kwargs)
        response_text: str = raw.choices[0].message.content or ""
        print("response_text: ", response_text)
        return _parse_response(response_text)

    async def check_input(self, messages: list[dict]) -> GuardrailsResult:
        prompt = _build_input_prompt(messages)
        return await self._classify(prompt)

    async def check_output(self, content: str) -> GuardrailsResult:
        prompt = _build_output_prompt(content)
        return await self._classify(prompt)

    async def check_output_chunk(self, chunk: str) -> GuardrailsResult:
        # Llama-Guard runs at completion only; per-chunk calls are a perf cliff.
        return GuardrailsResult()
