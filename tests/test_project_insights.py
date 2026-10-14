from __future__ import annotations

import time
from collections.abc import AsyncIterator
from pathlib import Path

import pytest

from chiliarch.config import Settings, SettingsManager
from chiliarch.database import Agent, Database, Task
from chiliarch.projects_models import Project, ProjectBinding, WorkAttempt, WorkItem
from chiliarch.service import OperationService
from chiliarch.task_stages import TaskCheckpoint


@pytest.fixture
async def service(tmp_path: Path) -> AsyncIterator[OperationService]:
    settings = Settings.model_validate(
        {"database": {"url": f"sqlite+aiosqlite:///{tmp_path}/state.db"}}
    )
    database = Database(settings.database.url)
    await database.initialize()
    try:
        yield OperationService(database, SettingsManager(settings=settings))
    finally:
        await database.close()


async def seed(service: OperationService) -> None:
    async with service.db.sessions.begin() as session:
        session.add(
            Project(
                id="p",
                name="Project",
                repository="owner/project",
                github_host="github.com",
                target_branch="trunk",
                repository_id="1",
            )
        )
        await session.flush()
        for agent_id, mode in [("ready", "managed"), ("manual", "manual"), ("offline", "managed")]:
            session.add(
                Agent(
                    id=agent_id,
                    name=agent_id,
                    machine="test",
                    credential_hash=agent_id,
                    capabilities=["python"],
                    last_seen=time.time() if agent_id != "offline" else 0,
                    metadata_json={
                        "supports_repository_commands": True,
                        "execution_mode": mode,
                        "operating_system": "linux",
                        "supports_task_workdir": True,
                        "capacity": 1,
                        "queued_tasks": 0,
                        "active_task_ids": [],
                    },
                )
            )
            await session.flush()
            session.add(
                ProjectBinding(
                    project_id="p",
                    agent_id=agent_id,
                    observation={"repository_id": 1},
                    observed_at=time.time(),
                )
            )


async def test_routing_explains_constraints_without_dispatch(service: OperationService) -> None:
    await seed(service)
    result = await service.execute(
        "recommend_project_agents",
        {"project_id": "p", "capabilities": ["python"], "operating_system": "linux"},
    )
    rows = {row["agent_id"]: row for row in result["candidates"]}
    assert rows["ready"]["eligible"]
    assert not rows["manual"]["eligible"]
    assert rows["manual"]["reasons"] == ["interactive_agent_requires_manual_claim"]
    assert rows["offline"]["reasons"] == ["offline_or_revoked"]
    assert rows["ready"]["push_authentication"] == "unverified"
    excluded = await service.execute(
        "recommend_project_agents",
        {"project_id": "p", "exclude_agent_ids": ["ready"], "operating_system": "windows"},
    )
    ready = next(row for row in excluded["candidates"] if row["agent_id"] == "ready")
    assert not ready["eligible"]
    assert set(ready["reasons"]) == {"excluded_identity", "operating_system_mismatch_or_unknown"}
    assert (await service.execute("list_tasks", {}))["tasks"] == []


async def test_usage_is_once_per_task_and_missing_remains_unknown(
    service: OperationService,
) -> None:
    await seed(service)
    async with service.db.sessions.begin() as session:
        session.add(
            WorkItem(
                id="w",
                project_id="p",
                title="Work",
                instruction="Work",
                delivery_mode="separate",
                stale_facts=["api"],
            )
        )
        await session.flush()
        for number, result in [
            (1, {"usage": {"input_tokens": 12, "output_tokens": 7}, "total_cost_usd": 0.1}),
            (2, {}),
        ]:
            session.add(
                Task(
                    id=f"t{number}",
                    agent_id="ready",
                    instruction="Work",
                    status="completed",
                    result=result,
                    requested_by="user",
                )
            )
            await session.flush()
            session.add(
                WorkAttempt(
                    id=f"a{number}",
                    work_item_id="w",
                    number=number,
                    agent_id="ready",
                    base_sha="a" * 40,
                    branch=f"task/{number}",
                    target_branch="trunk",
                    task_id=f"t{number}",
                    state="completed",
                )
            )
    first = await service.execute("get_project_insights", {"project_id": "p"})
    second = await service.execute("get_project_insights", {"project_id": "p"})
    assert first == second
    assert first["usage"]["totals"] == {"input_tokens": 12, "output_tokens": 7}
    assert first["usage"]["missing_usage_count"] == 1
    assert first["blockers"] == [{"kind": "stale_facts", "work_item_id": "w", "keys": ["api"]}]
    assert [row["at"] for row in first["timeline"]] == sorted(
        row["at"] for row in first["timeline"]
    )
    assert {row["task_id"] for row in first["timeline"] if row["kind"] == "task"} == {"t1", "t2"}


async def test_project_usage_keeps_prior_phases_after_attempt_advances(
    service: OperationService,
) -> None:
    await seed(service)
    async with service.db.sessions.begin() as session:
        session.add(
            WorkItem(
                id="w", project_id="p", title="Work", instruction="Work", delivery_mode="separate"
            )
        )
        for task_id, usage in [("first", 3), ("next", 5)]:
            session.add(
                Task(
                    id=task_id,
                    agent_id="ready",
                    instruction="Phase",
                    status="completed",
                    result={"usage": {"output_tokens": usage}},
                    requested_by="user",
                )
            )
        await session.flush()
        session.add(
            WorkAttempt(
                id="a",
                work_item_id="w",
                number=1,
                agent_id="ready",
                base_sha="a" * 40,
                branch="task/a",
                target_branch="trunk",
                task_id="next",
                state="completed",
            )
        )
        session.add(
            TaskCheckpoint(
                id="checkpoint",
                task_id="first",
                agent_id="ready",
                summary="phase done",
                state="resumed",
                resumed_task_id="next",
            )
        )
    result = await service.execute("get_project_insights", {"project_id": "p"})
    assert result["usage"]["totals"] == {"output_tokens": 8}
    assert {row["task_id"] for row in result["usage"]["attempts"]} == {"first", "next"}
    assert {row["attempt_id"] for row in result["usage"]["attempts"]} == {"a"}
    assert any(row["kind"] == "checkpoint" for row in result["timeline"])
