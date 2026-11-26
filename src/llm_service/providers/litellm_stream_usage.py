"""Capture OpenRouter usage fields that LiteLLM drops from streamed responses.

LiteLLM 1.83 strips the provider's usage from outgoing stream chunks and emits
usage rebuilt by ``stream_chunk_builder``. The rebuild keeps token counts but
not OpenRouter's ``server_tool_use_details`` (web search count) or ``cost``.
The wrapper object returned by ``acompletion(stream=True)`` still iterates the
raw parsed chunks through ``completion_stream``, so we watch them there.
"""
from __future__ import annotations

import logging
import math
from collections.abc import AsyncIterator
from typing import Any, Optional

logger = logging.getLogger(__name__)

_warned_missing_stream = False


class StreamUsageTap:
    """Last search count and cost seen on the raw provider chunks."""

    def __init__(self) -> None:
        self.web_search_requests: Optional[int] = None
        self.cost: Optional[float] = None

    def observe(self, chunk: Any) -> None:
        usage = chunk.get("usage") if isinstance(chunk, dict) else getattr(chunk, "usage", None)
        if not usage:
            return
        get = usage.get if isinstance(usage, dict) else lambda name: getattr(usage, name, None)
        for name in ("server_tool_use_details", "server_tool_use"):
            box = get(name)
            count = box.get("web_search_requests") if isinstance(box, dict) else getattr(box, "web_search_requests", None)
            if isinstance(count, int) and not isinstance(count, bool) and count >= 0:
                self.web_search_requests = count
                break
        try:
            cost = float(get("cost"))
        except (TypeError, ValueError, OverflowError):
            return
        if math.isfinite(cost) and cost >= 0:
            self.cost = cost


def install_stream_usage_tap(response_stream: Any) -> StreamUsageTap:
    """Wrap ``response_stream.completion_stream``; chunks pass through unchanged.

    Never raises. If this LiteLLM version has no ``completion_stream``, the tap
    stays empty and a warning is logged once.
    """
    global _warned_missing_stream
    tap = StreamUsageTap()
    inner = getattr(response_stream, "completion_stream", None)
    if inner is None or not hasattr(inner, "__aiter__"):
        if not _warned_missing_stream:
            _warned_missing_stream = True
            logger.warning(
                "LiteLLM stream has no completion_stream; OpenRouter search count "
                "and cost cannot be captured from streams"
            )
        return tap

    async def tapped() -> AsyncIterator[Any]:
        async for chunk in inner:
            try:
                tap.observe(chunk)
            except Exception:  # observing must never break a request
                pass
            yield chunk

    try:
        response_stream.completion_stream = tapped()
    except Exception:
        logger.warning("Could not install LiteLLM stream usage tap", exc_info=True)
    return tap
