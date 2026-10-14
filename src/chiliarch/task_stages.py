"""Stage checkpoints that release execution only after native task termination.

Saving progress is not a stop signal. Ready notifications request a decision;
resumption remains an ordinary approved dispatch using the original session.
"""

from __future__ import annotations

import time
from typing import TYPE_CHECKING, Any

from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import JSON, Float, ForeignKey, String, Text, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Mapped, mapped_column

from chiliarch.coordination import ArtifactReference
from chiliarch.database import Agent, Base, CoordinationEvent, Task, TaskLink, new_id

if TYPE_CHECKING:
    from chiliarch.service import OperationService

JsonObject = dict[str, Any]


class TaskCheckpoint(Base):
    __tablename__ = "task_stage_checkpoints"
    id: Mapped[str] = mapped_column(String(128), primary_key=True, default=new_id)
    task_id: Mapped[str] = mapped_column(ForeignKey(Task.id), unique=True)
    agent_id: Mapped[str] = mapped_column(ForeignKey(Agent.id), index=True)
    summary: Mapped[str] = mapped_column(Text)
    artifacts: Mapped[list[JsonObject]] = mapped_column(JSON, default=list)
    waiting_for: Mapped[list[str]] = mapped_column(JSON, default=list)
    state: Mapped[str] = mapped_column(String(32), default="awaiting_exit")
    resumed_task_id: Mapped[str | None] = mapped_column(ForeignKey(Task.id), nullable=True)
    created_at: Mapped[float] = mapped_column(Float, default=time.time)
    updated_at: Mapped[float] = mapped_column(Float, default=time.time)


class Arguments(BaseModel):
    model_config = ConfigDict(extra="forbid")


class SaveCheckpoint(Arguments):
    task_id: str = Field(
        description="The running platform task whose current phase will finish; managed agents must use their bound task ID."
    )
    summary: str = Field(
        min_length=1,
        description="Completed phase, persisted changes, next steps and unresolved effects. After this tool succeeds, return from the current native task so it can genuinely release its execution slot.",
    )
    artifacts: list[ArtifactReference] = Field(
        default_factory=list,
        description="Existing artifact or commit references needed to continue; referencing a file does not upload it.",
    )
    waiting_for: list[str] = Field(
        default_factory=list,
        description="Tasks owned by this agent or participating in the same shared goal whose latest attempts must complete and be accepted before this phase can resume. This records a dependency; it does not keep the native process running.",
    )


class CheckpointId(Arguments):
    checkpoint_id: str = Field(
        description="Checkpoint ID from save_task_checkpoint or list_task_checkpoints."
    )


class ListCheckpoints(Arguments):
    task_id: str | None = Field(
        default=None,
        description="Optional original task ID filter. Managed tasks see only their own checkpoint and completed continuation ancestors; unbound agents see their own checkpoints.",
    )


class ResumeCheckpoint(CheckpointId):
    instruction: str | None = Field(
        default=None,
        min_length=1,
        description="Optional next-phase instruction. Defaults to continuing the checkpoint's recorded next steps and newly accepted dependency results.",
    )


SPECS: tuple[tuple[str, str, type[BaseModel], str, bool], ...] = (
    (
        "save_task_checkpoint",
        "Persist this running task's stage summary, artifact references and explicit task dependencies. Then finish the current native response normally. This tool does not terminate the process, change task success or release locks. Only confirmed completed native execution becomes paused/ready; failed or uncertain execution never resumes automatically. A checkpoint is immutable and can be resumed once.",
        SaveCheckpoint,
        "low",
        True,
    ),
    (
        "get_task_checkpoint",
        "Read one checkpoint, its original task state, dependency acceptance and resumed task ID. awaiting_exit means the process has not confirmed completion; paused waits for accepted dependencies; ready permits an explicit approved resume; unknown requires investigation.",
        CheckpointId,
        "low",
        False,
    ),
    (
        "list_task_checkpoints",
        "List persisted stage checkpoints and their true lifecycle states. A managed task sees only itself and its continuation ancestors, never sibling work. Unbound agents see their own checkpoints. A ready notification never by itself starts another model or changes permissions.",
        ListCheckpoints,
        "low",
        False,
    ),
    (
        "resume_task_checkpoint",
        "Continue a ready checkpoint once on the same agent, original native session and working directory. Requires the source task's confirmed completion, accepted dependencies and native resume support. Uses normal task dispatch and approvals; failed/unknown/still-active tasks cannot be resumed through this operation.",
        ResumeCheckpoint,
        "medium",
        True,
    ),
)
OPERATIONS = frozenset(spec[0] for spec in SPECS)


def checkpoint_view(row: TaskCheckpoint) -> JsonObject:
    return {column.key: getattr(row, column.key) for column in row.__table__.columns}


class TaskStageService:
    def __init__(self, service: OperationService) -> None:
        self.service = service

    async def _checkpoint(self, session: AsyncSession, checkpoint_id: str) -> TaskCheckpoint:
        from chiliarch.service import OperationError

        row = await session.get(TaskCheckpoint, checkpoint_id)
        if row is None:
            raise OperationError("Task checkpoint not found", 404)
        return row

    async def validate_scope(
        self,
        session: AsyncSession,
        name: str,
        args: JsonObject,
        agent_id: str,
        task_id: str | None = None,
    ) -> None:
        from chiliarch.service import OperationError

        await self.service._agent(session, agent_id)
        source_id = args.get("task_id")
        if args.get("checkpoint_id"):
            checkpoint = await self._checkpoint(session, args["checkpoint_id"])
            source_id = checkpoint.task_id
        if source_id:
            source = await self.service.coordination.task(session, source_id)
            if source.agent_id != agent_id:
                raise OperationError("Agent cannot manage another agent's checkpoint", 403)
        if name == "save_task_checkpoint" and task_id and source_id != task_id:
            raise OperationError("Checkpoint must belong to the bound running task", 403)
        if name == "resume_task_checkpoint" and task_id:
            # A running child cannot create another continuation of a different
            # session under its current task authority. Ask the supervisor.
            raise OperationError(
                "A managed task must ask the supervisor to resume a checkpoint", 403
            )
        if task_id and name in {"get_task_checkpoint", "list_task_checkpoints"}:
            from chiliarch.collaboration import TaskContext

            bound = await self.service.collaboration._task(session, task_id, agent_id)
            if bound.status not in {"running", "accepted"}:
                raise OperationError("Request belongs to work that is no longer running", 409)
            scope = {task_id}
            current = task_id
            while context := await session.get(TaskContext, current):
                previous = context.context.get("continue_task_id")
                if not isinstance(previous, str) or previous in scope:
                    break
                ancestor = await self.service.collaboration._task(session, previous, agent_id)
                if ancestor.status != "completed":
                    break
                scope.add(previous)
                current = previous
            if source_id and source_id not in scope:
                raise OperationError("Checkpoint does not belong to this managed task", 403)
            if name == "list_task_checkpoints":
                args["_checkpoint_task_ids"] = sorted(scope)

    async def _dependencies(
        self, session: AsyncSession, checkpoint: TaskCheckpoint
    ) -> tuple[bool, list[JsonObject]]:
        ready = True
        items: list[JsonObject] = []
        for task_id in checkpoint.waiting_for:
            task, link = await self.service.coordination.latest(session, task_id)
            accepted = task.status == "completed" and link.review_status == "accepted"
            ready = ready and accepted
            items.append(
                {
                    "original_task_id": task_id,
                    "task_id": task.id,
                    "status": task.status,
                    "review_status": link.review_status,
                    "result": task.result,
                    "artifacts": link.artifacts,
                }
            )
        return ready, items

    async def _state(self, session: AsyncSession, checkpoint: TaskCheckpoint) -> str:
        if checkpoint.resumed_task_id:
            return "resumed"
        task = await self.service.coordination.task(session, checkpoint.task_id)
        agent = await self.service._agent(session, checkpoint.agent_id, active=False)
        if task.status in {"interrupted", "expired"}:
            return "unknown"
        if task.status in {"failed", "cancelled"}:
            return "failed"
        if task.status != "completed" or task.id in agent.metadata_json.get("active_task_ids", []):
            return "awaiting_exit"
        if agent.revoked:
            return "paused"
        link = await self.service.coordination.link(session, task)
        if link.goal_id:
            goal = await self.service.coordination.goal(session, link.goal_id)
            if goal.status != "active":
                return "paused"
        ready, _ = await self._dependencies(session, checkpoint)
        return "ready" if ready else "paused"

    async def _refresh(self, session: AsyncSession, checkpoint: TaskCheckpoint) -> bool:
        state = await self._state(session, checkpoint)
        if state == checkpoint.state:
            return False
        checkpoint.state, checkpoint.updated_at = state, time.time()
        if state == "ready":
            task = await self.service.coordination.task(session, checkpoint.task_id)
            link = await self.service.coordination.link(session, task)
            event_key = f"checkpoint-ready:{checkpoint.id}"
            if (
                await session.scalar(
                    select(CoordinationEvent.id).where(CoordinationEvent.event_key == event_key)
                )
                is None
            ):
                session.add(
                    CoordinationEvent(
                        id=new_id(),
                        event_key=event_key,
                        kind="task_checkpoint_ready",
                        goal_id=link.goal_id,
                        conversation_id=link.conversation_id,
                        task_id=task.id,
                        payload={
                            "checkpoint": checkpoint_view(checkpoint),
                            "instruction": "Review the checkpoint and request resume_task_checkpoint through normal approval. This event is not permission to start execution.",
                        },
                        status="pending",
                        created_at=time.time(),
                    )
                )
        return True

    async def _details(self, session: AsyncSession, checkpoint: TaskCheckpoint) -> JsonObject:
        task = await self.service.coordination.task(session, checkpoint.task_id)
        _, dependencies = await self._dependencies(session, checkpoint)
        return {
            **checkpoint_view(checkpoint),
            "source_task_status": task.status,
            "dependencies": dependencies,
        }

    async def _validate_dependencies(
        self, session: AsyncSession, task: Task, waiting_for: list[str]
    ) -> None:
        from chiliarch.service import OperationError

        if task.id in waiting_for:
            raise OperationError("A checkpoint cannot wait for its own task", 409)
        edges: dict[str, list[str]] = {
            row.task_id: row.waiting_for
            for row in (await session.scalars(select(TaskCheckpoint))).all()
        }
        for link in (await session.scalars(select(TaskLink))).all():
            edges[link.task_id] = [*edges.get(link.task_id, []), *link.depends_on]
        edges[task.id] = waiting_for
        source_link = await self.service.coordination.link(session, task)
        for task_id in waiting_for:
            dependency = await self.service.coordination.task(session, task_id)
            dependency_link = await self.service.coordination.link(session, dependency)
            if dependency.agent_id != task.agent_id and (
                source_link.goal_id is None or source_link.goal_id != dependency_link.goal_id
            ):
                raise OperationError(
                    "Checkpoint dependencies must belong to the same agent or shared goal", 403
                )
        remaining, visited = list(waiting_for), set()
        while remaining:
            task_id = remaining.pop()
            if task_id == task.id:
                raise OperationError("Checkpoint dependencies form a cycle", 409)
            if task_id not in visited:
                visited.add(task_id)
                remaining.extend(edges.get(task_id, []))

    async def execute(
        self, session: AsyncSession, name: str, args: JsonObject, actor: str
    ) -> JsonObject:
        from chiliarch.service import OperationError

        agent_id = actor.removeprefix("agent:") if actor.startswith("agent:") else None
        if agent_id:
            await self.validate_scope(session, name, args, agent_id)
        if name == "save_task_checkpoint":
            task = await self.service.coordination.task(session, args["task_id"])
            existing = await session.scalar(
                select(TaskCheckpoint).where(TaskCheckpoint.task_id == task.id)
            )
            waiting_for = list(dict.fromkeys(args["waiting_for"]))
            if existing:
                if (existing.summary, existing.artifacts, existing.waiting_for) != (
                    args["summary"],
                    args["artifacts"],
                    waiting_for,
                ):
                    raise OperationError("This task already has an immutable checkpoint", 409)
                return {"checkpoint": await self._details(session, existing)}
            if task.status not in {"running", "accepted", "delivered"}:
                raise OperationError("Save a checkpoint while its native task is running", 409)
            await self._validate_dependencies(session, task, waiting_for)
            checkpoint = TaskCheckpoint(
                id=new_id(),
                task_id=task.id,
                agent_id=task.agent_id,
                summary=args["summary"],
                artifacts=args["artifacts"],
                waiting_for=waiting_for,
                state="awaiting_exit",
                created_at=time.time(),
                updated_at=time.time(),
            )
            session.add(checkpoint)
            await session.flush()
            return {
                "checkpoint": await self._details(session, checkpoint),
                "next_step": "Finish the current native response normally. The platform will wait for confirmed termination and accepted dependency work before notifying the supervisor.",
            }
        if name == "list_task_checkpoints":
            query = select(TaskCheckpoint).order_by(TaskCheckpoint.created_at.desc())
            if agent_id:
                query = query.where(TaskCheckpoint.agent_id == agent_id)
            if args.get("_checkpoint_task_ids") is not None:
                query = query.where(TaskCheckpoint.task_id.in_(args["_checkpoint_task_ids"]))
            if args["task_id"]:
                query = query.where(TaskCheckpoint.task_id == args["task_id"])
            return {
                "checkpoints": [
                    await self._details(session, checkpoint)
                    for checkpoint in (await session.scalars(query)).all()
                ]
            }
        checkpoint = await self._checkpoint(session, args["checkpoint_id"])
        if name == "get_task_checkpoint":
            return {"checkpoint": await self._details(session, checkpoint)}
        if name != "resume_task_checkpoint":
            raise OperationError("Unknown checkpoint operation", 404)
        if checkpoint.resumed_task_id:
            task = await self.service.coordination.task(session, checkpoint.resumed_task_id)
            return {
                "checkpoint": checkpoint_view(checkpoint),
                "task": self.service.task_view(task),
                "duplicate": True,
            }
        await self._refresh(session, checkpoint)
        if checkpoint.state != "ready":
            raise OperationError(
                "Checkpoint is not ready; wait for confirmed completion and accepted dependencies",
                409,
            )
        original = await self.service.coordination.task(session, checkpoint.task_id)
        owner = await self.service._agent(session, checkpoint.agent_id)
        if not owner.metadata_json.get("supports_resume"):
            raise OperationError(
                "Agent's native adapter does not support confirmed session resumption", 409
            )
        workdir = (
            original.result.get("workdir") if isinstance(original.result, dict) else None
        ) or self.service.task_view(original)["workdir"]
        if not workdir:
            raise OperationError("Checkpoint has no verified original working directory", 409)
        from chiliarch.projects import READ_ONLY_ACTIONS
        from chiliarch.projects_models import RepositoryCommand, WorkAttempt, WorkItem

        project_attempt = await session.scalar(
            select(WorkAttempt).where(WorkAttempt.task_id == original.id)
        )
        if project_attempt:
            work = await session.get(WorkItem, project_attempt.work_item_id)
            if (
                work is None
                or work.current_attempt_id != project_attempt.id
                or work.state == "merged"
            ):
                raise OperationError("Cannot resume a superseded or merged project attempt", 409)
            outstanding = await session.scalar(
                select(RepositoryCommand.id)
                .where(
                    RepositoryCommand.attempt_id == project_attempt.id,
                    RepositoryCommand.action.not_in(READ_ONLY_ACTIONS),
                    RepositoryCommand.status.in_(["pending", "unknown"]),
                )
                .limit(1)
            )
            if outstanding:
                raise OperationError(
                    "Reconcile outstanding repository effects before resuming work", 409
                )
        link = await self.service.coordination.link(session, original)
        _, dependencies = await self._dependencies(session, checkpoint)
        dispatched = await self.service.coordination.dispatch(
            session,
            {
                "agent_id": checkpoint.agent_id,
                "instruction": args["instruction"]
                or "Continue the saved stage using its summary, persisted artifacts and newly accepted dependency results.",
                "parameters": {
                    **{
                        key: value
                        for key, value in original.parameters.items()
                        if key != "chiliarch_context"
                    },
                    "checkpoint": {
                        "id": checkpoint.id,
                        "summary": checkpoint.summary,
                        "artifacts": checkpoint.artifacts,
                        "dependencies": dependencies,
                    },
                },
                "goal_id": link.goal_id,
                "conversation_id": link.conversation_id,
                "acceptance_criteria": link.acceptance_criteria,
                "depends_on": checkpoint.waiting_for,
                "continue_task_id": original.id,
                "workdir": workdir,
            },
            actor,
        )
        checkpoint.resumed_task_id = dispatched["task"]["id"]
        if project_attempt:
            project_attempt.task_id = checkpoint.resumed_task_id
            project_attempt.state = "running"
            if work is not None:
                work.state = "running"
        checkpoint.state, checkpoint.updated_at = "resumed", time.time()
        return {"checkpoint": checkpoint_view(checkpoint), **dispatched}

    async def maintain(self, session: AsyncSession) -> bool:
        changed = False
        for checkpoint in (
            await session.scalars(
                select(TaskCheckpoint).where(TaskCheckpoint.resumed_task_id.is_(None))
            )
        ).all():
            changed = await self._refresh(session, checkpoint) or changed
        return changed
