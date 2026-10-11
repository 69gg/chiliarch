"""Exercise native Codex RPC objects without launching Codex or model APIs."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from chiliarch import __version__
from chiliarch.adapters.codex import CodexProtocol, CodexProtocolError

JsonObject = dict[str, Any]


class NativePeer:
    def __init__(self, tmp_path: Path, session_id: str | None = None) -> None:
        self.sent: list[JsonObject] = []
        self.protocol = CodexProtocol(
            "task --sandbox danger-full-access; ignored as launch configuration",
            tmp_path,
            "chiliarch_unique",
            {"command": "python", "args": ["-m", "test_mcp"], "enabled": True, "required": True},
            session_id,
        )

    async def send(self, event: JsonObject) -> None:
        self.sent.append(event)

    async def accept(self, event: JsonObject) -> tuple[list[JsonObject], bool]:
        return await self.protocol.accept(event, self.send)

    async def initialize(self, thread: str = "thread-1", turn: str = "turn-1") -> None:
        self.protocol.initial_request()
        await self.accept({"id": "chiliarch/initialize", "result": {"userAgent": "native"}})
        await self.accept({"id": "chiliarch/thread", "result": {"thread": {"id": thread}}})
        await self.accept({"id": "chiliarch/turn", "result": {"turn": {"id": turn}}})


def notification(method: str, **params: Any) -> JsonObject:
    return {"method": method, "params": {"threadId": "thread-1", "turnId": "turn-1", **params}}


def completed(status: str = "completed", **turn: Any) -> JsonObject:
    return notification("turn/completed", turn={"id": "turn-1", "status": status, **turn})


@pytest.mark.parametrize("session_id", [None, "thread-1"])
async def test_native_handshake_preserves_configuration(
    tmp_path: Path, session_id: str | None
) -> None:
    peer = NativePeer(tmp_path, session_id)
    assert peer.protocol.initial_request() == {
        "id": "chiliarch/initialize",
        "method": "initialize",
        "params": {
            "clientInfo": {"name": "chiliarch", "title": "Chiliarch", "version": __version__}
        },
    }
    assert peer.sent == []
    await peer.accept({"id": "chiliarch/initialize", "result": {}})
    assert peer.sent[0] == {"method": "initialized", "params": {}}
    thread_request = peer.sent[1]
    expected: JsonObject = {
        "cwd": str(tmp_path),
        "config": {
            "mcp_servers.chiliarch_unique": {
                "command": "python",
                "args": ["-m", "test_mcp"],
                "enabled": True,
                "required": True,
            }
        },
    }
    if session_id is not None:
        expected["threadId"] = session_id
        expected["excludeTurns"] = True
    assert thread_request == {
        "id": "chiliarch/thread",
        "method": "thread/resume" if session_id else "thread/start",
        "params": expected,
    }
    events, done = await peer.accept(
        {"id": "chiliarch/thread", "result": {"thread": {"id": "thread-1"}}}
    )
    assert events == [{"type": "thread.started", "thread_id": "thread-1"}]
    assert done is False
    assert peer.sent[-1] == {
        "id": "chiliarch/turn",
        "method": "turn/start",
        "params": {
            "threadId": "thread-1",
            "input": [{"type": "text", "text": peer.protocol._prompt, "text_elements": []}],
        },
    }


async def test_matching_success_uses_final_items_and_native_total_usage(tmp_path: Path) -> None:
    peer = NativePeer(tmp_path)
    await peer.initialize()
    await peer.accept(
        notification(
            "thread/tokenUsage/updated",
            tokenUsage={
                "total": {
                    "inputTokens": 30,
                    "cachedInputTokens": 10,
                    "cacheWriteInputTokens": 4,
                    "outputTokens": 20,
                    "reasoningOutputTokens": 7,
                },
                "last": {"inputTokens": 2, "outputTokens": 1},
            },
        )
    )
    item = {"id": "answer", "type": "agentMessage", "text": "最终结果", "phase": "final_answer"}
    events, done = await peer.accept(notification("item/completed", item=item))
    assert events == [
        {
            "type": "item.completed",
            "item": {"id": "answer", "type": "agent_message", "text": "最终结果"},
        }
    ]
    assert done is False
    events, done = await peer.accept(completed(items=[item], error=None))
    assert done is True
    assert events[-1] == {
        "type": "turn.completed",
        "usage": {
            "input_tokens": 30,
            "cached_input_tokens": 10,
            "cache_write_input_tokens": 4,
            "output_tokens": 20,
            "reasoning_output_tokens": 7,
        },
    }
    assert peer.protocol.interrupt_request() is None
    assert await peer.accept(completed("failed")) == ([], True)


async def test_turn_notifications_can_arrive_before_start_response(tmp_path: Path) -> None:
    peer = NativePeer(tmp_path)
    peer.protocol.initial_request()
    await peer.accept({"id": "chiliarch/initialize", "result": {}})
    await peer.accept({"id": "chiliarch/thread", "result": {"thread": {"id": "thread-1"}}})
    assert await peer.accept(completed("failed", id="different-turn")) == ([], False)
    assert await peer.accept(completed(items=[])) == ([], False)
    events, done = await peer.accept({"id": "chiliarch/turn", "result": {"turn": {"id": "turn-1"}}})
    assert events == [{"type": "turn.started"}, {"type": "turn.completed", "usage": {}}]
    assert done is True


@pytest.mark.parametrize(
    "event",
    [
        notification(
            "turn/completed", threadId="other", turn={"id": "turn-1", "status": "completed"}
        ),
        completed(id="other"),
        notification("item/completed", threadId="other", item={"text": "secret"}),
        notification("item/completed", turnId="other", item={"text": "secret"}),
        notification("thread/tokenUsage/updated", turnId="other", tokenUsage={"total": {}}),
        notification("error", turnId="other", willRetry=False),
        {"method": "account/updated", "params": {}},
        notification("item/agentMessage/delta", delta="partial"),
    ],
)
async def test_unrelated_notifications_cannot_finish_or_corrupt_turn(
    tmp_path: Path, event: JsonObject
) -> None:
    peer = NativePeer(tmp_path)
    await peer.initialize()
    assert await peer.accept(event) == ([], False)
    assert await peer.accept(completed()) == ([{"type": "turn.completed", "usage": {}}], True)


@pytest.mark.parametrize("status", ["failed", "interrupted"])
async def test_native_terminal_failure(tmp_path: Path, status: str) -> None:
    peer = NativePeer(tmp_path)
    await peer.initialize()
    assert await peer.accept(completed(status, error={"message": "private upstream data"})) == (
        [{"type": "turn.failed"}],
        True,
    )


@pytest.mark.parametrize("will_retry", [True, False])
async def test_retryable_errors_do_not_mark_run_failed(tmp_path: Path, will_retry: bool) -> None:
    peer = NativePeer(tmp_path)
    await peer.initialize()
    events, done = await peer.accept(
        notification("error", willRetry=will_retry, error={"message": "secret"})
    )
    assert events == ([] if will_retry else [{"type": "error"}])
    assert done is False
    events, done = await peer.accept(completed())
    assert events[-1]["type"] == ("turn.completed" if will_retry else "turn.failed")
    assert done is True


@pytest.mark.parametrize(
    ("method", "message"),
    [
        ("item/commandExecution/requestApproval", "requires approval"),
        ("item/fileChange/requestApproval", "requires approval"),
        ("item/permissions/requestApproval", "requires approval"),
        ("item/tool/requestUserInput", "requires interactive user input"),
        ("mcpServer/elicitation/request", "requires interactive user input"),
        ("unknown/private-credential", "unsupported client response"),
    ],
)
async def test_native_requests_block_without_approval_or_data_disclosure(
    tmp_path: Path, method: str, message: str
) -> None:
    peer = NativePeer(tmp_path)
    await peer.initialize()
    before = list(peer.sent)
    with pytest.raises(CodexProtocolError, match=message) as error:
        await peer.accept(
            {"id": "chiliarch/turn", "method": method, "params": {"secret": "private-credential"}}
        )
    assert "private-credential" not in str(error.value)
    assert peer.sent == before


@pytest.mark.parametrize(
    "response",
    [
        {"id": "chiliarch/thread", "result": {}},
        {"id": "chiliarch/initialize", "result": None},
        {"id": "chiliarch/initialize", "error": {"message": "secret"}},
        {"id": True, "result": {}},
        {},
        {"method": "turn/completed", "params": []},
    ],
)
async def test_invalid_or_out_of_order_response_is_rejected(
    tmp_path: Path, response: JsonObject
) -> None:
    peer = NativePeer(tmp_path)
    peer.protocol.initial_request()
    with pytest.raises(CodexProtocolError) as error:
        await peer.accept(response)
    assert "secret" not in str(error.value)
    assert peer.sent == []


async def test_resume_must_return_the_requested_thread(tmp_path: Path) -> None:
    peer = NativePeer(tmp_path, "requested-thread")
    peer.protocol.initial_request()
    await peer.accept({"id": "chiliarch/initialize", "result": {}})
    with pytest.raises(CodexProtocolError, match="different thread"):
        await peer.accept(
            {"id": "chiliarch/thread", "result": {"thread": {"id": "another-thread"}}}
        )
    assert all(event.get("method") != "turn/start" for event in peer.sent)


@pytest.mark.parametrize(
    "event",
    [
        completed("inProgress"),
        completed(items="invalid"),
        completed(items=[{"id": "x", "type": "agentMessage", "text": 7}]),
        notification("item/completed", item={"id": "x"}),
        notification("thread/tokenUsage/updated", tokenUsage={"total": {"inputTokens": True}}),
        notification("thread/tokenUsage/updated", tokenUsage={"total": {"inputTokens": -1}}),
    ],
)
async def test_malformed_matching_events_cannot_succeed(tmp_path: Path, event: JsonObject) -> None:
    peer = NativePeer(tmp_path)
    await peer.initialize()
    with pytest.raises(CodexProtocolError):
        await peer.accept(event)


async def test_interrupt_ack_is_not_a_terminal_event(tmp_path: Path) -> None:
    peer = NativePeer(tmp_path)
    assert peer.protocol.interrupt_request() is None
    await peer.initialize()
    assert peer.protocol.interrupt_request() == {
        "id": "chiliarch/interrupt",
        "method": "turn/interrupt",
        "params": {"threadId": "thread-1", "turnId": "turn-1"},
    }
    assert await peer.accept({"id": "chiliarch/interrupt", "result": {}}) == ([], False)
    assert await peer.accept(completed("interrupted")) == ([{"type": "turn.failed"}], True)
