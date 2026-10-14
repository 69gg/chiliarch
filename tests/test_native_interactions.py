from __future__ import annotations

import asyncio
import time
from collections.abc import AsyncIterator
from pathlib import Path

import pytest
from sqlalchemy import select

from chiliarch import native_interactions
from chiliarch.adapters.native import request_native_interaction
from chiliarch.client import ClientStore, TaskExecutionError
from chiliarch.config import Settings, SettingsManager
from chiliarch.database import AgentRequest, AuditEvent, Database, Task
from chiliarch.protocol import Envelope
from chiliarch.service import OperationError, OperationService


@pytest.fixture
async def service(tmp_path: Path) -> AsyncIterator[OperationService]:
    settings = Settings.model_validate(
        {
            "database": {"url": f"sqlite+aiosqlite:///{tmp_path}/native.db"},
            "supervisor": {"mode": "yolo"},
        }
    )
    database = Database(settings.database.url)
    await database.initialize()
    try:
        yield OperationService(database, SettingsManager(settings=settings))
    finally:
        await database.close()


async def running_task(service: OperationService) -> tuple[str, str]:
    code = await service.execute("create_pairing_code", {})
    agent_id = (await service.pair(code["code"], "native", "machine", []))["agent_id"]
    task = (await service.execute("dispatch_task", {"agent_id": agent_id, "instruction": "Work"}))[
        "task"
    ]
    await service.receive(
        agent_id, Envelope(type="task.status", payload={"task_id": task["id"], "status": "running"})
    )
    return agent_id, task["id"]


def permission(task_id: str) -> Envelope:
    return Envelope(
        type="native.request",
        payload={
            "interaction_id": "native-1",
            "task_id": task_id,
            "kind": "permission",
            "title": "Run native command?",
            "native_method": "item/commandExecution/requestApproval",
            "details": {"command": "git status"},
            "options": [
                {"id": "accept", "label": "Allow once"},
                {"id": "decline", "label": "Deny"},
            ],
        },
    )


async def test_native_permission_requires_human_even_in_yolo(service: OperationService) -> None:
    agent_id, task_id = await running_task(service)
    request = permission(task_id)
    await service.receive(agent_id, request)
    assert (await service.receive(agent_id, request))["duplicate"]
    listed = await service.execute("list_native_interactions", {}, actor="supervisor")
    assert listed["interactions"][0]["details"] == {"command": "git status"}
    with pytest.raises(OperationError, match="logged-in user"):
        await service.execute(
            "resolve_native_interaction",
            {"interaction_id": "native-1", "native_answer": {"choice": "accept"}},
            actor="supervisor",
        )
    with pytest.raises(OperationError, match="native permission choices"):
        await service.execute(
            "resolve_native_interaction",
            {"interaction_id": "native-1", "native_answer": {"choice": "unadvertised"}},
        )
    result = await service.execute(
        "resolve_native_interaction",
        {"interaction_id": "native-1", "native_answer": {"choice": "accept"}},
    )
    assert result["interaction"]["status"] == "answered"
    assert "answer" not in result["interaction"]
    await service.execute(
        "resolve_native_interaction",
        {"interaction_id": "native-1", "native_answer": {"choice": "accept"}},
    )
    responses = [item for item in await service.pending(agent_id) if item.type == "native.response"]
    assert len(responses) == 1
    assert responses[0].payload["answer"] == {"choice": "accept"}
    with pytest.raises(OperationError, match="already final"):
        await service.execute(
            "resolve_native_interaction",
            {"interaction_id": "native-1", "native_answer": {"choice": "decline"}},
        )


async def test_native_requests_cannot_cross_identity_or_resume_stopped_task(
    service: OperationService,
) -> None:
    agent_id, task_id = await running_task(service)
    other_id, _ = await running_task(service)
    with pytest.raises(OperationError, match="another task"):
        await service.receive(other_id, permission(task_id))
    await service.receive(agent_id, permission(task_id))
    await service.receive(
        agent_id, Envelope(type="task.status", payload={"task_id": task_id, "status": "cancelled"})
    )
    with pytest.raises(OperationError, match="expired or its task stopped"):
        await service.execute(
            "resolve_native_interaction",
            {"interaction_id": "native-1", "native_answer": {"choice": "accept"}},
        )
    async with service.db.sessions.begin() as session:
        await native_interactions.expire(service, session)
    assert (await service.execute("list_native_interactions", {}))["interactions"] == []
    assert (await service.execute("list_native_interactions", {"status": "expired"}))[
        "interactions"
    ][0]["status"] == "expired"


async def test_information_answer_obeys_mode_and_audit_masks_answer(
    service: OperationService,
) -> None:
    agent_id, task_id = await running_task(service)
    request = Envelope(
        type="native.request",
        payload={
            "interaction_id": "question",
            "task_id": task_id,
            "kind": "input",
            "title": "Which project?",
            "native_method": "item/tool/requestUserInput",
            "fields": [{"id": "project", "label": "Project", "secret": True}],
        },
    )
    await service.receive(agent_id, request)
    answer = {
        "interaction_id": "question",
        "native_answer": {"fields": {"project": "original-secret-value"}},
    }
    result = await service.execute("resolve_native_interaction", answer, actor="supervisor")
    assert result["interaction"]["answer"] == answer["native_answer"]
    async with service.db.sessions() as session:
        audits = (
            await session.scalars(
                select(AuditEvent).where(AuditEvent.action == "resolve_native_interaction")
            )
        ).all()
        assert audits
        assert audits[0].details["arguments"]["native_answer"] == "********"


async def test_expired_request_does_not_return_answer_to_native_process(
    service: OperationService,
) -> None:
    agent_id, task_id = await running_task(service)
    await service.receive(agent_id, permission(task_id))
    async with service.db.sessions.begin() as session:
        row = await session.get(native_interactions.NativeInteraction, "native-1")
        assert row
        row.expires_at = time.time() - 1
        task = await session.get(Task, task_id)
        assert task and task.status == "running"
    with pytest.raises(OperationError, match="expired"):
        await service.execute(
            "resolve_native_interaction",
            {"interaction_id": "native-1", "native_answer": {"choice": "accept"}},
        )
    assert not [item for item in await service.pending(agent_id) if item.type == "native.response"]


async def test_late_native_question_is_acknowledged_expired_after_cancellation(
    service: OperationService,
) -> None:
    agent_id, task_id = await running_task(service)
    await service.receive(
        agent_id, Envelope(type="task.status", payload={"task_id": task_id, "status": "cancelled"})
    )
    result = await service.receive(agent_id, permission(task_id))
    assert result["ack"]["type"] == "ack"
    response = next(
        item for item in await service.pending(agent_id) if item.type == "native.response"
    )
    assert response.payload["status"] == "expired"
    assert response.payload["answer"] == {}


async def test_expiry_releases_active_native_waiter_without_granting_permission(
    service: OperationService, tmp_path: Path
) -> None:
    agent_id, task_id = await running_task(service)
    state_path = tmp_path / "mailbox.db"
    store = ClientStore(state_path)
    await store.open()
    waiter = asyncio.create_task(
        request_native_interaction(
            state_path, task_id, permission(task_id).payload, poll_seconds=0.01
        )
    )
    try:
        async with asyncio.timeout(5):
            while True:
                requests = await store.pending()
                if requests:
                    break
                await asyncio.sleep(0.01)
        request = requests[0]
        await service.receive(agent_id, request)
        async with service.db.write_lock, service.db.sessions.begin() as session:
            row = await session.get(
                native_interactions.NativeInteraction, request.payload["interaction_id"]
            )
            assert row
            row.expires_at = time.time() - 1
            await native_interactions.expire(service, session)
            await native_interactions.expire(service, session)
        responses = [
            item for item in await service.pending(agent_id) if item.type == "native.response"
        ]
        assert len(responses) == 1
        assert responses[0].payload["status"] == "expired"
        assert responses[0].payload["answer"] == {}
        await store.accept(responses[0])
        with pytest.raises(TaskExecutionError, match="expired"):
            await asyncio.wait_for(waiter, 2)
    finally:
        waiter.cancel()
        await asyncio.gather(waiter, return_exceptions=True)
        await store.close()


async def test_native_query_filters_bound_task_without_changing_idempotency_arguments(
    service: OperationService,
) -> None:
    agent_id, first_id = await running_task(service)
    other_task = (
        await service.execute("dispatch_task", {"agent_id": agent_id, "instruction": "Other"})
    )["task"]
    await service.receive(
        agent_id,
        Envelope(type="task.status", payload={"task_id": other_task["id"], "status": "running"}),
    )
    await service.receive(agent_id, permission(other_task["id"]))
    query = Envelope(
        type="agent.request",
        payload={
            "request_id": "bound-list",
            "operation": "list_native_interactions",
            "arguments": {},
            "metadata": {"task_id": first_id},
        },
    )
    await service.receive(agent_id, query)
    await service.receive(agent_id, Envelope(type=query.type, payload=query.payload))
    async with service.db.sessions() as session:
        request = await session.scalar(
            select(AgentRequest).where(AgentRequest.external_id == "bound-list")
        )
        assert request and request.status == "approved"
        assert request.result == {"interactions": []}
        assert request.arguments["task_id"] is None
    assert len((await service.execute("list_native_interactions", {}))["interactions"]) == 1
