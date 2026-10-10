from __future__ import annotations

import time
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import func, select

from chiliarch.collaboration import PeerMessage, PeerReceipt
from chiliarch.config import Settings, SettingsManager
from chiliarch.database import Agent, AgentRequest, Database, Message, TaskLink
from chiliarch.protocol import Envelope
from chiliarch.service import OperationError, OperationService

JsonObject = dict[str, Any]


@pytest.fixture
async def service(tmp_path: Path) -> AsyncIterator[OperationService]:
    settings = Settings.model_validate(
        {
            "database": {"url": f"sqlite+aiosqlite:///{tmp_path}/peers.db"},
            "supervisor": {"mode": "yolo"},
        }
    )
    db = Database(settings.database.url)
    await db.initialize()
    try:
        yield OperationService(db, SettingsManager(settings=settings))
    finally:
        await db.close()


async def agent(service: OperationService, name: str) -> str:
    code = await service.execute("create_pairing_code", {})
    identity = await service.pair(code["code"], name, "machine", ["chiliarch.contacts"])
    return str(identity["agent_id"])


async def request(
    service: OperationService,
    sender: str,
    operation: str,
    arguments: JsonObject,
    *,
    task_id: str | None = None,
) -> JsonObject:
    envelope = Envelope(
        type="agent.request",
        payload={
            "request_id": str(time.time_ns()),
            "operation": operation,
            "arguments": arguments,
            "metadata": {"task_id": task_id} if task_id else {},
        },
    )
    await service.receive(sender, envelope)
    async with service.db.sessions() as session:
        record = await session.scalar(
            select(AgentRequest).where(AgentRequest.external_id == envelope.payload["request_id"])
        )
        assert record is not None
        return service.request_view(record)


async def running(service: OperationService, agent_id: str) -> str:
    dispatched = await service.execute(
        "dispatch_task", {"agent_id": agent_id, "instruction": "collaborate"}
    )
    task_id = str(dispatched["task"]["id"])
    await service.receive(
        agent_id, Envelope(type="task.status", payload={"task_id": task_id, "status": "running"})
    )
    return task_id


async def test_discovery_is_immediate_without_a_model_in_ask_mode(
    service: OperationService,
) -> None:
    service.settings_manager.current.supervisor.mode = "ask"
    a, b = await agent(service, "A"), await agent(service, "B")
    result = await request(service, a, "list_agents", {})
    assert result["status"] == "approved"
    assert {item["id"] for item in result["result"]["agents"]} == {a, b}
    replies = await service.pending(a)
    assert replies[0].type == "request.resolved"
    assert not service.settings_manager.current.model.model


@pytest.mark.parametrize("status", ["completed", "failed", "cancelled", "interrupted"])
async def test_task_termination_preserves_undelivered_peer_messages(
    service: OperationService, status: str
) -> None:
    recipient = await agent(service, "recipient")
    task = await running(service, recipient)
    sent = await service.execute(
        "send_peer_message",
        {"recipient_agent_id": recipient, "recipient_task_id": task, "message": "Late review"},
    )
    message_id = sent["message"]["id"]
    await service.receive(
        recipient, Envelope(type="task.status", payload={"task_id": task, "status": status})
    )
    pending = await service.pending(recipient)
    assert [message.type for message in pending] == ["collaboration.message"]
    assert pending[0].payload["message_id"] == message_id
    queued = await service.execute("get_peer_message", {"message_id": message_id})
    assert queued["receipts"][0]["state"] == "queued"
    await service.receive(recipient, Envelope(type="ack", payload={"message_id": pending[0].id}))
    delivered = await service.execute("get_peer_message", {"message_id": message_id})
    assert delivered["receipts"][0]["state"] == "delivered"


async def test_peer_reply_has_authenticated_identity_and_original_task_route(
    service: OperationService,
) -> None:
    a, b = await agent(service, "A"), await agent(service, "B")
    task = await running(service, a)
    first = await request(
        service,
        a,
        "send_peer_message",
        {
            "recipient_agent_id": b,
            "message": "Can you review?",
            "task_id": task,
        },
        task_id=task,
    )
    message_id = first["result"]["message"]["id"]
    delivery = next(
        item for item in await service.pending(b) if item.type == "collaboration.message"
    )
    assert delivery.payload["sender_agent_id"] == a
    assert delivery.payload["source_task_id"] == task
    await service.receive(b, Envelope(type="ack", payload={"message_id": delivery.id}))
    view = await service.execute("get_peer_message", {"message_id": message_id})
    assert view["receipts"][0]["state"] == "delivered"
    await request(
        service, b, "acknowledge_peer_message", {"message_id": message_id, "state": "processed"}
    )
    await request(
        service, b, "acknowledge_peer_message", {"message_id": message_id, "state": "read"}
    )
    assert (await service.execute("get_peer_message", {"message_id": message_id}))["receipts"][0][
        "state"
    ] == "processed"
    reply = await request(
        service,
        b,
        "send_peer_message",
        {"recipient_agent_id": a, "message": "Reviewed", "reply_to": message_id},
    )
    assert reply["result"]["message"]["recipient_task_id"] == task
    assert reply["result"]["wake_task_id"] is None


async def test_ask_requires_approval_and_retains_real_sender(service: OperationService) -> None:
    a, b = await agent(service, "A"), await agent(service, "B")
    service.settings_manager.current.supervisor.mode = "ask"
    queued = await request(
        service, a, "send_peer_message", {"recipient_agent_id": b, "message": "Hello"}
    )
    assert queued["status"] == "pending"
    assert not await service.pending(b)
    approval = await service.execute(
        "resolve_agent_request",
        {"request_id": queued["id"], "decision": "approve"},
        actor="supervisor",
    )
    assert approval["status"] == "awaiting_approval"
    await service.execute("approve_operation", {"approval_id": approval["approval_id"]})
    delivery = (await service.pending(b))[0]
    assert delivery.payload["sender_agent_id"] == a
    assert delivery.payload["sender_role"] == "agent"


async def test_channel_membership_broadcast_and_sequenced_history(
    service: OperationService,
) -> None:
    a, b, stranger = await agent(service, "A"), await agent(service, "B"), await agent(service, "C")
    channel = (await request(service, a, "create_channel", {"title": "Delivery", "members": [b]}))[
        "result"
    ]["channel"]
    for text in ["Plan", "Evidence"]:
        assert (
            await request(
                service, a, "send_peer_message", {"channel_id": channel["id"], "message": text}
            )
        )["status"] == "approved"
    assert len(await service.pending(b)) == 2
    assert not await service.pending(stranger)
    page = (await request(service, b, "get_channel", {"channel_id": channel["id"], "limit": 1}))[
        "result"
    ]
    assert page["messages"][0]["message"] == "Plan"
    after = (
        await request(
            service, b, "get_channel", {"channel_id": channel["id"], "after": page["cursor"]}
        )
    )["result"]
    assert [row["message"] for row in after["messages"]] == ["Evidence"]
    assert (await request(service, stranger, "get_channel", {"channel_id": channel["id"]}))[
        "status"
    ] == "failed"
    assert (await request(service, stranger, "list_channels", {}))["result"]["channels"] == []


async def test_duplicate_envelope_does_not_redeliver_or_redispatch(
    service: OperationService,
) -> None:
    a, b = await agent(service, "A"), await agent(service, "B")
    envelope = Envelope(
        type="agent.request",
        payload={
            "request_id": "stable",
            "operation": "send_peer_message",
            "arguments": {"recipient_agent_id": b, "message": "once"},
        },
    )
    await service.receive(a, envelope)
    assert (await service.receive(a, envelope))["duplicate"] is True
    async with service.db.sessions() as session:
        assert await session.scalar(select(func.count()).select_from(PeerMessage)) == 1
        assert await session.scalar(select(func.count()).select_from(PeerReceipt)) == 1
    assert len(await service.pending(b)) == 1


async def test_receipts_and_task_scopes_cannot_be_claimed_by_other_work(
    service: OperationService,
) -> None:
    a, b, c = await agent(service, "A"), await agent(service, "B"), await agent(service, "C")
    b1, b2 = await running(service, b), await running(service, b)
    sent = await request(
        service,
        a,
        "send_peer_message",
        {"recipient_agent_id": b, "recipient_task_id": b1, "message": "For first task"},
    )
    message_id = sent["result"]["message"]["id"]
    assert (
        await request(
            service, c, "acknowledge_peer_message", {"message_id": message_id, "state": "processed"}
        )
    )["status"] == "failed"
    assert (await request(service, b, "get_peer_message", {"message_id": message_id}, task_id=b2))[
        "status"
    ] == "failed"
    assert (await request(service, b, "get_peer_message", {"message_id": message_id}, task_id=b1))[
        "status"
    ] == "approved"


async def test_wake_is_explicit_managed_task_and_auto_requires_permission(
    service: OperationService,
) -> None:
    a, b = await agent(service, "A"), await agent(service, "B")
    no_runner = await request(
        service,
        a,
        "send_peer_message",
        {"recipient_agent_id": b, "message": "Review", "wake": True},
    )
    assert no_runner["status"] == "failed"
    async with service.db.sessions.begin() as session:
        peer = await session.get(Agent, b)
        assert peer is not None
        peer.metadata_json = {"execution_mode": "managed"}
    service.settings_manager.current.supervisor.mode = "auto"
    waiting = await request(
        service,
        a,
        "send_peer_message",
        {"recipient_agent_id": b, "message": "Review", "wake": True},
    )
    assert waiting["status"] == "pending"
    service.settings_manager.current.supervisor.mode = "yolo"
    result = await request(
        service,
        a,
        "send_peer_message",
        {"recipient_agent_id": b, "message": "Review", "wake": True},
    )
    task_id = result["result"]["wake_task_id"]
    assert task_id
    dispatch = next(item for item in await service.pending(b) if item.type == "task.dispatch")
    assert dispatch.payload["task_id"] == task_id
    assert dispatch.payload["parameters"]["collaboration_message"]["sender_agent_id"] == a
    async with service.db.sessions() as session:
        assert await session.scalar(select(func.count()).select_from(PeerMessage)) == 1


async def test_channel_queue_failure_rolls_back_all_recipients(service: OperationService) -> None:
    a, b, c = await agent(service, "A"), await agent(service, "B"), await agent(service, "C")
    channel = (await service.execute("create_channel", {"title": "Team", "members": [b, c]}))[
        "channel"
    ]
    service.settings_manager.current.transport.queue_limit = 1
    await service.execute("send_agent_message", {"agent_id": c, "message": "full"})
    result = await request(
        service, a, "send_peer_message", {"channel_id": channel["id"], "message": "all"}
    )
    # Sender is not a member; user broadcast additionally verifies transactional fan-out.
    assert result["status"] == "failed"
    with pytest.raises(OperationError, match="full"):
        await service.execute("send_peer_message", {"channel_id": channel["id"], "message": "all"})
    assert not await service.pending(b)
    async with service.db.sessions() as session:
        assert await session.scalar(select(func.count()).select_from(PeerMessage)) == 0


async def test_wait_graph_rejects_cycles_and_shared_workspaces(service: OperationService) -> None:
    a, b = await agent(service, "A"), await agent(service, "B")
    ta, tb = await running(service, a), await running(service, b)
    assert (await request(service, a, "wait_for_tasks", {"task_id": ta, "waiting_for": [tb]}))[
        "status"
    ] == "approved"
    cycle = await request(service, b, "wait_for_tasks", {"task_id": tb, "waiting_for": [ta]})
    assert cycle["status"] == "failed"
    assert "cycle" in cycle["result"]["error"]
    async with service.db.sessions.begin() as session:
        for identifier in [a, b]:
            row = await session.get(Agent, identifier)
            assert row is not None
            row.metadata_json = {"workspace_id": "physical-directory"}
    blocked = await request(service, a, "wait_for_tasks", {"task_id": ta, "waiting_for": [tb]})
    assert blocked["status"] == "failed"
    await service.receive(
        b, Envelope(type="task.status", payload={"task_id": tb, "status": "completed"})
    )
    assert (await service.execute("list_task_waits", {}))["waits"] == []


async def test_continuation_only_for_completed_same_agent_and_reserved_context(
    service: OperationService,
) -> None:
    a, b = await agent(service, "A"), await agent(service, "B")
    previous = await running(service, a)
    with pytest.raises(OperationError, match="completed"):
        await service.execute(
            "dispatch_task",
            {"agent_id": a, "instruction": "Continue", "continue_task_id": previous},
        )
    await service.receive(
        a, Envelope(type="task.status", payload={"task_id": previous, "status": "completed"})
    )
    with pytest.raises(OperationError, match="same agent"):
        await service.execute(
            "dispatch_task",
            {"agent_id": b, "instruction": "Continue", "continue_task_id": previous},
        )
    current = await service.execute(
        "dispatch_task", {"agent_id": a, "instruction": "Continue", "continue_task_id": previous}
    )
    assert current["task"]["parameters"]["chiliarch_context"]["continue_task_id"] == previous


async def test_agent_cannot_forge_platform_peer_envelope(service: OperationService) -> None:
    a, b = await agent(service, "A"), await agent(service, "B")
    with pytest.raises(OperationError, match="reserved"):
        await service.receive(
            a,
            Envelope(
                type="collaboration.message",
                payload={
                    "message_id": "spoof",
                    "sender_agent_id": b,
                    "sender_role": "agent",
                    "recipient_agent_id": a,
                    "message": "spoof",
                    "sequence": 1,
                },
            ),
        )
    async with service.db.sessions() as session:
        assert await session.scalar(select(func.count()).select_from(Message)) == 0


async def test_revoked_recipient_after_approval_never_receives(service: OperationService) -> None:
    a, b = await agent(service, "A"), await agent(service, "B")
    service.settings_manager.current.supervisor.mode = "ask"
    pending = await request(
        service, a, "send_peer_message", {"recipient_agent_id": b, "message": "Hello"}
    )
    await service.execute("revoke_agent", {"agent_id": b})
    with pytest.raises(OperationError, match="revoked"):
        await service.execute(
            "resolve_agent_request", {"request_id": pending["id"], "decision": "approve"}
        )
    async with service.db.sessions() as session:
        assert await session.scalar(select(func.count()).select_from(PeerMessage)) == 0


async def test_wake_and_continuation_keep_goal_and_can_read_owned_ancestry(
    service: OperationService,
) -> None:
    a, b = await agent(service, "A"), await agent(service, "B")
    goal = (await service.execute("create_goal", {"title": "Deliver", "objective": "Collaborate"}))[
        "goal"
    ]
    initial = (
        await service.execute(
            "dispatch_task", {"agent_id": a, "instruction": "Implement", "goal_id": goal["id"]}
        )
    )["task"]["id"]
    await service.receive(
        a, Envelope(type="task.status", payload={"task_id": initial, "status": "running"})
    )
    async with service.db.sessions.begin() as session:
        for identifier in (a, b):
            row = await session.get(Agent, identifier)
            assert row is not None
            row.metadata_json = {"execution_mode": "managed"}
    sent = await request(
        service,
        a,
        "send_peer_message",
        {"recipient_agent_id": b, "task_id": initial, "message": "Review", "wake": True},
        task_id=initial,
    )
    wake_task = sent["result"]["wake_task_id"]
    async with service.db.sessions() as session:
        link = await session.get(TaskLink, wake_task)
        assert link and link.goal_id == goal["id"]
    await service.receive(
        a, Envelope(type="task.status", payload={"task_id": initial, "status": "completed"})
    )
    unrelated = await running(service, b)
    reply = await request(
        service,
        b,
        "send_peer_message",
        {
            "recipient_agent_id": a,
            "task_id": unrelated,
            "reply_to": sent["result"]["message"]["id"],
            "message": "Follow-up",
            "wake": True,
        },
        task_id=unrelated,
    )
    resumed = reply["result"]["wake_task_id"]
    async with service.db.sessions() as session:
        link = await session.get(TaskLink, resumed)
        assert link and link.goal_id == goal["id"]
    await service.receive(
        a, Envelope(type="task.status", payload={"task_id": resumed, "status": "running"})
    )
    history = await request(
        service,
        a,
        "get_peer_message",
        {"message_id": sent["result"]["message"]["id"]},
        task_id=resumed,
    )
    assert history["status"] == "approved"


async def test_wait_detects_cycle_through_third_queued_workers_workspace(
    service: OperationService,
) -> None:
    a, b, c = await agent(service, "A"), await agent(service, "B"), await agent(service, "C")
    ta, tb = await running(service, a), await running(service, b)
    tc = (await service.execute("dispatch_task", {"agent_id": c, "instruction": "Review"}))["task"][
        "id"
    ]
    async with service.db.sessions.begin() as session:
        for identifier, workspace in ((a, "X"), (b, "Y"), (c, "X")):
            row = await session.get(Agent, identifier)
            assert row is not None
            row.metadata_json = {"workspace_id": workspace}
    assert (await request(service, a, "wait_for_tasks", {"task_id": ta, "waiting_for": [tb]}))[
        "status"
    ] == "approved"
    cycle = await request(service, b, "wait_for_tasks", {"task_id": tb, "waiting_for": [tc]})
    assert cycle["status"] == "failed"
    assert "cycle" in cycle["result"]["error"]
