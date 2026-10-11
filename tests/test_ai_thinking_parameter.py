from __future__ import annotations

import json
from typing import Any

import httpx
import pytest
from openai import BadRequestError
from test_ai_providers import (
    SSE,
    TOOLS,
    anthropic_tool,
    chat,
    chat_tool,
    responses_tool,
    text_response,
)

from chiliarch.ai import ModelTurn, Provider
from chiliarch.config import ModelSettings

JSONObject = dict[str, Any]


@pytest.mark.parametrize("explicit", [None, False, True])
async def test_disabling_parameter_connects_to_gateway_rejecting_even_false_values(
    explicit: bool | None,
) -> None:
    bodies: list[JSONObject] = []

    async def handle(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        bodies.append(body)
        if "enable_thinking" in body:
            return httpx.Response(
                400,
                json={
                    "error": {
                        "message": "Validation: Unsupported parameter(s): enable_thinking",
                        "type": "invalid_request_error",
                    }
                },
            )
        return httpx.Response(
            200,
            headers={"content-type": "text/event-stream"},
            stream=SSE(
                [chat({"reasoning_content": "check state"}), *text_response("chat_completions")]
            ),
        )

    params = {} if explicit is None else {"enable_thinking": explicit}
    for enabled in (True, False):
        config = ModelSettings(
            model="test",
            retries=0,
            reasoning_enabled=True,
            reasoning_effort="high",
            enable_thinking_param_enabled=enabled,
            request_params=params,
        )
        provider = Provider(
            config, http_client=httpx.AsyncClient(transport=httpx.MockTransport(handle))
        )
        turn = ModelTurn()
        try:
            if enabled:
                with pytest.raises(BadRequestError) as error:
                    async for _ in provider.stream([], [], "system", turn):
                        pass
                assert error.value.status_code == 400
            else:
                events = [event async for event in provider.stream([], [], "system", turn)]
                assert turn.content == "Done"
                assert turn.reasoning == "check state"
                assert any(event.type == "reasoning_delta" for event in events)
            assert config.request_params == params
        finally:
            await provider.close()
    assert len(bodies) == 2
    assert bodies[0]["enable_thinking"] is (True if explicit is None else explicit)
    assert "enable_thinking" not in bodies[1]
    assert bodies[1]["reasoning_effort"] == "high"


@pytest.mark.parametrize(
    ("reasoning", "explicit", "expected"),
    [(True, None, True), (False, None, None), (True, False, False), (False, True, True)],
)
async def test_default_parameter_control_preserves_automatic_and_explicit_requests(
    reasoning: bool, explicit: bool | None, expected: bool | None
) -> None:
    bodies: list[JSONObject] = []

    async def handle(request: httpx.Request) -> httpx.Response:
        bodies.append(json.loads(request.content))
        return httpx.Response(
            200,
            headers={"content-type": "text/event-stream"},
            stream=SSE(text_response("chat_completions")),
        )

    provider = Provider(
        ModelSettings(
            model="test",
            reasoning_enabled=reasoning,
            request_params={} if explicit is None else {"enable_thinking": explicit},
        ),
        http_client=httpx.AsyncClient(transport=httpx.MockTransport(handle)),
    )
    try:
        turn = ModelTurn()
        async for _ in provider.stream([], [], "system", turn):
            pass
        assert turn.content == "Done"
        if expected is None:
            assert "enable_thinking" not in bodies[0]
        else:
            assert bodies[0]["enable_thinking"] is expected
    finally:
        await provider.close()


@pytest.mark.parametrize("mode", ["chat_completions", "responses", "anthropic"])
async def test_parameter_control_preserves_native_thinking_and_reasoning_replay(mode: str) -> None:
    bodies: list[JSONObject] = []
    initial = {
        "chat_completions": chat_tool,
        "responses": responses_tool,
        "anthropic": anthropic_tool,
    }[mode]()

    async def handle(request: httpx.Request) -> httpx.Response:
        bodies.append(json.loads(request.content))
        return httpx.Response(
            200,
            headers={"content-type": "text/event-stream"},
            stream=SSE(initial if len(bodies) == 1 else text_response(mode)),
        )

    provider = Provider(
        ModelSettings.model_validate(
            {
                "api_mode": mode,
                "model": "test",
                "reasoning_enabled": True,
                "reasoning_effort": "high",
                "enable_thinking_param_enabled": False,
                "thinking_enabled": True,
                "thinking_budget_tokens": 1024,
                "anthropic_max_tokens": 8192,
                "request_params": {"enable_thinking": True, "custom_option": 7},
            }
        ),
        http_client=httpx.AsyncClient(transport=httpx.MockTransport(handle)),
    )
    try:
        turn = ModelTurn()
        history = [{"role": "user", "content": "Inspect a"}]
        async for _ in provider.stream(history, TOOLS, "system", turn):
            pass
        assert turn.reasoning == "check state"
        assert turn.tool_calls[0].name == "inspect"
        history.extend(
            [
                turn.message(mode),
                {"role": "tool", "tool_call_id": "call-1", "content": '{"online":true}'},
            ]
        )
        second = ModelTurn()
        async for _ in provider.stream(history, TOOLS, "system", second):
            pass
        assert second.content == "Done"
        assert all("enable_thinking" not in body and body["custom_option"] == 7 for body in bodies)
        if mode == "chat_completions":
            assert bodies[1]["reasoning_effort"] == "high"
            assert bodies[1]["messages"][-2]["reasoning_content"] == "check state"
        elif mode == "responses":
            assert bodies[1]["reasoning"]["effort"] == "high"
            assert any(item.get("type") == "reasoning" for item in bodies[1]["input"])
        else:
            assert bodies[1]["thinking"] == {"type": "enabled", "budget_tokens": 1024}
            assert bodies[1]["messages"][-2]["content"][0]["signature"] == "signed-value"
    finally:
        await provider.close()
