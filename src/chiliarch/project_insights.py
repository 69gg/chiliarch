"""Explainable project routing and causal, observed evidence; no predictive claims."""

from __future__ import annotations

import math
from typing import TYPE_CHECKING, Any, Literal

from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from chiliarch.database import Agent, Task
from chiliarch.projects_models import (
    DeliveryReview,
    Empty,
    Project,
    ProjectBinding,
    ProjectDelivery,
    ProjectFact,
    ProjectId,
    RepositoryCommand,
    WorkAttempt,
    WorkItem,
)
from chiliarch.task_stages import TaskCheckpoint

if TYPE_CHECKING:
    from chiliarch.service import OperationService

JsonObject = dict[str, Any]


class RecommendAgents(ProjectId):
    operating_system: Literal["windows", "linux", "darwin"] | None = Field(
        default=None,
        description="Required native OS; darwin means macOS. Unknown OS is not a match.",
    )
    capabilities: list[str] = Field(
        default_factory=list,
        description="Required declared agent capabilities; all values must be present for eligibility.",
    )
    exclude_agent_ids: list[str] = Field(
        default_factory=list,
        description="Exclude the author identity when selecting an independent reviewer.",
    )


SPECS: list[tuple[str, str, type[BaseModel], str, bool]] = [
    (
        "list_project_tools",
        "Discover GitHub project operation schemas, descriptions, approval risks and prerequisites before requesting work. If github.enabled is false, returns enabled=false and no schemas; previously discovered tools cannot bypass the switch. Returns no credentials and grants no permissions; project membership and current ask/auto/yolo policy are checked when an operation is requested.",
        Empty,
        "low",
        False,
    ),
    (
        "get_project_insights",
        "Read the project's causal timeline, blockers and reported native usage. Each attempt is counted once; missing usage is unknown, not zero. Timeline links command/attempt/task/PR identities and observation times; queued and unknown operations are not completed. No model or GitHub request is started.",
        ProjectId,
        "low",
        False,
    ),
    (
        "recommend_project_agents",
        "Explain which bound agents can accept repository work, based on live heartbeat, OS, declared capabilities, last repository access observation and current capacity. Returns all candidates and reasons; does not dispatch or imply credentials/signing were verified. Reinspect stale repository evidence before dispatch.",
        RecommendAgents,
        "low",
        False,
    ),
]
OPERATIONS = frozenset(spec[0] for spec in SPECS)


def _number(value: Any) -> float | None:
    if (
        isinstance(value, (int, float))
        and not isinstance(value, bool)
        and math.isfinite(value)
        and value >= 0
    ):
        return float(value)
    return None


async def execute(
    service: OperationService, session: AsyncSession, name: str, args: JsonObject, actor: str
) -> JsonObject:
    from chiliarch.service import OperationError

    if name == "list_project_tools":
        if not service.settings_manager.current.github.enabled:
            return {
                "enabled": False,
                "tools": [],
                "message": "GitHub functionality is disabled (github.enabled=false). Existing tasks and dispatched commands may finish; ordinary collaboration and task checkpoints remain available.",
            }
        from chiliarch.github_issues import OPERATIONS as ISSUE_OPERATIONS
        from chiliarch.projects import OPERATIONS as PROJECT_OPERATIONS
        from chiliarch.task_stages import OPERATIONS as STAGE_OPERATIONS

        names = PROJECT_OPERATIONS | OPERATIONS | STAGE_OPERATIONS | ISSUE_OPERATIONS
        if actor.startswith("agent:"):
            names = names - {
                "create_project",
                "update_project",
                "bind_project_agent",
                "configure_project_rule",
            }
        return {
            "enabled": True,
            "tools": [service.operations[key].schema() for key in sorted(names)],
        }
    project = await session.get(Project, args["project_id"])
    if project is None:
        raise OperationError("Project not found", 404)
    if name == "recommend_project_agents":
        rows = (
            await session.execute(
                select(Agent, ProjectBinding)
                .join(ProjectBinding, ProjectBinding.agent_id == Agent.id)
                .where(ProjectBinding.project_id == project.id)
            )
        ).all()
        candidates: list[JsonObject] = []
        for agent, binding in rows:
            view = service.agent_view(agent, actor)
            metadata = agent.metadata_json
            reasons: list[str] = []
            if not view["online"]:
                reasons.append("offline_or_revoked")
            if agent.id in args["exclude_agent_ids"]:
                reasons.append("excluded_identity")
            if not metadata.get("supports_repository_commands"):
                reasons.append("repository_executor_unavailable")
            if metadata.get("execution_mode") != "managed":
                reasons.append("interactive_agent_requires_manual_claim")
            if not metadata.get("supports_task_workdir"):
                reasons.append("task_workspace_unavailable")
            if metadata.get("state") == "blocked" or metadata.get("blocked_task_ids"):
                reasons.append("native_execution_blocked")
            if (
                args["operating_system"]
                and metadata.get("operating_system") != args["operating_system"]
            ):
                reasons.append("operating_system_mismatch_or_unknown")
            if not set(args["capabilities"]).issubset(agent.capabilities):
                reasons.append("missing_capabilities")
            capacity = _number(metadata.get("capacity"))
            queued = _number(metadata.get("queued_tasks")) or 0
            active = len(metadata.get("active_task_ids", []))
            available = max(0, int(capacity - active - queued)) if capacity is not None else 0
            if available == 0:
                reasons.append("no_observed_capacity")
            observation = binding.observation
            if not binding.observed_at or not observation.get("repository_id"):
                reasons.append("repository_access_unverified")
            elif str(observation["repository_id"]) != str(project.repository_id):
                reasons.append("repository_identity_mismatch")
            candidates.append(
                {
                    "agent_id": agent.id,
                    "name": agent.name,
                    "eligible": not reasons,
                    "reasons": reasons,
                    "available_slots": available,
                    "last_seen": agent.last_seen,
                    "repository_observed_at": binding.observed_at,
                    "operating_system": metadata.get("operating_system"),
                    "push_authentication": observation.get("push_authentication", "unverified"),
                    "signing_availability": observation.get("signing_availability", "unverified"),
                }
            )
        candidates.sort(
            key=lambda row: (not row["eligible"], -row["available_slots"], row["agent_id"])
        )
        return {"project_id": project.id, "candidates": candidates}

    items = list(
        (await session.scalars(select(WorkItem).where(WorkItem.project_id == project.id))).all()
    )
    item_ids = [item.id for item in items]
    attempts = list(
        (
            await session.scalars(select(WorkAttempt).where(WorkAttempt.work_item_id.in_(item_ids)))
        ).all()
    )
    task_attempts = {attempt.task_id: attempt.id for attempt in attempts if attempt.task_id}
    checkpoints = list((await session.scalars(select(TaskCheckpoint))).all())
    # Continuations retain all prior phase usage, even though the attempt points
    # to its newest task for workspace exclusion and delivery readiness.
    while True:
        previous = len(task_attempts)
        for checkpoint in checkpoints:
            if checkpoint.resumed_task_id in task_attempts:
                task_attempts[checkpoint.task_id] = task_attempts[checkpoint.resumed_task_id]
        if len(task_attempts) == previous:
            break
    tasks = {
        task.id: task
        for task in (await session.scalars(select(Task).where(Task.id.in_(task_attempts)))).all()
    }
    commands = list(
        (
            await session.scalars(
                select(RepositoryCommand).where(RepositoryCommand.project_id == project.id)
            )
        ).all()
    )
    deliveries = list(
        (
            await session.scalars(
                select(ProjectDelivery).where(ProjectDelivery.work_item_id.in_(item_ids))
            )
        ).all()
    )
    reviews = list(
        (
            await session.scalars(
                select(DeliveryReview).where(
                    DeliveryReview.delivery_id.in_([row.id for row in deliveries])
                )
            )
        ).all()
    )
    facts = list(
        (
            await session.scalars(select(ProjectFact).where(ProjectFact.project_id == project.id))
        ).all()
    )
    timeline: list[JsonObject] = []
    blockers: list[JsonObject] = []
    totals: dict[str, float] = {}
    ledger: list[JsonObject] = []
    for item in items:
        timeline.append(
            {
                "kind": "work_item",
                "at": item.created_at,
                "work_item_id": item.id,
                "title": item.title,
                "state": item.state,
            }
        )
        if item.stale_facts:
            blockers.append(
                {"kind": "stale_facts", "work_item_id": item.id, "keys": item.stale_facts}
            )
    for attempt in attempts:
        timeline.append(
            {
                "kind": "attempt",
                "at": attempt.created_at,
                "attempt_id": attempt.id,
                "work_item_id": attempt.work_item_id,
                "task_id": attempt.task_id,
                "branch": attempt.branch,
                "base_sha": attempt.base_sha,
                "state": attempt.state,
            }
        )
    for task in tasks.values():
        attempt_id = task_attempts[task.id]
        timeline.append(
            {
                "kind": "task",
                "at": task.updated_at,
                "task_id": task.id,
                "attempt_id": attempt_id,
                "state": task.status,
                "error": task.error,
            }
        )
        result = task.result if isinstance(task.result, dict) else {}
        usage = result.get("usage", {})
        numeric = (
            {key: count for key, value in usage.items() if (count := _number(value)) is not None}
            if isinstance(usage, dict)
            else {}
        )
        for key, count in numeric.items():
            totals[key] = totals.get(key, 0) + count
        ledger.append(
            {
                "task_id": task.id,
                "attempt_id": attempt_id,
                "agent_id": task.agent_id,
                "usage": numeric or None,
                "reported_cost_usd": _number(result.get("total_cost_usd")),
                "state": task.status,
            }
        )
    for checkpoint in checkpoints:
        if checkpoint.task_id in task_attempts:
            timeline.append(
                {
                    "kind": "checkpoint",
                    "at": checkpoint.updated_at,
                    "task_id": checkpoint.task_id,
                    "attempt_id": task_attempts[checkpoint.task_id],
                    "checkpoint_id": checkpoint.id,
                    "state": checkpoint.state,
                    "resumed_task_id": checkpoint.resumed_task_id,
                }
            )
    for command in commands:
        timeline.append(
            {
                "kind": "command",
                "at": command.updated_at,
                "command_id": command.id,
                "attempt_id": command.attempt_id,
                "action": command.action,
                "state": command.status,
                "error": command.error,
            }
        )
        if command.status in {"unknown", "failed"}:
            blockers.append(
                {
                    "kind": "command",
                    "command_id": command.id,
                    "state": command.status,
                    "error": command.error,
                }
            )
    for delivery in deliveries:
        timeline.append(
            {
                "kind": "delivery",
                "at": delivery.observed_at,
                "delivery_id": delivery.id,
                "attempt_id": delivery.attempt_id,
                "head_sha": delivery.head_sha,
                "base_sha": delivery.base_sha,
                "url": delivery.url,
                "state": delivery.state,
            }
        )
    for review in reviews:
        timeline.append(
            {
                "kind": "review",
                "at": review.created_at,
                "delivery_id": review.delivery_id,
                "head_sha": review.head_sha,
                "base_sha": review.base_sha,
                "reviewer": review.reviewer,
                "decision": review.decision,
            }
        )
    for fact in facts:
        timeline.append(
            {
                "kind": "fact",
                "at": fact.created_at,
                "key": fact.key,
                "version": fact.version,
                "source": fact.source,
            }
        )
    timeline.sort(key=lambda entry: entry["at"])
    return {
        "project_id": project.id,
        "timeline": timeline,
        "blockers": blockers,
        "usage": {
            "totals": totals,
            "attempts": ledger,
            "missing_usage_count": sum(row["usage"] is None for row in ledger),
            "source": "native_reported_not_billing",
        },
    }
