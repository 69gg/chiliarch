"""Durable liveness observations; missing progress never proves execution stopped."""

from __future__ import annotations

import time

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from chiliarch.config import Settings
from chiliarch.database import Agent, CoordinationEvent, Task, TaskLink


async def monitor_tasks(
    session: AsyncSession, settings: Settings, *, now: float | None = None
) -> int:
    """Add one notice per progress/connection epoch inside the caller's write transaction."""
    observed_at = time.time() if now is None else now
    tasks = (
        await session.scalars(
            select(Task).where(
                Task.status.in_(
                    ["queued", "delivered", "accepted", "running", "cancellation_requested"]
                )
            )
        )
    ).all()
    created = 0
    for task in tasks:
        agent = await session.get(Agent, task.agent_id)
        if agent is None:
            continue
        observations: list[tuple[str, float]] = []
        if observed_at - task.updated_at >= settings.supervisor.task_stall_seconds:
            observations.append(("task_stalled", task.updated_at))
        last_seen = agent.last_seen if agent.last_seen is not None else agent.created_at
        if observed_at - last_seen >= settings.transport.offline_after_seconds:
            observations.append(("agent_offline", last_seen))
        for kind, epoch in observations:
            key = f"monitor:{kind}:{task.id}:{float(epoch).hex()}"
            if await session.scalar(
                select(CoordinationEvent.id).where(CoordinationEvent.event_key == key)
            ):
                continue
            link = await session.get(TaskLink, task.id)
            session.add(
                CoordinationEvent(
                    event_key=key,
                    kind=kind,
                    goal_id=link.goal_id if link else None,
                    conversation_id=link.conversation_id if link else None,
                    task_id=task.id,
                    payload={
                        "task_id": task.id,
                        "agent_id": task.agent_id,
                        "status": task.status,
                        "last_progress_at": task.updated_at,
                        "last_seen": agent.last_seen,
                        "observed_at": observed_at,
                        "instruction": (
                            "请核查当前任务与 agent 状态。失联或长时间没有更新不表示执行停止；"
                            "不得据此直接重派、认定失败或重复外部操作。"
                        ),
                    },
                )
            )
            created += 1
    return created
