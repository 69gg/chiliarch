"""Interactive native RPCs use durable outbound messages without blocking stdout."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

import pytest

from chiliarch.adapters.codex import CodexProtocol, CodexProtocolError
from chiliarch.adapters.native import request_native_interaction
from chiliarch.adapters.opencode import OpenCodeProtocol
from chiliarch.adapters.runners import RunnerHandler, RunnerSettings
from chiliarch.client import ClientStore
from chiliarch.protocol import Envelope

JsonObject = dict[str, Any]


async def codex(directory: Path) -> tuple[CodexProtocol, list[JsonObject]]:
    protocol = CodexProtocol("task", directory, "mcp", {"command": "fixture"}, None)
    sent: list[JsonObject] = []

    async def send(frame: JsonObject) -> None:
        sent.append(frame)

    protocol.initial_request()
    await protocol.accept({"id": "chiliarch/initialize", "result": {}}, send)
    await protocol.accept({"id": "chiliarch/thread", "result": {"thread": {"id": "thread"}}}, send)
    await protocol.accept({"id": "chiliarch/turn", "result": {"turn": {"id": "turn"}}}, send)
    return protocol, sent


@pytest.mark.parametrize(
    "method, choice",
    [
        ("item/commandExecution/requestApproval", "accept"),
        ("item/commandExecution/requestApproval", "decline"),
        ("item/fileChange/requestApproval", "acceptForSession"),
        ("item/fileChange/requestApproval", "cancel"),
    ],
)
async def test_codex_preserves_native_permission_choice(
    tmp_path: Path, method: str, choice: str
) -> None:
    protocol, sent = await codex(tmp_path)
    requests: list[JsonObject] = []

    async def send(frame: JsonObject) -> None:
        sent.append(frame)

    async def interact(request: JsonObject) -> JsonObject:
        requests.append(request)
        return {"choice": choice}

    params = {
        "threadId": "thread",
        "turnId": "turn",
        "command": "literal command",
        "availableDecisions": [choice],
    }
    await protocol.handle_request({"id": 1, "method": method, "params": params}, send, interact)
    assert requests[0]["details"] == params
    assert requests[0]["options"][0]["id"] == choice
    assert sent[-1] == {"id": 1, "result": {"decision": choice}}


async def test_codex_rejects_unoffered_decision_and_other_turn(tmp_path: Path) -> None:
    protocol, sent = await codex(tmp_path)

    async def send(frame: JsonObject) -> None:
        sent.append(frame)

    async def interact(request: JsonObject) -> JsonObject:
        return {"choice": "acceptForSession"}

    event = {
        "id": 1,
        "method": "item/commandExecution/requestApproval",
        "params": {
            "threadId": "thread",
            "turnId": "turn",
            "availableDecisions": ["accept", "decline"],
        },
    }
    before = len(sent)
    with pytest.raises(CodexProtocolError):
        await protocol.handle_request(event, send, interact)
    event["params"]["turnId"] = "another"
    with pytest.raises(CodexProtocolError):
        await protocol.handle_request(event, send, interact)
    assert len(sent) == before


async def test_codex_maps_question_answers_and_retains_secret_flag(tmp_path: Path) -> None:
    protocol, sent = await codex(tmp_path)
    requests: list[JsonObject] = []

    async def send(frame: JsonObject) -> None:
        sent.append(frame)

    async def interact(request: JsonObject) -> JsonObject:
        requests.append(request)
        return {"fields": {"location": "0", "password": "actual secret"}}

    questions = [
        {
            "id": "location",
            "question": "Where?",
            "options": [{"label": "Local", "description": "Use local"}],
        },
        {"id": "password", "question": "Password?", "isSecret": True, "options": None},
    ]
    await protocol.handle_request(
        {
            "id": 8,
            "method": "item/tool/requestUserInput",
            "params": {"threadId": "thread", "turnId": "turn", "questions": questions},
        },
        send,
        interact,
    )
    assert requests[0]["fields"][1]["secret"] is True
    assert sent[-1] == {
        "id": 8,
        "result": {
            "answers": {
                "location": {"answers": ["Local"]},
                "password": {"answers": ["actual secret"]},
            }
        },
    }


@pytest.mark.parametrize("choice", ["once", "always", "reject"])
async def test_opencode_permission_response_uses_original_option_id(
    tmp_path: Path, choice: str
) -> None:
    protocol = OpenCodeProtocol("task", tmp_path, "mcp", {"command": "fixture"}, None)
    sent: list[JsonObject] = []

    async def send(frame: JsonObject) -> None:
        sent.append(frame)

    async def interact(request: JsonObject) -> JsonObject:
        assert request["details"]["toolCall"]["title"] == "Edit file"
        return {"choice": choice}

    protocol.initial_request()
    await protocol.accept(
        {
            "jsonrpc": "2.0",
            "id": "chiliarch/initialize",
            "result": {"protocolVersion": 1, "agentCapabilities": {}},
        },
        send,
    )
    await protocol.accept(
        {"jsonrpc": "2.0", "id": "chiliarch/session", "result": {"sessionId": "session"}}, send
    )
    options = [
        {"optionId": "once", "name": "Allow once", "kind": "allow_once"},
        {"optionId": "always", "name": "Always", "kind": "allow_always"},
        {"optionId": "reject", "name": "Reject", "kind": "reject_once"},
    ]
    await protocol.handle_request(
        {
            "jsonrpc": "2.0",
            "id": 3,
            "method": "session/request_permission",
            "params": {
                "sessionId": "session",
                "toolCall": {"title": "Edit file"},
                "options": options,
            },
        },
        send,
        interact,
    )
    assert sent[-1] == {
        "jsonrpc": "2.0",
        "id": 3,
        "result": {"outcome": {"outcome": "selected", "optionId": choice}},
    }


async def wait_requests(store: ClientStore, count: int = 1) -> list[Envelope]:
    async with asyncio.timeout(5):
        while True:
            requests = [item for item in await store.pending() if item.type == "native.request"]
            if len(requests) >= count:
                return requests
            await asyncio.sleep(0.01)


async def test_native_mailbox_answer_must_match_both_request_and_task(tmp_path: Path) -> None:
    path = tmp_path / "identity.db"
    store = ClientStore(path)
    await store.open()
    request = asyncio.create_task(
        request_native_interaction(
            path,
            "task",
            {"kind": "permission", "title": "Proceed?", "native_method": "fixture"},
            poll_seconds=0.01,
        )
    )
    try:
        message = (await wait_requests(store))[0]
        interaction = message.payload["interaction_id"]
        await store.accept(
            Envelope(
                type="native.response",
                payload={
                    "interaction_id": interaction,
                    "task_id": "other-task",
                    "status": "answered",
                    "answer": {"choice": "accept"},
                },
            )
        )
        await asyncio.sleep(0.03)
        assert not request.done()
        await store.accept(
            Envelope(
                type="native.response",
                payload={
                    "interaction_id": interaction,
                    "task_id": "task",
                    "status": "answered",
                    "answer": {"choice": "decline"},
                },
            )
        )
        assert await asyncio.wait_for(request, 2) == {"choice": "decline"}
    finally:
        request.cancel()
        await asyncio.gather(request, return_exceptions=True)
        await store.close()


def interactive_cli(directory: Path) -> Path:
    """A real subprocess implementing only a small fixture app-server protocol."""
    script = directory / "interactive.py"
    script.write_text(
        """import json, sys
from pathlib import Path
def read():
    return json.loads(sys.stdin.readline())
def send(frame):
    print(json.dumps(frame), flush=True)
read()
send({"id":"chiliarch/initialize","result":{}})
read()
read()
send({"id":"chiliarch/thread","result":{"thread":{"id":"thread"}}})
read()
params={"threadId":"thread","turnId":"turn","itemId":"item"}
send({"id":10,"method":"item/commandExecution/requestApproval","params":{**params,"command":"check project","availableDecisions":["accept","decline"]}})
send({"id":11,"method":"item/tool/requestUserInput","params":{**params,"questions":[{"id":"choice","question":"Which one?","options":[{"label":"Option A","description":"first"}]}]}})
# Requests arrive before the native turn response: the output reader must keep draining.
send({"id":"chiliarch/turn","result":{"turn":{"id":"turn"}}})
answers=[read(),read()]
Path("native-answers.json").write_text(json.dumps(answers))
send({"method":"turn/completed","params":{"threadId":"thread","turn":{"id":"turn","status":"completed","items":[{"id":"answer","type":"agentMessage","text":"All done"}]}}})
sys.stdin.read()
""",
        encoding="utf-8",
    )
    return script


async def test_runner_handles_overlapping_native_requests_without_blocking_stdout(
    tmp_path: Path,
) -> None:
    path = tmp_path / "identity.db"
    handler = RunnerHandler(
        RunnerSettings(
            kind="codex",
            workdir=tmp_path,
            executable=str(interactive_cli(tmp_path)),
            timeout_seconds=10,
        ),
        path,
    )
    store = ClientStore(path)
    await store.open()

    async def progress(value: float, message: str) -> None:
        pass

    worker = asyncio.create_task(
        handler({"task_id": "task", "instruction": "Use fixture", "parameters": {}}, progress)
    )
    try:
        requests = await wait_requests(store, 2)
        assert handler.runtime_metadata()["native_interactions"]["pending_task_ids"] == ["task"]
        for request in reversed(requests):
            answer = (
                {"choice": "accept"}
                if request.payload["kind"] == "permission"
                else {"fields": {"choice": "0"}}
            )
            await store.accept(
                Envelope(
                    type="native.response",
                    payload={
                        "interaction_id": request.payload["interaction_id"],
                        "task_id": "task",
                        "status": "answered",
                        "answer": answer,
                    },
                )
            )
        result = await asyncio.wait_for(worker, 5)
        assert result["answer"] == "All done"
        frames = json.loads((tmp_path / "native-answers.json").read_text())
        assert {frame["id"] for frame in frames} == {10, 11}
        assert next(frame["result"] for frame in frames if frame["id"] == 10) == {
            "decision": "accept"
        }
        assert next(frame["result"] for frame in frames if frame["id"] == 11) == {
            "answers": {"choice": {"answers": ["Option A"]}}
        }
        assert handler.runtime_metadata()["native_interactions"]["pending_task_ids"] == []
    finally:
        worker.cancel()
        await asyncio.gather(worker, return_exceptions=True)
        await store.close()


async def test_runner_cancellation_joins_native_waiters_and_releases_workspace(
    tmp_path: Path,
) -> None:
    from chiliarch.adapters.runtime import workspace_lock_path

    path = tmp_path / "identity.db"
    handler = RunnerHandler(
        RunnerSettings(
            kind="codex",
            workdir=tmp_path,
            executable=str(interactive_cli(tmp_path)),
            timeout_seconds=10,
            terminate_grace_seconds=0.1,
        ),
        path,
    )
    store = ClientStore(path)
    await store.open()

    async def progress(value: float, message: str) -> None:
        pass

    worker = asyncio.create_task(
        handler({"task_id": "task", "instruction": "Use fixture", "parameters": {}}, progress)
    )
    try:
        await wait_requests(store, 2)
        worker.cancel()
        with pytest.raises(asyncio.CancelledError):
            await worker
        assert handler.runtime_metadata()["native_interactions"]["pending_task_ids"] == []
        assert not workspace_lock_path(tmp_path).with_suffix(".unverified.json").exists()
    finally:
        worker.cancel()
        await asyncio.gather(worker, return_exceptions=True)
        await store.close()


@pytest.mark.parametrize("response_turn, accepted", [("turn", True), ("another", False)])
async def test_codex_steer_binds_exact_turn_and_requires_native_ack(
    tmp_path: Path, response_turn: str, accepted: bool
) -> None:
    protocol, sent = await codex(tmp_path)

    async def send(frame: JsonObject) -> None:
        sent.append(frame)

    delivery = asyncio.create_task(protocol.steer("message-1", "Peer update", send))
    await asyncio.sleep(0)
    assert sent[-1] == {
        "id": "message-1",
        "method": "turn/steer",
        "params": {
            "threadId": "thread",
            "expectedTurnId": "turn",
            "input": [{"type": "text", "text": "Peer update", "text_elements": []}],
        },
    }
    assert not delivery.done()
    await protocol.accept({"id": "message-1", "result": {"turnId": response_turn}}, send)
    assert await delivery is accepted


def peer_message(message_id: str = "peer-1", task_id: str = "task") -> Envelope:
    return Envelope(
        type="collaboration.message",
        payload={
            "message_id": message_id,
            "sender_agent_id": "peer-agent",
            "sender_role": "agent",
            "recipient_agent_id": "current-agent",
            "task_id": task_id,
            "message": "API changed to account_id",
            "sequence": 1,
        },
    )


@pytest.mark.parametrize("accepted", [True, False])
async def test_native_forwarding_journals_before_send_and_never_replays_uncertain_delivery(
    tmp_path: Path, accepted: bool
) -> None:
    from chiliarch.adapters.native import forward_native_messages

    path = tmp_path / "identity.db"
    store = ClientStore(path)
    await store.open()
    await store.accept(peer_message("unrelated", "another-task"))
    await store.accept(peer_message())
    stopped = asyncio.Event()
    sent: list[str] = []

    async def steer(request_id: str, text: str) -> bool:
        journal = await store.native_deliveries("task")
        assert journal == [
            {
                "task_id": "task",
                "message_id": "peer-1",
                "request_id": request_id,
                "state": "unknown",
            }
        ]
        sent.append(text)
        stopped.set()
        return accepted

    try:
        deliveries = await forward_native_messages(
            path, "task", steer, stopped, max_input_bytes=4096, poll_seconds=0.01
        )
        assert deliveries[0]["state"] == ("delivered" if accepted else "unknown")
        assert len(sent) == 1
        assert json.loads(sent[0].split("\n", 1)[1])["sender_agent_id"] == "peer-agent"
        assert json.loads(sent[0].split("\n", 1)[1])["message_id"] == "peer-1"
        stopped.clear()
        resumed = asyncio.create_task(
            forward_native_messages(
                path, "task", steer, stopped, max_input_bytes=4096, poll_seconds=0.01
            )
        )
        await asyncio.sleep(0.03)
        stopped.set()
        await resumed
        assert len(sent) == 1
        # Native acceptance does not consume the ordinary MCP inbox or report processing.
        assert any(
            message.payload.get("message_id") == "peer-1"
            for _, message in await store.messages(task_id="task")
        )
    finally:
        await store.close()


async def test_running_codex_receives_task_message_and_records_ack(tmp_path: Path) -> None:
    path = tmp_path / "identity.db"
    script = tmp_path / "steer.py"
    script.write_text(
        """import json, sys
from pathlib import Path
def read(): return json.loads(sys.stdin.readline())
def send(frame): print(json.dumps(frame), flush=True)
read()
send({"id":"chiliarch/initialize","result":{}})
read()
read()
send({"id":"chiliarch/thread","result":{"thread":{"id":"thread"}}})
read()
send({"id":"chiliarch/turn","result":{"turn":{"id":"turn"}}})
frame=read()
Path("steered.json").write_text(json.dumps(frame))
send({"id":frame["id"],"result":{"turnId":"turn"}})
send({"method":"turn/completed","params":{"threadId":"thread","turn":{"id":"turn","status":"completed","items":[{"id":"answer","type":"agentMessage","text":"Received peer change"}]}}})
sys.stdin.read()
""",
        encoding="utf-8",
    )
    store = ClientStore(path)
    await store.open()
    await store.accept(peer_message())
    handler = RunnerHandler(
        RunnerSettings(kind="codex", workdir=tmp_path, executable=str(script), timeout_seconds=10),
        path,
    )

    async def progress(value: float, message: str) -> None:
        pass

    try:
        result = await handler(
            {"task_id": "task", "instruction": "Wait for collaborator", "parameters": {}}, progress
        )
        assert result["answer"] == "Received peer change"
        assert result["native_message_deliveries"][0]["state"] == "delivered"
        frame = json.loads((tmp_path / "steered.json").read_text())
        assert frame["method"] == "turn/steer"
        assert frame["params"]["expectedTurnId"] == "turn"
        assert (
            json.loads(frame["params"]["input"][0]["text"].split("\n", 1)[1])["message"]
            == "API changed to account_id"
        )
    finally:
        await store.close()
