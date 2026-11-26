"""T28 offline check: web search tools reach the provider request body unchanged.

No network: the HTTP post is replaced and the body it would have sent is captured.
The backend sends web search as a raw tool (Anthropic) or the `openrouter:web_search`
server tool; LiteLLM must not drop `max_uses`, `user_location` or the whole tool.
"""
from __future__ import annotations

import json

import litellm
import pytest
from litellm.llms.custom_httpx.http_handler import AsyncHTTPHandler

from src.llm_service.providers.litellm import LiteLLMTransport
from src.llm_service.schemas.chat import ChatParams, NormalisedLLMRequest, Tool, MessageParam

LOCATION = {"type": "approximate", "country": "IN", "timezone": "Asia/Kolkata"}
ANTHROPIC_TOOL = {"type": "web_search_20250305", "name": "web_search", "max_uses": 2, "user_location": LOCATION}
OPENROUTER_TOOL = {
    "type": "openrouter:web_search",
    "parameters": {"engine": "native", "max_uses": 2, "user_location": LOCATION},
}


class _Stop(Exception):
    pass


@pytest.fixture
def sent_body(monkeypatch):
    """Run litellm.acompletion up to the HTTP post and return the JSON body it built."""
    captured: dict = {}

    async def _fake_post(self, url, data=None, json=None, headers=None, **kwargs):  # noqa: A002
        body = json if json is not None else data
        captured["body"] = __import__("json").loads(body) if isinstance(body, (str, bytes)) else body
        raise _Stop()

    monkeypatch.setattr(AsyncHTTPHandler, "post", _fake_post)

    async def _send(model: str, tools: list[dict], **extra) -> dict:
        captured.clear()
        with pytest.raises(Exception):
            await litellm.acompletion(
                model=model, messages=[{"role": "user", "content": "hi"}], tools=tools,
                api_key="x", drop_params=True, max_tokens=50, **extra,
            )
        return captured["body"]

    return _send


@pytest.mark.asyncio
async def test_anthropic_raw_web_search_tool_keeps_max_uses_and_location(sent_body) -> None:
    body = await sent_body("anthropic/claude-haiku-5-5", [ANTHROPIC_TOOL])
    assert body["tools"] == [ANTHROPIC_TOOL]


@pytest.mark.asyncio
async def test_openrouter_web_search_tool_reaches_the_body_unchanged(sent_body) -> None:
    body = await sent_body("openrouter/anthropic/claude-haiku-5.5", [OPENROUTER_TOOL])
    assert body["tools"] == [OPENROUTER_TOOL]
    assert "plugins" not in body


@pytest.mark.asyncio
async def test_openrouter_tool_keeps_cache_control_when_last(sent_body) -> None:
    tool = {**OPENROUTER_TOOL, "cache_control": {"type": "ephemeral", "ttl": "1h"}}
    body = await sent_body("openrouter/anthropic/claude-haiku-5.5", [tool])
    assert body["tools"] == [tool]


def test_gateway_adds_no_web_plugin_when_the_tool_is_sent_without_web_search_options() -> None:
    """The backend sends the tool and no web_search_options, so no deprecated `web` plugin is made."""
    request = NormalisedLLMRequest(
        provider="openrouter", model="anthropic/claude-haiku-5.5",
        messages=[MessageParam(role="user", content="hi")],
        tools=[Tool.model_validate(OPENROUTER_TOOL)], params=ChatParams(),
    )
    transport = LiteLLMTransport()
    kwargs = transport._openrouter_kwargs(request)
    assert "plugins" not in kwargs.get("extra_body", {})
    assert transport._serialize_tools(request.tools, "openrouter", request.params) == [OPENROUTER_TOOL]


def test_usage_block_passes_on_the_web_search_count() -> None:
    """The backend caps searches per turn from this count (T28); an absent count stays None."""
    from types import SimpleNamespace

    transport = LiteLLMTransport()
    with_object = SimpleNamespace(prompt_tokens=1, server_tool_use=SimpleNamespace(web_search_requests=2))
    with_dict = SimpleNamespace(prompt_tokens=1, server_tool_use={"web_search_requests": 1})
    assert transport._extract_usage(SimpleNamespace(usage=with_object)).web_search_requests == 2
    assert transport._extract_usage(SimpleNamespace(usage=with_dict)).web_search_requests == 1
    assert transport._extract_usage(SimpleNamespace(usage=SimpleNamespace(prompt_tokens=1))).web_search_requests is None


def test_usage_block_reads_the_openrouter_count_name() -> None:
    """OpenRouter reports the count as server_tool_use_details.web_search_requests (T28)."""
    from types import SimpleNamespace

    transport = LiteLLMTransport()
    as_dict = SimpleNamespace(prompt_tokens=1, server_tool_use_details={"web_search_requests": 2, "tool_calls_executed": 2})
    as_object = SimpleNamespace(prompt_tokens=1, server_tool_use_details=SimpleNamespace(web_search_requests=1))
    assert transport._extract_usage(SimpleNamespace(usage=as_dict)).web_search_requests == 2
    assert transport._extract_usage(SimpleNamespace(usage=as_object)).web_search_requests == 1


def test_anthropic_count_wins_and_a_zero_count_is_kept() -> None:
    from types import SimpleNamespace

    transport = LiteLLMTransport()
    both = SimpleNamespace(
        prompt_tokens=1,
        server_tool_use={"web_search_requests": 3},
        server_tool_use_details={"web_search_requests": 9},
    )
    zero = SimpleNamespace(prompt_tokens=1, server_tool_use_details={"web_search_requests": 0})
    empty_box = SimpleNamespace(prompt_tokens=1, server_tool_use_details={})
    assert transport._extract_usage(SimpleNamespace(usage=both)).web_search_requests == 3
    assert transport._extract_usage(SimpleNamespace(usage=zero)).web_search_requests == 0
    assert transport._extract_usage(SimpleNamespace(usage=empty_box)).web_search_requests is None
