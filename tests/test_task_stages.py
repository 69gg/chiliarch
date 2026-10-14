from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import func, select

from chiliarch.config import Settings, SettingsManager
from chiliarch.database import Agent, CoordinationEvent, Database, Task
from chiliarch.protocol import Envelope
from chiliarch.service import OperationError, OperationService
from chiliarch.task_stages import TaskCheckpoint


@pytest.fixture
async def service(tmp_path: Path) -> AsyncIterator[OperationService]:
    settings = Settings.model_validate(
        {"database": {"url": f"sqlite+aiosqlite:///{tmp_path}/stages.db"}}
    )
    db = Database(settings.database.url)
    await db.initialize()
    yield OperationService(db, SettingsManager(settings=settings))
    await db.close()


async def node(service: OperationService) -> str:
    code = await service.execute("create_pairing_code", {})
    paired = await service.pair(code["code"], "worker", "machine", ["chiliarch.task_workdir"])
    agent_id = str(paired["agent_id"])
    async with service.db.sessions.begin() as session:
        agent = await session.get(Agent, agent_id)
        assert agent
        agent.metadata_json = {"supports_resume": True, "active_task_ids": []}
    return agent_id


async def task(service: OperationService, agent_id: str, **extra: Any) -> str:
    result = await service.execute(
        "dispatch_task",
        {
            "agent_id": agent_id,
            "instruction": "Complete this stage",
            "workdir": "/managed/project",
            **extra,
        },
    )
    task_id = str(result["task"]["id"])
    await service.receive(
        agent_id, Envelope(type="task.status", payload={"task_id": task_id, "status": "running"})
    )
    return task_id


async def complete(service: OperationService, agent_id: str, task_id: str) -> None:
    await service.receive(
        agent_id,
        Envelope(
            type="task.status",
            payload={
                "task_id": task_id,
                "status": "completed",
                "result": {"workdir": "/managed/project", "session_id": "native-session"},
            },
        ),
    )


async def maintain(service: OperationService) -> bool:
    async with service.db.write_lock, service.db.sessions.begin() as session:
        return await service.stages.maintain(session)


async def test_checkpoint_does_not_stop_native_work_or_claim_success(
    service: OperationService,
) -> None:
    agent_id = await node(service)
    task_id = await task(service, agent_id)
    saved = await service.execute(
        "save_task_checkpoint",
        {"task_id": task_id, "summary": "Implementation saved; next stage validates it"},
    )
    assert saved["checkpoint"]["state"] == "awaiting_exit"
    assert not await maintain(service)
    async with service.db.sessions() as session:
        original = await session.get(Task, task_id)
        assert original and original.status == "running"
        assert await session.scalar(select(func.count()).select_from(Task)) == 1
    assert all(message.type != "task.cancel" for message in await service.pending(agent_id))
    with pytest.raises(OperationError, match="not ready"):
        await service.execute(
            "resume_task_checkpoint", {"checkpoint_id": saved["checkpoint"]["id"]}
        )


async def test_native_exit_and_accepted_dependency_trigger_notification_without_dispatch(
    service: OperationService,
) -> None:
    agent_id, peer = await node(service), await node(service)
    goal = await service.execute(
        "create_goal",
        {"title": "Shared delivery", "objective": "Implement and independently verify"},
    )
    original, dependency = (
        await task(service, agent_id, goal_id=goal["goal"]["id"]),
        await task(service, peer, goal_id=goal["goal"]["id"]),
    )
    saved = await service.execute(
        "save_task_checkpoint",
        {"task_id": original, "summary": "Wait for independent tests", "waiting_for": [dependency]},
    )
    checkpoint_id = saved["checkpoint"]["id"]
    await complete(service, agent_id, original)
    assert await maintain(service)
    async with service.db.sessions() as session:
        checkpoint = await session.get(TaskCheckpoint, checkpoint_id)
        assert checkpoint and checkpoint.state == "paused"
    await complete(service, peer, dependency)
    assert not await maintain(service)
    await service.execute(
        "review_task",
        {"task_id": dependency, "decision": "accepted", "note": "Verified actual test report"},
    )
    assert await maintain(service)
    assert not await maintain(service)
    async with service.db.sessions() as session:
        checkpoint = await session.get(TaskCheckpoint, checkpoint_id)
        assert checkpoint and checkpoint.state == "ready"
        assert await session.scalar(select(func.count()).select_from(Task)) == 2
        assert (
            await session.scalar(
                select(func.count())
                .select_from(CoordinationEvent)
                .where(CoordinationEvent.kind == "task_checkpoint_ready")
            )
            == 1
        )
    resumed = await service.execute("resume_task_checkpoint", {"checkpoint_id": checkpoint_id})
    assert resumed["checkpoint"]["state"] == "resumed"
    assert resumed["task"]["parameters"]["chiliarch_context"]["continue_task_id"] == original
    assert resumed["task"]["workdir"] == "/managed/project"
    assert (
        resumed["task"]["parameters"]["checkpoint"]["dependencies"][0]["review_status"]
        == "accepted"
    )
    second = await service.execute("resume_task_checkpoint", {"checkpoint_id": checkpoint_id})
    assert second["task"]["id"] == resumed["task"]["id"]
    assert second["duplicate"]
    async with service.db.sessions() as session:
        assert await session.scalar(select(func.count()).select_from(Task)) == 3


async def test_stale_active_runtime_report_prevents_early_ready(service: OperationService) -> None:
    agent_id = await node(service)
    original = await task(service, agent_id)
    saved = await service.execute(
        "save_task_checkpoint", {"task_id": original, "summary": "Stage done"}
    )
    async with service.db.sessions.begin() as session:
        agent = await session.get(Agent, agent_id)
        assert agent
        agent.metadata_json = {**agent.metadata_json, "active_task_ids": [original]}
    await complete(service, agent_id, original)
    assert not await maintain(service)
    async with service.db.sessions() as session:
        checkpoint = await session.get(TaskCheckpoint, saved["checkpoint"]["id"])
        assert checkpoint and checkpoint.state == "awaiting_exit"
    async with service.db.sessions.begin() as session:
        agent = await session.get(Agent, agent_id)
        assert agent
        agent.metadata_json = {**agent.metadata_json, "active_task_ids": []}
    assert await maintain(service)


@pytest.mark.parametrize(
    ("status", "checkpoint_state"),
    [
        ("failed", "failed"),
        ("cancelled", "failed"),
        ("interrupted", "unknown"),
        ("expired", "unknown"),
    ],
)
async def test_failed_or_unknown_execution_never_resumes(
    service: OperationService, status: str, checkpoint_state: str
) -> None:
    agent_id = await node(service)
    original = await task(service, agent_id)
    saved = await service.execute(
        "save_task_checkpoint", {"task_id": original, "summary": "Persisted partial progress"}
    )
    async with service.db.sessions.begin() as session:
        source = await session.get(Task, original)
        assert source
        source.status = status
    assert await maintain(service)
    async with service.db.sessions() as session:
        checkpoint = await session.get(TaskCheckpoint, saved["checkpoint"]["id"])
        assert checkpoint and checkpoint.state == checkpoint_state
    with pytest.raises(OperationError, match="not ready"):
        await service.execute(
            "resume_task_checkpoint", {"checkpoint_id": saved["checkpoint"]["id"]}
        )
    async with service.db.sessions() as session:
        assert await session.scalar(select(func.count()).select_from(Task)) == 1


async def test_checkpoint_scope_rejects_impersonation_and_dependency_cycles(
    service: OperationService,
) -> None:
    first, second = await node(service), await node(service)
    goal = await service.execute(
        "create_goal", {"title": "Shared delivery", "objective": "Coordinate stages"}
    )
    first_task, second_task = (
        await task(service, first, goal_id=goal["goal"]["id"]),
        await task(service, second, goal_id=goal["goal"]["id"]),
    )
    async with service.db.sessions() as session:
        with pytest.raises(OperationError, match="another agent"):
            await service.stages.validate_scope(
                session, "save_task_checkpoint", {"task_id": first_task}, second
            )
        with pytest.raises(OperationError, match="bound running"):
            await service.stages.validate_scope(
                session, "save_task_checkpoint", {"task_id": first_task}, first, second_task
            )
    with pytest.raises(OperationError, match="own task"):
        await service.execute(
            "save_task_checkpoint",
            {"task_id": first_task, "summary": "Wait", "waiting_for": [first_task]},
        )
    await service.execute(
        "save_task_checkpoint",
        {"task_id": first_task, "summary": "Wait", "waiting_for": [second_task]},
    )
    with pytest.raises(OperationError, match="cycle"):
        await service.execute(
            "save_task_checkpoint",
            {"task_id": second_task, "summary": "Wait", "waiting_for": [first_task]},
        )


async def test_supervisor_resume_uses_normal_ask_approval_and_only_dispatches_after_permission(
    service: OperationService,
) -> None:
    agent_id = await node(service)
    original = await task(service, agent_id)
    saved = await service.execute(
        "save_task_checkpoint", {"task_id": original, "summary": "Stage ready"}
    )
    await complete(service, agent_id, original)
    await maintain(service)
    service.settings_manager.current.supervisor.mode = "ask"
    result = await service.execute(
        "resume_task_checkpoint", {"checkpoint_id": saved["checkpoint"]["id"]}, actor="supervisor"
    )
    assert result["status"] == "awaiting_approval"
    async with service.db.sessions() as session:
        assert await session.scalar(select(func.count()).select_from(Task)) == 1
    approved = await service.execute("approve_operation", {"approval_id": result["approval_id"]})
    assert approved
    async with service.db.sessions() as session:
        checkpoint = await session.get(TaskCheckpoint, saved["checkpoint"]["id"])
        assert checkpoint and checkpoint.resumed_task_id
        assert await session.scalar(select(func.count()).select_from(Task)) == 2


async def test_checkpoint_cannot_exfiltrate_unrelated_task_results(
    service: OperationService,
) -> None:
    owner, outsider = await node(service), await node(service)
    source, private_task = await task(service, owner), await task(service, outsider)
    with pytest.raises(OperationError, match="shared goal"):
        await service.execute(
            "save_task_checkpoint",
            {
                "task_id": source,
                "summary": "Try to access unrelated result",
                "waiting_for": [private_task],
            },
        )
    async with service.db.sessions() as session:
        assert await session.scalar(select(func.count()).select_from(TaskCheckpoint)) == 0


async def test_bound_task_reads_only_own_and_continuation_ancestor_checkpoints(
    service: OperationService,
) -> None:
    agent_id = await node(service)
    original = await task(service, agent_id)
    original_checkpoint = (
        await service.execute(
            "save_task_checkpoint", {"task_id": original, "summary": "Previous stage"}
        )
    )["checkpoint"]
    await complete(service, agent_id, original)
    active = await task(service, agent_id, continue_task_id=original)
    sibling = await task(service, agent_id)
    own_checkpoint = (
        await service.execute(
            "save_task_checkpoint", {"task_id": active, "summary": "Current stage"}
        )
    )["checkpoint"]
    sibling_checkpoint = (
        await service.execute(
            "save_task_checkpoint", {"task_id": sibling, "summary": "Private sibling work"}
        )
    )["checkpoint"]

    async def request(
        request_id: str, operation: str, arguments: dict[str, Any], bound: bool = True
    ) -> dict[str, Any]:
        envelope = Envelope(
            type="agent.request",
            payload={
                "request_id": request_id,
                "operation": operation,
                "arguments": arguments,
                "metadata": {"task_id": active} if bound else {},
            },
        )
        await service.receive(agent_id, envelope)
        # A new transport envelope with the same request must stay idempotent
        # after applying private task filters inside the scope validator.
        await service.receive(agent_id, Envelope(type=envelope.type, payload=envelope.payload))
        replies = [
            message.payload
            for message in await service.pending(agent_id)
            if message.type == "request.resolved" and message.payload["request_id"] == request_id
        ]
        assert replies
        return dict(replies[-1])

    listed = await request("bound-list", "list_task_checkpoints", {})
    assert listed["status"] == "approved"
    assert {row["id"] for row in listed["result"]["checkpoints"]} == {
        own_checkpoint["id"],
        original_checkpoint["id"],
    }
    ancestor = await request(
        "ancestor", "get_task_checkpoint", {"checkpoint_id": original_checkpoint["id"]}
    )
    assert ancestor["result"]["checkpoint"]["summary"] == "Previous stage"
    for index, (operation, arguments) in enumerate(
        [
            ("get_task_checkpoint", {"checkpoint_id": sibling_checkpoint["id"]}),
            ("list_task_checkpoints", {"task_id": sibling}),
        ]
    ):
        denied = await request(f"sibling-{index}", operation, arguments)
        assert denied["status"] == "failed"
        assert "checkpoint" not in denied["result"]
    unbound = await request("unbound-list", "list_task_checkpoints", {}, bound=False)
    assert len(unbound["result"]["checkpoints"]) == 3


async def test_project_stage_resume_preserves_context_and_moves_live_task_guard(
    service: OperationService,
) -> None:
    from chiliarch.projects_models import WorkAttempt

    agent_id = await node(service)
    async with service.db.sessions.begin() as session:
        agent = await session.get(Agent, agent_id)
        assert agent
        agent.metadata_json = {**agent.metadata_json, "supports_repository_commands": True}
    project = await service.execute(
        "create_project", {"name": "Project", "repository": "owner/repo", "target_branch": "trunk"}
    )
    await service.execute(
        "bind_project_agent", {"project_id": project["project"]["id"], "agent_id": agent_id}
    )
    item = await service.execute(
        "create_project_work_item",
        {
            "project_id": project["project"]["id"],
            "title": "Stages",
            "instruction": "Work in stages",
        },
    )
    prepared = await service.execute(
        "start_project_work_item",
        {"work_item_id": item["work_item"]["id"], "agent_id": agent_id, "base_sha": "a" * 40},
    )
    async with service.db.sessions.begin() as session:
        response = await service.projects.receive_result(
            session,
            agent_id,
            {
                "command_id": prepared["command"]["id"],
                "status": "completed",
                "result": {
                    "branch": prepared["attempt"]["branch"],
                    "base_sha": "a" * 40,
                    "workdir": "/managed/project",
                    "workspace_id": "workspace",
                },
            },
        )
    source_id = response["attempt"]["task_id"]
    await service.receive(
        agent_id, Envelope(type="task.status", payload={"task_id": source_id, "status": "running"})
    )
    saved = await service.execute(
        "save_task_checkpoint", {"task_id": source_id, "summary": "First stage persisted"}
    )
    await complete(service, agent_id, source_id)
    await maintain(service)
    resumed = await service.execute(
        "resume_task_checkpoint", {"checkpoint_id": saved["checkpoint"]["id"]}
    )
    assert resumed["task"]["parameters"]["project"]["work_item_id"] == item["work_item"]["id"]
    async with service.db.sessions() as session:
        attempt = await session.get(WorkAttempt, prepared["attempt"]["id"])
        assert attempt and attempt.task_id == resumed["task"]["id"]
    with pytest.raises(OperationError, match="native task to stop"):
        await service.execute(
            "deliver_project_work_item",
            {
                "work_item_id": item["work_item"]["id"],
                "title": "Premature delivery",
                "message": "Do not race running continuation",
            },
        )
