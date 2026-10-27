"""T30: exercise the pinned LiteLLM adapters with HTTP replaced, never a provider.

The gateway's thinking_blocks retains Anthropic content blocks or OpenRouter's
raw reasoning_details. It is opaque provider data, not user-visible content.
"""
from __future__ import annotations

import copy
import json
import socket
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import httpx
import litellm
import pytest
from pydantic import ValidationError

from src.llm_service.providers.anthropic import AnthropicTransport
from src.llm_service.providers.litellm import LiteLLMTransport, _dedupe_leading_narration
from src.llm_service.schemas.chat import ChatParams, MessageParam, NormalisedLLMRequest, UsageBlock
from src.shared.db.enums import KeyFormat
from src.shared.secrets.backend import TenantKeyPayload


PATHS = [
    ("anthropic", "claude-haiku-5-5", "claude-haiku-4-5"),
    ("openrouter", "anthropic/claude-haiku-5.5", "anthropic/claude-haiku-4.5"),
]
THINKING = {"type": "adaptive"}
OUTPUT = {"effort": "low"}
ANTHROPIC_BLOCKS = [
    {"type": "thinking", "thinking": "Private thought", "signature": "sig-one"},
    {"type": "redacted_thinking", "data": "opaque-redacted"},
    {"type": "thinking", "thinking": "Second thought", "signature": "sig-two"},
]
OPENROUTER_BLOCKS = [
    {"type": "reasoning.text", "text": "Private thought", "signature": "sig-one",
     "id": "reason-0", "format": "anthropic-claude-v1", "index": 0},
    {"type": "reasoning.encrypted", "data": "opaque-redacted", "id": "reason-1",
     "format": "anthropic-claude-v1", "index": 1},
]
TOOL_CALL = {"id": "toolu_test", "type": "function", "function": {
    "name": "lookup", "arguments": '{"query":"test"}',
}}
TOOLS = [{"type": "function", "function": {
    "name": "lookup", "parameters": {"type": "object", "properties": {"query": {"type": "string"}}},
}}]
KEY = TenantKeyPayload(key_format=KeyFormat.API_KEY, data={"api_key": "offline-test-key"})


def request(provider, model, *, params=None, messages=None, tools=None):
    return NormalisedLLMRequest(
        provider=provider, model=model, params=params, tools=tools,
        messages=messages or [MessageParam(role="user", content="hi")],
    )


def blocks(provider):
    return copy.deepcopy(ANTHROPIC_BLOCKS if provider == "anthropic" else OPENROUTER_BLOCKS)


class BytesStream(httpx.AsyncByteStream):
    def __init__(self, data):
        self.data = data

    async def __aiter__(self):
        yield self.data


@pytest.fixture
def wire(monkeypatch):
    """Run LiteLLM's parameter filtering and adapters directly, without its I/O wrapper."""
    from litellm.llms.anthropic.chat.handler import ModelResponseIterator
    from litellm.llms.anthropic.chat.transformation import AnthropicConfig
    from litellm.llms.openrouter.chat.transformation import OpenrouterConfig, OpenRouterChatCompletionStreamingHandler
    from litellm.utils import get_optional_params

    state = SimpleNamespace(bodies=[], raw_bodies=[], kwargs=[], tool_reply=False, refusal=False)
    monkeypatch.setattr(litellm, "modify_params", True)

    def forbidden(*args, **kwargs):
        raise AssertionError("No HTTP or socket connections allowed")

    async def send(client, req, **kwargs):
        assert req.url.host in ("api.anthropic.com", "openrouter.ai"), str(req.url)
        body = json.loads(req.content)
        state.bodies.append(body)
        state.raw_bodies.append(req.content)
        direct = req.url.host == "api.anthropic.com"
        if body.get("stream"):
            events = stream_events("anthropic" if direct else "openrouter", refusal=state.refusal,
                                   tool_reply=state.tool_reply)
            return httpx.Response(200, headers={"content-type": "text/event-stream"},
                                  stream=BytesStream(events), request=req)
        if direct:
            content = blocks("anthropic")
            if state.tool_reply:
                content.append({"type": "tool_use", "id": "toolu_test", "name": "lookup", "input": {"query": "test"}})
            else:
                content.append({"type": "text", "text": "Answer"})
            result = {"id": "msg_test", "type": "message", "role": "assistant", "model": body["model"],
                      "content": content, "stop_reason": "refusal" if state.refusal else "tool_use" if state.tool_reply else "end_turn",
                      "stop_sequence": None, "usage": {"input_tokens": 10, "output_tokens": 5}}
        else:
            message = {"role": "assistant", "content": None if state.tool_reply else "Answer",
                       "reasoning": "Private thought", "reasoning_details": blocks("openrouter")}
            if state.tool_reply:
                message["tool_calls"] = [TOOL_CALL]
            result = {"id": "chat_test", "object": "chat.completion", "created": 1,
                      "model": body["model"], "choices": [{"index": 0, "message": message,
                      "finish_reason": "content_filter" if state.refusal else "tool_calls" if state.tool_reply else "stop"}],
                      "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15}}
        return httpx.Response(200, json=result, request=req)

    async def completion(**kwargs):
        state.kwargs.append(copy.deepcopy(kwargs))
        provider, model = kwargs["model"].split("/", 1)
        config = AnthropicConfig() if provider == "anthropic" else OpenrouterConfig()
        params = {k: v for k, v in kwargs.items() if k not in {"model", "messages", "api_key", "api_base"}}
        optional = get_optional_params(model=model, custom_llm_provider=provider, **params)
        messages = copy.deepcopy(kwargs["messages"])
        body = config.transform_request(model=model, messages=messages, optional_params=optional,
                                        litellm_params={}, headers={})
        url = "https://api.anthropic.com/v1/messages" if provider == "anthropic" else "https://openrouter.ai/api/v1/chat/completions"
        req = httpx.Request("POST", url, content=json.dumps(body).encode())
        response = await send(None, req)
        if kwargs.get("stream"):
            parser = (ModelResponseIterator([], sync_stream=False) if provider == "anthropic"
                      else OpenRouterChatCompletionStreamingHandler([], sync_stream=False))
            data = (await response.aread()).decode()

            async def chunks():
                for line in data.splitlines():
                    if line.startswith("data: ") and line != "data: [DONE]":
                        yield parser.chunk_parser(json.loads(line[6:]))

            return chunks()
        return config.transform_response(
            model=model, raw_response=response, model_response=litellm.ModelResponse(),
            logging_obj=Mock(), request_data=body, messages=messages, optional_params=optional,
            litellm_params={}, encoding=Mock(),
        )

    monkeypatch.setattr(litellm, "acompletion", completion)
    monkeypatch.setattr(httpx.AsyncClient, "send", forbidden)
    monkeypatch.setattr(httpx.Client, "send", forbidden)
    monkeypatch.setattr(socket.socket, "connect", forbidden)
    monkeypatch.setattr(socket, "create_connection", forbidden)
    monkeypatch.setattr("litellm.litellm_core_utils.streaming_chunk_builder_utils.token_counter", lambda **kwargs: 1)
    return state


def stream_events(provider, *, refusal=False, tool_reply=False):
    if provider == "anthropic":
        events = [{"type": "message_start", "message": {"id": "msg_test", "type": "message", "role": "assistant",
                   "model": "claude-haiku-5-5", "content": [], "usage": {"input_tokens": 10, "output_tokens": 0}}}]
        for index, block in enumerate(ANTHROPIC_BLOCKS):
            initial = {"type": "thinking", "thinking": ""} if block["type"] == "thinking" else block
            events.append({"type": "content_block_start", "index": index, "content_block": initial})
            if block["type"] == "thinking":
                for fragment in (block["thinking"][:7], block["thinking"][7:]):
                    events.append({"type": "content_block_delta", "index": index,
                                   "delta": {"type": "thinking_delta", "thinking": fragment}})
                events.append({"type": "content_block_delta", "index": index,
                               "delta": {"type": "signature_delta", "signature": block["signature"]}})
            events.append({"type": "content_block_stop", "index": index})
        events.extend([
            {"type": "content_block_start", "index": 3, "content_block":
             {"type": "tool_use", "id": "toolu_test", "name": "lookup", "input": {}} if tool_reply else {"type": "text", "text": ""}},
            {"type": "content_block_delta", "index": 3, "delta":
             {"type": "input_json_delta", "partial_json": TOOL_CALL["function"]["arguments"]} if tool_reply else {"type": "text_delta", "text": "Answer"}},
            {"type": "content_block_stop", "index": 3},
            {"type": "message_delta", "delta": {"stop_reason": "refusal" if refusal else "tool_use" if tool_reply else "end_turn", "stop_sequence": None},
             "usage": {"output_tokens": 5}}, {"type": "message_stop"},
        ])
        return "".join(f"event: {event['type']}\ndata: {json.dumps(event)}\n\n" for event in events).encode()
    # Split both text/signature and encrypted data across chunks, retaining metadata.
    first, encrypted = blocks("openrouter")
    deltas = [
        {"reasoning": "Private ", "reasoning_details": [{**first, "text": "Private ", "signature": "sig-"}]},
        {"reasoning": "thought", "reasoning_details": [{**first, "text": "thought", "signature": "one"}]},
        {"reasoning_details": [{**encrypted, "data": "opaque-"}]},
        {"reasoning_details": [{**encrypted, "data": "redacted"}]},
        {"tool_calls": [{"index": 0, **TOOL_CALL}]} if tool_reply else {"content": "Answer"}, {},
    ]
    events = [{"id": "chat_test", "object": "chat.completion.chunk", "created": 1,
               "model": "anthropic/claude-haiku-5.5", "choices": [{"index": 0, "delta": delta,
               "finish_reason": ("content_filter" if refusal else "tool_calls" if tool_reply else "stop") if i == len(deltas) - 1 else None}]}
              for i, delta in enumerate(deltas)]
    return ("".join(f"data: {json.dumps(event)}\n\n" for event in events) + "data: [DONE]\n\n").encode()


@pytest.mark.asyncio
@pytest.mark.parametrize("provider,model,old_model", PATHS)
@pytest.mark.parametrize("streaming", [False, True])
async def test_adaptive_low_exact_wire_body(wire, provider, model, old_model, streaming):
    req = request(provider, model, params=ChatParams(thinking=THINKING, output_config=OUTPUT))
    transport = LiteLLMTransport()
    if streaming:
        events = [event async for event in transport.stream(req, KEY)]
        assert [event.type for event in events][-1] == "finish", events
    else:
        await transport.chat(req, KEY)
    body = wire.bodies[-1]
    assert wire.kwargs[-1]["drop_params"] is True
    assert "reasoning_effort" not in wire.kwargs[-1]
    if provider == "anthropic":
        assert body["thinking"] == THINKING
        assert body["output_config"] == OUTPUT
        assert "reasoning" not in body
    else:
        assert body["reasoning"] == {"enabled": True, "effort": "low"}
        assert "thinking" not in body
        assert "output_config" not in body
    assert not {"reasoning_effort", "temperature", "top_p"} & body.keys()


@pytest.mark.asyncio
@pytest.mark.parametrize("provider,model,old_model", PATHS)
async def test_tool_loop_round_trip(wire, provider, model, old_model):
    wire.tool_reply = True
    transport = LiteLLMTransport()
    req = request(provider, model, params=ChatParams(thinking=THINKING, output_config=OUTPUT), tools=TOOLS)
    reply = await transport.chat(req, KEY)
    message = reply.choices[0].message
    assert message.content is None
    assert message.thinking_blocks == blocks(provider)
    req.messages.extend([
        MessageParam(**message.model_dump()),
        MessageParam(role="tool", tool_call_id="toolu_test", content="result"),
    ])
    await transport.chat(req, KEY)
    body = wire.bodies[-1]
    assistant = body["messages"][1]
    if provider == "anthropic":
        assert body["thinking"] == THINKING  # LiteLLM's missing-block guard did not fire.
        assert assistant["content"] == ANTHROPIC_BLOCKS + [
            {"type": "tool_use", "id": "toolu_test", "name": "lookup", "input": {"query": "test"}},
        ]
    else:
        assert body["reasoning"] == {"enabled": True, "effort": "low"}
        assert assistant["reasoning_details"] == OPENROUTER_BLOCKS
        assert assistant["tool_calls"] == [TOOL_CALL]
        assert "thinking_blocks" not in assistant


@pytest.mark.asyncio
@pytest.mark.parametrize("provider,model,old_model", PATHS)
async def test_stream_keeps_thinking_separate(wire, provider, model, old_model):
    req = request(provider, model, params=ChatParams(thinking=THINKING, output_config=OUTPUT))
    events = [event async for event in LiteLLMTransport().stream(req, KEY)]
    assert "".join(event.data.delta for event in events if event.type == "token") == "Answer"
    assert events[-1].type == "finish"
    assert events[-1].data.thinking_blocks == blocks(provider)


@pytest.mark.asyncio
@pytest.mark.parametrize("provider,model,old_model", PATHS)
@pytest.mark.parametrize("streaming", [False, True])
async def test_refusal_passes_through(wire, provider, model, old_model, streaming):
    wire.refusal = True
    req = request(provider, model, params=ChatParams(thinking=THINKING, output_config=OUTPUT))
    transport = LiteLLMTransport()
    if streaming:
        events = [event async for event in transport.stream(req, KEY)]
        assert events[-1].type == "finish"
        assert events[-1].data.finish_reason == "content_filter"
    else:
        assert (await transport.chat(req, KEY)).choices[0].finish_reason == "content_filter"


@pytest.mark.asyncio
@pytest.mark.parametrize("provider,model,old_model", PATHS)
async def test_45_wire_regression(wire, provider, model, old_model):
    await LiteLLMTransport().chat(request(provider, old_model, params=ChatParams(max_tokens=64)), KEY)
    # Frozen from the installed transformation before implementation, including key order.
    expected = {
        "anthropic": b'{"model": "claude-haiku-4-5", "messages": [{"role": "user", "content": [{"type": "text", "text": "hi"}]}], "max_tokens": 64}',
        "openrouter": b'{"model": "anthropic/claude-haiku-4.5", "messages": [{"role": "user", "content": "hi"}], "stream": false, "max_tokens": 64, "usage": {"include": true}}',
    }
    assert wire.raw_bodies[-1] == expected[provider]


@pytest.mark.asyncio
@pytest.mark.parametrize("provider,model,old_model", PATHS)
@pytest.mark.parametrize("params", [None, ChatParams()])
async def test_no_gateway_defaults(wire, provider, model, old_model, params):
    await LiteLLMTransport().chat(request(provider, model, params=params), KEY)
    assert not {"thinking", "output_config", "reasoning", "reasoning_effort", "temperature", "top_p"} & wire.bodies[-1].keys()


@pytest.mark.asyncio
@pytest.mark.parametrize("provider,model,old_model", PATHS)
@pytest.mark.parametrize("supplied,reasoning", [
    ({"thinking": {"type": "adaptive"}}, {"enabled": True}),
    ({"thinking": {"type": "disabled"}}, {"enabled": False}),
    ({"thinking": {"type": "enabled", "budget_tokens": 2048}}, {"enabled": True, "max_tokens": 2048}),
    ({"output_config": {"effort": "medium"}}, {"effort": "medium"}),
    ({"output_config": {"effort": "high"}}, {"effort": "high"}),
])
async def test_only_explicit_settings_are_forwarded(wire, provider, model, old_model, supplied, reasoning):
    await LiteLLMTransport().chat(request(provider, model, params=ChatParams(**supplied)), KEY)
    body = wire.bodies[-1]
    if provider == "openrouter":
        assert body["reasoning"] == reasoning
        assert "thinking" not in body and "output_config" not in body
    else:
        for key in ("thinking", "output_config"):
            assert body.get(key) == supplied.get(key)


def test_reasoning_registered_without_download():
    assert litellm.supports_reasoning("claude-haiku-5-5", custom_llm_provider="anthropic")


@pytest.mark.asyncio
@pytest.mark.parametrize("provider,model,old_model", PATHS)
async def test_stream_finish_cached_message_and_replay(wire, monkeypatch, provider, model, old_model):
    from starlette.requests import Request
    from src.llm_service.api.rest import chat as api
    from src.llm_service.schemas.chat import ChatRequest
    from src.shared.guardrails.base import GuardrailsResult

    wire.tool_reply = True
    monkeypatch.setattr(api.registry, "resolve", lambda *args: LiteLLMTransport())
    cache = SimpleNamespace(get=AsyncMock(return_value=None), set=AsyncMock())
    body = ChatRequest(**request(provider, model, params=ChatParams(thinking=THINKING, output_config=OUTPUT), tools=TOOLS).model_dump())
    dependencies = dict(
        request=Request({"type": "http", "headers": []}), tenant=SimpleNamespace(id="test-tenant"),
        secret_backend=SimpleNamespace(get_key=AsyncMock(return_value=KEY)),
        policy_checker=SimpleNamespace(check=AsyncMock()),
        guardrails=SimpleNamespace(check_input=AsyncMock(return_value=GuardrailsResult())),
        cache=cache, db=SimpleNamespace(add=Mock(), commit=AsyncMock(), rollback=AsyncMock()),
    )
    response = await api.chat_stream(body, **dependencies)
    events = [part async for part in response.body_iterator]
    finish = json.loads(next(part.split("data: ", 1)[1] for part in events if part.startswith("event: finish")))
    assert finish["thinking_blocks"] == blocks(provider)
    assert not any(part.startswith("event: token") for part in events)
    cached = cache.set.call_args.args[1]
    message = cached.choices[0].message
    assert message.content is None
    assert message.thinking_blocks == blocks(provider)
    assert message.tool_calls[0]["function"] == TOOL_CALL["function"]
    cache.get.return_value = cached
    calls = len(wire.bodies)
    response = await api.chat_stream(body, **dependencies)
    replay = [part async for part in response.body_iterator]
    replay_finish = json.loads(next(part.split("data: ", 1)[1] for part in replay if part.startswith("event: finish")))
    assert replay_finish["thinking_blocks"] == blocks(provider)
    assert len(wire.bodies) == calls
    # The assembled gateway message is also suitable for the next tool-loop call.
    continuation = request(provider, model, params=body.params, tools=TOOLS, messages=[
        MessageParam(role="user", content="hi"), MessageParam(**message.model_dump()),
        MessageParam(role="tool", tool_call_id="toolu_test", content="result"),
    ])
    await LiteLLMTransport().chat(continuation, KEY)
    assistant = wire.bodies[-1]["messages"][1]
    if provider == "anthropic":
        assert assistant["content"][:3] == ANTHROPIC_BLOCKS
        assert wire.bodies[-1]["thinking"] == THINKING
    else:
        assert assistant["reasoning_details"] == OPENROUTER_BLOCKS


@pytest.mark.parametrize("effort", ["max", "minimal", "invalid"])
def test_effort_is_limited(effort):
    with pytest.raises(ValidationError):
        ChatParams(output_config={"effort": effort})


@pytest.mark.parametrize("provider,model,old_model", PATHS)
@pytest.mark.parametrize("tokens,expected", [(100000, 0.0105), (100001, 0.0525005)])
@pytest.mark.parametrize("reported", [None, 0.001, 0.1])
def test_55_yaml_pricing_and_reported_cost(provider, model, old_model, tokens, expected, reported):
    from src.llm_service.api.rest.chat import _compute_cost
    cost = _compute_cost(provider, model, UsageBlock(input_tokens=tokens, output_tokens=1000), reported)
    assert cost.computed_usd == pytest.approx(max(expected, reported or 0) if provider == "openrouter" else expected)


def test_narration_with_thinking_is_text_only():
    raw = SimpleNamespace(_hidden_params={"original_response": [
        ANTHROPIC_BLOCKS[0], {"type": "text", "text": "Searching"},
        {"type": "server_tool_use", "id": "srv", "name": "web_search", "input": {}},
        {"type": "web_search_tool_result", "tool_use_id": "srv", "content": []},
        ANTHROPIC_BLOCKS[1], {"type": "text", "text": "Answer"},
    ]})
    assert _dedupe_leading_narration(raw, "SearchingAnswer") == "Answer"


@pytest.mark.asyncio
async def test_anthropic_batch_settings(monkeypatch):
    create = AsyncMock(return_value=SimpleNamespace(id="batch_test"))
    monkeypatch.setattr("src.llm_service.providers.anthropic.anthropic.AsyncAnthropic", Mock(return_value=
        SimpleNamespace(messages=SimpleNamespace(batches=SimpleNamespace(create=create)))))
    req = request("anthropic", "claude-haiku-5-5", params=ChatParams(thinking=THINKING, output_config=OUTPUT))
    job = SimpleNamespace(id="job", normalised_request=req.model_dump(exclude_none=True))
    await AnthropicTransport().batch_submit([job], KEY)
    params = create.call_args.kwargs["requests"][0]["params"]
    assert params["thinking"] == THINKING
    assert params["output_config"] == OUTPUT
    assert "reasoning_effort" not in params


@pytest.mark.asyncio
async def test_anthropic_batch_reply_keeps_raw_thinking(monkeypatch):
    from anthropic.types import Message

    message = Message(
        id="msg_test", role="assistant", type="message", model="claude-haiku-5-5",
        content=ANTHROPIC_BLOCKS + [{"type": "text", "text": "Answer"}],
        stop_reason="end_turn", usage={"input_tokens": 10, "output_tokens": 5},
    )

    async def results():
        yield SimpleNamespace(custom_id="job", result=SimpleNamespace(type="succeeded", message=message))

    batches = SimpleNamespace(
        retrieve=AsyncMock(return_value=SimpleNamespace(processing_status="ended")),
        results=AsyncMock(return_value=results()),
    )
    monkeypatch.setattr("src.llm_service.providers.anthropic.anthropic.AsyncAnthropic", Mock(return_value=
        SimpleNamespace(messages=SimpleNamespace(batches=batches))))
    job = SimpleNamespace(id="job", request_id="req_test", tenant_id="tenant", provider="anthropic", model="claude-haiku-5-5")
    await AnthropicTransport().batch_poll("batch_test", [job], KEY)
    returned = job.result["choices"][0]["message"]
    assert returned["content"] == "Answer"
    assert returned["thinking_blocks"] == ANTHROPIC_BLOCKS
