"""Native process questions are durable, task-scoped and resolved through one boundary."""

from __future__ import annotations

import time
from typing import TYPE_CHECKING, Any, Literal

from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import JSON, Float, ForeignKey, String, Text, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Mapped, mapped_column

from chiliarch.database import Base, CoordinationEvent, Task
from chiliarch.protocol import Envelope

if TYPE_CHECKING:
    from chiliarch.service import OperationService

JsonObject = dict[str, Any]
ACTIVE_TASK_STATES = frozenset({"accepted", "running"})


class NativeInteraction(Base):
    __tablename__ = "native_interactions"
    id: Mapped[str] = mapped_column(String(128), primary_key=True)
    agent_id: Mapped[str] = mapped_column(ForeignKey("agents.id"), index=True)
    task_id: Mapped[str] = mapped_column(ForeignKey("tasks.id"), index=True)
    kind: Mapped[str] = mapped_column(String(32))
    title: Mapped[str] = mapped_column(Text)
    payload: Mapped[JsonObject] = mapped_column(JSON)
    status: Mapped[str] = mapped_column(String(32), default="pending")
    answer: Mapped[JsonObject | None] = mapped_column(JSON, nullable=True)
    answered_by: Mapped[str | None] = mapped_column(String(256), nullable=True)
    created_at: Mapped[float] = mapped_column(Float, default=time.time)
    expires_at: Mapped[float] = mapped_column(Float)
    answered_at: Mapped[float | None] = mapped_column(Float, nullable=True)


class Arguments(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ListInteractions(Arguments):
    task_id: str | None = Field(
        default=None,
        description="Filter by a platform task ID; agents may query only their own task scope.",
    )
    status: Literal["pending", "answered", "expired"] | None = Field(
        default="pending",
        description="Return pending questions by default, a chosen final state, or null for all states.",
    )


class ResolveInteraction(Arguments):
    interaction_id: str = Field(
        description="Pending native interaction ID returned by list_native_interactions."
    )
    native_answer: JsonObject = Field(
        description="Permission: {choice: option.id}. Input: {fields: {field.id: string or list[str]}}. Match the advertised options and fields exactly; no permission escalation or invented choices."
    )


SPECS: list[tuple[str, str, type[BaseModel], str, bool]] = [
    (
        "list_native_interactions",
        "Read pending native agent questions/permissions and their original details. Permission requests require the logged-in human; native privileges cannot be granted by the supervisor or another agent. Questions expire if the originating task stops. Agent callers see only their own tasks.",
        ListInteractions,
        "low",
        False,
    ),
    (
        "resolve_native_interaction",
        "Answer a pending native question using its advertised fields or choices. Native permission requests are human-only even in yolo mode; the supervisor may answer information questions under normal platform approval rules. Expired/stopped tasks cannot resume, and answers are immutable after resolution.",
        ResolveInteraction,
        "medium",
        True,
    ),
]
OPERATIONS = frozenset(spec[0] for spec in SPECS)


async def validate_scope(
    session: AsyncSession, name: str, args: JsonObject, agent_id: str, task_id: str | None = None
) -> None:
    from chiliarch.service import OperationError

    if name != "list_native_interactions":
        raise OperationError("Agents cannot answer native interaction requests", 403)
    if task_id:
        if args.get("task_id") not in {None, task_id}:
            raise OperationError("Native interaction belongs to another task", 403)
        args["task_id"] = task_id
    if args.get("task_id"):
        task = await session.get(Task, args["task_id"])
        if task is None or task.agent_id != agent_id or (task_id and task.id != task_id):
            raise OperationError("Native interaction task belongs to another agent", 403)


def view(row: NativeInteraction, *, include_answer: bool = True) -> JsonObject:
    value = {
        **row.payload,
        "id": row.id,
        "agent_id": row.agent_id,
        "status": row.status,
        "created_at": row.created_at,
        "expires_at": row.expires_at,
        "answered_at": row.answered_at,
        "answered_by": row.answered_by,
    }
    if include_answer:
        value["answer"] = row.answer
    return value


async def receive(
    service: OperationService, session: AsyncSession, agent_id: str, envelope: Envelope
) -> JsonObject:
    from chiliarch.service import OperationError

    payload = envelope.payload
    task = await session.get(Task, payload["task_id"])
    if task is None or task.agent_id != agent_id:
        raise OperationError("Native interaction belongs to another task", 403)
    inactive = task.status in {"completed", "failed", "cancelled", "interrupted", "expired"}
    if task.status not in ACTIVE_TASK_STATES and not inactive:
        raise OperationError("Native interaction requires an active task", 409)
    existing = await session.get(NativeInteraction, payload["interaction_id"])
    if existing:
        if existing.agent_id != agent_id or existing.payload != payload:
            raise OperationError("Native interaction identity has conflicting content", 409)
        return {"interaction": view(existing)}
    options, fields = payload["options"], payload["fields"]
    if (payload["kind"] == "permission" and not options) or (
        payload["kind"] == "input" and not fields
    ):
        raise OperationError("Native interaction has no available answers", 422)
    for entries in [options, fields, *(field["options"] for field in fields)]:
        if len({entry["id"] for entry in entries}) != len(entries):
            raise OperationError("Native answer identifiers must be unique", 422)
    now = time.time()
    expiry = (
        envelope.expires_at.timestamp()
        if envelope.expires_at
        else now + service.settings_manager.current.transport.message_ttl_seconds
    )
    row = NativeInteraction(
        id=payload["interaction_id"],
        agent_id=agent_id,
        task_id=task.id,
        kind=payload["kind"],
        title=payload["title"],
        payload=payload,
        status="expired" if inactive else "pending",
        created_at=now,
        expires_at=expiry,
    )
    session.add(row)
    if inactive:
        await service._enqueue(
            session,
            agent_id,
            Envelope(
                type="native.response",
                correlation_id=row.id,
                payload={"interaction_id": row.id, "task_id": task.id, "status": "expired"},
            ),
        )
        return {"interaction": view(row)}
    link = await service.coordination.link(session, task)
    session.add(
        CoordinationEvent(
            event_key=f"native-interaction:{row.id}",
            kind="native_interaction",
            task_id=task.id,
            goal_id=link.goal_id if link else None,
            conversation_id=link.conversation_id if link else None,
            payload={
                "interaction_id": row.id,
                "task_id": task.id,
                "kind": row.kind,
                "title": row.title,
                "instruction": "原生权限须由用户处理；信息问题可按平台模式回答。不得通过重启任务绕过此请求。",
            },
        )
    )
    return {"interaction": view(row)}


def validate_answer(row: NativeInteraction, answer: JsonObject) -> None:
    from chiliarch.service import OperationError

    if row.kind == "permission":
        if set(answer) != {"choice"} or answer["choice"] not in [
            entry["id"] for entry in row.payload["options"]
        ]:
            raise OperationError("Select one of the native permission choices", 422)
        return
    if set(answer) != {"fields"} or not isinstance(answer["fields"], dict):
        raise OperationError("Native input requires a fields object", 422)
    values = answer["fields"]
    if set(values) != {field["id"] for field in row.payload["fields"]}:
        raise OperationError("Answer exactly the advertised native fields", 422)
    for field in row.payload["fields"]:
        value = values[field["id"]]
        items = value if isinstance(value, list) else [value]
        if not items or any(not isinstance(item, str) or not item.strip() for item in items):
            raise OperationError("Native field answers must contain text", 422)
        if not field["multiple"] and len(items) != 1:
            raise OperationError("Native field allows only one answer", 422)
        choices = {option["id"] for option in field["options"]}
        if choices and any(item not in choices for item in items):
            raise OperationError("Native field answer is not an advertised option", 422)


async def execute(
    service: OperationService, session: AsyncSession, name: str, args: JsonObject, actor: str
) -> JsonObject:
    from chiliarch.service import OperationError

    if name == "list_native_interactions":
        query = select(NativeInteraction).order_by(NativeInteraction.created_at)
        if args["task_id"]:
            query = query.where(NativeInteraction.task_id == args["task_id"])
        if args["status"]:
            query = query.where(NativeInteraction.status == args["status"])
        if actor.startswith("agent:"):
            query = query.where(NativeInteraction.agent_id == actor.removeprefix("agent:"))
        return {
            "interactions": [
                view(row, include_answer=actor == "supervisor")
                for row in (await session.scalars(query)).all()
            ]
        }
    row = await session.get(NativeInteraction, args["interaction_id"])
    if row is None:
        raise OperationError("Native interaction not found", 404)
    if actor.startswith("agent:") or (row.kind == "permission" and actor != "user"):
        raise OperationError("Only the logged-in user may grant native permissions", 403)
    if row.status == "answered":
        if row.answer != args["native_answer"]:
            raise OperationError("Native answer is already final", 409)
        return {"interaction": view(row, include_answer=actor == "supervisor")}
    task = await session.get(Task, row.task_id)
    if (
        row.status != "pending"
        or row.expires_at <= time.time()
        or task is None
        or task.status not in ACTIVE_TASK_STATES
    ):
        raise OperationError("Native interaction expired or its task stopped", 409)
    await service._agent(session, row.agent_id)
    validate_answer(row, args["native_answer"])
    row.answer, row.status, row.answered_by, row.answered_at = (
        args["native_answer"],
        "answered",
        actor,
        time.time(),
    )
    await service._enqueue(
        session,
        row.agent_id,
        Envelope(
            type="native.response",
            correlation_id=row.id,
            payload={
                "interaction_id": row.id,
                "task_id": row.task_id,
                "status": "answered",
                "answer": row.answer,
            },
        ),
    )
    return {"interaction": view(row, include_answer=actor == "supervisor")}


async def expire(service: OperationService, session: AsyncSession) -> None:
    rows = (
        await session.scalars(
            select(NativeInteraction).where(NativeInteraction.status == "pending")
        )
    ).all()
    for row in rows:
        task = await session.get(Task, row.task_id)
        if row.expires_at > time.time() and task and task.status in ACTIVE_TASK_STATES:
            continue
        row.status = "expired"
        if task and task.status in ACTIVE_TASK_STATES:
            # A request can expire before its still-running task. Release the
            # native waiter without granting permission or synthesizing input.
            await service._enqueue(
                session,
                row.agent_id,
                Envelope(
                    type="native.response",
                    correlation_id=row.id,
                    payload={
                        "interaction_id": row.id,
                        "task_id": row.task_id,
                        "status": "expired",
                    },
                ),
            )
