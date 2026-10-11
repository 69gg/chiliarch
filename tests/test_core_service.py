from __future__ import annotations

import asyncio
import time
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import func, inspect, select, text, update

from chiliarch.auth import Authentication, hash_password, verify_password
from chiliarch.config import Settings, SettingsManager
from chiliarch.database import Agent, AgentRequest, Database, Message, Task
from chiliarch.protocol import Envelope
from chiliarch.service import OperationError, OperationService


@pytest.fixture
async def service(tmp_path: Path) -> AsyncIterator[OperationService]:
    settings = Settings.model_validate(
        {
            "database": {"url": f"sqlite+aiosqlite:///{tmp_path}/test.db"},
            "logs": {"directory": str(tmp_path / "logs")},
            "auth": {"password_hash": hash_password("test-password")},
        }
    )
    database = Database(settings.database.url)
    await database.initialize()
    yield OperationService(database, SettingsManager(settings=settings))
    await database.close()


async def add_agent(service: OperationService, name: str = "demo") -> dict[str, Any]:
    pairing = await service.execute("create_pairing_code", {})
    return await service.pair(pairing["code"], name, "laptop", ["demo"])


async def test_pairing_code_has_one_winner_under_concurrency(service: OperationService) -> None:
    pairing = await service.execute("create_pairing_code", {})
    results = await asyncio.gather(
        *(service.pair(pairing["code"], str(index), "machine", []) for index in range(8)),
        return_exceptions=True,
    )
    successes = [result for result in results if isinstance(result, dict)]
    assert len(successes) == 1
    assert all(
        isinstance(result, OperationError) and result.status_code == 401
        for result in results
        if not isinstance(result, dict)
    )
    async with service.db.sessions() as session:
        assert await session.scalar(select(func.count()).select_from(Agent)) == 1
    await service.authenticate_agent(successes[0]["credential"])


async def test_ask_approval_binds_arguments_and_executes_once(service: OperationService) -> None:
    service.settings_manager.update({"supervisor": {"mode": "ask"}})
    agent = await add_agent(service)
    args = {
        "agent_id": agent["agent_id"],
        "instruction": "Inspect checkout",
        "parameters": {"read_only": True},
    }
    waiting = await service.execute("dispatch_task", args, "supervisor", "call-1")
    assert waiting["status"] == "awaiting_approval"
    assert (await service.execute("list_tasks", {}, "supervisor"))["tasks"] == []
    with pytest.raises(OperationError) as conflict:
        await service.execute(
            "dispatch_task", {**args, "instruction": "Different"}, "supervisor", "call-1"
        )
    assert conflict.value.status_code == 409
    with pytest.raises(OperationError):
        await service.execute(
            "approve_operation", {"approval_id": waiting["approval_id"]}, "supervisor"
        )
    results = await asyncio.gather(
        *(
            service.execute("approve_operation", {"approval_id": waiting["approval_id"]})
            for _ in range(4)
        )
    )
    assert all(result["approval"]["status"] == "approved" for result in results)
    resumed = await service.execute("dispatch_task", args, "supervisor", "call-1")
    assert resumed["task"]["instruction"] == args["instruction"]
    async with service.db.sessions() as session:
        assert await session.scalar(select(func.count()).select_from(Task)) == 1
        assert (
            await session.scalar(
                select(func.count()).select_from(Message).where(Message.direction == "outbox")
            )
            == 1
        )


async def test_rejected_operation_has_no_side_effect(service: OperationService) -> None:
    service.settings_manager.update({"supervisor": {"mode": "ask"}})
    agent = await add_agent(service)
    waiting = await service.execute(
        "rename_agent", {"agent_id": agent["agent_id"], "name": "changed"}, "supervisor", "rename"
    )
    await service.execute(
        "reject_operation", {"approval_id": waiting["approval_id"], "reason": "No"}
    )
    retried = await service.execute(
        "rename_agent", {"agent_id": agent["agent_id"], "name": "changed"}, "supervisor", "rename"
    )
    assert retried["status"] == "rejected"
    assert (await service.execute("get_agent", {"agent_id": agent["agent_id"]}))["agent"][
        "name"
    ] == "demo"


async def test_auto_assessment_cannot_reduce_intrinsic_high_risk(service: OperationService) -> None:
    assert service.settings_manager.current.supervisor.mode == "auto"
    low = {"level": "low", "reason": "Only naming a known local agent"}
    agent = await add_agent(service)
    renamed = await service.execute(
        "rename_agent", {"agent_id": agent["agent_id"], "name": "new", "_risk": low}, "supervisor"
    )
    assert renamed["agent"]["name"] == "new"
    waiting = await service.execute(
        "revoke_agent", {"agent_id": agent["agent_id"], "_risk": low}, "supervisor"
    )
    assert waiting["status"] == "awaiting_approval"
    assert not (await service.execute("get_agent", {"agent_id": agent["agent_id"]}))["agent"][
        "revoked"
    ]
    with pytest.raises(OperationError) as self_approval:
        await service.execute(
            "approve_operation", {"approval_id": waiting["approval_id"]}, "supervisor"
        )
    assert self_approval.value.status_code == 403
    service.settings_manager.update({"supervisor": {"mode": "yolo"}})
    assert (await service.execute("revoke_agent", {"agent_id": agent["agent_id"]}, "supervisor"))[
        "agent"
    ]["revoked"]


async def test_lower_agent_request_cannot_bypass_high_risk_gate(service: OperationService) -> None:
    assert service.settings_manager.current.supervisor.mode == "auto"
    agent = await add_agent(service)
    message = Envelope(
        type="agent.request",
        payload={
            "request_id": "r1",
            "operation": "revoke_agent",
            "arguments": {"agent_id": agent["agent_id"]},
        },
    )
    await service.receive(agent["agent_id"], message)
    request = (await service.execute("list_agent_requests", {}))["requests"][0]
    waiting = await service.execute(
        "resolve_agent_request",
        {
            "request_id": request["id"],
            "decision": "approve",
            "_risk": {"level": "low", "reason": "Claimed safe"},
        },
        "supervisor",
    )
    assert waiting["status"] == "awaiting_approval"
    assert (await service.execute("list_agent_requests", {}))["requests"][0]["status"] == "pending"
    assert not (await service.execute("get_agent", {"agent_id": agent["agent_id"]}))["agent"][
        "revoked"
    ]


@pytest.mark.parametrize("mode", ["ask", "auto", "yolo"])
async def test_lower_agent_read_query_uses_query_policy_and_returns_correlated_result(
    service: OperationService, mode: str
) -> None:
    service.settings_manager.update({"supervisor": {"mode": mode}})
    agent = await add_agent(service)
    await service.receive(
        agent["agent_id"],
        Envelope(
            type="agent.request",
            payload={"request_id": "discover-peers", "operation": "list_agents", "arguments": {}},
        ),
    )
    request = (await service.execute("list_agent_requests", {}))["requests"][0]
    result = await service.execute(
        "resolve_agent_request", {"request_id": request["id"], "decision": "approve"}, "supervisor"
    )
    assert result["request"]["status"] == "approved"
    assert result["request"]["result"]["agents"][0]["id"] == agent["agent_id"]
    assert (await service.execute("list_approvals", {}))["approvals"] == []
    reply = (await service.pending(agent["agent_id"]))[0]
    assert reply.type == "request.resolved"
    assert reply.correlation_id == reply.payload["request_id"] == "discover-peers"
    assert reply.payload["result"] == result["request"]["result"]


async def test_lower_agent_mutation_still_waits_for_user_in_ask(service: OperationService) -> None:
    service.settings_manager.update({"supervisor": {"mode": "ask"}})
    agent = await add_agent(service)
    await service.receive(
        agent["agent_id"],
        Envelope(
            type="agent.request",
            payload={
                "request_id": "rename-peer",
                "operation": "rename_agent",
                "arguments": {"agent_id": agent["agent_id"], "name": "renamed"},
            },
        ),
    )
    request = (await service.execute("list_agent_requests", {}))["requests"][0]
    waiting = await service.execute(
        "resolve_agent_request", {"request_id": request["id"], "decision": "approve"}, "supervisor"
    )
    assert waiting["status"] == "awaiting_approval"
    assert (await service.execute("get_agent", {"agent_id": agent["agent_id"]}))["agent"][
        "name"
    ] == "demo"
    assert await service.pending(agent["agent_id"]) == []
    await service.execute("approve_operation", {"approval_id": waiting["approval_id"]})
    assert (await service.execute("get_agent", {"agent_id": agent["agent_id"]}))["agent"][
        "name"
    ] == "renamed"
    assert (await service.pending(agent["agent_id"]))[0].payload["status"] == "approved"


async def test_supervisor_reply_keeps_agent_contact_correlation(service: OperationService) -> None:
    agent = await add_agent(service)
    contact = Envelope(type="agent.contact", payload={"message": "Which peer can help?"})
    await service.receive(agent["agent_id"], contact)
    queued = await service.execute(
        "send_agent_message",
        {
            "agent_id": agent["agent_id"],
            "message": "Use peer alpha",
            "correlation_id": contact.id,
        },
    )
    reply = (await service.pending(agent["agent_id"]))[0]
    assert reply.id == queued["message_id"]
    assert reply.correlation_id == contact.id
    assert reply.payload["message"] == "Use peer alpha"


async def test_queue_ack_and_execution_are_distinct_and_durable(service: OperationService) -> None:
    agent = await add_agent(service)
    task = (
        await service.execute(
            "dispatch_task", {"agent_id": agent["agent_id"], "instruction": "Summarize"}
        )
    )["task"]
    first = (await service.pending(agent["agent_id"]))[0]
    assert (await service.pending(agent["agent_id"]))[0].canonical() == first.canonical()
    restarted = OperationService(service.db, service.settings_manager)
    assert (await restarted.pending(agent["agent_id"]))[0].id == first.id
    await restarted.receive(
        agent["agent_id"], Envelope(type="ack", payload={"message_id": first.id})
    )
    assert await restarted.pending(agent["agent_id"]) == []
    assert (await restarted.execute("get_task", {"task_id": task["id"]}))["task"][
        "status"
    ] == "delivered"
    completed = Envelope(
        type="task.status",
        payload={"task_id": task["id"], "status": "completed", "result": {"files": 3}},
    )
    response = await restarted.receive(agent["agent_id"], completed)
    assert response["ack"]["payload"]["message_id"] == completed.id
    duplicate = await restarted.receive(agent["agent_id"], completed)
    assert duplicate["duplicate"]
    assert (await restarted.execute("get_task", {"task_id": task["id"]}))["task"]["result"] == {
        "files": 3
    }
    async with service.db.sessions() as session:
        assert (
            await session.scalar(
                select(func.count()).select_from(Message).where(Message.direction == "inbox")
            )
            == 1
        )


async def test_same_message_id_different_body_conflicts(service: OperationService) -> None:
    agent = await add_agent(service)
    original = Envelope(type="agent.contact", payload={"message": "original"})
    await service.receive(agent["agent_id"], original)
    changed = original.model_copy(update={"payload": {"message": "different", "metadata": {}}})
    with pytest.raises(OperationError) as conflict:
        await service.receive(agent["agent_id"], changed)
    assert conflict.value.status_code == 409


async def test_cancellation_suppresses_unsent_dispatch(service: OperationService) -> None:
    agent = await add_agent(service)
    task = (
        await service.execute(
            "dispatch_task", {"agent_id": agent["agent_id"], "instruction": "Write"}
        )
    )["task"]
    await service.execute("cancel_task", {"task_id": task["id"]})
    pending = await service.pending(agent["agent_id"])
    assert [message.type for message in pending] == ["task.cancel"]
    await service.receive(
        agent["agent_id"],
        Envelope(type="task.status", payload={"task_id": task["id"], "status": "cancelled"}),
    )
    assert await service.pending(agent["agent_id"]) == []
    assert (await service.execute("get_task", {"task_id": task["id"]}))["task"][
        "status"
    ] == "cancelled"


async def test_queue_expiry_changes_task_and_frees_capacity(service: OperationService) -> None:
    service.settings_manager.update({"transport": {"queue_limit": 1}})
    agent = await add_agent(service)
    task = (
        await service.execute(
            "dispatch_task", {"agent_id": agent["agent_id"], "instruction": "Write"}
        )
    )["task"]
    with pytest.raises(OperationError):
        await service.execute(
            "dispatch_task", {"agent_id": agent["agent_id"], "instruction": "Second"}
        )
    async with service.db.sessions.begin() as session:
        await session.execute(
            update(Message).where(Message.direction == "outbox").values(expires_at=time.time() - 1)
        )
    assert await service.pending(agent["agent_id"]) == []
    assert (await service.execute("get_task", {"task_id": task["id"]}))["task"][
        "status"
    ] == "expired"
    await service.execute("dispatch_task", {"agent_id": agent["agent_id"], "instruction": "Second"})


async def test_revoked_agent_cannot_send_or_read_queue(service: OperationService) -> None:
    agent = await add_agent(service)
    await service.execute("revoke_agent", {"agent_id": agent["agent_id"]})
    with pytest.raises(OperationError):
        await service.authenticate_agent(agent["credential"])
    with pytest.raises(OperationError):
        await service.receive(agent["agent_id"], Envelope(type="heartbeat", payload={}))
    with pytest.raises(OperationError):
        await service.pending(agent["agent_id"])


async def test_cross_agent_task_status_is_rejected_without_persistence(
    service: OperationService,
) -> None:
    first, other = await add_agent(service), await add_agent(service, "other")
    task = (
        await service.execute(
            "dispatch_task", {"agent_id": first["agent_id"], "instruction": "Test"}
        )
    )["task"]
    with pytest.raises(OperationError):
        await service.receive(
            other["agent_id"],
            Envelope(type="task.status", payload={"task_id": task["id"], "status": "completed"}),
        )
    async with service.db.sessions() as session:
        assert (
            await session.scalar(
                select(func.count()).select_from(Message).where(Message.direction == "inbox")
            )
            == 0
        )
    assert (await service.execute("get_task", {"task_id": task["id"]}))["task"][
        "status"
    ] == "queued"


async def test_pending_approved_extension_runs_exactly_once(service: OperationService) -> None:
    service.settings_manager.update({"supervisor": {"mode": "ask"}})
    calls: list[str] = []

    async def extension(args: dict[str, Any]) -> dict[str, Any]:
        calls.append(args["value"])
        async with service.db.sessions() as session:
            assert await session.scalar(select(func.count()).select_from(Agent)) == 0
        return {"value": args["value"]}

    service.register(
        "demo.extension",
        "Extension",
        {
            "type": "object",
            "properties": {"value": {"type": "string"}},
            "required": ["value"],
            "additionalProperties": False,
        },
        extension,
        mutating=True,
    )
    pending = await service.execute("demo.extension", {"value": "test"}, "supervisor", "external")
    assert calls == []
    await service.execute("approve_operation", {"approval_id": pending["approval_id"]})
    await service.execute("approve_operation", {"approval_id": pending["approval_id"]})
    assert calls == ["test"]
    assert await service.execute("demo.extension", {"value": "test"}, "supervisor", "external") == {
        "value": "test"
    }


async def test_request_resolution_preserves_stored_arguments(service: OperationService) -> None:
    service.settings_manager.update({"supervisor": {"mode": "ask"}})
    agent = await add_agent(service)
    envelope = Envelope(
        type="agent.request",
        payload={
            "request_id": "rename",
            "operation": "rename_agent",
            "arguments": {"agent_id": agent["agent_id"], "name": "from request"},
        },
    )
    await service.receive(agent["agent_id"], envelope)
    requests = (await service.execute("list_agent_requests", {}))["requests"]
    waiting = await service.execute(
        "resolve_agent_request",
        {"request_id": requests[0]["id"], "decision": "approve"},
        "supervisor",
        "request-resolution",
    )
    assert waiting["status"] == "awaiting_approval"
    await service.execute("approve_operation", {"approval_id": waiting["approval_id"]})
    await service.execute(
        "resolve_agent_request", {"request_id": requests[0]["id"], "decision": "approve"}
    )
    assert (await service.execute("get_agent", {"agent_id": agent["agent_id"]}))["agent"][
        "name"
    ] == "from request"
    async with service.db.sessions() as session:
        assert await session.scalar(select(func.count()).select_from(AgentRequest)) == 1
    assert len(await service.pending(agent["agent_id"])) == 1


async def test_password_change_invalidates_sessions_and_masks_configuration(
    service: OperationService,
) -> None:
    auth = Authentication(service.db, service.settings_manager)
    token, _ = await auth.login("test-password", "local")
    assert await auth.session(token) is not None
    await service.execute("change_password", {"password": "replacement-password"})
    assert await auth.session(token) is None
    assert verify_password(
        "replacement-password", service.settings_manager.current.auth.password_hash
    )
    assert not verify_password("test-password", service.settings_manager.current.auth.password_hash)
    assert (await service.execute("get_config", {}))["config"]["auth"][
        "password_hash"
    ] == "********"


async def test_public_config_masks_nested_request_params(service: OperationService) -> None:
    service.settings_manager.update(
        {
            "model": {
                "request_params": {
                    "api_key": "secret-param",
                    "routing": {"access_token": "nested-token"},
                    "temperature": 0.3,
                }
            }
        }
    )
    config = (await service.execute("get_config", {}))["config"]
    assert config["model"]["request_params"]["api_key"] == "********"
    assert config["model"]["request_params"]["routing"]["access_token"] == "********"
    assert config["model"]["request_params"]["temperature"] == 0.3


async def test_approval_and_audit_mask_arbitrary_auth_headers(service: OperationService) -> None:
    waiting = await service.execute(
        "update_config",
        {"patch": {"model": {"headers": {"X-Api-Key": "hidden", "Custom-Auth": "also-hidden"}}}},
        "supervisor",
        "headers",
    )
    approval = (await service.execute("list_approvals", {}))["approvals"][0]
    assert approval["id"] == waiting["approval_id"]
    assert approval["arguments"]["patch"]["model"]["headers"] == {
        "X-Api-Key": "********",
        "Custom-Auth": "********",
    }


async def test_extension_agent_request_uses_approval_then_one_handler_call(
    service: OperationService,
) -> None:
    calls: list[str] = []

    async def extension(args: dict[str, Any]) -> dict[str, Any]:
        calls.append(args["value"])
        # Extension callbacks may call services and own transactions; no lock is held here.
        assert (await service.execute("list_agents", {}))["agents"]
        return {"value": args["value"]}

    service.register(
        "demo.request",
        "Custom requested action",
        {
            "type": "object",
            "properties": {"value": {"type": "string"}},
            "required": ["value"],
            "additionalProperties": False,
        },
        extension,
        risk="high",
        mutating=True,
    )
    agent = await add_agent(service)
    await service.receive(
        agent["agent_id"],
        Envelope(
            type="agent.request",
            payload={
                "request_id": "external-request",
                "operation": "demo.request",
                "arguments": {"value": "exact argument"},
            },
        ),
    )
    request = (await service.execute("list_agent_requests", {}))["requests"][0]
    pending = await service.execute(
        "resolve_agent_request",
        {"request_id": request["id"], "decision": "approve"},
        "supervisor",
        "extension-request",
    )
    assert pending["status"] == "awaiting_approval"
    assert calls == []
    async with asyncio.timeout(2):
        await service.execute("approve_operation", {"approval_id": pending["approval_id"]})
    await service.execute("approve_operation", {"approval_id": pending["approval_id"]})
    await service.execute(
        "resolve_agent_request", {"request_id": request["id"], "decision": "approve"}
    )
    assert calls == ["exact argument"]
    recovered = await service.execute(
        "resolve_agent_request",
        {"request_id": request["id"], "decision": "approve"},
        "supervisor",
        "extension-request",
    )
    assert recovered["request"]["status"] == "approved"
    assert recovered["request"]["result"] == {"value": "exact argument"}
    messages = await service.pending(agent["agent_id"])
    assert len(messages) == 1
    assert messages[0].type == "request.resolved"
    assert messages[0].payload["request_id"] == "external-request"


async def test_approved_extension_failure_does_not_reexecute_and_resumes_waiter(
    service: OperationService,
) -> None:
    service.settings_manager.update({"supervisor": {"mode": "ask"}})
    calls: list[int] = []
    notifications: list[str] = []

    async def extension(_: dict[str, Any]) -> dict[str, Any]:
        calls.append(1)
        raise RuntimeError("Failure containing secret_api_key")

    async def listener(name: str, _: dict[str, Any]) -> None:
        notifications.append(name)

    service.register(
        "demo.failure",
        "Fails after a possible side effect",
        {"type": "object", "additionalProperties": False},
        extension,
        mutating=True,
    )
    service.add_listener(listener)
    pending = await service.execute("demo.failure", {}, "supervisor", "failed-extension")
    with pytest.raises(RuntimeError):
        await service.execute("approve_operation", {"approval_id": pending["approval_id"]})
    repeated = await service.execute("demo.failure", {}, "supervisor", "failed-extension")
    assert repeated == {
        "status": "failed",
        "error": "Handler failed; external effects may be incomplete (RuntimeError)",
    }
    assert "operation.rejected" in notifications
    await service.execute("approve_operation", {"approval_id": pending["approval_id"]})
    assert calls == [1]


async def test_unknown_database_version_rejected_before_creating_tables(tmp_path: Path) -> None:
    database = Database(f"sqlite+aiosqlite:///{tmp_path}/future.db")
    async with database.engine.begin() as connection:
        await connection.execute(
            text("CREATE TABLE schema_version (id INTEGER PRIMARY KEY, version INTEGER NOT NULL)")
        )
        await connection.execute(text("INSERT INTO schema_version VALUES (1, 99)"))
    try:
        with pytest.raises(RuntimeError):
            await database.initialize()
        async with database.engine.connect() as connection:
            tables = await connection.run_sync(lambda sync: inspect(sync).get_table_names())
        assert tables == ["schema_version"]
    finally:
        await database.close()


async def test_heartbeat_persists_liveness_without_filling_business_inbox(
    service: OperationService,
) -> None:
    agent = await add_agent(service)
    for _ in range(5):
        heartbeat = Envelope(
            type="heartbeat", payload={"status": "ready", "metadata": {"load": 0.1}}
        )
        received = await service.receive(agent["agent_id"], heartbeat)
        assert received["ack"]["payload"]["message_id"] == heartbeat.id
    observed = (await service.execute("get_agent", {"agent_id": agent["agent_id"]}))["agent"]
    assert observed["online"]
    assert observed["metadata"]["load"] == 0.1
    async with service.db.sessions() as session:
        assert await session.scalar(select(func.count()).select_from(Message)) == 0


@pytest.mark.parametrize("observed_transport", [None, "ws", "sse", "poll"])
async def test_heartbeat_cannot_override_platform_observed_transport(
    service: OperationService, observed_transport: str | None
) -> None:
    agent = await add_agent(service)
    if observed_transport is not None:
        await service.touch_agent(agent["agent_id"], observed_transport)
    await service.receive(
        agent["agent_id"],
        Envelope(
            type="heartbeat",
            payload={"status": "busy", "metadata": {"transport": "ssh", "load": 0.5}},
        ),
    )
    observed = (await service.execute("get_agent", {"agent_id": agent["agent_id"]}))["agent"]
    assert observed["transport"] == observed_transport
    assert observed["metadata"].get("transport") == observed_transport
    assert observed["metadata"]["load"] == 0.5
    assert observed["metadata"]["status"] == "busy"
    await service.touch_agent(agent["agent_id"], "poll")
    assert (await service.execute("get_agent", {"agent_id": agent["agent_id"]}))["agent"][
        "transport"
    ] == "poll"
