from __future__ import annotations

from dataclasses import dataclass, field

from src.llm_service.providers.base import BaseLLMProvider


@dataclass
class RoutingEntry:
    transport_cls: type[BaseLLMProvider]
    regions: list[str] = field(default_factory=list)

from src.llm_service.providers.anthropic import AnthropicTransport
from src.llm_service.providers.bedrock import BedrockTransport

_OVERRIDE: dict[tuple[str, str, str], RoutingEntry] = {
    ("anthropic", "*", "batch"): RoutingEntry(AnthropicTransport),
    ("bedrock", "*", "batch"): RoutingEntry(BedrockTransport),
}


def resolve(provider: str, model: str, feature: str) -> BaseLLMProvider:
    """Return the transport instance for (provider, model, feature)."""
    from src.llm_service.providers.litellm import LiteLLMTransport

    entry = (
        _OVERRIDE.get((provider, model, feature))
        or _OVERRIDE.get((provider, "*", feature))
        or RoutingEntry(transport_cls=LiteLLMTransport)
    )
    return entry.transport_cls(regions=entry.regions)
