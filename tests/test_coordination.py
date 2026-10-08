from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from chiliarch.chat import Conversation
from chiliarch.config import Settings, SettingsManager
from chiliarch.database import CoordinationEvent, Database, Goal, Message, Task, TaskLink
from chiliarch.protocol import Envelope
from chiliarch.service import OperationError, OperationService


@pytest.fixture
async def service(tmp_path: Path) -> AsyncIterator[OperationService]:
    settings = Settings.model_validate(
        {"database": {"url": f"sqlite+aiosqlite:///{tmp_path}/coordination.db"}}
    )
    database = Database(settings.database.url)
    await database.initialize()
    yield OperationService(database, SettingsManager(settings=settings))
    await database.close()


async def agent(service: OperationService) -> str:
    code = await service.execute("create_pairing_code", {})
    return str((await service.pair(code["code"], "worker", "machine", []))["agent_id"])


async def goal(service: OperationService) -> str:
    result = await service.execute(
        "create_goal",
        {
            "title": "Deliver feature",
            "objective": "Implement and independently test feature",
            "constraints": ["Preserve unrelated files"],
            "acceptance_criteria": ["Review and tests pass"],
        },
    )
    return str(result["goal"]["id"])


async def dispatch(service: OperationService, agent_id: str, **extra: Any) -> dict[str, Any]:
    result = await service.execute(
        "dispatch_task",
        {
            "agent_id": agent_id,
            "instruction": "Implement the feature",
            **extra,
        },
    )
    return dict(result["task"])


async def complete(service: OperationService, agent_id: str, task_id: str) -> Envelope:
    message = Envelope(
        type="task.status",
        payload={
            "task_id": task_id,
            "status": "completed",
            "result": {"commit": "sample-revision"},
        },
    )
    await service.receive(agent_id, message)
    return message


async def review(
    service: OperationService, task_id: str, decision: str = "accepted", **extra: Any
) -> dict[str, Any]:
    return await service.execute(
        "review_task",
        {
            "task_id": task_id,
            "decision": decision,
            "note": "Inspected changes and ran behavioral checks",
            **extra,
        },
    )


async def test_dependency_requires_acceptance_and_releases_exactly_once(
    service: OperationService,
) -> None:
    first_agent, second_agent = await agent(service), await agent(service)
    goal_id = await goal(service)
    first = await dispatch(
        service, first_agent, goal_id=goal_id, acceptance_criteria=["Implementation works"]
    )
    second = await dispatch(
        service,
        second_agent,
        goal_id=goal_id,
        depends_on=[first["id"]],
        acceptance_criteria=["Independent review passes"],
    )
    assert first["status"] == "queued"
    assert second["status"] == "blocked"
    assert await service.pending(second_agent) == []
    with pytest.raises(OperationError):
        await complete(service, second_agent, second["id"])
    message = await complete(service, first_agent, first["id"])
    assert await service.pending(second_agent) == []
    artifact = {
        "name": "Implementation",
        "uri": "https://example.test/repo/commit/revision",
        "kind": "commit",
        "digest": None,
    }
    results = await asyncio.gather(
        *(review(service, first["id"], artifacts=[artifact]) for _ in range(3))
    )
    assert sum(len(result["released_task_ids"]) for result in results) == 1
    pending = await service.pending(second_agent)
    assert len(pending) == 1
    context = pending[0].payload["parameters"]["chiliarch_context"]
    assert context["goal"]["constraints"] == ["Preserve unrelated files"]
    assert context["dependencies"][0]["artifacts"] == [artifact]
    assert context["acceptance_criteria"] == ["Independent review passes"]
    assert (await service.receive(first_agent, message))["duplicate"]
    # A terminal status replay with a fresh envelope must not create a second wake-up.
    await complete(service, first_agent, first["id"])
    async with service.db.sessions() as session:
        count = await session.scalar(
            select(func.count())
            .select_from(CoordinationEvent)
            .where(
                CoordinationEvent.task_id == first["id"], CoordinationEvent.kind == "task_terminal"
            )
        )
        assert count == 1


async def test_retry_chain_preserves_history_and_unlocks_original_dependency(
    service: OperationService,
) -> None:
    worker, reviewer = await agent(service), await agent(service)
    goal_id = await goal(service)
    first = await dispatch(service, worker, goal_id=goal_id)
    second = await dispatch(service, reviewer, goal_id=goal_id, depends_on=[first["id"]])
    await complete(service, worker, first["id"])
    await review(
        service,
        first["id"],
        "changes_requested",
        artifacts=[{"name": "Review", "uri": "file:///reports/review.md"}],
    )
    retried = await service.execute(
        "retry_task",
        {
            "task_id": first["id"],
            "instruction": "Fix the reviewed issue",
            "recovery_note": "Checked the original commit; amend only the failing behavior",
        },
    )
    retry_id = retried["task"]["id"]
    assert retry_id != first["id"]
    assert retried["task_link"]["retry_of"] == first["id"]
    assert await service.pending(reviewer) == []
    with pytest.raises(OperationError):
        await service.execute(
            "retry_task", {"task_id": first["id"], "recovery_note": "Attempt duplicate retry"}
        )
    await complete(service, worker, retry_id)
    await review(service, retry_id)
    assert (await service.pending(reviewer))[0].payload["task_id"] == second["id"]
    original = await service.execute("get_task", {"task_id": first["id"]})
    assert original["task"]["result"] == {"commit": "sample-revision"}
    assert original["task_link"]["review_status"] == "changes_requested"
    await complete(service, reviewer, second["id"])
    await review(service, second["id"])
    finished = await service.execute(
        "update_goal",
        {"goal_id": goal_id, "status": "completed", "summary": "Reviewed implementation delivered"},
    )
    assert finished["goal"]["status"] == "completed"
    assert len(finished["goal"]["tasks"]) == 3
    assert finished["goal"]["artifacts"][0]["name"] == "Review"


async def test_goal_completion_rejects_unaccepted_and_pending_work(
    service: OperationService,
) -> None:
    worker = await agent(service)
    goal_id = await goal(service)
    with pytest.raises(OperationError):
        await service.execute("update_goal", {"goal_id": goal_id, "status": "completed"})
    task = await dispatch(service, worker, goal_id=goal_id)
    with pytest.raises(OperationError):
        await review(service, task["id"])
    await complete(service, worker, task["id"])
    with pytest.raises(OperationError):
        await service.execute("update_goal", {"goal_id": goal_id, "status": "completed"})
    await review(service, task["id"])
    waiting = await service.execute(
        "dispatch_task",
        {"agent_id": worker, "instruction": "Do pending work", "goal_id": goal_id},
        "supervisor",
    )
    assert waiting["status"] == "awaiting_approval"
    with pytest.raises(OperationError):
        await service.execute("update_goal", {"goal_id": goal_id, "status": "completed"})
    await service.execute("reject_operation", {"approval_id": waiting["approval_id"]})
    assert (await service.execute("update_goal", {"goal_id": goal_id, "status": "completed"}))[
        "goal"
    ]["status"] == "completed"
    with pytest.raises(OperationError):
        await dispatch(service, worker, goal_id=goal_id)


async def test_goal_pause_and_cancel_do_not_release_waiting_work(service: OperationService) -> None:
    worker, reviewer = await agent(service), await agent(service)
    goal_id = await goal(service)
    first = await dispatch(service, worker, goal_id=goal_id)
    second = await dispatch(service, reviewer, goal_id=goal_id, depends_on=[first["id"]])
    await service.execute("update_goal", {"goal_id": goal_id, "status": "blocked"})
    await complete(service, worker, first["id"])
    await review(service, first["id"])
    assert await service.pending(reviewer) == []
    await service.execute("update_goal", {"goal_id": goal_id, "status": "cancelled"})
    assert (await service.execute("get_task", {"task_id": second["id"]}))["task"][
        "status"
    ] == "cancelled"
    assert await service.pending(reviewer) == []
    # Existing running tasks receive the same outbound cancellation used by manual control.
    another_goal = await goal(service)
    active = await dispatch(service, worker, goal_id=another_goal)
    await service.execute("update_goal", {"goal_id": another_goal, "status": "cancelled"})
    pending = await service.pending(worker)
    assert len(pending) == 1
    assert pending[0].type == "task.cancel"
    assert pending[0].payload["task_id"] == active["id"]


@pytest.mark.parametrize("execution_status", ["queued", "running"])
async def test_cancel_goal_with_revoked_agent_preserves_unknown_remote_execution(
    service: OperationService, execution_status: str
) -> None:
    revoked_worker, available_worker = await agent(service), await agent(service)
    goal_id = await goal(service)
    uncertain = await dispatch(service, revoked_worker, goal_id=goal_id)
    waiting = await dispatch(service, revoked_worker, goal_id=goal_id, depends_on=[uncertain["id"]])
    available = await dispatch(service, available_worker, goal_id=goal_id)
    if execution_status == "running":
        await service.receive(
            revoked_worker,
            Envelope(
                type="task.status",
                payload={"task_id": uncertain["id"], "status": execution_status},
            ),
        )
    await service.execute("revoke_agent", {"agent_id": revoked_worker})

    result = await service.execute("update_goal", {"goal_id": goal_id, "status": "cancelled"})

    assert result["goal"]["status"] == "cancelled"
    tasks = {task["id"]: task for task in result["goal"]["tasks"]}
    assert tasks[uncertain["id"]]["status"] == "cancellation_requested"
    assert tasks[uncertain["id"]]["result"] is None
    assert tasks[waiting["id"]]["status"] == "cancelled"
    pending = await service.pending(available_worker)
    assert [message.type for message in pending] == ["task.cancel"]
    assert pending[0].payload["task_id"] == available["id"]
    async with service.db.sessions() as session:
        assert (
            await session.scalar(
                select(func.count())
                .select_from(Message)
                .where(
                    Message.agent_id == revoked_worker,
                    Message.direction == "outbox",
                    Message.expired.is_(False),
                )
            )
            == 0
        )
        assert (
            await session.scalar(
                select(func.count())
                .select_from(CoordinationEvent)
                .where(CoordinationEvent.task_id == uncertain["id"])
            )
            == 0
        )
    # Revocation does not establish that the previous process stopped, so it
    # cannot turn an uncertain execution into an automatically retryable task.
    with pytest.raises(OperationError):
        await service.execute(
            "retry_task",
            {"task_id": uncertain["id"], "recovery_note": "No cancellation acknowledgment"},
        )


async def test_new_coordination_tools_keep_ask_and_lower_agent_gates(
    service: OperationService,
) -> None:
    worker = await agent(service)
    waiting = await service.execute(
        "create_goal", {"title": "Goal", "objective": "Do work"}, "supervisor", "new-goal"
    )
    assert waiting["status"] == "awaiting_approval"
    assert (await service.execute("list_goals", {}, "supervisor"))["goals"] == []
    approved = await service.execute("approve_operation", {"approval_id": waiting["approval_id"]})
    goal_id = approved["approval"]["result"]["goal"]["id"]
    task = await dispatch(service, worker, goal_id=goal_id)
    await complete(service, worker, task["id"])
    await service.receive(
        worker,
        Envelope(
            type="agent.request",
            payload={
                "request_id": "accept-my-work",
                "operation": "review_task",
                "arguments": {
                    "task_id": task["id"],
                    "decision": "accepted",
                    "note": "Please review my work",
                },
            },
        ),
    )
    request = (await service.execute("list_agent_requests", {}))["requests"][0]
    awaiting = await service.execute(
        "resolve_agent_request", {"request_id": request["id"], "decision": "approve"}, "supervisor"
    )
    assert awaiting["status"] == "awaiting_approval"
    assert (await service.execute("get_task", {"task_id": task["id"]}))["task_link"][
        "review_status"
    ] == "pending"
    await service.execute("approve_operation", {"approval_id": awaiting["approval_id"]})
    assert (await service.execute("get_task", {"task_id": task["id"]}))["task_link"][
        "review_status"
    ] == "accepted"
    service.settings_manager.update({"supervisor": {"mode": "yolo"}})
    finished = await service.execute(
        "update_goal", {"goal_id": goal_id, "status": "completed"}, "supervisor"
    )
    assert finished["goal"]["status"] == "completed"
    with pytest.raises(OperationError):
        await service.execute(
            "create_goal", {"title": "Bypass", "objective": "No"}, f"agent:{worker}"
        )


@pytest.mark.parametrize("revoked", [False, True])
async def test_cancel_goal_withdraws_pending_approvals_and_lower_agent_requests(
    service: OperationService, revoked: bool
) -> None:
    worker = await agent(service)
    goal_id = await goal(service)
    failed = await dispatch(service, worker, goal_id=goal_id)
    await service.receive(
        worker,
        Envelope(type="task.status", payload={"task_id": failed["id"], "status": "failed"}),
    )
    dispatch_arguments = {"agent_id": worker, "instruction": "Pending work", "goal_id": goal_id}
    retry_arguments = {"task_id": failed["id"], "recovery_note": "Checked failed attempt"}
    awaiting_dispatch = await service.execute(
        "dispatch_task", dispatch_arguments, "supervisor", "pending-dispatch"
    )
    awaiting_retry = await service.execute(
        "retry_task", retry_arguments, "supervisor", "pending-retry"
    )
    await service.receive(
        worker,
        Envelope(
            type="agent.request",
            payload={
                "request_id": "lower-task-request",
                "operation": "dispatch_task",
                "arguments": dispatch_arguments,
            },
        ),
    )
    request = (await service.execute("list_agent_requests", {}))["requests"][0]
    resolve_arguments = {"request_id": request["id"], "decision": "approve"}
    awaiting_resolution = await service.execute(
        "resolve_agent_request", resolve_arguments, "supervisor", "pending-resolution"
    )
    unrelated = await service.execute(
        "create_goal", {"title": "Unrelated", "objective": "Other work"}, "supervisor"
    )
    if revoked:
        await service.execute("revoke_agent", {"agent_id": worker})

    cancelled = await service.execute("update_goal", {"goal_id": goal_id, "status": "cancelled"})

    assert cancelled["goal"]["status"] == "cancelled"
    # These exact calls are what the supervisor approval sweeper rechecks.
    for name, arguments, key, awaiting in (
        ("dispatch_task", dispatch_arguments, "pending-dispatch", awaiting_dispatch),
        ("retry_task", retry_arguments, "pending-retry", awaiting_retry),
        (
            "resolve_agent_request",
            resolve_arguments,
            "pending-resolution",
            awaiting_resolution,
        ),
    ):
        replay = await service.execute(name, arguments, "supervisor", key)
        assert replay["status"] == "rejected"
        decision = await service.execute(
            "approve_operation", {"approval_id": awaiting["approval_id"]}
        )
        assert decision["approval"]["status"] == "rejected"
    approvals = (await service.execute("list_approvals", {}))["approvals"]
    assert (
        next(item for item in approvals if item["id"] == unrelated["approval_id"])["status"]
        == "pending"
    )
    state = await service.state()
    assert len(state["tasks"]) == 1
    resolved = (await service.execute("list_agent_requests", {}))["requests"][0]
    assert resolved["status"] == "rejected"
    if revoked:
        assert resolved["result"]["notification_status"] == "undeliverable_revoked"
        async with service.db.sessions() as session:
            assert (
                await session.scalar(
                    select(func.count())
                    .select_from(Message)
                    .where(
                        Message.agent_id == worker,
                        Message.direction == "outbox",
                        Message.expired.is_(False),
                        Message.acked.is_(False),
                    )
                )
                == 0
            )
    else:
        pending = await service.pending(worker)
        assert len(pending) == 1
        assert pending[0].type == "request.resolved"
        assert pending[0].payload["status"] == "rejected"
        assert pending[0].payload["request_id"] == "lower-task-request"


async def test_terminal_queue_expiry_is_durable_and_state_lists_links(
    service: OperationService,
) -> None:
    worker = await agent(service)
    task = await dispatch(service, worker)
    async with service.db.write_lock, service.db.sessions.begin() as session:
        message = await session.scalar(select(Message).where(Message.direction == "outbox"))
        assert message is not None
        message.expires_at = 0
    assert await service.pending(worker) == []
    state = await service.state()
    assert state["tasks"][0]["status"] == "expired"
    assert state["task_links"][0]["task_id"] == task["id"]
    assert state["goals"] == []
    async with service.db.sessions() as session:
        event = await session.scalar(select(CoordinationEvent))
        assert event is not None and event.kind == "task_terminal"
        assert event.payload["task"]["status"] == "expired"


async def test_cancel_goal_also_withdraws_approval_for_already_resolved_agent_request(
    service: OperationService,
) -> None:
    worker = await agent(service)
    goal_id = await goal(service)
    await service.receive(
        worker,
        Envelope(
            type="agent.request",
            payload={
                "request_id": "resolved-later",
                "operation": "dispatch_task",
                "arguments": {"agent_id": worker, "instruction": "Work", "goal_id": goal_id},
            },
        ),
    )
    request = (await service.execute("list_agent_requests", {}))["requests"][0]
    arguments = {"request_id": request["id"], "decision": "approve"}
    awaiting = await service.execute(
        "resolve_agent_request", arguments, "supervisor", "resolve-before-cancellation"
    )
    assert awaiting["status"] == "awaiting_approval"
    await service.execute(
        "resolve_agent_request", {"request_id": request["id"], "decision": "reject"}
    )
    await service.execute("update_goal", {"goal_id": goal_id, "status": "cancelled"})
    resumed = await service.execute(
        "resolve_agent_request", arguments, "supervisor", "resolve-before-cancellation"
    )
    assert resumed["status"] == "rejected"
    assert (await service.execute("list_tasks", {}))["tasks"] == []
    assert len(await service.pending(worker)) == 1


async def test_additive_tables_preserve_existing_database(tmp_path: Path) -> None:
    url = f"sqlite+aiosqlite:///{tmp_path}/legacy.db"
    database = Database(url)
    await database.initialize()
    async with database.sessions.begin() as session:
        session.add(
            Task(
                id="legacy",
                agent_id="legacy-agent",
                instruction="Existing task",
                parameters={},
                status="completed",
                requested_by="user",
                result="Existing result",
            )
        )
    async with database.engine.begin() as connection:
        for model in (CoordinationEvent, TaskLink, Goal):
            await connection.run_sync(lambda sync, table=model.__table__: table.drop(sync))
    await database.close()
    reopened = Database(url)
    await reopened.initialize()
    service = OperationService(reopened, SettingsManager(settings=Settings()))
    try:
        task = await service.execute("get_task", {"task_id": "legacy"})
        assert task["task"]["result"] == "Existing result"
        assert task["task_link"]["review_status"] == "pending"
        assert await goal(service)
    finally:
        await reopened.close()


async def test_review_survives_full_queue_and_maintenance_releases_later(
    service: OperationService,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service.settings_manager.update({"transport": {"queue_limit": 1}})
    worker, reviewer = await agent(service), await agent(service)
    first = await dispatch(service, worker)
    second = await dispatch(service, reviewer, depends_on=[first["id"]])
    occupied = await service.execute(
        "send_agent_message", {"agent_id": reviewer, "message": "Existing queued message"}
    )
    await complete(service, worker, first["id"])
    result = await review(service, first["id"])
    assert result["task_link"]["review_status"] == "accepted"
    assert result["released_task_ids"] == []
    assert (await service.execute("get_task", {"task_id": second["id"]}))["task"][
        "status"
    ] == "blocked"
    await service.receive(
        reviewer, Envelope(type="ack", payload={"message_id": occupied["message_id"]})
    )
    released = asyncio.Event()
    original_release = service.coordination.release

    async def observe_release(session: AsyncSession) -> list[str]:
        tasks = await original_release(session)
        if second["id"] in tasks:
            released.set()
        return tasks

    monkeypatch.setattr(service.coordination, "release", observe_release)
    await service.start()
    try:
        await asyncio.wait_for(released.wait(), 5)
        pending = await service.pending(reviewer)
        assert len(pending) == 1
        assert pending[0].payload["task_id"] == second["id"]
    finally:
        await service.stop()


async def test_resuming_paused_goal_releases_ready_tasks(service: OperationService) -> None:
    worker, reviewer = await agent(service), await agent(service)
    goal_id = await goal(service)
    first = await dispatch(service, worker, goal_id=goal_id)
    second = await dispatch(service, reviewer, goal_id=goal_id, depends_on=[first["id"]])
    await service.execute("update_goal", {"goal_id": goal_id, "status": "blocked"})
    await complete(service, worker, first["id"])
    await review(service, first["id"])
    assert await service.pending(reviewer) == []
    resumed = await service.execute("update_goal", {"goal_id": goal_id, "status": "active"})
    assert resumed["released_task_ids"] == [second["id"]]
    assert len(await service.pending(reviewer)) == 1


async def test_invalid_dispatch_relationships_rollback_without_queued_task(
    service: OperationService,
) -> None:
    worker = await agent(service)
    for extra in (
        {"goal_id": "missing-goal"},
        {"depends_on": ["missing-task"]},
        {"conversation_id": "missing-conversation"},
        {"parameters": {"chiliarch_context": {"trusted": True}}},
    ):
        with pytest.raises(OperationError):
            await dispatch(service, worker, **extra)
    assert (await service.execute("list_tasks", {}))["tasks"] == []
    assert await service.pending(worker) == []


async def test_retry_requires_explicit_recovery_and_retains_auto_high_risk(
    service: OperationService,
) -> None:
    worker = await agent(service)
    original = await dispatch(service, worker)
    await service.receive(
        worker,
        Envelope(
            type="task.status",
            payload={
                "task_id": original["id"],
                "status": "interrupted",
                "error": "Process ended with unverified external effects",
            },
        ),
    )
    with pytest.raises(OperationError):
        await service.execute("retry_task", {"task_id": original["id"]})
    service.settings_manager.update({"supervisor": {"mode": "auto"}})
    waiting = await service.execute(
        "retry_task",
        {
            "task_id": original["id"],
            "recovery_note": "Checked git state and build log before continuing",
            "_risk": {"level": "low", "reason": "Model claims safe"},
        },
        "supervisor",
    )
    assert waiting["status"] == "awaiting_approval"
    assert len((await service.execute("list_tasks", {}))["tasks"]) == 1
    assert await service.pending(worker) == []
    await service.execute("approve_operation", {"approval_id": waiting["approval_id"]})
    pending = await service.pending(worker)
    assert len(pending) == 1
    assert pending[0].payload["task_id"] != original["id"]
    assert (
        pending[0].payload["parameters"]["recovery_note"]
        == "Checked git state and build log before continuing"
    )


async def test_goal_and_tasks_share_the_validated_conversation(service: OperationService) -> None:
    async with service.db.sessions.begin() as session:
        session.add(Conversation(id="coordination-chat", title="Feature goal", source="user"))
        session.add(Conversation(id="other-chat", title="Unrelated", source="user"))
    worker = await agent(service)
    result = await service.execute(
        "create_goal",
        {"title": "Feature", "objective": "Build feature", "conversation_id": "coordination-chat"},
    )
    goal_id = result["goal"]["id"]
    task = await dispatch(service, worker, goal_id=goal_id)
    assert (await service.execute("get_task", {"task_id": task["id"]}))["task_link"][
        "conversation_id"
    ] == "coordination-chat"
    with pytest.raises(OperationError):
        await dispatch(service, worker, goal_id=goal_id, conversation_id="other-chat")
    await complete(service, worker, task["id"])
    async with service.db.sessions() as session:
        event = await session.scalar(
            select(CoordinationEvent).where(CoordinationEvent.task_id == task["id"])
        )
        assert event is not None
        assert event.conversation_id == "coordination-chat"
        assert event.goal_id == goal_id


@pytest.mark.parametrize(
    "uri",
    ["javascript:alert(1)", "data:text/html,unsafe", "https://user:password@example.test/report"],
)
async def test_artifact_references_reject_executable_schemes_and_credentials(
    service: OperationService, uri: str
) -> None:
    worker = await agent(service)
    task = await dispatch(service, worker)
    await complete(service, worker, task["id"])
    with pytest.raises(OperationError) as failure:
        await review(service, task["id"], artifacts=[{"name": "Unsafe reference", "uri": uri}])
    assert failure.value.status_code == 422
    assert (await service.execute("get_task", {"task_id": task["id"]}))["task_link"][
        "review_status"
    ] == "pending"
