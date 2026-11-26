"""OpenRouter stream usage survives LiteLLM's usage rebuild.

LiteLLM 1.83 replaces the provider's final usage chunk with one rebuilt by
stream_chunk_builder, which drops ``server_tool_use_details`` (OpenRouter's web
search count) and ``cost``. These tests drive the real ``litellm.acompletion``
stream path against a fake OpenRouter server on 127.0.0.1. No real provider is
called.
"""
from __future__ import annotations

import http.server
import json
import threading

import pytest

from src.llm_service.providers.litellm import LiteLLMTransport
from src.llm_service.schemas.chat import MessageParam, NormalisedLLMRequest
from src.shared.db.enums import KeyFormat
from src.shared.secrets.backend import TenantKeyPayload

_BASE = {"id": "c1", "object": "chat.completion.chunk", "created": 1,
         "model": "anthropic/claude-haiku-5.5"}


def _chunk(delta, finish=None):
    return {**_BASE, "choices": [{"index": 0, "delta": delta, "finish_reason": finish}]}


def _events(usage_extra):
    usage = {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15,
             "prompt_tokens_details": {"cached_tokens": 0}, **usage_extra}
    return [
        _chunk({"role": "assistant", "content": ""}),
        _chunk({"content": "Answer"}),
        _chunk({}, "stop"),
        {**_BASE, "choices": [], "usage": usage},
    ]


@pytest.fixture
def fake_openrouter():
    state = {"events": []}

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_POST(self):
            self.rfile.read(int(self.headers.get("content-length", 0)))
            self.send_response(200)
            self.send_header("content-type", "text/event-stream")
            self.end_headers()
            for event in state["events"]:
                self.wfile.write(f"data: {json.dumps(event)}\n\n".encode())
                self.wfile.flush()
            self.wfile.write(b"data: [DONE]\n\n")

        def log_message(self, *args):
            pass

    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    state["key"] = TenantKeyPayload(
        key_format=KeyFormat.API_KEY,
        data={"api_key": "sk-or-test", "api_base": f"http://127.0.0.1:{server.server_port}/api/v1"},
    )
    yield state
    server.shutdown()
    server.server_close()


async def _finish(state):
    req = NormalisedLLMRequest(
        provider="openrouter", model="anthropic/claude-haiku-5.5",
        messages=[MessageParam(role="user", content="hi")],
    )
    events = [e async for e in LiteLLMTransport().stream(req, state["key"])]
    assert events[-1].type == "finish", events
    return events[-1].data


@pytest.mark.asyncio
async def test_stream_keeps_openrouter_search_count_and_cost(fake_openrouter):
    fake_openrouter["events"] = _events(
        {"cost": 0.0123, "server_tool_use_details": {"web_search_requests": 2}})
    finish = await _finish(fake_openrouter)
    assert finish.usage.web_search_requests == 2
    assert finish.provider_reported_usd == pytest.approx(0.0123)
    assert finish.usage.input_tokens == 10
    assert finish.usage.output_tokens == 5


@pytest.mark.asyncio
async def test_stream_without_provider_search_fields_stays_null(fake_openrouter):
    fake_openrouter["events"] = _events({})
    finish = await _finish(fake_openrouter)
    assert finish.usage.web_search_requests is None
    assert finish.usage.output_tokens == 5


@pytest.mark.asyncio
async def test_stream_tap_does_not_change_chunks():
    from src.llm_service.providers.litellm_stream_usage import install_stream_usage_tap

    chunks = [object(), object()]

    async def raw():
        for chunk in chunks:
            yield chunk

    holder = type("Holder", (), {"completion_stream": raw()})()
    install_stream_usage_tap(holder)
    assert [c async for c in holder.completion_stream] == chunks


@pytest.mark.asyncio
async def test_stream_tap_skipped_without_completion_stream(caplog):
    import logging

    from src.llm_service.providers import litellm_stream_usage as tap_module

    tap_module._warned_missing_stream = False
    with caplog.at_level(logging.WARNING, logger=tap_module.__name__):
        first = tap_module.install_stream_usage_tap(object())
        tap_module.install_stream_usage_tap(object())
    assert first.web_search_requests is None and first.cost is None
    assert len([r for r in caplog.records if r.levelno == logging.WARNING]) == 1


@pytest.mark.asyncio
async def test_stream_tap_keeps_last_nonempty_values():
    from types import SimpleNamespace as NS

    from src.llm_service.providers.litellm_stream_usage import install_stream_usage_tap

    async def raw():
        yield NS(usage=NS(server_tool_use_details={"web_search_requests": 3}, cost=0.5))
        yield NS(usage=NS(server_tool_use_details=None, cost=None))
        yield NS(usage=None)

    holder = NS(completion_stream=raw())
    tap = install_stream_usage_tap(holder)
    _ = [c async for c in holder.completion_stream]
    assert tap.web_search_requests == 3
    assert tap.cost == 0.5


@pytest.mark.asyncio
async def test_litellm_provided_values_are_not_overridden(monkeypatch):
    """If LiteLLM's final usage carries a count and cost, the captured ones lose."""
    from types import SimpleNamespace as NS

    from src.llm_service.providers import litellm as transport_module

    async def raw():
        yield NS(choices=[], usage=NS(server_tool_use_details={"web_search_requests": 9}, cost=9.0))

    class Wrapper:
        """Like CustomStreamWrapper: consumes completion_stream, emits its own usage."""

        def __init__(self):
            self.completion_stream = raw()

        async def __aiter__(self):
            async for _ in self.completion_stream:
                pass
            yield NS(
                choices=[], _hidden_params={},
                usage=NS(prompt_tokens=1, completion_tokens=1, total_tokens=2, prompt_tokens_details=None,
                         server_tool_use={"web_search_requests": 1}, cost=0.25),
            )

    async def fake_acompletion(**kwargs):
        return Wrapper()

    monkeypatch.setattr(transport_module.litellm, "acompletion", fake_acompletion)
    monkeypatch.setattr(transport_module.litellm, "stream_chunk_builder", lambda chunks: None)
    key = TenantKeyPayload(key_format=KeyFormat.API_KEY, data={"api_key": "x"})
    req = NormalisedLLMRequest(
        provider="openrouter", model="anthropic/claude-haiku-5.5",
        messages=[MessageParam(role="user", content="hi")],
    )
    events = [e async for e in LiteLLMTransport().stream(req, key)]
    finish = events[-1].data
    assert finish.usage.web_search_requests == 1
    assert finish.provider_reported_usd == 0.25
