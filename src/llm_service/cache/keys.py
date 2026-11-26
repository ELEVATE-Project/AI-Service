from __future__ import annotations

import hashlib
import json

from src.llm_service.schemas.chat import ChatParams, NormalisedLLMRequest

_ANSWER_PARAMS = {
    "temperature", "max_tokens", "top_p", "stop", "seed", "web_search_options",
    "cache_options", "thinking", "output_config",
}


def make_cache_key(tenant_id: str, request: NormalisedLLMRequest) -> str:
    """Return a per-tenant SHA-256 cache key for the given normalized request."""
    payload = {
        # Old entries did not distinguish tool_choice; never replay them after this fix.
        "version": 2,
        "tenant_id": tenant_id,
        "provider": request.provider,
        "model": request.model,
        "messages": [m.model_dump(mode="json") for m in request.messages],
        "tools": (
            [t.model_dump(mode="json") for t in request.tools] if request.tools else None
        ),
        "tool_choice": request.tool_choice,
        "params": (request.params or ChatParams()).model_dump(mode="json", include=_ANSWER_PARAMS),
        "provider_options": request.provider_options,
    }
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode()).hexdigest()
