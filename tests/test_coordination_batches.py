from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

import httpx
import pytest
from sqlalchemy import func, select
from test_ai_providers import SSE, chat
from test_chat_supervisor import Upstream, manager, wait_status
from test_core_service import add_agent
from test_supervision_coordination import ObservingModel, goal_and_task, wait_event

from chiliarch.chat import AgentInbox, ChatMessage, ChatRun, Supervisor
from chiliarch.database import CoordinationEvent, Database
from chiliarch.protocol import Envelope
from chiliarch.service import OperationService
from chiliarch.supervision import consume_coordination, recover_coordination


async def complete_task(service: OperationService, agent_id: str, task_id: str) -> None:
    await service.receive(
        agent_id, Envelope(type="task.status", payload={"task_id": task_id, "status": "completed"})
    )


async def test_pending_conversation_events_share_one_atomic_run(tmp_path: Path) -> None:
    db = Database(f"sqlite+aiosqlite:///{tmp_path / 'batch.db'}")
    settings = manager("yolo")
    service = OperationService(db, settings)
    model = ObservingModel()
    model.gate = asyncio.Event()
    supervisor = Supervisor(db, service, settings, provider_factory=model.provider)
    await db.initialize()
    try:
        conversation = await supervisor.create_conversation()
        agent = await add_agent(service)
        goals = [
            await goal_and_task(service, conversation["id"], agent["agent_id"]) for _ in range(2)
        ]
        task_ids = [task_id for _, task_id in goals]
        for _ in range(3):
            task = await service.execute(
                "dispatch_task",
                {
                    "agent_id": agent["agent_id"],
                    "instruction": "并行核查",
                    "goal_id": goals[0][0],
                },
                actor="supervisor",
            )
            task_ids.append(task["task"]["id"])
        for task_id in task_ids:
            await complete_task(service, agent["agent_id"], task_id)
        await asyncio.gather(consume_coordination(supervisor), consume_coordination(supervisor))
        await asyncio.wait_for(model.observed.wait(), 5)
        async with db.sessions() as session:
            events = (await session.scalars(select(CoordinationEvent))).all()
            assert len(events) == 5
            assert {event.status for event in events} == {"started"}
            assert len({event.run_id for event in events}) == 1
            assert await session.scalar(select(func.count()).select_from(ChatRun)) == 1
        assert len(model.requests) == 1
        context = json.loads(model.requests[0]["messages"][-1]["content"].split("\n", 1)[1])
        assert len(context["events"]) == 5
        assert {goal["goal"]["id"] for goal in context["goals"]} == {goal for goal, _ in goals}
        assert len(context["goals"]) == 2
        model.gate.set()
        for event in events:
            await wait_event(db, event.id)
        await consume_coordination(supervisor)
        assert len(model.requests) == 1
        view = await supervisor.get_conversation(conversation["id"])
        notices = [message for message in view["messages"] if message["source"] == "coordination"]
        assert len(notices) == 1
    finally:
        model.gate.set()
        await supervisor.stop()
        await db.close()


async def test_changed_event_rolls_back_entire_batch_claim(tmp_path: Path) -> None:
    db = Database(f"sqlite+aiosqlite:///{tmp_path / 'atomic.db'}")
    settings = manager("yolo")
    service = OperationService(db, settings)
    supervisor = Supervisor(db, service, settings)
    await db.initialize()
    try:
        conversation = await supervisor.create_conversation()
        events = [
            CoordinationEvent(
                event_key=f"test:{index}", kind="goal_updated", conversation_id=conversation["id"]
            )
            for index in range(2)
        ]
        async with db.sessions.begin() as session:
            session.add_all(events)
        async with db.sessions.begin() as session:
            changed = await session.get(CoordinationEvent, events[1].id)
            assert changed is not None
            changed.status = "done"
        with pytest.raises(ValueError, match="batch"):
            await supervisor.send_message(
                conversation["id"],
                "stale batch",
                source="coordination",
                coordination_ids=tuple(event.id for event in events),
            )
        async with db.sessions() as session:
            untouched = await session.get(CoordinationEvent, events[0].id)
            assert (
                untouched is not None and untouched.status == "pending" and untouched.run_id is None
            )
            assert await session.scalar(select(func.count()).select_from(ChatRun)) == 0
            assert await session.scalar(select(func.count()).select_from(ChatMessage)) == 0
    finally:
        await supervisor.stop()
        await db.close()


class FailingModel(ObservingModel):
    def __init__(self) -> None:
        super().__init__()
        self.fail = True

    async def handle(self, request: httpx.Request) -> httpx.Response:
        body: dict[str, Any] = json.loads(request.content)
        self.requests.append(body)
        self.observed.set()
        if self.gate is not None:
            await self.gate.wait()
        if self.fail:
            return httpx.Response(400, json={"error": {"message": "test upstream failure"}})
        return httpx.Response(
            200,
            headers={"content-type": "text/event-stream"},
            stream=SSE([chat({"content": "已核对。"}, "stop")]),
        )


@pytest.mark.parametrize("outcome", ["cancelled", "failed"])
@pytest.mark.parametrize("resume", ["user_message", "goal_action"])
async def test_stop_or_failure_keeps_new_events_paused_until_explicit_resume(
    tmp_path: Path, outcome: str, resume: str
) -> None:
    db = Database(f"sqlite+aiosqlite:///{tmp_path / 'paused.db'}")
    settings = manager("yolo")
    service = OperationService(db, settings)
    model = FailingModel()
    model.gate = asyncio.Event()
    supervisor = Supervisor(db, service, settings, provider_factory=model.provider)
    await db.initialize()
    try:
        conversation = await supervisor.create_conversation(title="显式恢复")
        agent = await add_agent(service)
        goal_id, first_id = await goal_and_task(service, conversation["id"], agent["agent_id"])
        _, second_id = await goal_and_task(service, conversation["id"], agent["agent_id"])
        await complete_task(service, agent["agent_id"], first_id)
        await consume_coordination(supervisor)
        await asyncio.wait_for(model.observed.wait(), 5)
        async with db.sessions() as session:
            first = (await session.scalars(select(CoordinationEvent))).one()
            assert first.run_id
        await complete_task(service, agent["agent_id"], second_id)
        # This request precedes the stop/failure and cannot immediately undo it.
        await service.execute("update_goal", {"goal_id": goal_id, "status": "active"})
        if outcome == "cancelled":
            await supervisor.cancel_run(first.run_id)
        else:
            model.gate.set()
            await wait_status(supervisor, first.run_id, "failed")
        model.fail = False
        model.gate.set()
        await service.execute("update_goal", {"goal_id": goal_id, "title": "改名不代表恢复"})
        await consume_coordination(supervisor)
        assert len(model.requests) == 1
        # A linked agent letter must not bypass the stopped conversation either.
        envelope = Envelope(
            type="agent.contact",
            payload={"message": "任务结果已提交", "metadata": {"task_id": second_id}},
        )
        await supervisor.receive_agent_request(agent["agent_id"], envelope.model_dump(mode="json"))
        async with db.sessions() as session:
            inbox = (await session.scalars(select(AgentInbox))).one()
        with pytest.raises(ValueError, match="paused"):
            await supervisor.send_message(
                conversation["id"], "下级来信", source="agent", inbox_id=inbox.id
            )
        if resume == "user_message":
            response = await supervisor.send_message(conversation["id"], "继续核对任务")
            await wait_status(supervisor, response["run_id"], "completed")
        else:
            await service.execute("update_goal", {"goal_id": goal_id, "status": "active"})
        await consume_coordination(supervisor)
        async with db.sessions() as session:
            events = (await session.scalars(select(CoordinationEvent))).all()
        for event in events:
            await wait_event(db, event.id, "failed" if event.id == first.id else "done")
        assert len(model.requests) == (3 if resume == "user_message" else 2)
    finally:
        model.gate.set()
        await supervisor.stop()
        await db.close()


async def test_later_user_cancellation_prevents_old_restart_recovery(tmp_path: Path) -> None:
    db = Database(f"sqlite+aiosqlite:///{tmp_path / 'superseded.db'}")
    settings = manager("yolo")
    service = OperationService(db, settings)
    model = ObservingModel()
    model.gate = asyncio.Event()
    original = Supervisor(db, service, settings, provider_factory=model.provider)
    await db.initialize()
    conversation = await original.create_conversation()
    agent = await add_agent(service)
    _, task_id = await goal_and_task(service, conversation["id"], agent["agent_id"])
    await complete_task(service, agent["agent_id"], task_id)
    await consume_coordination(original)
    await asyncio.wait_for(model.observed.wait(), 5)
    await original.stop()
    restarted = Supervisor(
        db, OperationService(db, settings), settings, provider_factory=model.provider
    )
    try:
        model.observed.clear()
        response = await restarted.send_message(conversation["id"], "先暂停")
        await asyncio.wait_for(model.observed.wait(), 5)
        await restarted.cancel_run(response["run_id"])
        await recover_coordination(restarted)
        await consume_coordination(restarted)
        async with db.sessions() as session:
            assert await session.scalar(select(func.count()).select_from(CoordinationEvent)) == 1
        assert len(model.requests) == 2
    finally:
        model.gate.set()
        await restarted.stop()
        await db.close()


async def test_batch_waiting_approval_survives_restart_without_claiming_later_events(
    tmp_path: Path,
) -> None:
    db = Database(f"sqlite+aiosqlite:///{tmp_path / 'approval-batch.db'}")
    settings = manager("ask")
    service = OperationService(db, settings)
    model = Upstream("create_pairing_code", {"ttl_seconds": 900})
    supervisor = Supervisor(db, service, settings, provider_factory=model.provider)
    await db.initialize()
    restarted: Supervisor | None = None
    try:
        conversation = await supervisor.create_conversation()
        agent = await add_agent(service)
        # User-origin operations create valid task records without an AI approval.
        goal = await service.execute("create_goal", {"title": "审批", "objective": "等待审批"})
        task_ids = []
        for _ in range(3):
            response = await service.execute(
                "dispatch_task",
                {
                    "agent_id": agent["agent_id"],
                    "instruction": "执行工作",
                    "goal_id": goal["goal"]["id"],
                    "conversation_id": conversation["id"],
                },
            )
            task_ids.append(response["task"]["id"])
        # The unrelated goal-created event gets its own conversation; isolate
        # the two terminal updates whose shared approval lifecycle is under test.
        async with db.sessions.begin() as session:
            initial = (await session.scalars(select(CoordinationEvent))).one()
            initial.status = "done"
        for task_id in task_ids[:2]:
            await complete_task(service, agent["agent_id"], task_id)
        await consume_coordination(supervisor)
        async with db.sessions() as session:
            events = (
                await session.scalars(
                    select(CoordinationEvent).where(CoordinationEvent.status == "started")
                )
            ).all()
        assert len(events) == 2 and events[0].run_id == events[1].run_id
        assert events[0].run_id
        waiting = await wait_status(supervisor, events[0].run_id, "awaiting_approval")
        await complete_task(service, agent["agent_id"], task_ids[2])
        await consume_coordination(supervisor)
        assert len(model.requests) == 1
        await supervisor.stop()
        restored_service = OperationService(db, settings)
        restarted = Supervisor(db, restored_service, settings, provider_factory=model.provider)
        await restarted.start()
        await restored_service.execute(
            "approve_operation", {"approval_id": waiting["pending_approval_id"]}
        )
        for event in events:
            await wait_event(db, event.id)
        async with db.sessions() as session:
            pending = await session.scalar(
                select(CoordinationEvent).where(CoordinationEvent.task_id == task_ids[2])
            )
            assert pending is not None
        await wait_event(db, pending.id)
        assert len(model.requests) == 3
    finally:
        await supervisor.stop()
        if restarted:
            await restarted.stop()
        await db.close()


async def test_interrupted_batch_recovers_once_and_keeps_later_joined_tasks(tmp_path: Path) -> None:
    db = Database(f"sqlite+aiosqlite:///{tmp_path / 'batch-recovery.db'}")
    settings = manager("yolo")
    service = OperationService(db, settings)
    model = ObservingModel()
    model.gate = asyncio.Event()
    original = Supervisor(db, service, settings, provider_factory=model.provider)
    await db.initialize()
    conversation = await original.create_conversation()
    agent = await add_agent(service)
    task_ids: list[str] = []
    for _ in range(4):
        task = await service.execute(
            "dispatch_task",
            {
                "agent_id": agent["agent_id"],
                "instruction": "核查",
                "conversation_id": conversation["id"],
            },
        )
        task_ids.append(task["task"]["id"])
    for task_id in task_ids[:3]:
        await complete_task(service, agent["agent_id"], task_id)
    await consume_coordination(original)
    await asyncio.wait_for(model.observed.wait(), 5)
    await original.stop()
    second = Supervisor(
        db, OperationService(db, settings), settings, provider_factory=model.provider
    )
    await recover_coordination(second)
    # A new terminal event joins the recovery's next run, which is interrupted
    # again. Both the original three references and this fourth must survive.
    await complete_task(service, agent["agent_id"], task_ids[3])
    model.observed.clear()
    await consume_coordination(second)
    await asyncio.wait_for(model.observed.wait(), 5)
    await second.stop()
    final = Supervisor(
        db, OperationService(db, settings), settings, provider_factory=model.provider
    )
    try:
        await recover_coordination(final)
        async with db.sessions() as session:
            events = (await session.scalars(select(CoordinationEvent))).all()
            recoveries = [event for event in events if event.kind == "recovery"]
            assert len(events) == 5 and len(recoveries) == 1
            assert recoveries[0].status == "pending"
        model.gate.set()
        await consume_coordination(final)
        await wait_event(db, recoveries[0].id)
        context = json.loads(model.requests[-1]["messages"][-1]["content"].split("\n", 1)[1])
        assert {task["task"]["id"] for task in context["tasks"]} == set(task_ids)
        assert len(model.requests) == 3
        await recover_coordination(final)
        await consume_coordination(final)
        assert len(model.requests) == 3
    finally:
        model.gate.set()
        await final.stop()
        await db.close()
