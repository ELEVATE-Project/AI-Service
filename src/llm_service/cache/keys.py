from __future__ import annotations

import hashlib
import json

from src.llm_service.schemas.chat import NormalisedLLMRequest


def make_cache_key(tenant_id: str, request: NormalisedLLMRequest) -> str:
    """Return a per-tenant SHA-256 cache key for the given normalized request."""
    payload = {
        "tenant_id": tenant_id,
        "provider": request.provider,
        "model": request.model,
        "messages": [m.model_dump(mode="json") for m in request.messages],
        "tools": (
            [t.model_dump(mode="json") for t in request.tools] if request.tools else None
        ),
        "params": request.params.model_dump(mode="json") if request.params else None,
    }
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode()).hexdigest()
