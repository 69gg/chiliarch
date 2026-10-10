from __future__ import annotations

import asyncio
import json
import time
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import httpx
import pytest
from sqlalchemy import func, select
from test_ai_providers import SSE, chat
from test_chat_supervisor import Upstream, manager, register_change, wait_status
from test_core_service import add_agent
from test_supervision_coordination import ObservingModel, wait_event

from chiliarch.chat import ChatRun, Supervisor
from chiliarch.database import Agent, CoordinationEvent, Database, Message, Task
from chiliarch.monitoring import monitor_tasks
from chiliarch.service import OperationService
from chiliarch.supervision import consume_coordination, recover_coordination


async def test_monitoring_is_durable_rearms_after_activity_and_never_reissues_work(
    tmp_path: Path,
) -> None:
    db = Database(f"sqlite+aiosqlite:///{tmp_path / 'monitor.db'}")
    settings = manager("yolo")
    service = OperationService(db, settings)
    await db.initialize()
    try:
        agent = await add_agent(service)
        response = await service.execute(
            "dispatch_task",
            {
                "agent_id": agent["agent_id"],
                "instruction": "检查跨机器结果",
            },
        )
        task_id = response["task"]["id"]
        async with db.sessions.begin() as session:
            task = await session.get(Task, task_id)
            owner = await session.get(Agent, agent["agent_id"])
            assert task is not None and owner is not None
            task.status, task.updated_at = "running", 1000
            owner.last_seen = 1000
            assert await monitor_tasks(session, settings.current, now=1301) == 2
        # A new DB connection (as after a restart) finds the durable episode keys.
        async with db.sessions.begin() as session:
            assert await monitor_tasks(session, settings.current, now=2000) == 0
            assert await session.scalar(select(func.count()).select_from(Message)) == 1
            task = await session.get(Task, task_id)
            owner = await session.get(Agent, agent["agent_id"])
            assert task is not None and owner is not None
            assert task.status == "running"
            owner.last_seen = 2000
            task.updated_at = 2000
            assert await monitor_tasks(session, settings.current, now=2001) == 0
        async with db.sessions.begin() as session:
            assert await monitor_tasks(session, settings.current, now=2301) == 2
            task = await session.get(Task, task_id)
            assert task is not None
            task.status = "completed"
        async with db.sessions.begin() as session:
            assert await monitor_tasks(session, settings.current, now=9000) == 0
            assert await session.scalar(select(func.count()).select_from(Message)) == 1
            events = (await session.scalars(select(CoordinationEvent))).all()
            assert len(events) == 4
            assert {event.task_id for event in events} == {task_id}
            assert {event.kind for event in events} == {"task_stalled", "agent_offline"}
    finally:
        await db.close()


class RecoveringModel(Upstream):
    def __init__(self) -> None:
        super().__init__()
        self.failure = True

    async def handle(self, request: httpx.Request) -> httpx.Response:
        if not self.requests:
            return await super().handle(request)
        self.requests.append(json.loads(request.content))
        if self.failure:
            return httpx.Response(503, json={"error": {"message": "temporary failure"}})
        return httpx.Response(
            200,
            headers={"content-type": "text/event-stream"},
            stream=SSE([chat({"content": "已核对已有变更，继续等待结果。"}, "stop")]),
        )


async def due_recovery(db: Database) -> CoordinationEvent:
    async with db.sessions.begin() as session:
        row = (
            await session.scalars(
                select(CoordinationEvent).where(
                    CoordinationEvent.kind == "model_recovery",
                    CoordinationEvent.status == "pending",
                )
            )
        ).one()
        row.payload = {**row.payload, "ready_at": time.time() - 1}
        return row


async def test_transient_model_failure_reconciles_after_backoff_without_replaying_tools(
    tmp_path: Path,
) -> None:
    db = Database(f"sqlite+aiosqlite:///{tmp_path / 'recover.db'}")
    settings = manager("yolo")
    settings.current.model.retries = 0
    settings.current.supervisor.model_recovery_base_seconds = 30
    service = OperationService(db, settings)
    model = RecoveringModel()
    effects: list[int] = []
    register_change(service, effects)
    supervisor = Supervisor(db, service, settings, provider_factory=model.provider)
    await db.initialize()
    try:
        conversation = await supervisor.create_conversation(title="恢复行为")
        run = await supervisor.send_message(conversation["id"], "执行一次变更，然后核对")
        await wait_status(supervisor, run["run_id"], "failed")
        assert effects == [7]
        await consume_coordination(supervisor)
        assert len(model.requests) == 2
        model.failure = False
        event = await due_recovery(db)
        await consume_coordination(supervisor)
        await wait_event(db, event.id)
        assert effects == [7]
        assert len(model.requests) == 3
        history = model.requests[-1]["messages"]
        results = [
            json.loads(message["content"]) for message in history if message["role"] == "tool"
        ]
        assert results == [{"status": "changed", "value": 7}]
        context = json.loads(history[-1]["content"].split("\n", 1)[1])
        assert context["event"] == "model_recovery"
        assert context["data"]["failed_run_id"] == run["run_id"]
        async with db.sessions() as session:
            runs = (await session.scalars(select(ChatRun))).all()
            assert {row.status for row in runs} == {"failed", "completed"}
    finally:
        await supervisor.stop()
        await db.close()


class UnavailableModel(ObservingModel):
    def __init__(self, status: int | str = 503) -> None:
        super().__init__()
        self.status = status

    async def handle(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(json.loads(request.content))
        self.observed.set()
        if self.gate is not None:
            await self.gate.wait()
        if self.status == "connection":
            raise httpx.ConnectError("unavailable", request=request)
        if self.status == "malformed":
            return httpx.Response(
                200,
                headers={"content-type": "text/event-stream"},
                stream=SSE([chat({"content": "incomplete"})]),
            )
        assert isinstance(self.status, int)
        return httpx.Response(self.status, json={"error": {"message": "test failure"}})


@pytest.mark.parametrize("api_mode", ["chat_completions", "anthropic"])
@pytest.mark.parametrize(
    "status,recoverable",
    [(429, True), (503, True), (401, False), (400, False), ("connection", True)],
)
async def test_only_transient_provider_failures_schedule_reconciliation(
    tmp_path: Path, api_mode: str, status: int | str, recoverable: bool
) -> None:
    db = Database(f"sqlite+aiosqlite:///{tmp_path / 'classification.db'}")
    settings = manager("yolo")
    settings.current.model.api_mode = api_mode  # type: ignore[assignment]
    settings.current.model.retries = 0
    settings.current.model.request_params = {"max_tokens": 100}
    service = OperationService(db, settings)
    model = UnavailableModel(status)
    supervisor = Supervisor(db, service, settings, provider_factory=model.provider)
    await db.initialize()
    try:
        conversation = await supervisor.create_conversation(title="故障分类")
        run = await supervisor.send_message(conversation["id"], "核查当前任务")
        await wait_status(supervisor, run["run_id"], "failed")
        async with db.sessions() as session:
            events = (await session.scalars(select(CoordinationEvent))).all()
            assert len(events) == int(recoverable)
            if events:
                assert events[0].payload["attempt"] == 1
    finally:
        await supervisor.stop()
        await db.close()


async def test_recovery_is_bounded_across_restart_and_exhaustion_stays_paused(
    tmp_path: Path,
) -> None:
    db = Database(f"sqlite+aiosqlite:///{tmp_path / 'bounded.db'}")
    settings = manager("yolo")
    settings.current.model.retries = 0
    settings.current.supervisor.model_recovery_attempts = 2
    service = OperationService(db, settings)
    model = UnavailableModel()
    supervisor = Supervisor(db, service, settings, provider_factory=model.provider)
    await db.initialize()
    try:
        conversation = await supervisor.create_conversation(title="重试上限")
        first = await supervisor.send_message(conversation["id"], "核查当前任务")
        await wait_status(supervisor, first["run_id"], "failed")
        await supervisor.stop()
        service = OperationService(db, settings)
        supervisor = Supervisor(db, service, settings, provider_factory=model.provider)
        for attempt in (1, 2):
            event = await due_recovery(db)
            assert event.payload["attempt"] == attempt
            await consume_coordination(supervisor)
            await wait_event(db, event.id, "failed")
        async with db.sessions.begin() as session:
            assert (
                await session.scalar(
                    select(func.count())
                    .select_from(CoordinationEvent)
                    .where(
                        CoordinationEvent.kind == "model_recovery",
                        CoordinationEvent.status == "pending",
                    )
                )
                == 0
            )
            session.add(
                CoordinationEvent(
                    event_key="new-update", kind="task_stalled", conversation_id=conversation["id"]
                )
            )
        await consume_coordination(supervisor)
        assert len(model.requests) == 3
    finally:
        await supervisor.stop()
        await db.close()


@pytest.mark.parametrize("action", ["stop", "user_message"])
async def test_user_action_supersedes_queued_recovery(tmp_path: Path, action: str) -> None:
    db = Database(f"sqlite+aiosqlite:///{tmp_path / 'stop.db'}")
    settings = manager("yolo")
    settings.current.model.retries = 0
    service = OperationService(db, settings)
    model = UnavailableModel()
    supervisor = Supervisor(db, service, settings, provider_factory=model.provider)
    await db.initialize()
    try:
        conversation = await supervisor.create_conversation(title="用户控制")
        run = await supervisor.send_message(conversation["id"], "核查当前任务")
        await wait_status(supervisor, run["run_id"], "failed")
        event = await due_recovery(db)
        if action == "stop":
            await supervisor.cancel_run(run["run_id"])
            await consume_coordination(supervisor)
            assert len(model.requests) == 1
        else:
            model.status = 400
            followup = await supervisor.send_message(conversation["id"], "取消旧计划，处理新问题")
            await wait_status(supervisor, followup["run_id"], "failed")
            await consume_coordination(supervisor)
            assert len(model.requests) == 2
        async with db.sessions() as session:
            stored = await session.get(CoordinationEvent, event.id)
            assert stored is not None and stored.status in {"cancelled", "superseded"}
    finally:
        await supervisor.stop()
        await db.close()


async def test_stop_wins_when_recovery_was_claimed_concurrently(tmp_path: Path) -> None:
    db = Database(f"sqlite+aiosqlite:///{tmp_path / 'stop-claimed.db'}")
    settings = manager("yolo")
    settings.current.model.retries = 0
    service = OperationService(db, settings)
    model = UnavailableModel()
    supervisor = Supervisor(db, service, settings, provider_factory=model.provider)
    await db.initialize()
    try:
        conversation = await supervisor.create_conversation(title="竞争停止")
        first = await supervisor.send_message(conversation["id"], "核查当前任务")
        await wait_status(supervisor, first["run_id"], "failed")
        event = await due_recovery(db)
        model.gate = asyncio.Event()
        await consume_coordination(supervisor)
        # The retry is already claimed; stopping the failed parent must follow it.
        await supervisor.cancel_run(first["run_id"])
        async with db.sessions.begin() as session:
            stored = await session.get(CoordinationEvent, event.id)
            assert stored is not None and stored.run_id is not None
            retried = await session.get(ChatRun, stored.run_id)
            assert retried is not None and retried.status == "cancelled"
        model.gate.set()
        await consume_coordination(supervisor)
        async with db.sessions() as session:
            assert await session.scalar(select(func.count()).select_from(ChatRun)) == 2
    finally:
        if model.gate is not None:
            model.gate.set()
        await supervisor.stop()
        await db.close()


@pytest.mark.parametrize("status,attempts", [("malformed", 3), (503, 0)])
async def test_incomplete_turn_or_disabled_recovery_remains_paused(
    tmp_path: Path, status: int | str, attempts: int
) -> None:
    db = Database(f"sqlite+aiosqlite:///{tmp_path / 'no-recovery.db'}")
    settings = manager("yolo")
    settings.current.model.retries = 0
    settings.current.supervisor.model_recovery_attempts = attempts
    service = OperationService(db, settings)
    model = UnavailableModel(status)
    supervisor = Supervisor(db, service, settings, provider_factory=model.provider)
    await db.initialize()
    try:
        conversation = await supervisor.create_conversation(title="不应重试")
        first = await supervisor.send_message(conversation["id"], "核查当前任务")
        await wait_status(supervisor, first["run_id"], "failed")
        await consume_coordination(supervisor)
        async with db.sessions() as session:
            assert await session.scalar(select(func.count()).select_from(CoordinationEvent)) == 0
        assert len(model.requests) == 1
    finally:
        await supervisor.stop()
        await db.close()


async def test_liveness_notices_wait_for_model_and_then_wake_supervisor_once(
    tmp_path: Path,
) -> None:
    db = Database(f"sqlite+aiosqlite:///{tmp_path / 'wakeup.db'}")
    settings = manager("yolo", model="")
    service = OperationService(db, settings)
    model = ObservingModel()
    supervisor = Supervisor(db, service, settings, provider_factory=model.provider)
    await db.initialize()
    try:
        agent = await add_agent(service)
        conversation = await supervisor.create_conversation(title="持续监护")
        task = await service.execute(
            "dispatch_task",
            {
                "agent_id": agent["agent_id"],
                "instruction": "执行耗时检查",
                "conversation_id": conversation["id"],
            },
        )
        async with db.sessions.begin() as session:
            row = await session.get(Task, task["task"]["id"])
            assert row is not None
            assert await monitor_tasks(session, settings.current, now=row.updated_at + 301) == 2
        await consume_coordination(supervisor)
        assert not model.requests
        settings.current.model.model = "test"
        await consume_coordination(supervisor)
        async with db.sessions() as session:
            events = (await session.scalars(select(CoordinationEvent))).all()
        for event in events:
            await wait_event(db, event.id)
        assert len(model.requests) == 1
        context = json.loads(model.requests[0]["messages"][-1]["content"].split("\n", 1)[1])
        assert len(context["tasks"]) == 1
        assert context["tasks"][0]["task"]["status"] == "queued"
        await consume_coordination(supervisor)
        assert len(model.requests) == 1
    finally:
        await supervisor.stop()
        await db.close()


class BrokenToolStreamModel(ObservingModel):
    async def handle(self, request: httpx.Request) -> httpx.Response:
        if self.requests:
            return await super().handle(request)
        self.requests.append(json.loads(request.content))

        class Broken(httpx.AsyncByteStream):
            async def __aiter__(self) -> AsyncIterator[bytes]:
                event = chat(
                    {
                        "tool_calls": [
                            {
                                "index": 0,
                                "id": "call-broken",
                                "function": {
                                    "name": "change_state",
                                    "arguments": '{"value":7}',
                                },
                            }
                        ]
                    }
                )
                yield ("data: " + json.dumps(event) + "\n\n").encode()
                raise httpx.ReadError("stream disconnected", request=request)

        return httpx.Response(200, headers={"content-type": "text/event-stream"}, stream=Broken())


async def test_partial_stream_tool_is_never_executed_during_recovery(tmp_path: Path) -> None:
    db = Database(f"sqlite+aiosqlite:///{tmp_path / 'broken-stream.db'}")
    settings = manager("yolo")
    settings.current.model.retries = 0
    service = OperationService(db, settings)
    effects: list[int] = []
    register_change(service, effects)
    model = BrokenToolStreamModel()
    supervisor = Supervisor(db, service, settings, provider_factory=model.provider)
    await db.initialize()
    try:
        conversation = await supervisor.create_conversation(title="流中断")
        first = await supervisor.send_message(conversation["id"], "检查结果")
        await wait_status(supervisor, first["run_id"], "failed")
        assert effects == []
        event = await due_recovery(db)
        await consume_coordination(supervisor)
        await wait_event(db, event.id)
        assert effects == []
        assert len(model.requests) == 2
        assert not any(message.get("tool_calls") for message in model.requests[1]["messages"])
    finally:
        await supervisor.stop()
        await db.close()


async def test_interrupted_reconciliation_preserves_recovery_budget(tmp_path: Path) -> None:
    db = Database(f"sqlite+aiosqlite:///{tmp_path / 'interrupted.db'}")
    settings = manager("yolo")
    settings.current.model.retries = 0
    settings.current.supervisor.model_recovery_attempts = 2
    service = OperationService(db, settings)
    model = UnavailableModel()
    supervisor = Supervisor(db, service, settings, provider_factory=model.provider)
    await db.initialize()
    try:
        conversation = await supervisor.create_conversation(title="中断恢复预算")
        first = await supervisor.send_message(conversation["id"], "核查当前任务")
        await wait_status(supervisor, first["run_id"], "failed")
        await due_recovery(db)
        model.gate = asyncio.Event()
        model.observed.clear()
        await consume_coordination(supervisor)
        await asyncio.wait_for(model.observed.wait(), 5)
        await supervisor.stop()
        model.gate.set()
        service = OperationService(db, settings)
        supervisor = Supervisor(db, service, settings, provider_factory=model.provider)
        await recover_coordination(supervisor)
        async with db.sessions() as session:
            restarted = (
                await session.scalars(
                    select(CoordinationEvent).where(
                        CoordinationEvent.kind == "recovery", CoordinationEvent.status == "pending"
                    )
                )
            ).one()
        await consume_coordination(supervisor)
        await wait_event(db, restarted.id, "failed")
        next_attempt = await due_recovery(db)
        assert next_attempt.payload["attempt"] == 2
        await consume_coordination(supervisor)
        await wait_event(db, next_attempt.id, "failed")
        async with db.sessions() as session:
            assert (
                await session.scalar(
                    select(func.count())
                    .select_from(CoordinationEvent)
                    .where(
                        CoordinationEvent.kind == "model_recovery",
                        CoordinationEvent.status == "pending",
                    )
                )
                == 0
            )
    finally:
        if model.gate is not None:
            model.gate.set()
        await supervisor.stop()
        await db.close()


async def test_stop_revokes_recovery_when_failure_commits_after_status_read(tmp_path: Path) -> None:
    db = Database(f"sqlite+aiosqlite:///{tmp_path / 'stop-failure-race.db'}")
    settings = manager("yolo")
    settings.current.model.retries = 0
    service = OperationService(db, settings)
    model = UnavailableModel()
    model.gate = asyncio.Event()
    supervisor = Supervisor(db, service, settings, provider_factory=model.provider)
    await db.initialize()
    stop_read = asyncio.Event()
    return_status = asyncio.Event()
    original_get_run = supervisor.get_run
    stop_task: asyncio.Task[dict[str, Any]] | None = None
    intercepted = False

    async def delayed_get_run(run_id: str) -> dict[str, Any]:
        nonlocal intercepted
        result = await original_get_run(run_id)
        if not intercepted:
            intercepted = True
            stop_read.set()
            await return_status.wait()
        return result

    try:
        conversation = await supervisor.create_conversation(title="停止与失败竞争")
        first = await supervisor.send_message(conversation["id"], "核查当前任务")
        await asyncio.wait_for(model.observed.wait(), 5)
        execution = supervisor.tasks[first["run_id"]]
        supervisor.get_run = delayed_get_run  # type: ignore[method-assign]
        stop_task = asyncio.create_task(supervisor.cancel_run(first["run_id"]))
        await asyncio.wait_for(stop_read.wait(), 5)
        model.gate.set()
        await asyncio.wait_for(execution, 5)
        return_status.set()
        stopped = await asyncio.wait_for(stop_task, 5)
        assert stopped["status"] == "cancelled"
        await consume_coordination(supervisor)
        assert len(model.requests) == 1
        async with db.sessions() as session:
            event = (await session.scalars(select(CoordinationEvent))).one()
            assert event.status == "cancelled"
    finally:
        model.gate.set()
        return_status.set()
        if stop_task is not None:
            await asyncio.gather(stop_task, return_exceptions=True)
        await supervisor.stop()
        await db.close()


async def test_disabling_recovery_also_blocks_an_already_prepared_batch(tmp_path: Path) -> None:
    db = Database(f"sqlite+aiosqlite:///{tmp_path / 'disable-pending.db'}")
    settings = manager("yolo")
    settings.current.model.retries = 0
    service = OperationService(db, settings)
    model = UnavailableModel()
    supervisor = Supervisor(db, service, settings, provider_factory=model.provider)
    await db.initialize()
    try:
        conversation = await supervisor.create_conversation(title="恢复热重载")
        first = await supervisor.send_message(conversation["id"], "核查当前任务")
        await wait_status(supervisor, first["run_id"], "failed")
        event = await due_recovery(db)
        settings.current.supervisor.model_recovery_attempts = 0
        await consume_coordination(supervisor)
        assert len(model.requests) == 1
        with pytest.raises(ValueError, match="disabled"):
            await supervisor.send_message(
                conversation["id"],
                "已经准备的恢复请求",
                source="coordination",
                coordination_id=event.id,
            )
        settings.current.supervisor.model_recovery_attempts = 3
        await consume_coordination(supervisor)
        await wait_event(db, event.id, "failed")
        assert len(model.requests) == 2
    finally:
        await supervisor.stop()
        await db.close()
