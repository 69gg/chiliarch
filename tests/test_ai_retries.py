"""One retry budget covers real SDK HTTP and SSE failures before public output."""

from __future__ import annotations

import asyncio
import json
import logging
from copy import deepcopy
from dataclasses import dataclass, field
from typing import Any

import httpx
import pytest
from anthropic import APIError as AnthropicAPIError
from openai import APIError as OpenAIAPIError
from test_ai_providers import (
    SSE,
    TOOLS,
    anthropic_tool,
    chat,
    chat_tool,
    responses_tool,
    text_response,
)

from chiliarch.ai import ModelError, ModelEvent, ModelTurn, Provider
from chiliarch.config import ModelSettings
from chiliarch.logging import JsonFormatter

JSONObject = dict[str, Any]
APIS = ["chat_completions", "responses", "anthropic"]
MODEL_FAILURES = (ModelError, OpenAIAPIError, AnthropicAPIError)


@dataclass
class Reply:
    events: list[JSONObject] = field(default_factory=list)
    status: int = 200
    headers: dict[str, str] = field(default_factory=dict)
    body: JSONObject | None = None


class Upstream:
    def __init__(self, replies: list[Reply]) -> None:
        self.replies = replies
        self.requests: list[httpx.Request] = []
        self.streams: list[SSE] = []

    async def handle(self, request: httpx.Request) -> httpx.Response:
        index = len(self.requests)
        self.requests.append(request)
        # Repeat the last error if retries exceed the promised budget so the
        # assertion sees the actual network count, without replacing SDK errors.
        reply = self.replies[min(index, len(self.replies) - 1)]
        if reply.status != 200:
            return httpx.Response(reply.status, headers=reply.headers, json=reply.body)
        stream = SSE(deepcopy(reply.events))
        self.streams.append(stream)
        return httpx.Response(
            200,
            headers={"content-type": "text/event-stream", **reply.headers},
            stream=stream,
        )

    def provider(self, api: str, retries: int = 2, **settings: Any) -> Provider:
        return Provider(
            ModelSettings.model_validate(
                {
                    "api_mode": api,
                    "model": "retry-test",
                    "base_url": "https://model.invalid/v1",
                    "anthropic_max_tokens": 4096,
                    "retries": retries,
                    **settings,
                }
            ),
            http_client=httpx.AsyncClient(transport=httpx.MockTransport(self.handle)),
        )


@pytest.fixture(autouse=True)
def retry_delays(monkeypatch: pytest.MonkeyPatch) -> list[float]:
    delays: list[float] = []

    async def sleep(delay: float) -> None:
        delays.append(delay)

    monkeypatch.setattr("chiliarch.ai.providers.sleep", sleep)
    return delays


def stream_error(
    api: str, *, transient: bool = True, message: str = "temporary upstream failure"
) -> JSONObject:
    error_type = (
        ("overloaded_error" if api == "anthropic" else "server_error")
        if transient
        else "invalid_request_error"
    )
    return {
        "type": "error",
        "error": {"type": error_type, "code": error_type, "message": message},
    }


def http_error(status: int, headers: dict[str, str] | None = None) -> Reply:
    kind = "server_error" if status >= 500 else "invalid_request_error"
    return Reply(
        status=status,
        headers=headers or {},
        body={"type": "error", "error": {"type": kind, "code": kind, "message": "HTTP failure"}},
    )


async def consume(provider: Provider, turn: ModelTurn, *, tools: bool = False) -> list[ModelEvent]:
    return [
        event
        async for event in provider.stream(
            [{"role": "user", "content": "hello"}], TOOLS if tools else [], "system", turn
        )
    ]


def visible_prefix(api: str, *, reasoning: bool) -> list[JSONObject]:
    if api == "chat_completions":
        return [chat({"reasoning_content" if reasoning else "content": "Already visible"})]
    if api == "responses":
        event = deepcopy(responses_tool()[1 if reasoning else 0])
        event["delta"] = "Already visible"
        return [event]
    start = deepcopy(anthropic_tool()[0])
    return [
        start,
        {
            "type": "content_block_start",
            "index": 0,
            "content_block": (
                {"type": "thinking", "thinking": "", "signature": ""}
                if reasoning
                else {"type": "text", "text": ""}
            ),
        },
        {
            "type": "content_block_delta",
            "index": 0,
            "delta": (
                {"type": "thinking_delta", "thinking": "Already visible"}
                if reasoning
                else {"type": "text_delta", "text": "Already visible"}
            ),
        },
    ]


def failed_tool_prefix(api: str) -> list[JSONObject]:
    if api == "chat_completions":
        usage = deepcopy(chat_tool()[-1])
        usage["usage"] = {"prompt_tokens": 999, "completion_tokens": 998, "total_tokens": 1997}
        return [
            usage,
            chat({"reasoning_details": [{"type": "reasoning.encrypted", "data": "failed-native"}]}),
            chat(
                {
                    "tool_calls": [
                        {
                            "index": 0,
                            "id": "failed-call",
                            "type": "function",
                            "function": {"name": "inspect", "arguments": '{"agent_id":'},
                        }
                    ]
                }
            ),
        ]
    if api == "responses":
        final = deepcopy(responses_tool()[-1])
        final["response"]["output"] = [
            {
                "id": "failed-reasoning",
                "type": "reasoning",
                "summary": [],
                "encrypted_content": "failed-native",
            },
            {
                "id": "failed-function",
                "type": "function_call",
                "call_id": "failed-call",
                "name": "inspect",
                "arguments": '{"agent_id":"failed-agent"}',
                "status": "completed",
            },
        ]
        final["response"]["usage"] = {
            "input_tokens": 999,
            "output_tokens": 998,
            "total_tokens": 1997,
        }
        return [final]
    start = deepcopy(anthropic_tool()[0])
    start["message"]["usage"] = {"input_tokens": 999, "output_tokens": 998}
    return [
        start,
        {
            "type": "content_block_start",
            "index": 0,
            "content_block": {"type": "redacted_thinking", "data": "failed-native"},
        },
        {"type": "content_block_stop", "index": 0},
        {
            "type": "content_block_start",
            "index": 1,
            "content_block": {
                "type": "tool_use",
                "id": "failed-call",
                "name": "inspect",
                "input": {},
            },
        },
        {
            "type": "content_block_delta",
            "index": 1,
            "delta": {"type": "input_json_delta", "partial_json": '{"agent_id":'},
        },
    ]


@pytest.mark.parametrize("api", APIS)
async def test_sdk_sse_error_retries_once_and_preserves_original_request(
    api: str, retry_delays: list[float]
) -> None:
    upstream = Upstream([Reply([stream_error(api)]), Reply(text_response(api))])
    provider = upstream.provider(api)
    turn = ModelTurn()
    try:
        events = await consume(provider, turn)
        assert turn.content == "Done" and turn.tool_calls == []
        assert [event.data["delta"] for event in events if event.type == "text_delta"] == ["Done"]
        assert len(upstream.requests) == 2 and len(retry_delays) == 1
        assert json.loads(upstream.requests[0].content) == json.loads(upstream.requests[1].content)
        assert all(stream.closed for stream in upstream.streams)
    finally:
        await provider.close()


@pytest.mark.parametrize("api", APIS)
@pytest.mark.parametrize("retries", [0, 2])
async def test_sse_error_exhaustion_uses_exactly_one_plus_configured_retries(
    api: str, retries: int, retry_delays: list[float]
) -> None:
    upstream = Upstream([Reply([stream_error(api)])])
    provider = upstream.provider(api, retries)
    turn = ModelTurn()
    try:
        with pytest.raises(MODEL_FAILURES):
            await consume(provider, turn)
        assert len(upstream.requests) == 1 + retries
        assert len(retry_delays) == retries
        assert turn == ModelTurn()
        assert all(stream.closed for stream in upstream.streams)
    finally:
        await provider.close()


@pytest.mark.parametrize("api", APIS)
@pytest.mark.parametrize("retries", [1, 2])
async def test_http_and_sse_failures_share_one_budget_without_hidden_sdk_retries(
    api: str, retries: int, retry_delays: list[float]
) -> None:
    upstream = Upstream([http_error(503), Reply([stream_error(api)]), Reply(text_response(api))])
    provider = upstream.provider(api, retries)
    turn = ModelTurn()
    try:
        if retries == 1:
            with pytest.raises(MODEL_FAILURES):
                await consume(provider, turn)
            assert turn == ModelTurn()
        else:
            await consume(provider, turn)
            assert turn.content == "Done"
        assert len(upstream.requests) == 1 + retries
        assert len(retry_delays) == retries
    finally:
        await provider.close()


@pytest.mark.parametrize("api", APIS)
@pytest.mark.parametrize("reasoning", [False, True])
async def test_transient_stream_failure_after_public_output_does_not_repeat(
    api: str, reasoning: bool, retry_delays: list[float]
) -> None:
    upstream = Upstream(
        [
            Reply([*visible_prefix(api, reasoning=reasoning), stream_error(api)]),
            Reply(text_response(api)),
        ]
    )
    provider = upstream.provider(api)
    turn = ModelTurn()
    events: list[ModelEvent] = []
    try:
        with pytest.raises(MODEL_FAILURES):
            async for event in provider.stream([], [], "system", turn):
                events.append(event)
        assert len(upstream.requests) == 1 and retry_delays == []
        assert turn.reasoning == ("Already visible" if reasoning else "")
        assert turn.content == ("" if reasoning else "Already visible")
        public_type = "reasoning_delta" if reasoning else "text_delta"
        assert [event.data["delta"] for event in events if event.type == public_type] == [
            "Already visible"
        ]
        assert turn.tool_calls == []
    finally:
        await provider.close()


@pytest.mark.parametrize("api", APIS)
async def test_failed_attempt_usage_native_and_tool_fragments_do_not_pollute_success(
    api: str,
) -> None:
    success = {
        "chat_completions": chat_tool,
        "responses": responses_tool,
        "anthropic": anthropic_tool,
    }[api]()
    baseline = Upstream([Reply(success)])
    clean_provider = baseline.provider(api)
    clean_turn = ModelTurn()
    try:
        clean_events = await consume(clean_provider, clean_turn, tools=True)
    finally:
        await clean_provider.close()
    upstream = Upstream([Reply([*failed_tool_prefix(api), stream_error(api)]), Reply(success)])
    provider = upstream.provider(api)
    turn = ModelTurn()
    try:
        events = await consume(provider, turn, tools=True)
        assert len(upstream.requests) == 2
        assert turn == clean_turn
        assert events == clean_events
        assert [call.as_dict() for call in turn.tool_calls] == [
            {"id": "call-1", "name": "inspect", "arguments": {"agent_id": "a"}}
        ]
        assert all(stream.closed for stream in upstream.streams)
    finally:
        await provider.close()


@pytest.mark.parametrize("api", APIS)
@pytest.mark.parametrize("status", [400, 401, 403])
async def test_permanent_http_errors_do_not_retry(
    api: str, status: int, retry_delays: list[float]
) -> None:
    upstream = Upstream([http_error(status), Reply(text_response(api))])
    provider = upstream.provider(api)
    try:
        with pytest.raises(MODEL_FAILURES):
            await consume(provider, ModelTurn())
        assert len(upstream.requests) == 1 and retry_delays == []
    finally:
        await provider.close()


@pytest.mark.parametrize("api", APIS)
async def test_structured_sse_validation_error_does_not_retry(
    api: str, retry_delays: list[float]
) -> None:
    upstream = Upstream([Reply([stream_error(api, transient=False)]), Reply(text_response(api))])
    provider = upstream.provider(api)
    try:
        with pytest.raises(MODEL_FAILURES):
            await consume(provider, ModelTurn())
        assert len(upstream.requests) == 1 and retry_delays == []
    finally:
        await provider.close()


@pytest.mark.parametrize("api", APIS)
async def test_truncated_stream_without_completion_does_not_retry(
    api: str, retry_delays: list[float]
) -> None:
    events = deepcopy(anthropic_tool()[:1]) if api == "anthropic" else []
    upstream = Upstream([Reply(events), Reply(text_response(api))])
    provider = upstream.provider(api)
    try:
        with pytest.raises(ModelError):
            await consume(provider, ModelTurn())
        assert len(upstream.requests) == 1 and retry_delays == []
    finally:
        await provider.close()


@pytest.mark.parametrize("api", APIS)
async def test_invalid_complete_tool_json_fails_without_replaying_a_model_request(
    api: str, retry_delays: list[float]
) -> None:
    if api == "chat_completions":
        events = [
            chat(
                {
                    "tool_calls": [
                        {
                            "index": 0,
                            "id": "bad-call",
                            "type": "function",
                            "function": {"name": "inspect", "arguments": "{"},
                        }
                    ]
                },
                "tool_calls",
            )
        ]
    elif api == "responses":
        final = deepcopy(responses_tool()[-1])
        final["response"]["output"] = [
            {
                "id": "bad-function",
                "type": "function_call",
                "call_id": "bad-call",
                "name": "inspect",
                "arguments": "{",
                "status": "completed",
            }
        ]
        events = [final]
    else:
        events = [
            deepcopy(anthropic_tool()[0]),
            {
                "type": "content_block_start",
                "index": 0,
                "content_block": {
                    "type": "tool_use",
                    "id": "bad-call",
                    "name": "inspect",
                    "input": {},
                },
            },
            {
                "type": "content_block_delta",
                "index": 0,
                "delta": {
                    "type": "input_json_delta",
                    "partial_json": "{",
                },
            },
            {"type": "content_block_stop", "index": 0},
        ]
    upstream = Upstream([Reply(events), Reply(text_response(api))])
    provider = upstream.provider(api)
    turn = ModelTurn()
    try:
        with pytest.raises(ModelError):
            await consume(provider, turn, tools=True)
        assert len(upstream.requests) == 1 and retry_delays == []
        assert turn.tool_calls == []
    finally:
        await provider.close()


@pytest.mark.parametrize("api", APIS)
async def test_local_request_validation_does_not_contact_provider_or_retry(
    api: str, retry_delays: list[float]
) -> None:
    upstream = Upstream([Reply(text_response(api))])
    provider = upstream.provider(api, request_params={"messages": "forbidden replacement"})
    try:
        with pytest.raises(ModelError):
            await consume(provider, ModelTurn())
        assert upstream.requests == [] and retry_delays == []
    finally:
        await provider.close()


@pytest.mark.parametrize(
    ("kind", "code", "retry"),
    [
        ("response.failed", "server_error", True),
        ("response.failed", "invalid_request_error", False),
        ("response.incomplete", "max_output_tokens", False),
    ],
)
async def test_responses_terminal_failure_classification_uses_structured_fields(
    kind: str, code: str, retry: bool, retry_delays: list[float]
) -> None:
    terminal = {
        "type": kind,
        "sequence_number": 0,
        "response": {
            "id": "failed-response",
            "object": "response",
            "created_at": 1,
            "model": "test",
            "status": "failed" if kind == "response.failed" else "incomplete",
            "output": [],
            "error": {"code": code, "message": "Failure detail"},
            "incomplete_details": {"reason": code},
        },
    }
    upstream = Upstream([Reply([terminal]), Reply(text_response("responses"))])
    provider = upstream.provider("responses")
    turn = ModelTurn()
    try:
        if retry:
            await consume(provider, turn)
            assert turn.content == "Done" and len(upstream.requests) == 2
            assert len(retry_delays) == 1
        else:
            with pytest.raises(ModelError):
                await consume(provider, turn)
            assert len(upstream.requests) == 1 and retry_delays == []
    finally:
        await provider.close()


@pytest.mark.parametrize("api", APIS)
@pytest.mark.parametrize(
    ("header", "value", "seconds"), [("retry-after", "2", 2.0), ("retry-after-ms", "125", 0.125)]
)
async def test_retry_after_header_controls_cancellable_provider_backoff(
    api: str, header: str, value: str, seconds: float, retry_delays: list[float]
) -> None:
    upstream = Upstream([http_error(503, {header: value}), Reply(text_response(api))])
    provider = upstream.provider(api)
    try:
        await consume(provider, ModelTurn())
        assert len(upstream.requests) == 2
        assert retry_delays == [seconds]
    finally:
        await provider.close()


@pytest.mark.parametrize("api", APIS)
@pytest.mark.parametrize(("status", "hint", "retry"), [(400, "true", True), (503, "false", False)])
async def test_explicit_provider_retry_hint_takes_precedence_over_http_status(
    api: str, status: int, hint: str, retry: bool, retry_delays: list[float]
) -> None:
    upstream = Upstream([http_error(status, {"x-should-retry": hint}), Reply(text_response(api))])
    provider = upstream.provider(api)
    turn = ModelTurn()
    try:
        if retry:
            await consume(provider, turn)
            assert turn.content == "Done" and len(upstream.requests) == 2
            assert len(retry_delays) == 1
        else:
            with pytest.raises(MODEL_FAILURES):
                await consume(provider, turn)
            assert len(upstream.requests) == 1 and retry_delays == []
    finally:
        await provider.close()


@pytest.mark.parametrize("api", APIS)
@pytest.mark.parametrize("allow_retry", [False, True])
async def test_http_200_sse_errors_preserve_retry_directives_from_stream_headers(
    api: str, allow_retry: bool, retry_delays: list[float]
) -> None:
    headers = {"retry-after": "3", "x-should-retry": "true" if allow_retry else "false"}
    upstream = Upstream(
        [
            Reply([stream_error(api)], headers=headers),
            Reply(text_response(api)),
        ]
    )
    provider = upstream.provider(api)
    turn = ModelTurn()
    try:
        if allow_retry:
            await consume(provider, turn)
            assert turn.content == "Done" and len(upstream.requests) == 2
            assert retry_delays == [3.0]
        else:
            with pytest.raises(MODEL_FAILURES):
                await consume(provider, turn)
            assert len(upstream.requests) == 1 and retry_delays == []
    finally:
        await provider.close()


async def test_excessive_retry_after_does_not_retry_or_hold_a_run(
    retry_delays: list[float],
) -> None:
    upstream = Upstream(
        [http_error(503, {"retry-after": "121"}), Reply(text_response("chat_completions"))]
    )
    provider = upstream.provider("chat_completions")
    try:
        with pytest.raises(MODEL_FAILURES):
            await consume(provider, ModelTurn())
        assert len(upstream.requests) == 1 and retry_delays == []
    finally:
        await provider.close()


async def test_cancelling_retry_sleep_prevents_the_next_attempt(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    entered = asyncio.Event()
    cancelled = asyncio.Event()

    async def blocking_sleep(_: float) -> None:
        entered.set()
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.set()

    monkeypatch.setattr("chiliarch.ai.providers.sleep", blocking_sleep)
    upstream = Upstream(
        [Reply([stream_error("chat_completions")]), Reply(text_response("chat_completions"))]
    )
    provider = upstream.provider("chat_completions")
    turn = ModelTurn()
    task = asyncio.create_task(consume(provider, turn))
    try:
        await asyncio.wait_for(entered.wait(), 2)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert cancelled.is_set() and len(upstream.requests) == 1
        assert turn == ModelTurn() and upstream.streams[0].closed
    finally:
        if not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        await provider.close()


@pytest.mark.parametrize("api", APIS)
async def test_retry_logs_do_not_include_upstream_error_body_or_credentials(
    api: str, caplog: pytest.LogCaptureFixture
) -> None:
    error_message = "provider private detail: upstream-secret"
    upstream = Upstream(
        [Reply([stream_error(api, message=error_message)]), Reply(text_response(api))]
    )
    provider = upstream.provider(
        api,
        api_key="configured-private-api-key",
        headers={"X-Private": "configured-private-header"},
    )
    try:
        with caplog.at_level(logging.INFO, logger="chiliarch.ai"):
            await consume(provider, ModelTurn())
        records = [record for record in caplog.records if record.name.startswith("chiliarch.ai.")]
        assert records and len(upstream.requests) == 2
        rendered = [JsonFormatter().format(record) for record in records]
        for line in rendered:
            assert error_message not in line
            assert "upstream-secret" not in line
            assert "configured-private-api-key" not in line
            assert "configured-private-header" not in line
    finally:
        await provider.close()
