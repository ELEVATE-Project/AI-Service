"""Rules for the gateway's full-response cache, separate from provider prompt caching."""
from __future__ import annotations

from src.llm_service.schemas.chat import ChatResponse, NormalisedLLMRequest


def can_cache_request(request: NormalisedLLMRequest, ttl_seconds: int) -> bool:
    if ttl_seconds <= 0 or (request.metadata or {}).get("batch") is True:
        return False
    params = request.params
    if params:
        if params.temperature is not None and params.temperature > 0:
            return False
        if params.web_search_options is not None:
            return False
        if params.thinking is not None and params.thinking.get("type") != "disabled":
            return False
        # OpenRouter can enable reasoning through effort without a thinking object.
        if params.output_config and params.output_config.effort is not None:
            return False
    if any(tool.type.split(":")[-1].startswith("web_search") for tool in request.tools or []):
        return False
    if any(plugin.get("id") == "web" for plugin in (request.provider_options or {}).get("plugins") or []):
        return False
    return True


def can_cache_response(response: ChatResponse) -> bool:
    if (response.usage.web_search_requests or 0) > 0:
        return False
    return bool(response.choices) and not any(
        choice.message.citations or choice.message.thinking_blocks for choice in response.choices
    )
