from __future__ import annotations

from src.llm_service.providers.base import BaseLLMProvider

# Direct adapter overrides land here as each provider adapter is added.
# Any (provider, model, feature) not matched falls through to LiteLLMTransport.
_OVERRIDE: dict[tuple[str, str, str], type[BaseLLMProvider]] = {
    # ("openai",    "*", "chat"):   OpenAICompatibleTransport,
    # ("anthropic", "*", "chat"):   AnthropicTransport,
    # ("bedrock",   "*", "chat"):   BedrockTransport,
}


def resolve(provider: str, model: str, feature: str) -> BaseLLMProvider:
    """Return the transport instance for (provider, model, feature)."""
    from src.llm_service.providers.litellm import LiteLLMTransport

    transport_cls = (
        _OVERRIDE.get((provider, model, feature))
        or _OVERRIDE.get((provider, "*", feature))
        or LiteLLMTransport
    )
    return transport_cls()