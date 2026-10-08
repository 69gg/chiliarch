"""Durable goals, dependency gates and explicit acceptance, inside operation transactions."""

from __future__ import annotations

import time
from pathlib import PureWindowsPath
from typing import TYPE_CHECKING, Any, Literal
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field, field_validator
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from chiliarch.database import (
    AgentRequest,
    Approval,
    CoordinationEvent,
    Goal,
    Task,
    TaskLink,
    new_id,
)
from chiliarch.protocol import Envelope

if TYPE_CHECKING:
    from chiliarch.service import OperationService

JsonObject = dict[str, Any]
TERMINAL_STATUSES = frozenset({"completed", "failed", "cancelled", "expired", "interrupted"})


class CoordinationArguments(BaseModel):
    model_config = ConfigDict(extra="forbid")


class GoalArguments(CoordinationArguments):
    goal_id: str = Field(min_length=1)


class CreateGoalArguments(CoordinationArguments):
    title: str = Field(min_length=1, max_length=256)
    objective: str = Field(min_length=1, max_length=100000)
    constraints: list[str] = Field(default_factory=list)
    acceptance_criteria: list[str] = Field(default_factory=list)
    conversation_id: str | None = None


class UpdateGoalArguments(GoalArguments):
    title: str | None = Field(default=None, min_length=1, max_length=256)
    constraints: list[str] | None = None
    acceptance_criteria: list[str] | None = None
    summary: str | None = None
    status: Literal["active", "blocked", "completed", "cancelled"] | None = None


class ArtifactReference(CoordinationArguments):
    name: str = Field(min_length=1, max_length=256)
    uri: str = Field(min_length=1, max_length=4096)
    kind: str = Field(default="reference", min_length=1, max_length=64)
    digest: str | None = Field(default=None, max_length=256)

    @field_validator("uri")
    @classmethod
    def no_embedded_credentials(cls, value: str) -> str:
        parsed = urlsplit(value)
        if (
            parsed.scheme.lower() not in {"", "http", "https", "file", "git", "ssh"}
            and not PureWindowsPath(value).is_absolute()
        ):
            raise ValueError("Artifact references must use a file, repository or web URI")
        if parsed.username is not None or parsed.password is not None:
            raise ValueError("Artifact references must not contain embedded credentials")
        return value


class ReviewTaskArguments(CoordinationArguments):
    task_id: str = Field(min_length=1)
    decision: Literal["accepted", "changes_requested"]
    note: str = Field(min_length=1, max_length=100000)
    artifacts: list[ArtifactReference] = Field(default_factory=list)


class RetryTaskArguments(CoordinationArguments):
    task_id: str = Field(min_length=1)
    agent_id: str | None = Field(default=None, min_length=1)
    instruction: str | None = Field(default=None, min_length=1, max_length=100000)
    recovery_note: str = Field(min_length=1, max_length=100000)

    @field_validator("recovery_note")
    @classmethod
    def meaningful_recovery_note(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("Recovery note must explain why retry is safe")
        return value


def goal_view(goal: Goal) -> JsonObject:
    return {
        field: getattr(goal, field)
        for field in (
            "id",
            "title",
            "objective",
            "constraints",
            "acceptance_criteria",
            "status",
            "conversation_id",
            "summary",
            "created_at",
            "updated_at",
        )
    }


def task_link_view(link: TaskLink) -> JsonObject:
    return {
        field: getattr(link, field)
        for field in (
            "task_id",
            "goal_id",
            "conversation_id",
            "depends_on",
            "acceptance_criteria",
            "review_status",
            "review_note",
            "artifacts",
            "retry_of",
        )
    }


class CoordinationService:
    """All methods use the caller's existing authorization and database transaction."""

    def __init__(self, service: OperationService) -> None:
        self.service = service

    async def goal(self, session: AsyncSession, goal_id: str) -> Goal:
        from chiliarch.service import OperationError

        goal = await session.get(Goal, goal_id)
        if goal is None:
            raise OperationError("Goal not found", 404)
        return goal

    async def task(self, session: AsyncSession, task_id: str) -> Task:
        from chiliarch.service import OperationError

        task = await session.get(Task, task_id)
        if task is None:
            raise OperationError("Task not found", 404)
        return task

    async def link(self, session: AsyncSession, task: Task) -> TaskLink:
        link = await session.get(TaskLink, task.id)
        if link is None:
            link = TaskLink(
                task_id=task.id,
                goal_id=None,
                conversation_id=None,
                depends_on=[],
                acceptance_criteria=[],
                review_status="pending",
                review_note="",
                artifacts=[],
                retry_of=None,
            )
            session.add(link)
            await session.flush()
        return link

    async def latest(self, session: AsyncSession, task_id: str) -> tuple[Task, TaskLink]:
        task = await self.task(session, task_id)
        while True:
            next_link = await session.scalar(select(TaskLink).where(TaskLink.retry_of == task.id))
            if next_link is None:
                return task, await self.link(session, task)
            task = await self.task(session, next_link.task_id)

    async def validate_conversation(
        self, session: AsyncSession, conversation_id: str | None
    ) -> None:
        if conversation_id is None:
            return
        from chiliarch.chat import Conversation
        from chiliarch.service import OperationError

        if await session.get(Conversation, conversation_id) is None:
            raise OperationError("Conversation not found", 404)

    async def details(self, session: AsyncSession, goal: Goal) -> JsonObject:
        links = (await session.scalars(select(TaskLink).where(TaskLink.goal_id == goal.id))).all()
        tasks = [self.service.task_view(await self.task(session, link.task_id)) for link in links]
        return {
            **goal_view(goal),
            "tasks": tasks,
            "task_links": [task_link_view(link) for link in links],
            "artifacts": [artifact for link in links for artifact in link.artifacts],
        }

    async def event(
        self,
        session: AsyncSession,
        kind: str,
        *,
        task: Task | None = None,
        goal: Goal | None = None,
        event_key: str | None = None,
        resume_requested: bool = False,
    ) -> None:
        from chiliarch.service import redact

        key = event_key or f"{kind}:{new_id()}"
        existing = await session.scalar(
            select(CoordinationEvent.id).where(CoordinationEvent.event_key == key)
        )
        if existing is not None:
            return
        link = await self.link(session, task) if task is not None else None
        if goal is None and link is not None and link.goal_id is not None:
            goal = await self.goal(session, link.goal_id)
        payload: JsonObject = {}
        if task is not None:
            payload["task"] = self.service.task_view(task)
        if link is not None:
            payload["task_link"] = task_link_view(link)
        if goal is not None:
            payload["goal"] = goal_view(goal)
        if resume_requested:
            payload["resume_requested"] = True
        session.add(
            CoordinationEvent(
                id=new_id(),
                event_key=key,
                kind=kind,
                goal_id=goal.id if goal else None,
                conversation_id=(link.conversation_id if link else None)
                or (goal.conversation_id if goal else None),
                task_id=task.id if task else None,
                payload=redact(payload),
                status="pending",
                created_at=time.time(),
            )
        )

    async def terminal(self, session: AsyncSession, task: Task) -> None:
        await self.event(session, "task_terminal", task=task, event_key=f"task-terminal:{task.id}")

    async def dependencies(
        self, session: AsyncSession, link: TaskLink
    ) -> tuple[bool, list[JsonObject]]:
        ready = True
        summaries: list[JsonObject] = []
        for dependency_id in link.depends_on:
            task, dependency = await self.latest(session, dependency_id)
            ready = ready and task.status == "completed" and dependency.review_status == "accepted"
            summaries.append(
                {
                    "task_id": task.id,
                    "original_task_id": dependency_id,
                    "review_status": dependency.review_status,
                    "review_note": dependency.review_note,
                    "artifacts": dependency.artifacts,
                    "result": task.result,
                }
            )
        return ready, summaries

    async def enqueue(self, session: AsyncSession, task: Task, link: TaskLink) -> bool:
        from chiliarch.service import redact

        ready, dependencies = await self.dependencies(session, link)
        goal = await self.goal(session, link.goal_id) if link.goal_id else None
        if not ready or (goal is not None and goal.status != "active"):
            return False
        context: JsonObject = {
            "goal": goal_view(goal) if goal else None,
            "acceptance_criteria": link.acceptance_criteria,
            "dependencies": dependencies,
            "retry_of": link.retry_of,
        }
        if link.retry_of is not None:
            previous = await self.task(session, link.retry_of)
            previous_link = await self.link(session, previous)
            # Carry the previous result, not its recursively nested dispatch context.
            context["previous_attempt"] = {
                "task": {
                    key: value
                    for key, value in self.service.task_view(previous).items()
                    if key != "parameters"
                },
                "review_status": previous_link.review_status,
                "review_note": previous_link.review_note,
                "artifacts": previous_link.artifacts,
                "retry_of": previous_link.retry_of,
                "recovery_note": task.parameters.get("recovery_note"),
            }
        if goal is not None or link.depends_on or link.acceptance_criteria or link.retry_of:
            task.parameters = {**task.parameters, "chiliarch_context": redact(context)}
        await self.service._enqueue(
            session,
            task.agent_id,
            Envelope(
                type="task.dispatch",
                correlation_id=task.id,
                payload={
                    "task_id": task.id,
                    "instruction": task.instruction,
                    "parameters": task.parameters,
                },
            ),
        )
        task.status, task.updated_at = "queued", time.time()
        return True

    async def release(self, session: AsyncSession) -> list[str]:
        from chiliarch.service import OperationError

        released: list[str] = []
        for task in (await session.scalars(select(Task).where(Task.status == "blocked"))).all():
            link = await self.link(session, task)
            try:
                await self.service._agent(session, task.agent_id)
                if await self.enqueue(session, task, link):
                    released.append(task.id)
            except OperationError as exc:
                # Queue capacity and revoked credentials are observed again by maintenance.
                if exc.status_code not in {404, 409}:
                    raise
        return released

    async def dispatch(
        self,
        session: AsyncSession,
        args: JsonObject,
        actor: str,
        *,
        retry_of: str | None = None,
    ) -> JsonObject:
        from chiliarch.service import OperationError

        await self.service._agent(session, args["agent_id"])
        if "chiliarch_context" in args["parameters"]:
            raise OperationError("chiliarch_context is reserved for platform coordination", 422)
        goal = await self.goal(session, args["goal_id"]) if args.get("goal_id") else None
        if goal and goal.status in {"completed", "cancelled"}:
            raise OperationError("Cannot dispatch into a closed goal", 409)
        conversation_id = args.get("conversation_id") or (goal.conversation_id if goal else None)
        await self.validate_conversation(session, conversation_id)
        if goal is not None and goal.conversation_id and conversation_id != goal.conversation_id:
            raise OperationError("A goal's tasks must use its coordination conversation", 409)
        depends_on = list(dict.fromkeys(args.get("depends_on", [])))
        for dependency_id in depends_on:
            await self.task(session, dependency_id)
        task = Task(
            id=new_id(),
            agent_id=args["agent_id"],
            instruction=args["instruction"],
            parameters=args["parameters"],
            requested_by=actor,
            status="blocked",
            created_at=time.time(),
            updated_at=time.time(),
        )
        session.add(task)
        await session.flush()
        link = TaskLink(
            task_id=task.id,
            goal_id=goal.id if goal else None,
            conversation_id=conversation_id,
            depends_on=depends_on,
            acceptance_criteria=args.get("acceptance_criteria", []),
            review_status="pending",
            review_note="",
            artifacts=[],
            retry_of=retry_of,
        )
        session.add(link)
        await session.flush()
        await self.enqueue(session, task, link)
        return {"task": self.service.task_view(task), "task_link": task_link_view(link)}

    async def execute(
        self, session: AsyncSession, name: str, args: JsonObject, actor: str
    ) -> JsonObject:
        from chiliarch.service import OperationError

        if name == "create_goal":
            await self.validate_conversation(session, args["conversation_id"])
            goal = Goal(
                id=new_id(),
                **args,
                status="active",
                summary="",
                created_at=time.time(),
                updated_at=time.time(),
            )
            session.add(goal)
            await session.flush()
            if actor == "user":
                await self.event(session, "goal_updated", goal=goal)
            return {"goal": await self.details(session, goal)}
        if name == "list_goals":
            return {
                "goals": [
                    await self.details(session, goal)
                    for goal in (
                        await session.scalars(select(Goal).order_by(Goal.created_at.desc()))
                    ).all()
                ]
            }
        if name == "get_goal":
            return {"goal": await self.details(session, await self.goal(session, args["goal_id"]))}
        if name == "update_goal":
            goal = await self.goal(session, args["goal_id"])
            links = (
                await session.scalars(select(TaskLink).where(TaskLink.goal_id == goal.id))
            ).all()
            superseded = {link.retry_of for link in links if link.retry_of}
            current = [link for link in links if link.task_id not in superseded]
            if args.get("status") == "completed":
                await self.ensure_no_pending_work(session, goal.id)
                if not current:
                    raise OperationError("A goal needs an accepted task before completion", 409)
                for link in current:
                    task = await self.task(session, link.task_id)
                    if task.status != "completed" or link.review_status != "accepted":
                        raise OperationError(
                            "Every current task must be completed and accepted before completing the goal",
                            409,
                        )
            if args.get("status") == "cancelled":
                await self.withdraw_pending_work(session, goal.id, actor)
                for link in current:
                    task = await self.task(session, link.task_id)
                    if task.status not in TERMINAL_STATUSES:
                        await self.service._core(
                            session,
                            "cancel_task",
                            {"task_id": task.id, "reason": "Parent goal cancelled"},
                            actor,
                        )
            for field in ("title", "constraints", "acceptance_criteria", "summary", "status"):
                if args.get(field) is not None:
                    setattr(goal, field, args[field])
            goal.updated_at = time.time()
            await session.flush()
            released = await self.release(session) if goal.status == "active" else []
            if actor == "user":
                await self.event(
                    session,
                    "goal_updated",
                    goal=goal,
                    resume_requested=args.get("status") == "active",
                )
            return {"goal": await self.details(session, goal), "released_task_ids": released}
        task, link = await self.latest(session, args["task_id"])
        if task.id != args["task_id"]:
            raise OperationError(f"Task was superseded; use its latest attempt {task.id}", 409)
        if name == "review_task":
            if task.status != "completed":
                raise OperationError("Only an execution-completed task can be reviewed", 409)
            if link.review_status == "accepted" and args["decision"] != "accepted":
                raise OperationError(
                    "Accepted work cannot be withdrawn after releasing dependencies; create a follow-up task",
                    409,
                )
            changed = (link.review_status, link.review_note, link.artifacts) != (
                args["decision"],
                args["note"],
                args["artifacts"],
            )
            link.review_status, link.review_note, link.artifacts = (
                args["decision"],
                args["note"],
                args["artifacts"],
            )
            await session.flush()
            released = await self.release(session) if args["decision"] == "accepted" else []
            if changed and actor == "user":
                await self.event(session, "task_reviewed", task=task)
            return {
                "task": self.service.task_view(task),
                "task_link": task_link_view(link),
                "released_task_ids": released,
            }
        if name == "retry_task":
            if task.status not in TERMINAL_STATUSES or (
                task.status == "completed" and link.review_status != "changes_requested"
            ):
                raise OperationError(
                    "Retry requires a failed/interrupted/cancelled/expired task or completed work needing changes",
                    409,
                )
            parameters = {
                key: value for key, value in task.parameters.items() if key != "chiliarch_context"
            }
            parameters["recovery_note"] = args["recovery_note"]
            result = await self.dispatch(
                session,
                {
                    "agent_id": args.get("agent_id") or task.agent_id,
                    "instruction": args["instruction"] or task.instruction,
                    "parameters": parameters,
                    "goal_id": link.goal_id,
                    "conversation_id": link.conversation_id,
                    "depends_on": link.depends_on,
                    "acceptance_criteria": link.acceptance_criteria,
                },
                actor,
                retry_of=task.id,
            )
            if actor == "user":
                await self.event(
                    session, "task_retried", task=await self.task(session, result["task"]["id"])
                )
            return result
        raise OperationError(f"Unknown coordination operation: {name}", 404)

    async def pending_work(
        self, session: AsyncSession, goal_id: str
    ) -> tuple[list[Approval], list[AgentRequest]]:
        """Find direct dispatch/retry approvals and approvals wrapping agent requests."""

        async def belongs_to_goal(operation: str, arguments: JsonObject) -> bool:
            if operation == "dispatch_task":
                return arguments.get("goal_id") == goal_id
            if operation == "retry_task":
                link = await session.get(TaskLink, arguments.get("task_id"))
                return link is not None and link.goal_id == goal_id
            if operation == "resolve_agent_request" and arguments.get("decision") == "approve":
                request = await session.get(AgentRequest, arguments.get("request_id"))
                if request is not None and request.operation in {"dispatch_task", "retry_task"}:
                    return await belongs_to_goal(request.operation, request.arguments)
            return False

        requests = [
            request
            for request in (
                await session.scalars(select(AgentRequest).where(AgentRequest.status == "pending"))
            ).all()
            if await belongs_to_goal(request.operation, request.arguments)
        ]
        approvals = [
            approval
            for approval in (
                await session.scalars(select(Approval).where(Approval.status == "pending"))
            ).all()
            if await belongs_to_goal(approval.operation, approval.arguments)
        ]
        return approvals, requests

    async def withdraw_pending_work(self, session: AsyncSession, goal_id: str, actor: str) -> None:
        approvals, requests = await self.pending_work(session, goal_id)
        reason = "Parent goal cancelled; outstanding task request withdrawn"
        for approval in approvals:
            # Reuse rejection so the original idempotency record also changes;
            # suspended supervisor runs then resume through their normal sweep.
            await self.service._core(
                session,
                "reject_operation",
                {"approval_id": approval.id, "reason": reason},
                actor,
            )
        for request in requests:
            await self.service._core(
                session,
                "resolve_agent_request",
                {"request_id": request.id, "decision": "reject", "reason": reason},
                actor,
            )

    async def ensure_no_pending_work(self, session: AsyncSession, goal_id: str) -> None:
        """Unapproved dispatches count as outstanding work even before a Task exists."""
        from chiliarch.service import OperationError

        approvals, requests = await self.pending_work(session, goal_id)
        if approvals or requests:
            raise OperationError("Goal still has a pending task request or approval", 409)
