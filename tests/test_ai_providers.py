from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from typing import Any

import httpx
import pytest
from pydantic import SecretStr, ValidationError

from chiliarch.ai import ModelError, ModelTurn, Provider
from chiliarch.config import ModelSettings

JSONObject = dict[str, Any]
TOOLS: list[JSONObject] = [
    {
        "name": "inspect",
        "description": "Inspect an agent",
        "parameters": {
            "type": "object",
            "properties": {"agent_id": {"type": "string"}},
            "required": ["agent_id"],
        },
        "risk": "low",
        "mutating": False,
    }
]


@pytest.mark.parametrize(
    ("mode", "limit"),
    [
        ("chat_completions", "max_tokens"),
        ("chat_completions", "max_completion_tokens"),
        ("responses", "max_output_tokens"),
    ],
)
async def test_openai_defaults_to_no_output_cap_and_honors_explicit_provider_limit(
    mode: str, limit: str
) -> None:
    bodies: list[JSONObject] = []

    async def handle(request: httpx.Request) -> httpx.Response:
        bodies.append(json.loads(request.content))
        return httpx.Response(
            200, headers={"content-type": "text/event-stream"}, stream=SSE(text_response(mode))
        )

    for params in ({}, {limit: 32768, "custom_option": 7}):
        provider = Provider(
            ModelSettings.model_validate(
                {
                    "api_mode": mode,
                    "model": "test",
                    "anthropic_max_tokens": 4096,
                    "request_params": params,
                }
            ),
            http_client=httpx.AsyncClient(transport=httpx.MockTransport(handle)),
        )
        try:
            turn = ModelTurn()
            async for _ in provider.stream(
                [{"role": "user", "content": "hello"}], [], "system", turn
            ):
                pass
            assert turn.content == "Done"
        finally:
            await provider.close()
    limits = {"max_tokens", "max_completion_tokens", "max_output_tokens"}
    assert not limits.intersection(bodies[0])
    assert limits.intersection(bodies[1]) == {limit}
    assert bodies[1][limit] == 32768
    assert bodies[1]["custom_option"] == 7


@pytest.mark.parametrize(
    ("explicit", "legacy", "expected"),
    [(32768, None, 32768), (None, 8192, 8192), (4096, 8192, 4096), (4096, "invalid", 4096)],
)
async def test_anthropic_uses_explicit_output_limit_before_legacy_request_parameter(
    explicit: int | None, legacy: object, expected: int
) -> None:
    bodies: list[JSONObject] = []

    async def handle(request: httpx.Request) -> httpx.Response:
        bodies.append(json.loads(request.content))
        return httpx.Response(
            200,
            headers={"content-type": "text/event-stream"},
            stream=SSE(text_response("anthropic")),
        )

    config = ModelSettings(
        api_mode="anthropic",
        model="test",
        anthropic_max_tokens=explicit,
        request_params={"max_tokens": legacy, "custom_option": 7},
    )
    provider = Provider(
        config, http_client=httpx.AsyncClient(transport=httpx.MockTransport(handle))
    )
    try:
        turn = ModelTurn()
        async for _ in provider.stream([{"role": "user", "content": "hello"}], [], "system", turn):
            pass
        assert turn.content == "Done"
        assert bodies[0]["max_tokens"] == expected
        assert bodies[0]["custom_option"] == 7
        assert config.request_params["max_tokens"] == legacy
    finally:
        await provider.close()


@pytest.mark.parametrize("legacy", [None, 0, -1, True, 1.5, "private-token", {}])
async def test_anthropic_requires_output_limit_before_any_network_request(legacy: object) -> None:
    requests: list[httpx.Request] = []

    async def handle(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        raise AssertionError("An invalid Anthropic configuration must not contact the provider")

    config = ModelSettings(
        api_mode="anthropic",
        model="test",
        request_params={"max_tokens": legacy, "private_key": "private-token"},
    )
    assert config.anthropic_max_tokens is None
    provider = Provider(
        config, http_client=httpx.AsyncClient(transport=httpx.MockTransport(handle))
    )
    try:
        with pytest.raises(ModelError, match="model.anthropic_max_tokens") as error:
            async for _ in provider.stream([], [], "system", ModelTurn()):
                pass
        assert "private-token" not in str(error.value)
        assert requests == []
    finally:
        await provider.close()


@pytest.mark.parametrize("value", [0, -1, True, 1.5, "4096"])
def test_anthropic_output_limit_configuration_requires_a_positive_integer(value: object) -> None:
    with pytest.raises(ValidationError):
        ModelSettings.model_validate({"anthropic_max_tokens": value})


@pytest.mark.parametrize("mode", ["chat_completions", "responses", "anthropic"])
async def test_context_usage_includes_cached_input_without_double_counting(mode: str) -> None:
    events = {
        "chat_completions": chat_tool,
        "responses": responses_tool,
        "anthropic": anthropic_tool,
    }[mode]()
    if mode == "anthropic":
        events[0]["message"]["usage"].update(
            {"cache_creation_input_tokens": 120, "cache_read_input_tokens": 300}
        )
    elif mode == "responses":
        events[-1]["response"]["usage"]["input_tokens_details"] = {"cached_tokens": 7}
    else:
        events[-1]["usage"]["prompt_tokens_details"] = {"cached_tokens": 7}

    async def handle(_: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200, headers={"content-type": "text/event-stream"}, stream=SSE(events)
        )

    provider = Provider(
        ModelSettings.model_validate(
            {"api_mode": mode, "model": "test", "anthropic_max_tokens": 4096}
        ),
        http_client=httpx.AsyncClient(transport=httpx.MockTransport(handle)),
    )
    try:
        turn = ModelTurn()
        async for _ in provider.stream(
            [{"role": "user", "content": "inspect"}], TOOLS, "system", turn
        ):
            pass
        assert turn.usage["input_tokens"] == (430 if mode == "anthropic" else 10)
        assert turn.usage["output_tokens"] == 3
        assert turn.usage["total_tokens"] == (433 if mode == "anthropic" else 13)
    finally:
        await provider.close()


class SSE(httpx.AsyncByteStream):
    def __init__(
        self, events: list[JSONObject], gate: asyncio.Event | None = None, pause_before: int = 1
    ) -> None:
        self.events, self.gate = events, gate
        self.pause_before = pause_before
        self.paused = asyncio.Event()
        self.closed = False

    async def __aiter__(self) -> AsyncIterator[bytes]:
        for index, event in enumerate(self.events):
            if index == self.pause_before and self.gate is not None:
                self.paused.set()
                await self.gate.wait()
            kind = event.get("type")
            prefix = f"event: {kind}\n" if kind else ""
            yield (prefix + "data: " + json.dumps(event) + "\n\n").encode()
        yield b"data: [DONE]\n\n"

    async def aclose(self) -> None:
        self.closed = True


def chat(delta: JSONObject, finish: str | None = None) -> JSONObject:
    return {
        "id": "response",
        "object": "chat.completion.chunk",
        "created": 1,
        "model": "test",
        "choices": [{"index": 0, "delta": delta, "finish_reason": finish}],
    }


def chat_tool() -> list[JSONObject]:
    return [
        chat({"content": "Working "}),
        chat(
            {
                "reasoning_content": "check state",
                "reasoning_details": [{"type": "reasoning.encrypted", "data": "opaque"}],
            }
        ),
        chat(
            {
                "tool_calls": [
                    {
                        "index": 0,
                        "id": "call-1",
                        "type": "function",
                        "function": {"name": "inspect", "arguments": '{"agent_id":'},
                    }
                ]
            }
        ),
        chat({"tool_calls": [{"index": 0, "function": {"arguments": '"a"}'}}]}, "tool_calls"),
        {
            "id": "response",
            "object": "chat.completion.chunk",
            "created": 1,
            "model": "test",
            "choices": [],
            "usage": {"prompt_tokens": 10, "completion_tokens": 3, "total_tokens": 13},
        },
    ]


def responses_tool() -> list[JSONObject]:
    output = [
        {
            "id": "r-1",
            "type": "reasoning",
            "summary": [{"type": "summary_text", "text": "check state"}],
            "encrypted_content": "opaque",
        },
        {
            "id": "f-1",
            "type": "function_call",
            "call_id": "call-1",
            "name": "inspect",
            "arguments": '{"agent_id":"a"}',
            "status": "completed",
        },
    ]
    return [
        {
            "type": "response.output_text.delta",
            "delta": "Working ",
            "output_index": 0,
            "content_index": 0,
            "item_id": "m-1",
            "sequence_number": 0,
            "logprobs": [],
        },
        {
            "type": "response.reasoning_summary_text.delta",
            "delta": "check state",
            "output_index": 0,
            "summary_index": 0,
            "item_id": "r-1",
            "sequence_number": 1,
        },
        {
            "type": "response.output_item.done",
            "output_index": 1,
            "item": output[1],
            "sequence_number": 2,
        },
        {
            "type": "response.completed",
            "sequence_number": 3,
            "response": {
                "id": "response",
                "object": "response",
                "created_at": 1,
                "status": "completed",
                "model": "test",
                "output": output,
                "usage": {"input_tokens": 10, "output_tokens": 3, "total_tokens": 13},
            },
        },
    ]


def anthropic_tool() -> list[JSONObject]:
    return [
        {
            "type": "message_start",
            "message": {
                "id": "response",
                "type": "message",
                "role": "assistant",
                "model": "test",
                "content": [],
                "usage": {"input_tokens": 10, "output_tokens": 0},
            },
        },
        {
            "type": "content_block_start",
            "index": 0,
            "content_block": {"type": "thinking", "thinking": "", "signature": ""},
        },
        {
            "type": "content_block_delta",
            "index": 0,
            "delta": {"type": "thinking_delta", "thinking": "check state"},
        },
        {
            "type": "content_block_delta",
            "index": 0,
            "delta": {"type": "signature_delta", "signature": "signed-value"},
        },
        {"type": "content_block_stop", "index": 0},
        {
            "type": "content_block_start",
            "index": 1,
            "content_block": {"type": "redacted_thinking", "data": "opaque"},
        },
        {"type": "content_block_stop", "index": 1},
        {"type": "content_block_start", "index": 2, "content_block": {"type": "text", "text": ""}},
        {
            "type": "content_block_delta",
            "index": 2,
            "delta": {"type": "text_delta", "text": "Working "},
        },
        {"type": "content_block_stop", "index": 2},
        {
            "type": "content_block_start",
            "index": 3,
            "content_block": {"type": "tool_use", "id": "call-1", "name": "inspect", "input": {}},
        },
        {
            "type": "content_block_delta",
            "index": 3,
            "delta": {"type": "input_json_delta", "partial_json": '{"agent_id":'},
        },
        {
            "type": "content_block_delta",
            "index": 3,
            "delta": {"type": "input_json_delta", "partial_json": '"a"}'},
        },
        {"type": "content_block_stop", "index": 3},
        {
            "type": "message_delta",
            "delta": {"stop_reason": "tool_use", "stop_sequence": None},
            "usage": {"output_tokens": 3},
        },
        {"type": "message_stop"},
    ]


def text_response(api: str) -> list[JSONObject]:
    if api == "chat_completions":
        return [chat({"content": "Done"}, "stop")]
    if api == "responses":
        output = [
            {
                "id": "m-1",
                "type": "message",
                "role": "assistant",
                "status": "completed",
                "content": [{"type": "output_text", "text": "Done", "annotations": []}],
            }
        ]
        return [
            {
                "type": "response.completed",
                "sequence_number": 0,
                "response": {
                    "id": "response-2",
                    "object": "response",
                    "created_at": 1,
                    "status": "completed",
                    "model": "test",
                    "output": output,
                },
            }
        ]
    return [
        {
            "type": "message_start",
            "message": {
                "id": "response-2",
                "type": "message",
                "role": "assistant",
                "model": "test",
                "content": [],
                "usage": {"input_tokens": 12, "output_tokens": 0},
            },
        },
        {"type": "content_block_start", "index": 0, "content_block": {"type": "text", "text": ""}},
        {
            "type": "content_block_delta",
            "index": 0,
            "delta": {"type": "text_delta", "text": "Done"},
        },
        {"type": "content_block_stop", "index": 0},
        {
            "type": "message_delta",
            "delta": {"stop_reason": "end_turn"},
            "usage": {"output_tokens": 1},
        },
        {"type": "message_stop"},
    ]


@pytest.mark.parametrize("same_chunk", [True, False])
async def test_chat_reasoning_precedes_content_only_within_the_same_chunk(
    same_chunk: bool,
) -> None:
    transition = (
        [chat({"content": "你好！", "reasoning_content": "."})]
        if same_chunk
        else [chat({"content": "你好！"}), chat({"reasoning_content": "."})]
    )
    events = [
        chat({"reasoning_content": "Let me respond in Chinese"}),
        *transition,
        chat({"content": "我是 Chiliarch AI。"}, "stop"),
    ]

    async def handle(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200, headers={"content-type": "text/event-stream"}, stream=SSE(events)
        )

    provider = Provider(
        ModelSettings(api_mode="chat_completions", model="test"),
        http_client=httpx.AsyncClient(transport=httpx.MockTransport(handle)),
    )
    turn = ModelTurn()
    try:
        received = [
            (event.type, event.data["delta"])
            async for event in provider.stream(
                [{"role": "user", "content": "你好！"}], [], "system", turn
            )
        ]
    finally:
        await provider.close()
    transition_expected = [("reasoning_delta", "."), ("text_delta", "你好！")]
    if not same_chunk:
        transition_expected.reverse()
    assert received == [
        ("reasoning_delta", "Let me respond in Chinese"),
        *transition_expected,
        ("text_delta", "我是 Chiliarch AI。"),
    ]
    assert turn.reasoning == "Let me respond in Chinese."
    assert turn.content == "你好！我是 Chiliarch AI。"
    assert turn.native[0]["reasoning_content"] == turn.reasoning
    assert turn.native[0]["content"] == turn.content


@pytest.mark.parametrize("api", ["chat_completions", "responses", "anthropic"])
async def test_sdk_stream_tool_result_and_native_reasoning_replay(api: str) -> None:
    requests: list[httpx.Request] = []
    events = {
        "chat_completions": chat_tool,
        "responses": responses_tool,
        "anthropic": anthropic_tool,
    }[api]()

    async def handle(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(
            200,
            headers={"content-type": "text/event-stream"},
            stream=SSE(events if len(requests) == 1 else text_response(api)),
        )

    config = ModelSettings(
        api_mode=api,
        model="test",
        api_key=SecretStr("test-key"),
        base_url="https://upstream.invalid/v1"
        if api != "anthropic"
        else "https://upstream.invalid",
        headers={"X-Trace": "test"},
        request_params={"custom_option": 7},
        anthropic_max_tokens=4096,
    )
    provider = Provider(
        config, http_client=httpx.AsyncClient(transport=httpx.MockTransport(handle))
    )
    turn = ModelTurn()
    received = [
        event
        async for event in provider.stream(
            [{"role": "user", "content": "Inspect a"}], TOOLS, "system", turn
        )
    ]
    assert turn.tool_calls[0].arguments == {"agent_id": "a"}
    assert turn.usage["total_tokens"] == 13
    assert turn.reasoning == "check state"
    assert all("opaque" not in event.data.get("delta", "") for event in received)
    history = [
        {"role": "user", "content": "Inspect a"},
        turn.message(api),
        {"role": "tool", "tool_call_id": "call-1", "content": '{"online":true}'},
    ]
    second = ModelTurn()
    assert [event async for event in provider.stream(history, TOOLS, "system", second)]
    assert second.content == "Done"
    body = json.loads(requests[1].content)
    assert body["stream"] is True and body["custom_option"] == 7
    assert requests[1].headers["X-Trace"] == "test"
    if api == "chat_completions":
        assert body["messages"][2]["reasoning_content"] == "check state"
        assert body["messages"][2]["reasoning_details"] == [
            {"type": "reasoning.encrypted", "data": "opaque"}
        ]
        assert body["messages"][3]["tool_call_id"] == "call-1"
    elif api == "responses":
        assert body["store"] is False and "previous_response_id" not in body
        assert body["input"][1]["encrypted_content"] == "opaque"
        assert body["input"][-1] == {
            "type": "function_call_output",
            "call_id": "call-1",
            "output": '{"online":true}',
        }
    else:
        blocks = body["messages"][1]["content"]
        assert blocks[:2] == [
            {"type": "thinking", "thinking": "check state", "signature": "signed-value"},
            {"type": "redacted_thinking", "data": "opaque"},
        ]
        assert body["messages"][-1]["content"][0]["tool_use_id"] == "call-1"
    await provider.close()


@pytest.mark.parametrize("api", ["chat_completions", "responses", "anthropic"])
async def test_first_display_delta_arrives_before_provider_finishes(api: str) -> None:
    gate = asyncio.Event()
    if api == "anthropic":
        events = text_response(api)
        # Gate after a real text delta, before message_stop, not after message_start.
        before = events[:3]
        after = events[3:]
    else:
        events = (
            [chat({"content": "first"}), chat({}, "stop")]
            if api == "chat_completions"
            else [
                {
                    "type": "response.output_text.delta",
                    "delta": "first",
                    "output_index": 0,
                    "content_index": 0,
                    "item_id": "m-1",
                    "sequence_number": 0,
                    "logprobs": [],
                },
                *text_response(api),
            ]
        )
        before, after = events[:1], events[1:]

    class Controlled(httpx.AsyncByteStream):
        async def __aiter__(self) -> AsyncIterator[bytes]:
            for event in before:
                prefix = f"event: {event['type']}\n" if "type" in event else ""
                yield (prefix + "data: " + json.dumps(event) + "\n\n").encode()
            await gate.wait()
            for event in after:
                prefix = f"event: {event['type']}\n" if "type" in event else ""
                yield (prefix + "data: " + json.dumps(event) + "\n\n").encode()

    async def handle(_: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200, headers={"content-type": "text/event-stream"}, stream=Controlled()
        )

    provider = Provider(
        ModelSettings(
            api_mode=api, model="test", api_key=SecretStr("test"), anthropic_max_tokens=4096
        ),
        http_client=httpx.AsyncClient(transport=httpx.MockTransport(handle)),
    )
    turn = ModelTurn()
    iterator = provider.stream([{"role": "user", "content": "hello"}], [], "system", turn)
    event = await asyncio.wait_for(anext(iterator), 1)
    assert event.type == "text_delta"
    assert not gate.is_set()
    gate.set()
    await asyncio.wait_for(asyncio.create_task(_consume(iterator)), 1)
    await provider.close()


async def _consume(iterator: AsyncIterator[Any]) -> None:
    async for _ in iterator:
        pass


@pytest.mark.parametrize("finish", [None, "length", "content_filter"])
async def test_incomplete_chat_stream_never_produces_executable_tool_calls(
    finish: str | None,
) -> None:
    events = chat_tool()[:-1]
    events[-1]["choices"][0]["finish_reason"] = finish

    async def handle(_: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200, headers={"content-type": "text/event-stream"}, stream=SSE(events)
        )

    provider = Provider(
        ModelSettings(model="test"),
        http_client=httpx.AsyncClient(transport=httpx.MockTransport(handle)),
    )
    turn = ModelTurn()
    with pytest.raises(ModelError):
        await _consume(provider.stream([], TOOLS, "system", turn))
    assert turn.tool_calls == []
    await provider.close()


async def test_invalid_completed_tool_json_fails_before_execution() -> None:
    events = [
        chat(
            {
                "tool_calls": [
                    {"index": 0, "id": "broken", "function": {"name": "inspect", "arguments": "{"}}
                ]
            },
            "tool_calls",
        )
    ]

    async def handle(_: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200, headers={"content-type": "text/event-stream"}, stream=SSE(events)
        )

    provider = Provider(
        ModelSettings(model="test"),
        http_client=httpx.AsyncClient(transport=httpx.MockTransport(handle)),
    )
    with pytest.raises(ModelError):
        await _consume(provider.stream([], TOOLS, "system", ModelTurn()))
    await provider.close()
