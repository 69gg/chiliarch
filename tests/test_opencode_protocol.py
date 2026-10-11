"""ACP v1 contracts shared by OpenCode legacy and current releases."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from chiliarch.adapters.opencode import OpenCodeProtocol, OpenCodeProtocolError

JsonObject = dict[str, Any]


class NativePeer:
    def __init__(self, directory: Path, session_id: str | None = None) -> None:
        self.sent: list[JsonObject] = []
        self.protocol = OpenCodeProtocol(
            prompt="Coordinate this task",
            cwd=directory,
            mcp_name="chiliarch_unique",
            mcp={"command": "python", "args": ["-m", "test_mcp"]},
            session_id=session_id,
        )
        self.initial = self.protocol.initial_request()

    async def send(self, event: JsonObject) -> None:
        self.sent.append(event)

    async def accept(self, event: JsonObject) -> tuple[list[JsonObject], bool]:
        return await self.protocol.accept({"jsonrpc": "2.0", **event}, self.send)

    async def reply(self, result: JsonObject) -> tuple[list[JsonObject], bool]:
        request = self.sent[-1] if self.sent else self.initial
        return await self.accept({"id": request["id"], "result": result})

    async def initialize(self) -> None:
        await self.reply({"protocolVersion": 1, "agentCapabilities": {"loadSession": True}})
        await self.reply({"sessionId": "session-1"})


def update(update_type: str, *, session: str = "session-1", **fields: Any) -> JsonObject:
    return {
        "method": "session/update",
        "params": {"sessionId": session, "update": {"sessionUpdate": update_type, **fields}},
    }


@pytest.mark.parametrize("resume", [False, True])
async def test_handshake_injects_only_current_task_mcp_without_native_policy_overrides(
    tmp_path: Path, resume: bool
) -> None:
    peer = NativePeer(tmp_path, "session-1" if resume else None)
    assert peer.initial["jsonrpc"] == "2.0"
    assert peer.initial["method"] == "initialize"
    assert peer.initial["params"]["protocolVersion"] == 1
    assert peer.initial["params"]["clientCapabilities"] == {}
    await peer.reply({"protocolVersion": 1, "agentCapabilities": {"loadSession": True}})
    session = peer.sent[-1]
    assert session["method"] == ("session/load" if resume else "session/new")
    expected: JsonObject = {
        "cwd": str(tmp_path),
        "mcpServers": [
            {"name": "chiliarch_unique", "command": "python", "args": ["-m", "test_mcp"], "env": []}
        ],
    }
    if resume:
        expected["sessionId"] = "session-1"
    assert session["params"] == expected
    events, done = await peer.reply({} if resume else {"sessionId": "session-1"})
    assert not done
    assert any(event.get("thread_id") == "session-1" for event in events)
    assert peer.sent[-1]["method"] == "session/prompt"
    assert peer.sent[-1]["params"] == {
        "sessionId": "session-1",
        "prompt": [{"type": "text", "text": "Coordinate this task"}],
    }


async def test_message_chunks_concatenate_but_thoughts_and_load_replay_do_not(
    tmp_path: Path,
) -> None:
    peer = NativePeer(tmp_path, "session-1")
    await peer.reply({"protocolVersion": 1, "agentCapabilities": {"loadSession": True}})
    assert await peer.accept(
        update("agent_message_chunk", content={"type": "text", "text": "old answer"})
    ) == ([], False)
    await peer.reply({})
    collected: list[JsonObject] = []
    assert await peer.accept(
        update(
            "agent_message_chunk",
            content={"type": "text", "text": "child answer"},
            _meta={"opencode/child-session": {"sessionId": "child-1"}},
        )
    ) == ([], False)
    for kind, text in (
        ("agent_message_chunk", "已检查"),
        ("agent_thought_chunk", "private thought"),
        ("agent_message_chunk", "工作区。"),
    ):
        events, done = await peer.accept(update(kind, content={"type": "text", "text": text}))
        assert not done
        collected.extend(events)
    events, done = await peer.reply({"stopReason": "end_turn"})
    collected.extend(events)
    assert done
    answers = {
        event["item"]["id"]: event["item"]["text"]
        for event in collected
        if event.get("type") == "item.completed"
        and event.get("item", {}).get("type") == "agent_message"
    }
    assert "".join(answers.values()) == "已检查工作区。"
    assert "private thought" not in str(collected)
    assert peer.protocol.interrupt_request() is None


async def test_native_usage_is_normalized_and_tool_updates_report_progress(tmp_path: Path) -> None:
    peer = NativePeer(tmp_path)
    await peer.initialize()
    events, done = await peer.accept(
        update("tool_call", toolCallId="tool-1", title="inspect", status="in_progress", kind="read")
    )
    assert events and not done
    events, done = await peer.reply(
        {
            "stopReason": "end_turn",
            "usage": {
                "inputTokens": 20,
                "outputTokens": 10,
                "totalTokens": 36,
                "thoughtTokens": 3,
                "cachedReadTokens": 2,
                "cachedWriteTokens": 1,
            },
        }
    )
    assert done
    assert events[-1] == {
        "type": "turn.completed",
        "usage": {
            "input_tokens": 20,
            "output_tokens": 10,
            "reasoning_output_tokens": 3,
            "cache_read_input_tokens": 2,
            "cache_write_input_tokens": 1,
        },
    }


@pytest.mark.parametrize("stop", ["cancelled", "max_tokens", "refusal", "max_turn_requests"])
async def test_unsuccessful_stop_reasons_never_complete(tmp_path: Path, stop: str) -> None:
    peer = NativePeer(tmp_path)
    await peer.initialize()
    events, done = await peer.reply({"stopReason": stop})
    assert done
    assert events[-1]["type"] == "turn.failed"


async def test_permission_request_is_cancelled_instead_of_selecting_allow(tmp_path: Path) -> None:
    peer = NativePeer(tmp_path)
    await peer.initialize()
    with pytest.raises(OpenCodeProtocolError, match="approval") as error:
        await peer.accept(
            {
                "id": "permission-1",
                "method": "session/request_permission",
                "params": {
                    "sessionId": "session-1",
                    "toolCall": {"toolCallId": "tool-1", "title": "fixture-private-content"},
                    "options": [{"optionId": "allow", "name": "Allow", "kind": "allow_once"}],
                },
            }
        )
    assert peer.sent[-1] == {
        "jsonrpc": "2.0",
        "id": "permission-1",
        "result": {"outcome": {"outcome": "cancelled"}},
    }
    assert "fixture-private-content" not in str(error.value)


async def test_unadvertised_client_request_is_safely_rejected(tmp_path: Path) -> None:
    peer = NativePeer(tmp_path)
    await peer.initialize()
    with pytest.raises(OpenCodeProtocolError) as error:
        await peer.accept(
            {"id": 99, "method": "fs/read_text_file", "params": {"path": "private-content"}}
        )
    assert peer.sent[-1]["id"] == 99
    assert peer.sent[-1]["error"]["code"] == -32601
    assert "private-content" not in str(error.value)


async def test_interrupt_is_notification_and_does_not_report_completion(tmp_path: Path) -> None:
    peer = NativePeer(tmp_path)
    assert peer.protocol.interrupt_request() is None
    await peer.initialize()
    assert peer.protocol.interrupt_request() == {
        "jsonrpc": "2.0",
        "method": "session/cancel",
        "params": {"sessionId": "session-1"},
    }
    assert await peer.accept(
        update("agent_message_chunk", session="other", content={"type": "text", "text": "alien"})
    ) == ([], False)


@pytest.mark.parametrize("capabilities", [{}, {"loadSession": False}])
async def test_continuation_requires_native_load_capability(
    tmp_path: Path, capabilities: JsonObject
) -> None:
    peer = NativePeer(tmp_path, "session-1")
    with pytest.raises(OpenCodeProtocolError):
        await peer.reply({"protocolVersion": 1, "agentCapabilities": capabilities})
    assert peer.sent == []


@pytest.mark.parametrize(
    "response",
    [
        {"id": "unknown", "result": {}},
        {"id": "chiliarch/initialize", "result": None},
        {"id": "chiliarch/initialize", "error": {"code": -32000, "message": "private-content"}},
        {"id": "chiliarch/initialize", "result": {"protocolVersion": 2}},
        {"id": True, "result": {}},
        {},
    ],
)
async def test_invalid_handshake_is_rejected_without_sensitive_diagnostics(
    tmp_path: Path, response: JsonObject
) -> None:
    peer = NativePeer(tmp_path)
    with pytest.raises(OpenCodeProtocolError) as error:
        await peer.accept(response)
    assert "private-content" not in str(error.value)
    assert peer.sent == []


async def test_load_must_confirm_the_requested_native_session(tmp_path: Path) -> None:
    peer = NativePeer(tmp_path, "session-1")
    await peer.reply({"protocolVersion": 1, "agentCapabilities": {"loadSession": True}})
    with pytest.raises(OpenCodeProtocolError, match="different session"):
        await peer.reply({"sessionId": "another-session"})
    assert all(event["method"] != "session/prompt" for event in peer.sent)


@pytest.mark.parametrize("content", [None, {"type": "text", "text": 7}])
async def test_malformed_parent_message_cannot_succeed(tmp_path: Path, content: Any) -> None:
    peer = NativePeer(tmp_path)
    await peer.initialize()
    with pytest.raises(OpenCodeProtocolError):
        await peer.accept(update("agent_message_chunk", content=content))


@pytest.mark.parametrize("usage", [{"inputTokens": True}, {"outputTokens": -1}, []])
async def test_invalid_usage_cannot_turn_into_success(tmp_path: Path, usage: Any) -> None:
    peer = NativePeer(tmp_path)
    await peer.initialize()
    with pytest.raises(OpenCodeProtocolError):
        await peer.reply({"stopReason": "end_turn", "usage": usage})
