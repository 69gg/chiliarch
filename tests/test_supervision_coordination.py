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

from chiliarch.ai import Provider
from chiliarch.chat import AgentInbox, ChatRun, Conversation, Supervisor
from chiliarch.database import CoordinationEvent, Database, Goal, Task
from chiliarch.protocol import Envelope
from chiliarch.service import OperationService

JsonObject = dict[str, Any]


class ObservingModel:
    def __init__(self) -> None:
        self.requests: list[JsonObject] = []
        self.gate: asyncio.Event | None = None
        self.observed = asyncio.Event()

    async def handle(self, request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        if body.get("tools"):
            self.requests.append(body)
            self.observed.set()
            if self.gate is not None:
                await self.gate.wait()
        return httpx.Response(
            200,
            headers={"content-type": "text/event-stream"},
            stream=SSE([chat({"content": "已核对当前任务，等待验收证据。"}, "stop")]),
        )

    def provider(self, config: Any) -> Provider:
        return Provider(
            config, http_client=httpx.AsyncClient(transport=httpx.MockTransport(self.handle))
        )


async def wait_event(db: Database, event_id: str, status: str = "done") -> CoordinationEvent:
    async with asyncio.timeout(8):
        while True:
            async with db.sessions() as session:
                event = await session.get(CoordinationEvent, event_id)
                if event is not None and event.status == status:
                    return event
                if event is not None and event.status == "failed" and status != "failed":
                    run = await session.get(ChatRun, event.run_id)
                    raise AssertionError(run.error if run else event.payload)
            await asyncio.sleep(0.01)


async def goal_and_task(
    service: OperationService, conversation_id: str, agent_id: str
) -> tuple[str, str]:
    goal = await service.execute(
        "create_goal",
        {
            "title": "跨 agent 协作",
            "objective": "实现并复核功能",
            "constraints": ["保留用户数据"],
            "conversation_id": conversation_id,
        },
        actor="supervisor",
    )
    task = await service.execute(
        "dispatch_task",
        {"agent_id": agent_id, "instruction": "实现功能", "goal_id": goal["goal"]["id"]},
        actor="supervisor",
    )
    return goal["goal"]["id"], task["task"]["id"]


async def test_terminal_event_wakes_original_conversation_once_after_active_turn(
    tmp_path: Path,
) -> None:
    db = Database(f"sqlite+aiosqlite:///{tmp_path / 'coordination.db'}")
    settings = manager("yolo")
    service = OperationService(db, settings)
    model = ObservingModel()
    model.gate = asyncio.Event()
    supervisor = Supervisor(db, service, settings, provider_factory=model.provider)
    await db.initialize()
    await supervisor.start()
    try:
        conversation = await supervisor.create_conversation()
        agent = await add_agent(service)
        goal_id, task_id = await goal_and_task(service, conversation["id"], agent["agent_id"])
        first = await supervisor.send_message(conversation["id"], "请记住：不要修改原始数据")
        await asyncio.wait_for(model.observed.wait(), 5)
        terminal = Envelope(
            type="task.status",
            payload={
                "task_id": task_id,
                "status": "completed",
                "result": {"commit": "abc", "tests": "passed"},
            },
        )
        await service.receive(agent["agent_id"], terminal)
        await service.receive(agent["agent_id"], terminal)
        async with db.sessions() as session:
            event = (await session.scalars(select(CoordinationEvent))).one()
            assert event.status == "pending"
        assert len(model.requests) == 1
        model.gate.set()
        await wait_status(supervisor, first["run_id"], "completed")
        completed = await wait_event(db, event.id)
        assert completed.conversation_id == conversation["id"]
        assert len(model.requests) == 2
        visible = await supervisor.get_conversation(conversation["id"])
        notice = next(
            message for message in visible["messages"] if message["source"] == "coordination"
        )
        assert notice["content"] == "任务「实现功能」执行结束，等待核对验收。"
        history = model.requests[-1]["messages"]
        assert any(message.get("content") == "请记住：不要修改原始数据" for message in history)
        context = json.loads(history[-1]["content"].split("\n", 1)[1])
        assert context["goal"]["goal"]["id"] == goal_id
        assert context["goal"]["goal"]["constraints"] == ["保留用户数据"]
        assert context["goal"]["goal"]["tasks"][0]["result"]["commit"] == "abc"
        async with db.sessions() as session:
            assert await session.scalar(select(func.count()).select_from(Conversation)) == 1
            assert await session.scalar(select(func.count()).select_from(CoordinationEvent)) == 1
    finally:
        model.gate.set()
        await supervisor.stop()
        await db.close()


@pytest.mark.parametrize("kind", ["agent.contact", "agent.request"])
async def test_owned_task_letters_rejoin_goal_and_spoofed_reference_does_not(
    tmp_path: Path, kind: str
) -> None:
    db = Database(f"sqlite+aiosqlite:///{tmp_path / 'letters.db'}")
    settings = manager("yolo")
    service = OperationService(db, settings)
    supervisor = Supervisor(db, service, settings)
    await db.initialize()
    try:
        conversation = await supervisor.create_conversation()
        owner = await add_agent(service, "owner")
        other = await add_agent(service, "other")
        _, task_id = await goal_and_task(service, conversation["id"], owner["agent_id"])
        payload: JsonObject = {"message": "需要协作", "metadata": {"task_id": task_id}}
        if kind == "agent.request":
            payload.update(request_id="request-1", operation="list_agents", arguments={})
        message = Envelope(type=kind, payload=payload)
        await supervisor.receive_agent_request(owner["agent_id"], message.model_dump(mode="json"))
        await supervisor.receive_agent_request(other["agent_id"], message.model_dump(mode="json"))
        async with db.sessions() as session:
            letters = (await session.scalars(select(AgentInbox))).all()
            assert len(letters) == 2
            by_agent = {letter.agent_id: letter for letter in letters}
            assert by_agent[owner["agent_id"]].conversation_id == conversation["id"]
            assert by_agent[other["agent_id"]].conversation_id != conversation["id"]
    finally:
        await db.close()


async def test_restart_reconciles_interrupted_goal_without_redispatching_task(
    tmp_path: Path,
) -> None:
    db = Database(f"sqlite+aiosqlite:///{tmp_path / 'recovery.db'}")
    settings = manager("yolo")
    service = OperationService(db, settings)
    model = ObservingModel()
    original = Supervisor(db, service, settings, provider_factory=model.provider)
    await db.initialize()
    conversation = await original.create_conversation()
    agent = await add_agent(service)
    goal_id, task_id = await goal_and_task(service, conversation["id"], agent["agent_id"])
    model.gate = asyncio.Event()
    await original.start()
    response = await original.send_message(conversation["id"], "继续协调")
    await asyncio.wait_for(model.observed.wait(), 5)
    await original.stop()
    await wait_status(original, response["run_id"], "interrupted")
    model.gate = None
    restarted = Supervisor(
        db, OperationService(db, settings), settings, provider_factory=model.provider
    )
    await restarted.start()
    try:
        async with db.sessions() as session:
            recovery = (await session.scalars(select(CoordinationEvent))).one()
        assert recovery.kind == "recovery" and recovery.goal_id == goal_id
        await wait_event(db, recovery.id)
        async with db.sessions() as session:
            assert await session.scalar(select(func.count()).select_from(Task)) == 1
            task = await session.get(Task, task_id)
            assert task is not None and task.status == "queued"
        # Another restart does not re-run an already reconciled interruption.
        await restarted.stop()
        again = Supervisor(
            db, OperationService(db, settings), settings, provider_factory=model.provider
        )
        await again.start()
        await again.stop()
        async with db.sessions() as session:
            assert await session.scalar(select(func.count()).select_from(CoordinationEvent)) == 1
    finally:
        await restarted.stop()
        await db.close()


async def test_active_goal_protects_its_conversation_history(tmp_path: Path) -> None:
    db = Database(f"sqlite+aiosqlite:///{tmp_path / 'deletion.db'}")
    settings = manager("yolo")
    service = OperationService(db, settings)
    supervisor = Supervisor(db, service, settings)
    await db.initialize()
    try:
        conversation = await supervisor.create_conversation()
        goal = await service.execute(
            "create_goal",
            {
                "title": "未完成目标",
                "objective": "保留上下文",
                "conversation_id": conversation["id"],
            },
            actor="supervisor",
        )
        with pytest.raises(ValueError, match="associated goal"):
            await supervisor._tool_delete({"conversation_id": conversation["id"]})
        await service.execute(
            "update_goal",
            {"goal_id": goal["goal"]["id"], "status": "cancelled"},
            actor="supervisor",
        )
        await supervisor._tool_delete({"conversation_id": conversation["id"]})
        async with db.sessions() as session:
            stored = await session.get(Goal, goal["goal"]["id"])
            assert stored is not None and stored.conversation_id is None
    finally:
        await db.close()


async def test_goal_tool_binding_survives_user_approval(tmp_path: Path) -> None:
    db = Database(f"sqlite+aiosqlite:///{tmp_path / 'approval.db'}")
    settings = manager("ask")
    service = OperationService(db, settings)
    model = Upstream("create_goal", {"title": "共同目标", "objective": "实现与验收"})
    supervisor = Supervisor(db, service, settings, provider_factory=model.provider)
    await db.initialize()
    await supervisor.start()
    try:
        conversation = await supervisor.create_conversation()
        response = await supervisor.send_message(conversation["id"], "请安排实现和测试")
        waiting = await wait_status(supervisor, response["run_id"], "awaiting_approval")
        async with db.sessions() as session:
            assert await session.scalar(select(func.count()).select_from(Goal)) == 0
        await service.execute("approve_operation", {"approval_id": waiting["pending_approval_id"]})
        await wait_status(supervisor, response["run_id"], "completed")
        async with db.sessions() as session:
            goals = (await session.scalars(select(Goal))).all()
            assert len(goals) == 1 and goals[0].conversation_id == conversation["id"]
            assert await session.scalar(select(func.count()).select_from(CoordinationEvent)) == 0
    finally:
        await supervisor.stop()
        await db.close()


async def test_two_agents_dependency_review_retry_and_goal_delivery(tmp_path: Path) -> None:
    """Exercise real operations and protocol messages with a deterministic model transport."""
    db = Database(f"sqlite+aiosqlite:///{tmp_path / 'workflow.db'}")
    settings = manager("yolo")
    service = OperationService(db, settings)
    await db.initialize()
    implementation = await add_agent(service, "implementation")
    reviewer = await add_agent(service, "reviewer")
    calls: list[tuple[str, JsonObject] | None] = []
    index = 0

    async def handle(request: httpx.Request) -> httpx.Response:
        nonlocal index
        body = json.loads(request.content)
        assert body.get("tools")
        call = calls[index]
        index += 1
        event = (
            chat(
                {
                    "tool_calls": [
                        {
                            "index": 0,
                            "id": f"call-{index}",
                            "function": {"name": call[0], "arguments": json.dumps(call[1])},
                        }
                    ]
                },
                "tool_calls",
            )
            if call
            else chat({"content": "已核对，继续等待或交付。"}, "stop")
        )
        return httpx.Response(
            200, headers={"content-type": "text/event-stream"}, stream=SSE([event])
        )

    def provider(config: Any) -> Provider:
        return Provider(
            config, http_client=httpx.AsyncClient(transport=httpx.MockTransport(handle))
        )

    supervisor = Supervisor(db, service, settings, provider_factory=provider)
    conversation = await supervisor.create_conversation()
    goal_id, implementation_id = await goal_and_task(
        service, conversation["id"], implementation["agent_id"]
    )
    review = await service.execute(
        "dispatch_task",
        {
            "agent_id": reviewer["agent_id"],
            "instruction": "独立复核实现",
            "goal_id": goal_id,
            "depends_on": [implementation_id],
        },
        actor="supervisor",
    )
    review_id = review["task"]["id"]
    assert review["task"]["status"] == "blocked"
    assert await service.pending(reviewer["agent_id"]) == []
    calls.extend(
        [
            (
                "review_task",
                {
                    "task_id": implementation_id,
                    "decision": "accepted",
                    "note": "实现单元验证通过，提交已固定，继续独立评审",
                    "artifacts": [
                        {
                            "name": "implementation",
                            "uri": "https://example.invalid/commit/abc",
                            "kind": "commit",
                        }
                    ],
                },
            ),
            None,
            (
                "retry_task",
                {
                    "task_id": review_id,
                    "recovery_note": "已核查：评审进程退出前未修改工作区，使用同一提交重新进行只读评审",
                },
            ),
            None,
        ]
    )
    await supervisor.start()
    try:

        async def report(agent_id: str, task_id: str, status: str) -> CoordinationEvent:
            await service.receive(
                agent_id,
                Envelope(
                    type="task.status",
                    payload={
                        "task_id": task_id,
                        "status": status,
                        "result": {"tests": "passed"} if status == "completed" else None,
                    },
                ),
            )
            async with db.sessions() as session:
                event = await session.scalar(
                    select(CoordinationEvent).where(CoordinationEvent.task_id == task_id)
                )
            assert event is not None
            return await wait_event(db, event.id)

        await report(implementation["agent_id"], implementation_id, "completed")
        pending = await service.pending(reviewer["agent_id"])
        assert len(pending) == 1 and pending[0].payload["task_id"] == review_id
        context = pending[0].payload["parameters"]["chiliarch_context"]
        assert context["goal"]["id"] == goal_id
        assert (
            context["dependencies"][0]["artifacts"][0]["uri"]
            == "https://example.invalid/commit/abc"
        )
        await report(reviewer["agent_id"], review_id, "failed")
        state = await service.state()
        retried = next(link for link in state["task_links"] if link["retry_of"] == review_id)
        retried_id = retried["task_id"]
        calls.extend(
            [
                (
                    "review_task",
                    {"task_id": retried_id, "decision": "accepted", "note": "独立复核与验证通过"},
                ),
                (
                    "update_goal",
                    {
                        "goal_id": goal_id,
                        "status": "completed",
                        "summary": "实现与独立复核完成，评审故障已核查后恢复。",
                    },
                ),
                None,
            ]
        )
        await report(reviewer["agent_id"], retried_id, "completed")
        result = await service.execute("get_goal", {"goal_id": goal_id})
        assert result["goal"]["status"] == "completed"
        assert len(result["goal"]["tasks"]) == 3
        assert (
            next(task for task in result["goal"]["tasks"] if task["id"] == review_id)["status"]
            == "failed"
        )
        assert index == len(calls)
        async with db.sessions() as session:
            assert await session.scalar(select(func.count()).select_from(Conversation)) == 1
    finally:
        await supervisor.stop()
        await db.close()


@pytest.mark.parametrize("with_goal", [False, True])
async def test_repeated_restart_reuses_interrupted_reconciliation(
    tmp_path: Path, with_goal: bool
) -> None:
    db = Database(f"sqlite+aiosqlite:///{tmp_path / 'repeated.db'}")
    settings = manager("yolo")
    service = OperationService(db, settings)
    model = ObservingModel()
    original = Supervisor(db, service, settings, provider_factory=model.provider)
    await db.initialize()
    conversation = await original.create_conversation()
    agent = await add_agent(service)
    if with_goal:
        _, task_id = await goal_and_task(service, conversation["id"], agent["agent_id"])
    else:
        task = await service.execute(
            "dispatch_task",
            {
                "agent_id": agent["agent_id"],
                "instruction": "独立任务",
                "conversation_id": conversation["id"],
            },
            actor="supervisor",
        )
        task_id = task["task"]["id"]
    model.gate = asyncio.Event()
    await original.start()
    await service.receive(
        agent["agent_id"],
        Envelope(type="task.status", payload={"task_id": task_id, "status": "completed"}),
    )
    await asyncio.wait_for(model.observed.wait(), 5)
    await original.stop()
    # Interrupt the recovery turn itself, twice. Each restart must reuse that
    # durable reconciliation instead of growing a chain of recovery events.
    for _ in range(2):
        model.observed.clear()
        restarted = Supervisor(
            db, OperationService(db, settings), settings, provider_factory=model.provider
        )
        await restarted.start()
        await asyncio.wait_for(model.observed.wait(), 5)
        await restarted.stop()
    async with db.sessions() as session:
        assert await session.scalar(select(func.count()).select_from(CoordinationEvent)) == 2
        assert await session.scalar(select(func.count()).select_from(Task)) == 1
        recovery = await session.scalar(
            select(CoordinationEvent).where(CoordinationEvent.kind == "recovery")
        )
        assert recovery is not None
    model.gate = None
    final = Supervisor(
        db, OperationService(db, settings), settings, provider_factory=model.provider
    )
    await final.start()
    try:
        await wait_event(db, recovery.id)
        await final.stop()
        completed_restart = Supervisor(
            db, OperationService(db, settings), settings, provider_factory=model.provider
        )
        await completed_restart.start()
        await completed_restart.stop()
        async with db.sessions() as session:
            assert await session.scalar(select(func.count()).select_from(CoordinationEvent)) == 2
    finally:
        await final.stop()
        await db.close()
