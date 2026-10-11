"""Task directories survive approval/queues and govern the actual native process and lock."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest
from jsonschema import Draft202012Validator
from pydantic import ValidationError

from chiliarch.adapters.runners import RunnerError, RunnerHandler
from chiliarch.adapters.runtime import workspace_owner
from chiliarch.database import Agent
from chiliarch.protocol import Envelope, protocol_schema
from chiliarch.service import OperationError, OperationService
from tests.test_coordination import agent, complete, dispatch, goal, review
from tests.test_coordination import service as service
from tests.test_peer_service import request
from tests.test_runner_continuations import Kind, client_for, observed, run, settings, task

JsonObject = dict[str, Any]


@pytest.mark.parametrize(
    "workdir", ["/srv/project 中文", "C:\\Projects\\app", "\\\\server\\share\\app"]
)
def test_portable_directory_protocol_roundtrips(workdir: str) -> None:
    envelope = Envelope(
        type="task.dispatch", payload={"task_id": "t", "instruction": "Build", "workdir": workdir}
    )
    Draft202012Validator(protocol_schema()).validate(envelope.model_dump(mode="json"))
    assert Envelope.model_validate_json(envelope.model_dump_json()).payload["workdir"] == workdir


@pytest.mark.parametrize(
    "workdir", ["", "relative/project", "C:relative", "~/project", "/a\x00b", "/a\nb"]
)
def test_invalid_directory_rejected_by_both_protocols(workdir: str) -> None:
    data = task("t").model_dump(mode="json")
    data["payload"]["workdir"] = workdir
    assert list(Draft202012Validator(protocol_schema()).iter_errors(data))
    with pytest.raises(ValidationError):
        Envelope.model_validate(data)


def test_legacy_dispatch_fingerprint_does_not_gain_a_null_directory() -> None:
    payload: JsonObject = {"task_id": "t", "instruction": "Build", "parameters": {}}
    assert Envelope(type="task.dispatch", payload=payload).payload == payload
    assert Envelope(type="task.dispatch", payload={**payload, "workdir": None}).payload == payload


@pytest.mark.parametrize("kind", ["codex", "claude-code"])
async def test_override_runs_in_requested_directory_and_does_not_change_default(
    tmp_path: Path, kind: Kind
) -> None:
    root = await asyncio.to_thread(tmp_path.resolve)
    chosen = tmp_path / "another project 中文"
    chosen.mkdir()
    configuration = settings(tmp_path, kind)
    client = await client_for(tmp_path, configuration)
    try:
        override = task("override")
        override.payload["workdir"] = str(chosen)
        result = await run(client, override)
        assert result["status"] == "completed"
        assert result["result"]["workdir"] == str(chosen.resolve())
        assert observed(chosen)["cwd"] == str(chosen.resolve())
        assert not (tmp_path / "observed.json").exists()
        default = await run(client, task("default"))
        assert default["status"] == "completed"
        assert observed(tmp_path)["cwd"] == str(root)
        assert configuration.workdir == root
    finally:
        await client.store.close()


@pytest.mark.parametrize("kind", ["codex", "claude-code"])
async def test_continuation_keeps_actual_directory_after_default_changes(
    tmp_path: Path, kind: Kind
) -> None:
    project = tmp_path / "original project"
    project.mkdir()
    configuration = settings(tmp_path, kind)
    first = await client_for(tmp_path, configuration)
    try:
        original = task("original")
        original.payload["workdir"] = str(project)
        assert (await run(first, original))["status"] == "completed"
    finally:
        await first.store.close()
    new_default = tmp_path / "new default"
    new_default.mkdir()
    configuration = type(configuration).model_validate(
        {**configuration.model_dump(), "workdir": new_default}
    )
    restarted = await client_for(tmp_path, configuration)
    try:
        result = await run(restarted, task("follow-up", "original"))
        assert result["status"] == "completed"
        assert result["result"]["workdir"] == str(project.resolve())
        assert "Task ID: follow-up" in observed(project)["stdin"]
        assert not (tmp_path / "observed.json").exists()
    finally:
        await restarted.store.close()


async def test_target_lock_blocks_override_but_other_directory_still_runs(tmp_path: Path) -> None:
    root = await asyncio.to_thread(tmp_path.resolve)
    chosen = tmp_path / "locked project"
    chosen.mkdir()
    handler = RunnerHandler(settings(tmp_path), tmp_path / "identity.db")
    waiting = asyncio.Event()

    async def progress(value: float, message: str) -> None:
        if "blocked" in handler.runtime_metadata()["blocked_task_ids"]:
            waiting.set()

    override = task("blocked").payload
    override["workdir"] = str(chosen)
    async with workspace_owner(chosen):
        blocked = asyncio.create_task(handler(override, progress))
        try:
            await asyncio.wait_for(waiting.wait(), 5)
            metadata = handler.runtime_metadata()
            assert metadata["task_workspaces"]["blocked"]["workspace"] == str(chosen.resolve())
            assert metadata["default_workdir"] == str(root)
            assert not (chosen / "observed.json").exists()
            chosen.rmdir()
            missing = handler.runtime_metadata()
            assert missing["task_workspaces"]["blocked"] == {"workspace": str(chosen)}
            chosen.mkdir()
            default = await asyncio.wait_for(handler(task("default").payload, progress), 10)
            assert default["workdir"] == str(root)
        finally:
            blocked.cancel()
            with pytest.raises(asyncio.CancelledError):
                await blocked
    assert handler.runtime_metadata()["task_workspaces"] == {}
    result = await handler(override, progress)
    assert result["workdir"] == str(chosen.resolve())


@pytest.mark.parametrize("relative", [False, True])
async def test_unusable_task_directory_fails_without_launch_or_creation(
    tmp_path: Path, relative: bool
) -> None:
    handler = RunnerHandler(settings(tmp_path), tmp_path / "identity.db")
    payload = task("invalid").payload
    payload["workdir"] = "relative" if relative else str(tmp_path / "absent")

    async def progress(value: float, message: str) -> None:
        pytest.fail("Invalid directory must fail before native startup")

    with pytest.raises(RunnerError):
        await handler(payload, progress)
    assert not (tmp_path / "observed.json").exists()
    assert not (tmp_path / "absent").exists()


async def capable(service: OperationService) -> str:
    worker = await agent(service)
    await service.receive(
        worker, Envelope(type="heartbeat", payload={"metadata": {"supports_task_workdir": True}})
    )
    return worker


async def test_directory_survives_approval_dependencies_and_service_restart(
    service: OperationService,
) -> None:
    worker = await capable(service)
    goal_id = await goal(service)
    first = await dispatch(service, worker, goal_id=goal_id)
    args = {
        "agent_id": worker,
        "instruction": "Review",
        "workdir": "D:\\Projects\\review",
        "goal_id": goal_id,
        "depends_on": [first["id"]],
    }
    waiting = await service.execute("dispatch_task", args, "supervisor", "directory-call")
    with pytest.raises(OperationError):
        await service.execute(
            "dispatch_task", {**args, "workdir": "D:\\Other"}, "supervisor", "directory-call"
        )
    await service.execute("approve_operation", {"approval_id": waiting["approval_id"]})
    blocked = (await service.execute("dispatch_task", args, "supervisor", "directory-call"))["task"]
    assert blocked["status"] == "blocked"
    assert blocked["workdir"] == args["workdir"]
    restarted = OperationService(service.db, service.settings_manager)
    await complete(restarted, worker, first["id"])
    await review(restarted, first["id"])
    envelopes = [
        item
        for item in await restarted.pending(worker)
        if item.payload.get("task_id") == blocked["id"]
    ]
    assert len(envelopes) == 1
    assert envelopes[0].payload["workdir"] == args["workdir"]
    assert (await restarted.execute("get_task", {"task_id": blocked["id"]}))["task"][
        "workdir"
    ] == args["workdir"]


async def test_unsupported_agent_rejects_override_but_default_still_works(
    service: OperationService,
) -> None:
    worker = await agent(service)
    with pytest.raises(OperationError, match="support task workdirs"):
        await dispatch(service, worker, workdir="/projects/app")
    assert (await service.execute("list_tasks", {}))["tasks"] == []
    assert (await dispatch(service, worker))["workdir"] is None
    assert "workdir" not in (await service.pending(worker))[0].payload


@pytest.mark.parametrize(
    "change_agent,explicit",
    [(False, None), (True, None), (False, "/new/work"), (True, "/new/work")],
)
async def test_retry_directory_inheritance_is_scoped_to_same_agent(
    service: OperationService, change_agent: bool, explicit: str | None
) -> None:
    worker, other = await capable(service), await capable(service)
    initial = await dispatch(service, worker, workdir="/original/work")
    await service.receive(
        worker,
        Envelope(
            type="task.status",
            payload={"task_id": initial["id"], "status": "failed", "error": "Native task failed"},
        ),
    )
    result = await service.execute(
        "retry_task",
        {
            "task_id": initial["id"],
            "agent_id": other if change_agent else worker,
            "workdir": explicit,
            "recovery_note": "Verified previous process stopped",
        },
    )
    assert result["task"]["workdir"] == (explicit or (None if change_agent else "/original/work"))


async def test_continuation_inherits_actual_result_and_validates_untrusted_path(
    service: OperationService,
) -> None:
    worker = await capable(service)
    initial = await dispatch(service, worker)
    await service.receive(
        worker,
        Envelope(
            type="task.status",
            payload={
                "task_id": initial["id"],
                "status": "completed",
                "result": {"workdir": "/actual/work"},
            },
        ),
    )
    continued = await dispatch(service, worker, continue_task_id=initial["id"])
    assert continued["workdir"] == "/actual/work"
    invalid = await dispatch(service, worker)
    await service.receive(
        worker,
        Envelope(
            type="task.status",
            payload={
                "task_id": invalid["id"],
                "status": "completed",
                "result": {"workdir": ["malformed"]},
            },
        ),
    )
    with pytest.raises(OperationError, match="absolute path"):
        await dispatch(service, worker, continue_task_id=invalid["id"])


async def test_agent_requests_use_same_directory_dispatch_and_approval(
    service: OperationService,
) -> None:
    sender, worker = await capable(service), await capable(service)
    pending = await request(
        service,
        sender,
        "dispatch_task",
        {"agent_id": worker, "instruction": "Review", "workdir": "/reviews/app"},
    )
    assert pending["status"] == "pending"
    assert await service.pending(worker) == []
    await service.execute(
        "resolve_agent_request", {"request_id": pending["id"], "decision": "approve"}
    )
    assert (await service.pending(worker))[0].payload["workdir"] == "/reviews/app"


@pytest.mark.parametrize("same_target", [False, True])
async def test_wait_graph_uses_task_directory_instead_of_agent_default(
    service: OperationService, same_target: bool
) -> None:
    service.settings_manager.current.supervisor.mode = "yolo"
    a, b = await capable(service), await capable(service)
    async with service.db.sessions.begin() as session:
        for worker in (a, b):
            record = await session.get(Agent, worker)
            assert record is not None
            record.metadata_json = {
                "workspace": "/common/default",
                "workspace_id": "common-default",
                "supports_task_workdir": True,
            }
    first = await dispatch(service, a, workdir="/actual/a")
    second = await dispatch(service, b, workdir="/actual/a" if same_target else "/actual/b")
    await service.receive(
        a, Envelope(type="task.status", payload={"task_id": first["id"], "status": "running"})
    )
    response = await request(
        service, a, "wait_for_tasks", {"task_id": first["id"], "waiting_for": [second["id"]]}
    )
    assert response["status"] == ("failed" if same_target else "approved")
