"""The live GitHub switch gates new work without falsifying in-flight outcomes."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import func, select
from test_projects import SHA_A, maintain, node, project, work
from test_projects import service as service

from chiliarch.chat import Supervisor
from chiliarch.config import Settings, SettingsManager, atomic_write
from chiliarch.database import AgentRequest, Approval, Goal, Task
from chiliarch.projects_models import Project, RepositoryCommand, WorkAttempt, WorkItem
from chiliarch.protocol import Envelope
from chiliarch.service import PROJECT_OPERATIONS, OperationError, OperationService


@pytest.mark.parametrize("content", [None, "[model]\n", "[github]\n"])
def test_github_defaults_enabled_in_existing_and_new_configs(
    tmp_path: Path, content: str | None, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("CHILIARCH_GITHUB__ENABLED", raising=False)
    path = tmp_path / "config.toml"
    if content is not None:
        path.write_text(content, encoding="utf-8")
    manager = SettingsManager(path)
    assert manager.current.github.enabled
    assert manager.public()["github"] == {"enabled": True}
    manager.update({"model": {"model": "configured"}})
    assert SettingsManager(path).current.github.enabled


def test_github_switch_persists_hot_reloads_and_honors_environment(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("CHILIARCH_GITHUB__ENABLED", raising=False)
    path = tmp_path / "config.toml"
    manager = SettingsManager(path)
    observed: list[bool] = []
    manager.subscribe(lambda settings: observed.append(settings.github.enabled))
    changed = manager.update({"github": {"enabled": False}})
    assert changed["pending_restart"] == []
    assert not manager.current.github.enabled
    assert not SettingsManager(path).current.github.enabled
    assert observed == [False]
    atomic_write(path, Settings.model_validate({"github": {"enabled": True}}))
    assert manager.reload()
    assert manager.current.github.enabled
    assert observed == [False, True]
    monkeypatch.setenv("CHILIARCH_GITHUB__ENABLED", "false")
    assert manager.reload()
    assert not manager.current.github.enabled
    assert manager.saved.github.enabled
    assert not SettingsManager(path).current.github.enabled


async def test_switch_removes_model_tools_but_discovery_explains_and_config_can_reenable(
    service: OperationService,
) -> None:
    supervisor = Supervisor(service.db, service, service.settings_manager)
    original = {schema["name"] for schema in supervisor.tool_schemas()}
    assert PROJECT_OPERATIONS <= original
    await service.execute("update_config", {"patch": {"github": {"enabled": False}}})
    disabled = {schema["name"] for schema in supervisor.tool_schemas()}
    assert disabled == original - PROJECT_OPERATIONS
    assert {"dispatch_task", "send_peer_message", "save_task_checkpoint"} <= disabled
    discovery = await service.execute("list_project_tools", {})
    assert discovery["enabled"] is False and discovery["tools"] == []
    assert "disabled" in discovery["message"]
    for actor in ("user", "supervisor"):
        with pytest.raises(OperationError, match="GitHub functionality is disabled"):
            await service.execute("list_projects", {}, actor=actor)
        with pytest.raises(OperationError, match="GitHub functionality is disabled"):
            await service.execute(
                "create_project",
                {"name": "No new project", "repository": "owner/new", "target_branch": "trunk"},
                actor=actor,
            )
    assert (await service.execute("get_config", {}))["config"]["github"]["enabled"] is False
    async with service.db.sessions() as session:
        assert await session.scalar(select(func.count()).select_from(Project)) == 0
        assert await session.scalar(select(func.count()).select_from(Approval)) == 0
    await service.execute("update_config", {"patch": {"github": {"enabled": True}}})
    assert {schema["name"] for schema in supervisor.tool_schemas()} == original
    assert (await service.execute("list_project_tools", {}))["enabled"] is True


async def test_disabled_switch_blocks_previously_approved_entry_paths_and_rejection_still_works(
    service: OperationService,
) -> None:
    agent_id = await node(service)
    registered = await project(service, agent_id)
    service.settings_manager.update({"supervisor": {"mode": "ask"}})
    direct_approval = await service.execute(
        "create_project",
        {"name": "Needs permission", "repository": "owner/pending", "target_branch": "trunk"},
        actor="supervisor",
    )
    await service.receive(
        agent_id,
        Envelope(
            type="agent.request",
            payload={
                "request_id": "pending-child",
                "operation": "create_project_work_item",
                "arguments": {
                    "project_id": registered["id"],
                    "title": "Pending work",
                    "instruction": "Wait for permission",
                },
            },
        ),
    )
    async with service.db.sessions() as session:
        child = await session.scalar(
            select(AgentRequest).where(AgentRequest.external_id == "pending-child")
        )
        assert child and child.status == "pending"
        child_id = child.id
    child_approval = await service.execute(
        "resolve_agent_request", {"request_id": child_id, "decision": "approve"}, actor="supervisor"
    )
    service.settings_manager.update({"github": {"enabled": False}})
    for approval in (direct_approval, child_approval):
        with pytest.raises(OperationError, match="GitHub functionality is disabled"):
            await service.execute("approve_operation", {"approval_id": approval["approval_id"]})
    with pytest.raises(OperationError, match="GitHub functionality is disabled"):
        await service.execute(
            "resolve_agent_request", {"request_id": child_id, "decision": "approve"}
        )
    async with service.db.sessions() as session:
        assert await session.scalar(select(func.count()).select_from(Project)) == 1
        assert await session.scalar(select(func.count()).select_from(WorkItem)) == 0
        for approval in (direct_approval, child_approval):
            stored = await session.get(Approval, approval["approval_id"])
            assert stored and stored.status == "pending"
    rejected = await service.execute(
        "reject_operation", {"approval_id": direct_approval["approval_id"], "reason": "Disabled"}
    )
    assert rejected["approval"]["status"] == "rejected"
    rejected_child = await service.execute(
        "resolve_agent_request",
        {"request_id": child_id, "decision": "reject", "reason": "Disabled"},
    )
    assert rejected_child["request"]["status"] == "rejected"


async def test_disabled_agent_request_gets_durable_failure_without_poisoning_mailbox(
    service: OperationService,
) -> None:
    agent_id = await node(service)
    registered = await project(service, agent_id)
    service.settings_manager.update({"github": {"enabled": False}, "supervisor": {"mode": "ask"}})
    payload: dict[str, Any] = {
        "request_id": "disabled-child",
        "operation": "create_project_work_item",
        "arguments": {
            "project_id": registered["id"],
            "title": "Stale tool schema",
            "instruction": "Must not be queued",
        },
    }
    for _ in range(2):
        received = await service.receive(agent_id, Envelope(type="agent.request", payload=payload))
        assert received["ack"]
    await service.receive(
        agent_id,
        Envelope(
            type="agent.request",
            payload={
                "request_id": "discover-disabled",
                "operation": "list_project_tools",
                "arguments": {},
            },
        ),
    )
    await service.receive(
        agent_id,
        Envelope(
            type="agent.request",
            payload={"request_id": "ordinary-read", "operation": "list_agents", "arguments": {}},
        ),
    )
    replies = {
        message.payload["request_id"]: message.payload
        for message in await service.pending(agent_id)
        if message.type == "request.resolved"
    }
    assert replies["disabled-child"]["status"] == "failed"
    assert "disabled" in replies["disabled-child"]["result"]["error"]
    assert replies["discover-disabled"]["result"]["enabled"] is False
    assert replies["ordinary-read"]["status"] == "approved"
    async with service.db.sessions() as session:
        assert await session.scalar(select(func.count()).select_from(WorkItem)) == 0
        assert await session.scalar(select(func.count()).select_from(AgentRequest)) == 3


async def test_disabled_background_observation_preserves_history_and_stops_event_triggers(
    service: OperationService,
) -> None:
    agent_id = await node(service)
    registered = await project(service, agent_id)
    await service.execute(
        "configure_project_rule",
        {
            "project_id": registered["id"],
            "event_type": "IssueCommentEvent",
            "allowed_actors": ["owner"],
            "command": "/work",
            "objective_template": "{body}",
        },
    )
    await service.receive(agent_id, Envelope(type="heartbeat", payload={}))
    assert await maintain(service)
    commands = (await service.execute("list_project_commands", {"project_id": registered["id"]}))[
        "commands"
    ]
    assert len(commands) == 1
    service.settings_manager.update({"github": {"enabled": False}})
    assert not await maintain(service)
    await service.receive(
        agent_id,
        Envelope(
            type="repository.result",
            payload={
                "command_id": commands[0]["id"],
                "status": "completed",
                "result": {
                    "cursor": "late-event",
                    "cursor_found": True,
                    "events": [
                        {
                            "id": "late-event",
                            "type": "IssueCommentEvent",
                            "actor": "owner",
                            "body": "/work a new feature",
                        }
                    ],
                },
            },
        ),
    )
    async with service.db.sessions() as session:
        stored = await session.get(RepositoryCommand, commands[0]["id"])
        assert stored and stored.status == "completed" and stored.result["cursor"] == "late-event"
        assert await session.scalar(select(func.count()).select_from(Goal)) == 0
        assert await session.scalar(select(func.count()).select_from(Project)) == 1
        assert await session.scalar(select(func.count()).select_from(RepositoryCommand)) == 1
    service.settings_manager.update({"github": {"enabled": True}})
    assert (await service.execute("get_project", {"project_id": registered["id"]}))["commands"][0][
        "status"
    ] == "completed"


async def test_dispatched_repository_work_can_finish_after_disabling_without_replaying(
    service: OperationService,
) -> None:
    agent_id = await node(service)
    registered = await project(service, agent_id)
    item = await work(service, registered["id"])
    started = await service.execute(
        "start_project_work_item",
        {"work_item_id": item["id"], "agent_id": agent_id, "base_sha": SHA_A},
    )
    service.settings_manager.update({"github": {"enabled": False}})
    pending = [
        message
        for message in await service.pending(agent_id)
        if message.type == "repository.command"
    ]
    assert [message.payload["command_id"] for message in pending] == [started["command"]["id"]]
    payload = {
        "command_id": started["command"]["id"],
        "status": "completed",
        "result": {
            "branch": started["attempt"]["branch"],
            "base_sha": SHA_A,
            "workdir": "/managed/work",
            "workspace_id": "workspace",
        },
    }
    for _ in range(2):
        assert (
            await service.receive(agent_id, Envelope(type="repository.result", payload=payload))
        )["ack"]
    async with service.db.sessions() as session:
        attempt = await session.get(WorkAttempt, started["attempt"]["id"])
        assert attempt and attempt.task_id
        task_id = attempt.task_id
        assert await session.scalar(select(func.count()).select_from(Task)) == 1
        stored = await session.get(RepositoryCommand, started["command"]["id"])
        assert stored and stored.status == "completed"
    await service.receive(
        agent_id, Envelope(type="task.status", payload={"task_id": task_id, "status": "running"})
    )
    saved = await service.execute(
        "save_task_checkpoint", {"task_id": task_id, "summary": "Progress remains usable"}
    )
    assert saved["checkpoint"]["state"] == "awaiting_exit"
    await service.receive(
        agent_id,
        Envelope(
            type="task.status",
            payload={"task_id": task_id, "status": "completed", "result": {"tests": "passed"}},
        ),
    )
    assert (await service.execute("get_task", {"task_id": task_id}))["task"][
        "status"
    ] == "completed"
    with pytest.raises(OperationError, match="GitHub functionality is disabled"):
        await service.execute(
            "deliver_project_work_item",
            {"work_item_id": item["id"], "title": "Must wait", "message": "No new GitHub command"},
        )
    async with service.db.sessions() as session:
        assert await session.scalar(select(func.count()).select_from(RepositoryCommand)) == 1
