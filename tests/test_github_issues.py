from __future__ import annotations

import pytest
from sqlalchemy import func, select
from test_projects import node, project
from test_projects import service as service

from chiliarch.database import Goal, Message
from chiliarch.projects_models import RepositoryCommand
from chiliarch.protocol import Envelope
from chiliarch.service import OperationError, OperationService


async def test_issue_import_is_explicit_scoped_and_idempotent(service: OperationService) -> None:
    agent_id = await node(service)
    registered = await project(service, agent_id)
    args = {"project_id": registered["id"], "agent_id": agent_id, "issue_number": 7}
    issue = {
        "number": 7,
        "url": "https://github.com/owner/example/issues/7",
        "title": "Feature",
        "body": "Original requested work",
        "state": "open",
        "labels": [],
    }
    for operation in ["inspect_project_issue", "import_project_issue", "import_project_issue"]:
        command = (await service.execute(operation, args))["command"]
        envelope = Envelope(
            type="repository.result",
            payload={
                "command_id": command["id"],
                "status": "completed",
                "result": {"issue": issue},
            },
        )
        await service.receive(agent_id, envelope)
        # Same business result in a new envelope must remain valid, too.
        await service.receive(agent_id, Envelope(type=envelope.type, payload=envelope.payload))
        snapshot = await service.execute("get_project", {"project_id": registered["id"]})
        assert snapshot["issues"][0]["observation"]["body"] == issue["body"]
        if operation == "inspect_project_issue":
            assert snapshot["issues"][0]["goal_id"] is None
        else:
            assert snapshot["issues"][0]["goal_id"]
    async with service.db.sessions() as session:
        assert await session.scalar(select(func.count()).select_from(Goal)) == 1
    other = await node(service, "unbound")
    request = Envelope(
        type="agent.request",
        payload={
            "request_id": "outside",
            "operation": "inspect_project_issue",
            "arguments": {**args, "agent_id": other},
        },
    )
    await service.receive(other, request)
    replies = [item for item in await service.pending(other) if item.type == "request.resolved"]
    assert replies[0].payload["status"] == "failed"


async def test_issue_update_requires_real_change_and_preserves_empty_labels(
    service: OperationService,
) -> None:
    agent_id = await node(service)
    registered = await project(service, agent_id)
    args = {"project_id": registered["id"], "agent_id": agent_id, "issue_number": 7}
    with pytest.raises(OperationError, match="Choose an Issue"):
        await service.execute("update_project_issue", args)
    queued = await service.execute("update_project_issue", {**args, "labels": []})
    assert queued["command"]["arguments"]["labels"] == []
    assert "state" not in queued["command"]["arguments"]
    assert queued["command"]["action"] == "update_issue"


async def test_missing_delivery_returns_request_failure_instead_of_poisoning_transport(
    service: OperationService,
) -> None:
    agent_id = await node(service)
    request = Envelope(
        type="agent.request",
        payload={
            "request_id": "missing-delivery",
            "operation": "merge_project_delivery",
            "arguments": {"delivery_id": "not-a-delivery"},
        },
    )
    received = await service.receive(agent_id, request)
    assert received["ack"]["type"] == "ack"
    replies = [item for item in await service.pending(agent_id) if item.type == "request.resolved"]
    assert len(replies) == 1
    assert replies[0].payload["status"] == "failed"
    assert replies[0].payload["result"]["error"] == "Project delivery not found"
    assert (await service.receive(agent_id, request))["duplicate"]


async def test_reconcile_restores_imported_goal_and_issue_updates_once(
    service: OperationService,
) -> None:
    agent_id = await node(service)
    registered = await project(service, agent_id)
    args = {"project_id": registered["id"], "agent_id": agent_id, "issue_number": 7}
    issue = {
        "number": 7,
        "url": "https://github.com/owner/example/issues/7",
        "title": "Recovered feature",
        "body": "Work retained across a lost result",
        "state": "open",
        "labels": [],
    }
    for operation, extra in [
        ("import_project_issue", {}),
        ("update_project_issue", {"state": "closed"}),
    ]:
        command = (await service.execute(operation, {**args, **extra}))["command"]
        await service.receive(
            agent_id,
            Envelope(
                type="repository.result",
                payload={"command_id": command["id"], "status": "unknown", "result": {}},
            ),
        )
        reconciled = await service.execute(
            "reconcile_project_command", {"command_id": command["id"]}
        )
        observed = {**issue, **extra}
        payload = {
            "command_id": reconciled["command"]["id"],
            "status": "completed",
            "result": {
                "original_command_id": command["id"],
                "original_status": "completed",
                "original_result": {"issue": observed},
                "original_error": None,
            },
        }
        await service.receive(agent_id, Envelope(type="repository.result", payload=payload))
        await service.receive(agent_id, Envelope(type="repository.result", payload=payload))
        snapshot = await service.execute("get_project", {"project_id": registered["id"]})
        assert len(snapshot["issues"]) == 1
        assert snapshot["issues"][0]["observation"] == observed
        assert snapshot["issues"][0]["goal_id"]
        async with service.db.sessions() as session:
            assert await session.scalar(select(func.count()).select_from(Goal)) == 1
            restored = await session.get(RepositoryCommand, command["id"])
            assert restored and restored.status == "completed"


@pytest.mark.parametrize("entrypoint", ["maintenance", "pending"])
async def test_expired_repository_delivery_remains_unknown_not_failed(
    service: OperationService,
    entrypoint: str,
) -> None:
    agent_id = await node(service)
    registered = await project(service, agent_id)
    command = (
        await service.execute(
            "inspect_project_issue",
            {"project_id": registered["id"], "agent_id": agent_id, "issue_number": 7},
        )
    )["command"]
    async with service.db.write_lock, service.db.sessions.begin() as session:
        message = await session.scalar(
            select(Message).where(Message.direction == "outbox", Message.agent_id == agent_id)
        )
        assert message
        message.expires_at = 0
    for _ in range(2):
        if entrypoint == "maintenance":
            async with service.db.write_lock, service.db.sessions.begin() as session:
                await service._expire_messages(session)
        else:
            assert await service.pending(agent_id) == []
    async with service.db.sessions() as session:
        stored = await session.get(RepositoryCommand, command["id"])
        assert stored and stored.status == "unknown"
    assert await service.pending(agent_id) == []
