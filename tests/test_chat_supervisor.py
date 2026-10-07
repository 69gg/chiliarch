from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import httpx
import pytest
from sqlalchemy import select
from test_ai_providers import SSE, chat, text_response

from chiliarch.ai import Provider
from chiliarch.chat import AgentInbox, ChatRun, Supervisor
from chiliarch.config import Settings, SettingsManager
from chiliarch.database import Database, OperationRecord
from chiliarch.protocol import Envelope
from chiliarch.service import OperationService

JSONObject = dict[str, Any]


class Upstream:
    def __init__(self, name: str = "change_state", arguments: JSONObject | None = None) -> None:
        self.name = name
        self.arguments = arguments or {"value": 7}
        self.requests: list[JSONObject] = []
        self.second_gate: asyncio.Event | None = None
        self.first_gate: asyncio.Event | None = None
        self.first_requested = asyncio.Event()
        self.second_requested = asyncio.Event()
        self.first_stream: SSE | None = None

    async def handle(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(json.loads(request.content))
        (self.first_requested if len(self.requests) == 1 else self.second_requested).set()
        if len(self.requests) == 1:
            raw = json.dumps(self.arguments)
            events = [
                chat({"content": "Working "}),
                chat(
                    {
                        "tool_calls": [
                            {
                                "index": 0,
                                "id": "call-1",
                                "function": {"name": self.name, "arguments": raw[:3]},
                            }
                        ]
                    }
                ),
                chat(
                    {"tool_calls": [{"index": 0, "function": {"arguments": raw[3:]}}]}, "tool_calls"
                ),
            ]
            stream = SSE(events, self.first_gate, pause_before=2)
            self.first_stream = stream
        elif self.second_gate is not None:
            gate = self.second_gate

            class Delayed(httpx.AsyncByteStream):
                async def __aiter__(self) -> AsyncIterator[bytes]:
                    yield ("data: " + json.dumps(chat({"content": "Finishing "})) + "\n\n").encode()
                    await gate.wait()
                    yield ("data: " + json.dumps(chat({}, "stop")) + "\n\n").encode()

            stream = Delayed()
        else:
            stream = SSE(text_response("chat_completions"))
        return httpx.Response(200, headers={"content-type": "text/event-stream"}, stream=stream)

    def provider(self, config: Any) -> Provider:
        return Provider(
            config, http_client=httpx.AsyncClient(transport=httpx.MockTransport(self.handle))
        )


async def wait_status(supervisor: Supervisor, run_id: str, status: str) -> JSONObject:
    async with asyncio.timeout(5):
        while True:
            result = await supervisor.get_run(run_id)
            if result["status"] == status:
                return result
            if (
                result["status"] in {"failed", "interrupted", "cancelled"}
                and status != result["status"]
            ):
                raise AssertionError(result)
            await asyncio.sleep(0.01)


def manager(mode: str = "ask", model: str = "test") -> SettingsManager:
    return SettingsManager(
        settings=Settings.model_validate(
            {
                "model": {
                    "model": model,
                    "api_key": "secret",
                    "base_url": "https://upstream.invalid/v1",
                },
                "supervisor": {"mode": mode, "inbox_poll_seconds": 0.02},
            }
        )
    )


def register_change(service: OperationService, effects: list[int], risk: str = "low") -> None:
    async def handler(arguments: JSONObject) -> JSONObject:
        effects.append(arguments["value"])
        return {"status": "changed", "value": arguments["value"]}

    service.register(
        "change_state",
        "Change observable state",
        {
            "type": "object",
            "properties": {"value": {"type": "integer"}},
            "required": ["value"],
            "additionalProperties": False,
        },
        handler,
        risk=risk,
        mutating=True,
    )


@pytest.mark.parametrize("decision", ["approve_operation", "reject_operation"])
async def test_ask_approval_has_no_effect_until_decision_and_replays_once(
    tmp_path: Path, decision: str
) -> None:
    db = Database(f"sqlite+aiosqlite:///{tmp_path / 'state.db'}")
    settings = manager()
    service = OperationService(db, settings)
    effects: list[int] = []
    register_change(service, effects)
    upstream = Upstream()
    supervisor = Supervisor(db, service, settings, provider_factory=upstream.provider)
    await db.initialize()
    await supervisor.start()
    try:
        conversation = await supervisor.create_conversation()
        run = await supervisor.send_message(conversation["id"], "change state")
        suspended = await wait_status(supervisor, run["run_id"], "awaiting_approval")
        assert effects == [] and len(upstream.requests) == 1
        approval_id = suspended["pending_approval_id"]
        assert approval_id
        await service.execute(decision, {"approval_id": approval_id}, actor="user")
        await wait_status(supervisor, run["run_id"], "completed")
        assert effects == ([7] if decision == "approve_operation" else [])
        await service.execute(decision, {"approval_id": approval_id}, actor="user")
        assert effects == ([7] if decision == "approve_operation" else [])
        second_body = upstream.requests[1]
        assert second_body["messages"][-1]["role"] == "tool"
        result = json.loads(second_body["messages"][-1]["content"])
        assert result["status"] == ("changed" if decision == "approve_operation" else "rejected")
        snapshot = await supervisor.get_conversation(conversation["id"])
        assert snapshot["messages"][-1]["content"] == "Working Done"
        assert snapshot["last_event_seq"] > 0
        tool_results = [event for event in snapshot["events"] if event["type"] == "tool_end"]
        assert len(tool_results) == 1
        events = snapshot["events"]
        assert [event["seq"] for event in events] == sorted(event["seq"] for event in events)
        first_text = next(event for event in events if event["type"] == "text_delta")
        tool_start = next(event for event in events if event["type"] == "tool_start")
        final_text = next(
            event
            for event in events
            if event["type"] == "text_delta" and event["seq"] > tool_results[0]["seq"]
        )
        assert first_text["seq"] < tool_start["seq"] < tool_results[0]["seq"] < final_text["seq"]
        assert first_text["data"]["delta"] == "Working "
        assert final_text["data"]["delta"] == "Done"
        assert tool_start["data"]["key"] == tool_results[0]["data"]["key"]
        assert tool_start["data"]["key"].startswith(run["run_id"] + ":")
        assert events[-1]["seq"] == snapshot["last_event_seq"]
        # A later user message sends the native assistant/tool-result chain,
        # rather than an invalid hidden {turns: ...} container or plain summary.
        following = await supervisor.send_message(conversation["id"], "what changed?")
        await wait_status(supervisor, following["run_id"], "completed")
        history = upstream.requests[-1]["messages"]
        assert [item["role"] for item in history] == [
            "system",
            "user",
            "assistant",
            "tool",
            "assistant",
            "user",
        ]
        assert history[3]["tool_call_id"] == "call-1"
    finally:
        await supervisor.stop()
        await db.close()


async def test_pending_approval_survives_restart_without_reexecuting_side_effect(
    tmp_path: Path,
) -> None:
    db = Database(f"sqlite+aiosqlite:///{tmp_path / 'state.db'}")
    settings = manager()
    settings.update({"model": {"request_params": {"auth": {"api_key": "old-key", "limit": 7}}}})
    effects: list[int] = []
    upstream = Upstream()
    service = OperationService(db, settings)
    register_change(service, effects)
    original = Supervisor(db, service, settings, provider_factory=upstream.provider)
    await db.initialize()
    await original.start()
    conversation = await original.create_conversation()
    run = await original.send_message(conversation["id"], "change state")
    suspended = await wait_status(original, run["run_id"], "awaiting_approval")
    await original.stop()
    settings.update({"model": {"request_params": {"auth": {"api_key": "new-key", "limit": 99}}}})
    service = OperationService(db, settings)
    register_change(service, effects)
    restored = Supervisor(db, service, settings, provider_factory=upstream.provider)
    await restored.start()
    try:
        assert (await restored.get_run(run["run_id"]))["status"] == "awaiting_approval"
        assert effects == []
        await service.execute(
            "approve_operation", {"approval_id": suspended["pending_approval_id"]}, actor="user"
        )
        await wait_status(restored, run["run_id"], "completed")
        assert effects == [7]
        assert upstream.requests[-1]["auth"] == {"api_key": "new-key", "limit": 7}
        async with db.sessions() as session:
            records = (
                await session.scalars(
                    select(OperationRecord).where(
                        OperationRecord.actor == "supervisor",
                        OperationRecord.name == "change_state",
                    )
                )
            ).all()
            assert len(records) == 1 and records[0].status == "completed"
    finally:
        await restored.stop()
        await db.close()


async def test_service_shutdown_marks_running_unknown_and_never_repeats_operation(
    tmp_path: Path,
) -> None:
    db = Database(f"sqlite+aiosqlite:///{tmp_path / 'state.db'}")
    settings = manager("yolo")
    effects: list[int] = []
    upstream = Upstream()
    upstream.second_gate = asyncio.Event()
    service = OperationService(db, settings)
    register_change(service, effects)
    supervisor = Supervisor(db, service, settings, provider_factory=upstream.provider)
    await db.initialize()
    await supervisor.start()
    conversation = await supervisor.create_conversation()
    run = await supervisor.send_message(conversation["id"], "change state")
    await asyncio.wait_for(upstream.second_requested.wait(), 5)
    await supervisor.stop()
    assert (await supervisor.get_run(run["run_id"]))["status"] == "interrupted"
    assert effects == [7]
    service = OperationService(db, settings)
    register_change(service, effects)
    restored = Supervisor(db, service, settings, provider_factory=upstream.provider)
    await restored.start()
    try:
        await asyncio.sleep(0.1)
        assert effects == [7] and len(upstream.requests) == 2
    finally:
        await restored.stop()
        await db.close()


async def test_agent_inbox_is_durable_idempotent_and_processed_after_model_configuration(
    tmp_path: Path,
) -> None:
    db = Database(f"sqlite+aiosqlite:///{tmp_path / 'state.db'}")
    settings = manager(model="")
    service = OperationService(db, settings)
    upstream = Upstream("list_agents", {})
    upstream.arguments = {}
    supervisor = Supervisor(db, service, settings, provider_factory=upstream.provider)
    await db.initialize()
    await supervisor.start()
    try:
        envelope = {
            "id": "incoming-1",
            "type": "agent.contact",
            "payload": {"message": "status?"},
            "correlation_id": "task-1",
            "protocol_version": "1.0",
        }
        await supervisor.receive_agent_request("agent-1", envelope)
        await supervisor.receive_agent_request("agent-1", envelope)
        async with db.sessions() as session:
            inbox = (await session.scalars(select(AgentInbox))).all()
        assert len(inbox) == 1 and inbox[0].status == "pending" and not upstream.requests
        settings.update({"model": {"model": "test"}})
        async with asyncio.timeout(5):
            while True:
                async with db.sessions() as session:
                    inbox_row = await session.get(AgentInbox, inbox[0].id)
                if inbox_row and inbox_row.run_id:
                    break
                await asyncio.sleep(0.01)
        await wait_status(supervisor, inbox_row.run_id, "completed")
        payload = json.loads(upstream.requests[0]["messages"][-1]["content"].split("\n", 1)[1])
        assert payload["agent_id"] == "agent-1" and payload["envelope"] == envelope
        assert len((await supervisor.list_conversations())["conversations"]) == 1
    finally:
        await supervisor.stop()
        await db.close()


async def test_partial_tool_arguments_are_not_executed_and_ui_cursor_resumes(
    tmp_path: Path,
) -> None:
    db = Database(f"sqlite+aiosqlite:///{tmp_path / 'state.db'}")
    settings = manager("yolo")
    effects: list[int] = []
    service = OperationService(db, settings)
    register_change(service, effects)
    upstream = Upstream()
    upstream.first_gate = asyncio.Event()
    supervisor = Supervisor(db, service, settings, provider_factory=upstream.provider)
    await db.initialize()
    await supervisor.start()
    try:
        conversation = await supervisor.create_conversation()
        run = await supervisor.send_message(conversation["id"], "change state")
        async with asyncio.timeout(5):
            while True:
                snapshot = await supervisor.get_conversation(conversation["id"])
                if snapshot["messages"][-1]["content"]:
                    break
                await asyncio.sleep(0.01)
        assert snapshot["messages"][-1]["content"] == "Working " and effects == []
        assert upstream.first_stream is not None
        await asyncio.wait_for(upstream.first_stream.paused.wait(), 2)
        assert effects == []
        cursor = snapshot["last_event_seq"]
        upstream.first_gate.set()
        await wait_status(supervisor, run["run_id"], "completed")
        iterator = supervisor.events(conversation["id"], cursor)
        events: list[JSONObject] = []
        async with asyncio.timeout(5):
            async for wire in iterator:
                payload = json.loads(wire.split("data: ", 1)[1])
                events.append(payload)
                if payload["type"] == "completed":
                    break
        await iterator.aclose()
        assert all(event["seq"] > cursor for event in events)
        assert [event["data"]["delta"] for event in events if event["type"] == "text_delta"] == [
            "Done"
        ]
        assert effects == [7]
    finally:
        await supervisor.stop()
        await db.close()


@pytest.mark.parametrize("risk,expected", [("low", "completed"), ("high", "awaiting_approval")])
async def test_auto_assessment_cannot_lower_intrinsic_high_risk(
    tmp_path: Path, risk: str, expected: str
) -> None:
    db = Database(f"sqlite+aiosqlite:///{tmp_path / 'state.db'}")
    settings = manager("auto")
    effects: list[int] = []
    service = OperationService(db, settings)
    register_change(service, effects, risk)
    upstream = Upstream(
        arguments={"value": 7, "_risk": {"level": "low", "reason": "User requested this change"}}
    )
    supervisor = Supervisor(db, service, settings, provider_factory=upstream.provider)
    await db.initialize()
    await supervisor.start()
    try:
        conversation = await supervisor.create_conversation()
        run = await supervisor.send_message(conversation["id"], "change state")
        await wait_status(supervisor, run["run_id"], expected)
        assert effects == ([7] if expected == "completed" else [])
        assert any(
            "_risk" in tool["function"]["parameters"].get("properties", {})
            for tool in upstream.requests[0]["tools"]
        )
    finally:
        await supervisor.stop()
        await db.close()


async def test_running_configuration_snapshot_and_conversation_tools(tmp_path: Path) -> None:
    db = Database(f"sqlite+aiosqlite:///{tmp_path / 'state.db'}")
    settings = manager("yolo")
    effects: list[int] = []
    service = OperationService(db, settings)
    register_change(service, effects)
    upstream = Upstream()
    upstream.first_gate = asyncio.Event()
    supervisor = Supervisor(db, service, settings, provider_factory=upstream.provider)
    await db.initialize()
    await supervisor.start()
    try:
        conversation = await service.execute(
            "create_conversation", {"title": "before"}, actor="user"
        )
        run = await service.execute(
            "send_chat_message",
            {"conversation_id": conversation["id"], "content": "change"},
            actor="user",
        )
        await asyncio.wait_for(upstream.first_requested.wait(), 5)
        settings.update({"model": {"model": "replacement"}})
        upstream.first_gate.set()
        await wait_status(supervisor, run["run_id"], "completed")
        assert [request["model"] for request in upstream.requests] == ["test", "test"]
        renamed = await service.execute(
            "rename_conversation",
            {"conversation_id": conversation["id"], "title": "after"},
            actor="user",
        )
        assert renamed["title"] == "after"
        async with db.sessions() as session:
            saved = await session.get(ChatRun, run["run_id"])
            assert (
                saved is not None
                and "api_key" not in saved.model_snapshot
                and "headers" not in saved.model_snapshot
            )
        await service.execute(
            "delete_conversation", {"conversation_id": conversation["id"]}, actor="user"
        )
        assert (await supervisor.list_conversations())["conversations"] == []
    finally:
        await supervisor.stop()
        await db.close()


async def test_user_cancels_pending_run_withdraws_approval_and_late_approval_has_no_effect(
    tmp_path: Path,
) -> None:
    db = Database(f"sqlite+aiosqlite:///{tmp_path / 'state.db'}")
    settings = manager()
    effects: list[int] = []
    service = OperationService(db, settings)
    register_change(service, effects)
    upstream = Upstream()
    supervisor = Supervisor(db, service, settings, provider_factory=upstream.provider)
    await db.initialize()
    await supervisor.start()
    try:
        conversation = await supervisor.create_conversation()
        response = await supervisor.send_message(conversation["id"], "change")
        pending = await wait_status(supervisor, response["run_id"], "awaiting_approval")
        cancelled = await asyncio.wait_for(
            service.execute("cancel_chat_run", {"run_id": response["run_id"]}, actor="user"), 2
        )
        assert cancelled["status"] == "cancelled"
        # Approval of a withdrawn request returns its existing final result.
        await service.execute(
            "approve_operation", {"approval_id": pending["pending_approval_id"]}, actor="user"
        )
        assert effects == [] and len(upstream.requests) == 1
        assert (await supervisor.get_run(response["run_id"]))["status"] == "cancelled"
    finally:
        await supervisor.stop()
        await db.close()


async def test_multi_tool_turn_resume_keeps_prior_results_and_executes_each_call_once(
    tmp_path: Path,
) -> None:
    db = Database(f"sqlite+aiosqlite:///{tmp_path / 'state.db'}")
    settings = manager()
    effects: list[int] = []
    reads: list[str] = []
    service = OperationService(db, settings)
    register_change(service, effects)

    async def inspect(arguments: JSONObject) -> JSONObject:
        reads.append(arguments["label"])
        return {"label": arguments["label"]}

    service.register(
        "inspect_state",
        "Read state",
        {
            "type": "object",
            "properties": {"label": {"type": "string"}},
            "required": ["label"],
            "additionalProperties": False,
        },
        inspect,
    )
    upstream = Upstream()

    async def handle(request: httpx.Request) -> httpx.Response:
        upstream.requests.append(json.loads(request.content))
        calls = [
            {
                "index": 0,
                "id": "read-before",
                "type": "function",
                "function": {"name": "inspect_state", "arguments": '{"label":"before"}'},
            },
            {
                "index": 1,
                "id": "write",
                "type": "function",
                "function": {"name": "change_state", "arguments": '{"value":7}'},
            },
            {
                "index": 2,
                "id": "read-after",
                "type": "function",
                "function": {"name": "inspect_state", "arguments": '{"label":"after"}'},
            },
        ]
        events = (
            [chat({"tool_calls": calls}, "tool_calls")]
            if len(upstream.requests) == 1
            else text_response("chat_completions")
        )
        return httpx.Response(
            200, headers={"content-type": "text/event-stream"}, stream=SSE(events)
        )

    def provider(config: Any) -> Provider:
        return Provider(
            config, http_client=httpx.AsyncClient(transport=httpx.MockTransport(handle))
        )

    supervisor = Supervisor(db, service, settings, provider_factory=provider)
    await db.initialize()
    await supervisor.start()
    try:
        conversation = await supervisor.create_conversation()
        response = await supervisor.send_message(conversation["id"], "inspect, change, inspect")
        pending = await wait_status(supervisor, response["run_id"], "awaiting_approval")
        assert reads == ["before"] and effects == []
        await service.execute(
            "approve_operation", {"approval_id": pending["pending_approval_id"]}, actor="user"
        )
        await wait_status(supervisor, response["run_id"], "completed")
        assert reads == ["before", "after"] and effects == [7]
        tool_results = [
            message for message in upstream.requests[1]["messages"] if message["role"] == "tool"
        ]
        assert [message["tool_call_id"] for message in tool_results] == [
            "read-before",
            "write",
            "read-after",
        ]
    finally:
        await supervisor.stop()
        await db.close()


async def test_public_tool_events_mask_secrets_without_altering_approved_execution(
    tmp_path: Path,
) -> None:
    db = Database(f"sqlite+aiosqlite:///{tmp_path / 'state.db'}")
    settings = manager()
    settings.update(
        {"model": {"request_params": {"custom_auth": {"api_key": "provider-secret", "limit": 7}}}}
    )
    service = OperationService(db, settings)
    executed: list[JSONObject] = []

    async def save(arguments: JSONObject) -> JSONObject:
        executed.append(arguments)
        return {"status": "saved", "api_key": arguments["api_key"]}

    service.register(
        "save_secret",
        "Save a secret",
        {
            "type": "object",
            "properties": {"api_key": {"type": "string"}, "headers": {"type": "object"}},
            "required": ["api_key", "headers"],
            "additionalProperties": False,
        },
        save,
        mutating=True,
    )
    upstream = Upstream(
        "save_secret", {"api_key": "new-secret", "headers": {"X-Custom": "header-secret"}}
    )
    supervisor = Supervisor(db, service, settings, provider_factory=upstream.provider)
    await db.initialize()
    await supervisor.start()
    try:
        conversation = await supervisor.create_conversation()
        run = await supervisor.send_message(conversation["id"], "save")
        pending = await wait_status(supervisor, run["run_id"], "awaiting_approval")
        snapshot = await supervisor.get_conversation(conversation["id"])
        argument_events = [
            event
            for event in snapshot["events"]
            if event["type"] in {"tool_start", "approval_required"}
        ]
        assert {event["type"] for event in argument_events} == {
            "tool_start",
            "approval_required",
        }
        for event in argument_events:
            assert event["data"]["arguments"] == {
                "api_key": "********",
                "headers": {"X-Custom": "********"},
            }
        async with db.sessions() as session:
            stored = await session.get(ChatRun, run["run_id"])
            assert stored is not None
            assert stored.model_snapshot["request_params"] == {
                "custom_auth": {"api_key": "********", "limit": 7}
            }
        await service.execute(
            "approve_operation", {"approval_id": pending["pending_approval_id"]}, actor="user"
        )
        await wait_status(supervisor, run["run_id"], "completed")
        assert executed == [{"api_key": "new-secret", "headers": {"X-Custom": "header-secret"}}]
        snapshot = await supervisor.get_conversation(conversation["id"])
        result = [
            event["data"]["result"] for event in snapshot["events"] if event["type"] == "tool_end"
        ]
        assert result == [{"status": "saved", "api_key": "********"}]
        assert json.loads(upstream.requests[1]["messages"][-1]["content"])["api_key"] == "********"
    finally:
        await supervisor.stop()
        await db.close()


async def test_protocol_commit_listener_gap_is_recovered_and_deleted_inbox_stays_archived(
    tmp_path: Path,
) -> None:
    db = Database(f"sqlite+aiosqlite:///{tmp_path / 'state.db'}")
    settings = manager(model="")
    service = OperationService(db, settings)
    supervisor = Supervisor(db, service, settings)
    await db.initialize()
    pairing = await service.execute("create_pairing_code", {})
    identity = await service.pair(pairing["code"], "demo", "machine", [])
    # Protocol acceptance happened before the supervisor listener was alive.
    envelope = Envelope(type="agent.contact", payload={"message": "hello"})
    await service.receive(identity["agent_id"], envelope)
    await supervisor.start()
    conversations = (await supervisor.list_conversations())["conversations"]
    assert len(conversations) == 1 and conversations[0]["agent_id"] == identity["agent_id"]
    await service.execute(
        "delete_conversation", {"conversation_id": conversations[0]["id"]}, actor="user"
    )
    await supervisor.stop()
    restored = Supervisor(db, OperationService(db, settings), settings)
    await restored.start()
    try:
        assert (await restored.list_conversations())["conversations"] == []
        async with db.sessions() as session:
            rows = (await session.scalars(select(AgentInbox))).all()
        assert len(rows) == 1 and rows[0].status == "archived" and rows[0].conversation_id is None
    finally:
        await restored.stop()
        await db.close()


async def test_inbox_reservation_and_run_are_committed_before_model_start_and_not_replayed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db = Database(f"sqlite+aiosqlite:///{tmp_path / 'state.db'}")
    settings = manager()
    service = OperationService(db, settings)
    upstream = Upstream()
    original = Supervisor(db, service, settings, provider_factory=upstream.provider)
    await db.initialize()
    await original.receive_agent_request(
        "agent-1", {"id": "inbox-1", "type": "agent.contact", "payload": {"message": "hello"}}
    )
    async with db.sessions() as session:
        inbox = (await session.scalars(select(AgentInbox))).one()
    assert inbox.conversation_id
    # Simulate process termination immediately after durable reservation, before
    # the scheduled model task starts. The inbox is already linked atomically.
    monkeypatch.setattr(original, "_schedule", lambda _: None)
    response = await original.send_message(
        inbox.conversation_id, "agent request", source="agent", inbox_id=inbox.id
    )
    async with db.sessions() as session:
        reserved = await session.get(AgentInbox, inbox.id)
        run = await session.get(ChatRun, response["run_id"])
        assert (
            reserved is not None
            and reserved.status == "started"
            and reserved.run_id == response["run_id"]
        )
        assert run is not None and run.status == "queued"
    await original.stop()
    restored = Supervisor(
        db, OperationService(db, settings), settings, provider_factory=upstream.provider
    )
    await restored.start()
    try:
        await asyncio.sleep(settings.current.supervisor.inbox_poll_seconds * 3)
        async with db.sessions() as session:
            runs = (await session.scalars(select(ChatRun))).all()
        assert len(runs) == 1 and runs[0].status == "interrupted"
        assert upstream.requests == []
    finally:
        await restored.stop()
        await db.close()


async def test_agent_contact_model_reply_is_queued_on_existing_outbound_protocol(
    tmp_path: Path,
) -> None:
    db = Database(f"sqlite+aiosqlite:///{tmp_path / 'state.db'}")
    settings = manager("yolo")
    service = OperationService(db, settings)
    await db.initialize()
    pairing = await service.execute("create_pairing_code", {})
    identity = await service.pair(pairing["code"], "demo", "machine", [])
    upstream = Upstream(
        "send_agent_message",
        {
            "agent_id": identity["agent_id"],
            "message": "请等待任务确认",
            "metadata": {"source_request": "contact-1"},
        },
    )
    supervisor = Supervisor(db, service, settings, provider_factory=upstream.provider)
    await supervisor.start()
    try:
        await service.receive(
            identity["agent_id"],
            Envelope(id="contact-1", type="agent.contact", payload={"message": "Can I continue?"}),
        )
        async with asyncio.timeout(5):
            while True:
                async with db.sessions() as session:
                    inbox = (await session.scalars(select(AgentInbox))).first()
                if inbox is not None and inbox.run_id:
                    break
                await asyncio.sleep(0.01)
        await wait_status(supervisor, inbox.run_id, "completed")
        messages = await service.pending(identity["agent_id"])
        assert len(messages) == 1 and messages[0].type == "agent.message"
        assert messages[0].payload["message"] == "请等待任务确认"
        assert messages[0].payload["metadata"] == {"source_request": "contact-1"}
    finally:
        await supervisor.stop()
        await db.close()
