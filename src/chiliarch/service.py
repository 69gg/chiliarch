"""One authorization and durable operation boundary for UI, AI and agents."""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import json
import logging
import re
import secrets
import time
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any, Literal, cast

from jsonschema import Draft202012Validator
from pydantic import BaseModel, ConfigDict, Field, ValidationError
from sqlalchemy import delete, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from chiliarch.artifacts import (
    CONTINUATION_OPERATIONS as ARTIFACT_CONTINUATIONS,
)
from chiliarch.artifacts import (
    INTERNAL_OPERATIONS as ARTIFACT_INTERNAL,
)
from chiliarch.artifacts import (
    OPERATIONS as ARTIFACT_OPERATIONS,
)
from chiliarch.artifacts import (
    SPECS as ARTIFACT_SPECS,
)
from chiliarch.artifacts import (
    ArtifactService,
)
from chiliarch.auth import hash_password, hash_token
from chiliarch.collaboration import (
    DIRECT_OPERATIONS,
    TELEMETRY_OPERATIONS,
    CollaborationService,
    RequestContext,
)
from chiliarch.collaboration import (
    OPERATIONS as COLLABORATION_OPERATIONS,
)
from chiliarch.collaboration import (
    SPECS as COLLABORATION_SPECS,
)
from chiliarch.coordination import (
    CoordinationService,
    CreateGoalArguments,
    GoalArguments,
    RetryTaskArguments,
    ReviewTaskArguments,
    UpdateGoalArguments,
    goal_view,
    task_link_view,
)
from chiliarch.database import (
    Agent,
    AgentRequest,
    Approval,
    AuditEvent,
    Database,
    Goal,
    Message,
    OperationRecord,
    PairingCode,
    Task,
    TaskLink,
    UserSession,
    new_id,
)
from chiliarch.protocol import AGENT_MESSAGE_TYPES, SERVER_MESSAGE_TYPES, Envelope, TaskWorkdir

if TYPE_CHECKING:
    from chiliarch.config import SettingsManager

JsonObject = dict[str, Any]
Handler = Callable[[JsonObject], Awaitable[JsonObject]]
Listener = Callable[[str, JsonObject], Awaitable[None]]
logger = logging.getLogger("chiliarch.service")


class OperationError(Exception):
    def __init__(self, message: str, status_code: int = 400) -> None:
        super().__init__(message)
        self.status_code = status_code


class Arguments(BaseModel):
    model_config = ConfigDict(extra="forbid")


class EmptyArguments(Arguments):
    pass


class AgentArguments(Arguments):
    agent_id: str = Field(min_length=1)


class RenameArguments(AgentArguments):
    name: str = Field(min_length=1, max_length=256)


class PairingArguments(Arguments):
    ttl_seconds: int = Field(default=900, ge=60, le=86400)


class DispatchArguments(AgentArguments):
    instruction: str = Field(min_length=1, max_length=100000)
    parameters: JsonObject = Field(default_factory=dict)
    goal_id: str | None = None
    conversation_id: str | None = None
    depends_on: list[str] = Field(default_factory=list)
    acceptance_criteria: list[str] = Field(default_factory=list)
    continue_task_id: str | None = None
    workdir: TaskWorkdir | None = None


class TaskArguments(Arguments):
    task_id: str = Field(min_length=1)


class CancelArguments(TaskArguments):
    reason: str = "User requested cancellation"


class SendMessageArguments(AgentArguments):
    message: str = Field(min_length=1, max_length=100000)
    metadata: JsonObject = Field(default_factory=dict)
    correlation_id: str | None = Field(default=None, min_length=1, max_length=128)


class ResolveRequestArguments(Arguments):
    request_id: str = Field(min_length=1)
    decision: Literal["approve", "reject"]
    reason: str = ""


class ApprovalArguments(Arguments):
    approval_id: str = Field(min_length=1)
    reason: str = ""


class ConfigArguments(Arguments):
    patch: JsonObject


class LogsArguments(Arguments):
    limit: int = Field(default=100, ge=1, le=1000)


class PasswordArguments(Arguments):
    password: str = Field(min_length=8, max_length=1024)


class Operation:
    def __init__(
        self,
        name: str,
        description: str,
        parameters: JsonObject,
        handler: Handler | None,
        risk: str,
        mutating: bool,
        arguments: type[BaseModel] | None = None,
    ) -> None:
        self.name, self.description, self.parameters = name, description, parameters
        self.handler, self.risk, self.mutating, self.arguments = handler, risk, mutating, arguments

    def schema(self) -> JsonObject:
        return {
            "name": self.name,
            "description": self.description,
            "parameters": self.parameters,
            "risk": self.risk,
            "mutating": self.mutating,
        }


SENSITIVE_PATTERN = re.compile(
    r"password|credential|authorization|api[_-]?key|secret|cookie|(^|_)token$", re.I
)

SENSITIVE_NAMES = frozenset(
    {
        "password",
        "password_hash",
        "api_key",
        "credential",
        "token",
        "code",
        "authorization",
        "data_base64",
        "secret",
    }
)


def redact(value: Any, key: str = "") -> Any:
    if key.lower() in SENSITIVE_NAMES or SENSITIVE_PATTERN.search(key):
        return "********" if value else value
    if isinstance(value, dict):
        if key.lower() == "headers":
            return {str(k): "********" if v else v for k, v in value.items()}
        return {str(k): redact(v, str(k)) for k, v in value.items()}
    if isinstance(value, list):
        return [redact(item) for item in value]
    return value


def fingerprint(value: Any) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()
    ).hexdigest()


class OperationService:
    def __init__(self, db: Database, settings_manager: SettingsManager) -> None:
        self.db, self.settings_manager = db, settings_manager
        self.operations: dict[str, Operation] = {}
        self.listeners: list[Listener] = []
        self.changed = asyncio.Event()
        self._maintenance_task: asyncio.Task[None] | None = None
        self.coordination = CoordinationService(self)
        self.collaboration = CollaborationService(self)
        self.artifacts = ArtifactService(self)
        self._register_core()

    def register(
        self,
        name: str,
        description: str,
        parameters: JsonObject,
        handler: Handler,
        risk: str = "low",
        mutating: bool = False,
    ) -> None:
        if name in self.operations:
            raise ValueError(f"Operation {name} is already registered")
        if risk not in {"low", "medium", "high"}:
            raise ValueError("risk must be low, medium or high")
        Draft202012Validator.check_schema(parameters)
        self.operations[name] = Operation(name, description, parameters, handler, risk, mutating)

    def _register_core(self) -> None:
        specs: list[tuple[str, str, type[BaseModel], str, bool]] = [
            (
                "list_agents",
                "List agents and their observed availability",
                EmptyArguments,
                "low",
                False,
            ),
            ("get_agent", "Read a registered agent", AgentArguments, "low", False),
            (
                "list_tasks",
                "List dispatched tasks and execution status",
                EmptyArguments,
                "low",
                False,
            ),
            ("get_task", "Read task progress and result", TaskArguments, "low", False),
            (
                "create_goal",
                "Create a durable goal with constraints and acceptance criteria",
                CreateGoalArguments,
                "medium",
                True,
            ),
            (
                "list_goals",
                "List goals, their tasks, reviews and artifacts",
                EmptyArguments,
                "low",
                False,
            ),
            (
                "get_goal",
                "Read a goal and all its task attempts and artifacts",
                GoalArguments,
                "low",
                False,
            ),
            (
                "update_goal",
                "Update goal constraints or status; completion requires accepted work; cancellation cancels unfinished tasks",
                UpdateGoalArguments,
                "medium",
                True,
            ),
            (
                "review_task",
                "Accept completed work after checking its criteria and artifacts, or request changes",
                ReviewTaskArguments,
                "medium",
                True,
            ),
            (
                "retry_task",
                "Create a new attempt after inspecting actual effects, optionally assign another agent_id; inherits goal, dependencies and prior result; recovery_note must explain why retry is safe",
                RetryTaskArguments,
                "high",
                True,
            ),
            (
                "create_pairing_code",
                "Create an expiring one-time agent registration code",
                PairingArguments,
                "high",
                True,
            ),
            ("rename_agent", "Rename an agent", RenameArguments, "low", True),
            (
                "revoke_agent",
                "Revoke agent credentials and terminate existing connections",
                AgentArguments,
                "high",
                True,
            ),
            (
                "dispatch_task",
                "Queue an instruction for an agent; the server never connects to its machine",
                DispatchArguments,
                "medium",
                True,
            ),
            (
                "cancel_task",
                "Request task cancellation; cancellation depends on the agent",
                CancelArguments,
                "medium",
                True,
            ),
            (
                "send_agent_message",
                "Send a response or message through the agent's outbound connection",
                SendMessageArguments,
                "medium",
                True,
            ),
            (
                "list_agent_requests",
                "Read requests submitted by agents",
                EmptyArguments,
                "low",
                False,
            ),
            (
                "resolve_agent_request",
                "Approve the exact stored request or reject it",
                ResolveRequestArguments,
                "medium",
                True,
            ),
            ("list_approvals", "List operation approvals", EmptyArguments, "low", False),
            (
                "approve_operation",
                "Approve a stored operation; the supervisor cannot approve itself",
                ApprovalArguments,
                "high",
                True,
            ),
            ("reject_operation", "Reject a stored operation", ApprovalArguments, "high", True),
            ("get_config", "Read configuration with secrets masked", EmptyArguments, "low", False),
            (
                "update_config",
                "Update and validate project configuration",
                ConfigArguments,
                "high",
                True,
            ),
            ("get_logs", "Read recent redacted operational logs", LogsArguments, "low", False),
            (
                "change_password",
                "Change the user password and invalidate all sessions",
                PasswordArguments,
                "high",
                True,
            ),
        ]
        for name, description, arguments, risk, mutating in [
            *specs,
            *COLLABORATION_SPECS,
            *ARTIFACT_SPECS,
        ]:
            self.operations[name] = Operation(
                name, description, arguments.model_json_schema(), None, risk, mutating, arguments
            )

    def schemas(self) -> list[JsonObject]:
        return [
            operation.schema()
            for operation in self.operations.values()
            if operation.name not in ARTIFACT_INTERNAL
        ]

    def add_listener(self, callback: Listener) -> None:
        self.listeners.append(callback)

    async def emit(self, event: str, data: JsonObject) -> None:
        self.changed.set()
        for listener in self.listeners:
            try:
                await listener(event, data)
            except Exception:
                logger.exception("Event listener failed", extra={"event": event})

    def _validate(self, name: str, arguments: JsonObject) -> JsonObject:
        operation = self.operations.get(name)
        if operation is None:
            raise OperationError(f"Unknown operation: {name}", 404)
        if operation.arguments is not None:
            try:
                validated = operation.arguments.model_validate(arguments).model_dump(mode="json")
                if name == "retry_task" and validated.get("agent_id") is None:
                    # Preserve pre-handover idempotency fingerprints and stored requests.
                    validated.pop("agent_id", None)
                if name == "dispatch_task" and validated.get("continue_task_id") is None:
                    validated.pop("continue_task_id", None)
                if name in {"dispatch_task", "retry_task"} and validated.get("workdir") is None:
                    validated.pop("workdir", None)
                return validated
            except ValidationError as exc:
                error = exc.errors(include_input=False, include_url=False)[0]
                field = ".".join(str(part) for part in error["loc"])
                raise OperationError(
                    f"Invalid operation arguments ({field}): {error['msg']}", 422
                ) from exc
        errors = list(Draft202012Validator(operation.parameters).iter_errors(arguments))
        if errors:
            raise OperationError(
                "Invalid operation arguments at "
                + ".".join(str(part) for part in errors[0].path)
                + f" ({errors[0].validator})",
                422,
            )
        return arguments

    def _approval_reason(
        self, operation: Operation, actor: str, assessment: JsonObject | None
    ) -> str | None:
        if actor == "user" or not operation.mutating:
            return None
        mode = self.settings_manager.current.supervisor.mode
        if mode == "yolo":
            return None
        if mode == "ask":
            return "ask mode requires user approval for a state-changing operation"
        level = operation.risk
        if operation.risk != "high" and assessment:
            level = str(assessment.get("level", level))
        if level == "low":
            return None
        return f"auto mode requires approval for {level} or unclassified risk: " + str(
            (assessment or {}).get("reason", operation.description)
        )

    def _policy(self, name: str, arguments: JsonObject) -> Operation:
        operation = self.operations[name]
        if name == "send_peer_message" and arguments.get("wake"):
            return Operation(
                operation.name,
                operation.description,
                operation.parameters,
                operation.handler,
                "medium",
                True,
                operation.arguments,
            )
        return operation

    async def execute(
        self,
        name: str,
        arguments: JsonObject,
        actor: str = "user",
        idempotency_key: str | None = None,
        risk_assessment: JsonObject | None = None,
    ) -> JsonObject:
        if actor.startswith("agent:"):
            raise OperationError(
                "Agents must submit requests; they cannot invoke management operations", 403
            )
        if actor not in {"user", "supervisor"}:
            raise OperationError("Unknown actor", 403)
        if actor == "supervisor" and name in ARTIFACT_INTERNAL:
            raise OperationError(
                "Binary transfer is performed by local artifact tools, not model context", 403
            )
        raw_arguments = dict(arguments)
        supplied_assessment = raw_arguments.pop("_risk", None)
        if supplied_assessment is not None:
            if (
                not isinstance(supplied_assessment, dict)
                or supplied_assessment.get("level") not in {"low", "medium", "high"}
                or not isinstance(supplied_assessment.get("reason"), str)
            ):
                raise OperationError("_risk requires level and reason", 422)
            risk_assessment = supplied_assessment
        validated = self._validate(name, raw_arguments)
        operation = self._policy(name, validated)
        if actor == "supervisor" and name in {"approve_operation", "reject_operation"}:
            raise OperationError(
                "Only the authenticated user may resolve supervisor approvals", 403
            )
        args_hash = fingerprint({"name": name, "arguments": validated})
        event: tuple[str, JsonObject] | None = None
        execution_handler = operation.handler
        execution_arguments = validated
        extension_approval_id: str | None = None
        extension_request_id: str | None = None
        async with self.db.write_lock:
            async with self.db.sessions.begin() as session:
                if idempotency_key is not None:
                    existing = (
                        await session.execute(
                            select(OperationRecord).where(
                                OperationRecord.actor == actor,
                                OperationRecord.idempotency_key == idempotency_key,
                            )
                        )
                    ).scalar_one_or_none()
                    if existing is not None:
                        if existing.arguments_hash != args_hash:
                            raise OperationError(
                                "Idempotency key already belongs to different arguments", 409
                            )
                        return dict(existing.result)
                record = OperationRecord(
                    id=new_id(),
                    actor=actor,
                    idempotency_key=idempotency_key,
                    name=name,
                    arguments_hash=args_hash,
                )
                session.add(record)
                effective_operation = operation
                if name == "resolve_agent_request" and validated["decision"] == "approve":
                    request = await session.get(AgentRequest, validated["request_id"])
                    if request is None:
                        raise OperationError("Agent request not found", 404)
                    requested_operation = (
                        self._policy(request.operation, request.arguments)
                        if request.operation in self.operations
                        else None
                    )
                    if requested_operation is not None:
                        effective_operation = Operation(
                            operation.name,
                            operation.description,
                            operation.parameters,
                            operation.handler,
                            requested_operation.risk,
                            requested_operation.mutating,
                        )
                reason = self._approval_reason(effective_operation, actor, risk_assessment)
                if not reason:
                    (
                        execution_handler,
                        execution_arguments,
                        extension_request_id,
                    ) = await self._extension_target(session, name, validated)
                if name == "approve_operation":
                    target_approval = await session.get(Approval, validated["approval_id"])
                    if target_approval is not None and target_approval.status == "pending":
                        (
                            target_handler,
                            target_args,
                            target_request_id,
                        ) = await self._extension_target(
                            session, target_approval.operation, target_approval.arguments
                        )
                        if target_handler is not None:
                            execution_handler, execution_arguments = target_handler, target_args
                            extension_request_id = target_request_id
                            extension_approval_id = target_approval.id
                            target_approval.status = "execution_unknown"
                            original_record = await session.get(
                                OperationRecord, target_approval.operation_record_id
                            )
                            if original_record:
                                original_record.status = "execution_unknown"
                                original_record.result = {
                                    "status": "execution_unknown",
                                    "approval_id": target_approval.id,
                                }
                if extension_request_id is not None:
                    executing_request = await session.get(AgentRequest, extension_request_id)
                    if executing_request:
                        executing_request.status = "execution_unknown"
                if reason:
                    approval = Approval(
                        id=new_id(),
                        operation=name,
                        arguments=validated,
                        requested_by=actor,
                        reason=reason,
                        operation_record_id=record.id,
                    )
                    session.add(approval)
                    result: JsonObject = {"status": "awaiting_approval", "approval_id": approval.id}
                    record.status, record.result = "awaiting_approval", result
                    session.add(
                        AuditEvent(
                            actor=actor,
                            action="operation.awaiting_approval",
                            details={
                                "operation": name,
                                "arguments": redact(validated),
                                "approval_id": approval.id,
                            },
                        )
                    )
                elif execution_handler is None:
                    result, event = await self._core(session, name, validated, actor)
                    record.status, record.result = "completed", result
                    session.add(
                        AuditEvent(
                            actor=actor,
                            action=name,
                            details={"arguments": redact(validated), "result": redact(result)},
                        )
                    )
                else:
                    # Extension handlers own their transactions. A durable reservation prevents
                    # replaying an unknown side effect following a process crash.
                    record.result = {"status": "execution_unknown", "operation_id": record.id}
                    result = record.result
        if execution_handler is not None and not reason:
            try:
                result = await execution_handler(execution_arguments)
            except Exception as exc:
                failure = {
                    "status": "failed",
                    "error": f"Handler failed; external effects may be incomplete ({type(exc).__name__})",
                }
                async with self.db.write_lock, self.db.sessions.begin() as session:
                    saved = await session.get(OperationRecord, record.id)
                    if saved:
                        saved.status, saved.result = "failed", failure
                    if extension_request_id is not None:
                        failed_request = await session.get(AgentRequest, extension_request_id)
                        if failed_request:
                            failed_request.status, failed_request.result = "failed", failure
                            failed_request.resolved_at = time.time()
                            await self._enqueue(
                                session,
                                failed_request.agent_id,
                                Envelope(
                                    type="request.resolved",
                                    correlation_id=failed_request.external_id,
                                    payload={
                                        "request_id": failed_request.external_id,
                                        "status": "failed",
                                        "error": failure["error"],
                                    },
                                ),
                            )
                    if extension_approval_id is not None:
                        failed_approval = await session.get(Approval, extension_approval_id)
                        if failed_approval:
                            failed_approval.status, failed_approval.result = "failed", failure
                            failed_approval.resolved_at = time.time()
                            original_record = await session.get(
                                OperationRecord, failed_approval.operation_record_id
                            )
                            if original_record:
                                original_record.status, original_record.result = "failed", failure
                            event = (
                                "operation.rejected",
                                {
                                    "approval_id": failed_approval.id,
                                    "name": failed_approval.operation,
                                    "actor": failed_approval.requested_by,
                                    "result": failure,
                                },
                            )
                if event:
                    await self.emit(*event)
                raise
            async with self.db.write_lock, self.db.sessions.begin() as session:
                if extension_request_id is not None:
                    completed_request = await session.get(AgentRequest, extension_request_id)
                    if completed_request:
                        completed_request.status, completed_request.result = "approved", result
                        completed_request.resolved_at = time.time()
                        await self._enqueue(
                            session,
                            completed_request.agent_id,
                            Envelope(
                                type="request.resolved",
                                correlation_id=completed_request.external_id,
                                payload={
                                    "request_id": completed_request.external_id,
                                    "status": "approved",
                                    "result": result,
                                },
                            ),
                        )
                        result = {"request": self.request_view(completed_request)}
                saved = await session.get(OperationRecord, record.id)
                if saved:
                    saved.status, saved.result = "completed", result
                if extension_approval_id is not None:
                    target_approval = await session.get(Approval, extension_approval_id)
                    if target_approval:
                        target_approval.status, target_approval.result = "approved", result
                        target_approval.resolved_at = time.time()
                        original_record = await session.get(
                            OperationRecord, target_approval.operation_record_id
                        )
                        if original_record:
                            original_record.status, original_record.result = "completed", result
                        event = (
                            "operation.approved",
                            {
                                "approval_id": target_approval.id,
                                "name": target_approval.operation,
                                "actor": target_approval.requested_by,
                                "result": redact(result),
                            },
                        )
                        result = {"approval": self.approval_view(target_approval)}
                        if saved:
                            saved.result = result
                session.add(
                    AuditEvent(
                        actor=actor,
                        action=name,
                        details={"arguments": redact(validated), "result": redact(result)},
                    )
                )
        logger.info(
            "Operation executed",
            extra={"actor": actor, "operation": name, "status": result.get("status", "completed")},
        )
        await self.emit(
            "operation.executed", {"name": name, "actor": actor, "result": redact(result)}
        )
        if event:
            await self.emit(*event)
        return result

    async def _extension_target(
        self, session: AsyncSession, name: str, args: JsonObject
    ) -> tuple[Handler | None, JsonObject, str | None]:
        operation = self.operations[name]
        if operation.handler is not None:
            return operation.handler, args, None
        if name == "resolve_agent_request" and args["decision"] == "approve":
            request = await session.get(AgentRequest, args["request_id"])
            if request is None:
                raise OperationError("Agent request not found", 404)
            if request.status == "pending":
                if request.operation in {
                    "approve_operation",
                    "reject_operation",
                    "resolve_agent_request",
                }:
                    raise OperationError(
                        "An agent request cannot resolve approvals or other requests", 403
                    )
                requested_operation = self.operations[request.operation]
                if requested_operation.handler is not None:
                    return (
                        requested_operation.handler,
                        self._validate(request.operation, request.arguments),
                        request.id,
                    )
        return None, args, None

    async def _agent(self, session: AsyncSession, agent_id: str, active: bool = True) -> Agent:
        agent = await session.get(Agent, agent_id)
        if agent is None:
            raise OperationError("Agent not found", 404)
        if active and agent.revoked:
            raise OperationError("Agent credential is revoked", 409)
        return agent

    async def _connection_agent(self, session: AsyncSession, agent_id: str) -> Agent:
        agent = await session.get(Agent, agent_id)
        if agent is None or agent.revoked:
            raise OperationError("Invalid or revoked agent credential", 401)
        return agent

    def agent_view(self, agent: Agent) -> JsonObject:
        return {
            "id": agent.id,
            "name": agent.name,
            "machine": agent.machine,
            "capabilities": agent.capabilities,
            "revoked": agent.revoked,
            "created_at": agent.created_at,
            "last_seen": agent.last_seen,
            "online": not agent.revoked
            and agent.last_seen is not None
            and time.time() - agent.last_seen
            < self.settings_manager.current.transport.offline_after_seconds,
            "metadata": redact(agent.metadata_json),
            "transport": agent.metadata_json.get("transport"),
        }

    @staticmethod
    def task_view(task: Task) -> JsonObject:
        return {
            "workdir": task.parameters.get("chiliarch_context", {}).get("workdir"),
            **{
                field: getattr(task, field)
                for field in (
                    "id",
                    "agent_id",
                    "instruction",
                    "parameters",
                    "status",
                    "progress",
                    "result",
                    "error",
                    "message",
                    "created_at",
                    "updated_at",
                    "requested_by",
                )
            },
        }

    @staticmethod
    def request_view(request: AgentRequest) -> JsonObject:
        return cast(
            JsonObject,
            redact(
                {
                    field: getattr(request, field)
                    for field in (
                        "id",
                        "agent_id",
                        "external_id",
                        "operation",
                        "arguments",
                        "message",
                        "status",
                        "result",
                        "created_at",
                        "resolved_at",
                    )
                }
            ),
        )

    @staticmethod
    def approval_view(approval: Approval) -> JsonObject:
        return cast(
            JsonObject,
            redact(
                {
                    field: getattr(approval, field)
                    for field in (
                        "id",
                        "operation",
                        "arguments",
                        "requested_by",
                        "status",
                        "reason",
                        "created_at",
                        "resolved_at",
                        "result",
                    )
                }
            ),
        )

    async def start(self) -> None:
        self._maintenance_task = asyncio.create_task(
            self._maintenance(), name="chiliarch-queue-expiry"
        )

    async def stop(self) -> None:
        if self._maintenance_task:
            self._maintenance_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._maintenance_task
            self._maintenance_task = None

    async def _maintenance(self) -> None:
        from chiliarch.monitoring import monitor_tasks

        while True:
            try:
                async with self.db.write_lock, self.db.sessions.begin() as session:
                    await self._expire_messages(session)
                    await self.coordination.release(session)
                    await monitor_tasks(session, self.settings_manager.current)
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("Queue expiry maintenance failed")
            await asyncio.sleep(self.settings_manager.current.transport.retry_seconds)

    async def _expire_messages(self, session: AsyncSession, agent_id: str | None = None) -> None:
        query = select(Message).where(
            Message.direction == "outbox",
            Message.acked.is_(False),
            Message.expired.is_(False),
            Message.expires_at < time.time(),
        )
        if agent_id is not None:
            query = query.where(Message.agent_id == agent_id)
        for message in (await session.scalars(query)).all():
            message.expired = True
            if message.body.get("type") == "task.dispatch":
                task = await session.get(Task, message.body["payload"]["task_id"])
                if task and task.status == "queued":
                    task.status, task.updated_at = "expired", time.time()
                    await self.coordination.terminal(session, task)
            session.add(
                AuditEvent(
                    actor="system",
                    action="message.expired",
                    details={"agent_id": message.agent_id, "message_id": message.message_id},
                )
            )

    async def _enqueue(self, session: AsyncSession, agent_id: str, envelope: Envelope) -> None:
        await self._expire_messages(session, agent_id)
        pending = (
            await session.execute(
                select(Message.id).where(
                    Message.agent_id == agent_id,
                    Message.direction == "outbox",
                    Message.acked.is_(False),
                    Message.expired.is_(False),
                )
            )
        ).all()
        if len(pending) >= self.settings_manager.current.transport.queue_limit:
            raise OperationError("Agent offline queue is full", 409)
        if envelope.expires_at is None:
            envelope.expires_at = datetime.now(UTC) + timedelta(
                seconds=self.settings_manager.current.transport.message_ttl_seconds
            )
        body = envelope.model_dump(mode="json")
        session.add(
            Message(
                agent_id=agent_id,
                direction="outbox",
                message_id=envelope.id,
                body=body,
                body_hash=fingerprint(body),
                expires_at=envelope.expires_at.timestamp(),
            )
        )

    async def _core(
        self, session: AsyncSession, name: str, args: JsonObject, actor: str
    ) -> tuple[JsonObject, tuple[str, JsonObject] | None]:
        event: tuple[str, JsonObject] | None = None
        if name in ARTIFACT_OPERATIONS:
            return await self.artifacts.execute(session, name, args, actor), None
        if name in COLLABORATION_OPERATIONS:
            return await self.collaboration.execute(session, name, args, actor), None
        if name in {
            "create_goal",
            "list_goals",
            "get_goal",
            "update_goal",
            "review_task",
            "retry_task",
        }:
            return await self.coordination.execute(session, name, args, actor), None
        if name == "list_agents":
            return {
                "agents": [
                    self.agent_view(a)
                    for a in (
                        await session.scalars(select(Agent).order_by(Agent.created_at.desc()))
                    ).all()
                ]
            }, None
        if name == "get_agent":
            return {
                "agent": self.agent_view(await self._agent(session, args["agent_id"], False))
            }, None
        if name == "list_tasks":
            return {
                "tasks": [
                    self.task_view(t)
                    for t in (
                        await session.scalars(select(Task).order_by(Task.created_at.desc()))
                    ).all()
                ]
            }, None
        if name == "get_task":
            task = await session.get(Task, args["task_id"])
            if task is None:
                raise OperationError("Task not found", 404)
            link = await self.coordination.link(session, task)
            return {"task": self.task_view(task), "task_link": task_link_view(link)}, None
        if name == "create_pairing_code":
            code, expires_at = secrets.token_urlsafe(24), time.time() + args["ttl_seconds"]
            pairing = PairingCode(id=new_id(), code_hash=hash_token(code), expires_at=expires_at)
            session.add(pairing)
            return {"id": pairing.id, "code": code, "expires_at": expires_at}, None
        if name == "rename_agent":
            agent = await self._agent(session, args["agent_id"])
            agent.name = args["name"]
            return {"agent": self.agent_view(agent)}, None
        if name == "revoke_agent":
            agent = await self._agent(session, args["agent_id"], False)
            agent.revoked = True
            await session.execute(
                update(Message)
                .where(
                    Message.agent_id == agent.id,
                    Message.direction == "outbox",
                    Message.acked.is_(False),
                )
                .values(expired=True)
            )
            return {"agent": self.agent_view(agent)}, ("agent.revoked", {"agent_id": agent.id})
        if name == "dispatch_task":
            return await self.coordination.dispatch(session, args, actor), None
        if name == "cancel_task":
            task = await session.get(Task, args["task_id"])
            if task is None:
                raise OperationError("Task not found", 404)
            if task.status in {"completed", "failed", "cancelled", "expired", "interrupted"}:
                return {"task": self.task_view(task)}, None
            if task.status == "blocked":
                task.status, task.updated_at = "cancelled", time.time()
                task.message = args["reason"]
                await self.coordination.terminal(session, task)
                return {"task": self.task_view(task)}, None
            agent = await self._agent(session, task.agent_id, active=False)
            pending = (
                await session.scalars(
                    select(Message).where(
                        Message.agent_id == task.agent_id,
                        Message.direction == "outbox",
                        Message.acked.is_(False),
                        Message.expired.is_(False),
                    )
                )
            ).all()
            for message in pending:
                if (
                    message.body.get("type") == "task.dispatch"
                    and message.body.get("payload", {}).get("task_id") == task.id
                ):
                    message.expired = True
            task.status, task.updated_at = "cancellation_requested", time.time()
            task.message = args["reason"]
            if agent.revoked:
                # A queued dispatch may already be executing when its ACK was
                # lost. Revocation prevents further delivery, not remote work;
                # preserve the intent without asserting that execution stopped.
                task.message += (
                    " (Agent credential is revoked; cancellation could not be delivered; "
                    "remote execution state is unknown.)"
                )
                return {"task": self.task_view(task)}, None
            await self._enqueue(
                session,
                task.agent_id,
                Envelope(
                    type="task.cancel",
                    correlation_id=task.id,
                    payload={"task_id": task.id, "reason": args["reason"]},
                ),
            )
            return {"task": self.task_view(task)}, None
        if name == "send_agent_message":
            await self._agent(session, args["agent_id"])
            envelope = Envelope(
                type="agent.message",
                correlation_id=args["correlation_id"],
                payload={"message": args["message"], "metadata": args["metadata"]},
            )
            await self._enqueue(session, args["agent_id"], envelope)
            return {"message_id": envelope.id, "status": "queued"}, None
        if name == "list_agent_requests":
            return {
                "requests": [
                    self.request_view(r)
                    for r in (
                        await session.scalars(
                            select(AgentRequest).order_by(AgentRequest.created_at.desc())
                        )
                    ).all()
                ]
            }, None
        if name == "resolve_agent_request":
            request = await session.get(AgentRequest, args["request_id"])
            if request is None:
                raise OperationError("Agent request not found", 404)
            if request.status != "pending":
                return {"request": self.request_view(request)}, None
            if request.operation in {
                "approve_operation",
                "reject_operation",
                "resolve_agent_request",
            }:
                raise OperationError(
                    "An agent request cannot resolve approvals or other requests", 403
                )
            if args["decision"] == "approve":
                validated = self._validate(request.operation, request.arguments)
                if self.operations[request.operation].handler is not None:
                    raise OperationError(
                        "Extension requests must be handled by an explicit plugin decision handler",
                        409,
                    )
                request_actor = (
                    f"agent:{request.agent_id}"
                    if request.operation in DIRECT_OPERATIONS | ARTIFACT_OPERATIONS
                    else actor
                )
                context = await session.get(RequestContext, request.id)
                if request.operation in DIRECT_OPERATIONS | ARTIFACT_OPERATIONS:
                    validator = (
                        self.artifacts
                        if request.operation in ARTIFACT_OPERATIONS
                        else self.collaboration
                    )
                    await validator.validate_scope(
                        session,
                        request.operation,
                        validated,
                        request.agent_id,
                        context.task_id if context else None,
                    )
                result, event = await self._core(
                    session, request.operation, validated, request_actor
                )
                request.status, request.result = "approved", result
            else:
                request.status, request.result = "rejected", {"reason": args["reason"]}
            request.resolved_at = time.time()
            requesting_agent = await self._agent(session, request.agent_id, active=False)
            if requesting_agent.revoked:
                request.result = {
                    **request.result,
                    "notification_status": "undeliverable_revoked",
                }
                return {"request": self.request_view(request)}, event
            await self._enqueue(
                session,
                request.agent_id,
                Envelope(
                    type="request.resolved",
                    correlation_id=request.external_id,
                    payload={
                        "request_id": request.external_id,
                        "status": request.status,
                        "result": request.result,
                    },
                ),
            )
            return {"request": self.request_view(request)}, event
        if name in {"approve_operation", "reject_operation"}:
            approval = await session.get(Approval, args["approval_id"])
            if approval is None:
                raise OperationError("Approval not found", 404)
            if approval.status != "pending":
                return {"approval": self.approval_view(approval)}, None
            record = await session.get(OperationRecord, approval.operation_record_id)
            if record is None:
                raise OperationError("Approval has no operation record", 409)
            if name == "approve_operation":
                result, nested_event = await self._core(
                    session, approval.operation, approval.arguments, approval.requested_by
                )
                if nested_event:
                    # Persist business effect before emitting notifications.
                    asyncio.get_running_loop().call_soon(self.changed.set)
                approval.status, approval.result = "approved", result
                record.status, record.result = "completed", result
                event = (
                    "operation.approved",
                    {
                        "approval_id": approval.id,
                        "name": approval.operation,
                        "actor": approval.requested_by,
                        "result": redact(result),
                    },
                )
            else:
                approval.status = "rejected"
                approval.result = {
                    "status": "rejected",
                    "approval_id": approval.id,
                    "reason": args["reason"],
                }
                record.status, record.result = "rejected", approval.result
                event = (
                    "operation.rejected",
                    {
                        "approval_id": approval.id,
                        "name": approval.operation,
                        "actor": approval.requested_by,
                        "result": approval.result,
                    },
                )
            approval.resolved_at = time.time()
            return {"approval": self.approval_view(approval)}, event
        if name == "list_approvals":
            return {
                "approvals": [
                    self.approval_view(a)
                    for a in (
                        await session.scalars(select(Approval).order_by(Approval.created_at.desc()))
                    ).all()
                ]
            }, None
        if name == "get_config":
            return {"config": self.settings_manager.public()}, None
        if name == "update_config":
            try:
                return self.settings_manager.update(args["patch"]), None
            except ValueError as exc:
                raise OperationError(
                    "Configuration patch failed validation; previous configuration remains active",
                    422,
                ) from exc
        if name == "get_logs":
            from chiliarch.logging import tail_logs

            return {"logs": tail_logs(self.settings_manager.current.logs, args["limit"])}, None
        if name == "change_password":
            self.settings_manager.update(
                {"auth": {"password_hash": hash_password(args["password"])}}
            )
            await session.execute(delete(UserSession))
            return {"status": "changed", "sessions_invalidated": True}, None
        raise OperationError(f"Operation {name} is not a core operation", 404)

    async def pair(self, code: str, name: str, machine: str, capabilities: list[str]) -> JsonObject:
        if not name.strip() or not machine.strip() or len(name) > 256 or len(machine) > 256:
            raise OperationError(
                "Agent name and machine must be nonempty and at most 256 characters", 422
            )
        async with self.db.write_lock:
            async with self.db.sessions.begin() as session:
                redeemed = (
                    await session.execute(
                        update(PairingCode)
                        .where(
                            PairingCode.code_hash == hash_token(code),
                            PairingCode.redeemed_at.is_(None),
                            PairingCode.expires_at > time.time(),
                        )
                        .values(redeemed_at=time.time())
                        .returning(PairingCode.id)
                    )
                ).scalar_one_or_none()
                if redeemed is None:
                    raise OperationError(
                        "Pairing code is invalid, expired or already redeemed", 401
                    )
                credential = secrets.token_urlsafe(48)
                agent = Agent(
                    id=new_id(),
                    name=name,
                    machine=machine,
                    capabilities=capabilities,
                    credential_hash=hash_token(credential),
                    metadata_json={},
                    revoked=False,
                    created_at=time.time(),
                )
                session.add(agent)
                session.add(
                    AuditEvent(
                        actor=f"agent:{agent.id}",
                        action="agent.paired",
                        details={"name": name, "machine": machine},
                    )
                )
        await self.emit("agent.paired", {"agent_id": agent.id})
        return {"agent_id": agent.id, "credential": credential, "protocol_version": "1.0"}

    async def authenticate_agent(self, credential: str | None) -> Agent:
        if not credential:
            raise OperationError("Agent bearer credential required", 401)
        async with self.db.sessions() as session:
            agent = (
                await session.execute(
                    select(Agent).where(
                        Agent.credential_hash == hash_token(credential), Agent.revoked.is_(False)
                    )
                )
            ).scalar_one_or_none()
            if agent is None:
                raise OperationError("Invalid or revoked agent credential", 401)
            return agent

    async def touch_agent(self, agent_id: str, transport: str) -> None:
        async with self.db.write_lock:
            async with self.db.sessions.begin() as session:
                agent = await self._connection_agent(session, agent_id)
                agent.last_seen = time.time()
                agent.metadata_json = {**agent.metadata_json, "transport": transport}

    async def receive(self, agent_id: str, envelope: Envelope) -> JsonObject:
        if envelope.type not in AGENT_MESSAGE_TYPES and envelope.type in SERVER_MESSAGE_TYPES:
            raise OperationError("Message type is reserved for the server", 403)
        body = envelope.model_dump(mode="json")
        if (
            len(envelope.canonical().encode())
            > self.settings_manager.current.transport.max_message_bytes
        ):
            raise OperationError("Message exceeds configured size limit", 413)
        if envelope.expires_at is not None and envelope.expires_at.timestamp() < time.time():
            raise OperationError("Message has expired", 410)
        event: tuple[str, JsonObject] | None = None
        async with self.db.write_lock:
            async with self.db.sessions.begin() as session:
                agent = await self._connection_agent(session, agent_id)
                agent.last_seen = time.time()
                if envelope.type == "heartbeat":
                    # Liveness frames do not carry a business side effect. Persist
                    # the observed state, while keeping the durable inbox bounded
                    # by business traffic rather than every heartbeat tick.
                    agent.metadata_json = {
                        **agent.metadata_json,
                        # Transport is observed by the receiving endpoint,
                        # rather than supplied by agent telemetry.
                        **{
                            key: value
                            for key, value in envelope.payload["metadata"].items()
                            if key != "transport"
                        },
                        "status": envelope.payload["status"],
                    }
                    return {
                        "ack": Envelope(
                            type="ack",
                            correlation_id=envelope.id,
                            payload={"message_id": envelope.id},
                        ).model_dump(mode="json"),
                        "duplicate": False,
                    }
                if envelope.type == "ack":
                    message = (
                        await session.execute(
                            select(Message).where(
                                Message.agent_id == agent_id,
                                Message.direction == "outbox",
                                Message.message_id == envelope.payload["message_id"],
                            )
                        )
                    ).scalar_one_or_none()
                    if message is not None:
                        message.acked = True
                        await self.collaboration.delivered(session, message.message_id, agent_id)
                        if message.body.get("type") == "task.dispatch":
                            task = await session.get(Task, message.body["payload"]["task_id"])
                            if task and task.status == "queued":
                                task.status, task.updated_at = "delivered", time.time()
                    return {"ack": None, "duplicate": False}
                existing = (
                    await session.execute(
                        select(Message).where(
                            Message.agent_id == agent_id,
                            Message.direction == "inbox",
                            Message.message_id == envelope.id,
                        )
                    )
                ).scalar_one_or_none()
                if existing is not None:
                    if existing.body_hash != fingerprint(body):
                        raise OperationError("Message ID already belongs to different content", 409)
                    return {
                        "ack": Envelope(
                            type="ack",
                            correlation_id=envelope.id,
                            payload={"message_id": envelope.id},
                        ).model_dump(mode="json"),
                        "duplicate": True,
                    }
                session.add(
                    Message(
                        agent_id=agent_id,
                        direction="inbox",
                        message_id=envelope.id,
                        body=body,
                        body_hash=fingerprint(body),
                        acked=True,
                    )
                )
                if envelope.type == "task.status":
                    task = await session.get(Task, envelope.payload["task_id"])
                    if task is None or task.agent_id != agent_id:
                        raise OperationError("Task is not assigned to this agent", 403)
                    if task.status == "blocked":
                        raise OperationError(
                            "Task dependencies have not been accepted; task was not dispatched", 409
                        )
                    new_status = envelope.payload["status"]
                    terminal = {"completed", "failed", "cancelled", "expired", "interrupted"}
                    if task.status in terminal:
                        if new_status != task.status:
                            raise OperationError(
                                "A completed task cannot change execution status", 409
                            )
                    elif task.status == "running" and new_status == "accepted":
                        raise OperationError("Task status cannot move backwards", 409)
                    else:
                        task.status, task.updated_at = new_status, time.time()
                        for field in ("progress", "result", "error", "message"):
                            setattr(task, field, envelope.payload.get(field))
                        if new_status in terminal:
                            await self.coordination.terminal(session, task)
                            for message in (
                                await session.scalars(
                                    select(Message).where(
                                        Message.agent_id == agent_id,
                                        Message.direction == "outbox",
                                        Message.acked.is_(False),
                                    )
                                )
                            ).all():
                                if (
                                    message.body.get("type") in {"task.dispatch", "task.cancel"}
                                    and message.body.get("payload", {}).get("task_id") == task.id
                                ):
                                    message.acked = True
                    event = ("task.updated", {"agent_id": agent_id, "task": self.task_view(task)})
                elif envelope.type == "agent.request":
                    requested = envelope.payload
                    try:
                        arguments = self._validate(requested["operation"], requested["arguments"])
                    except OperationError as exc:
                        if exc.status_code == 404:
                            raise OperationError("Unknown requested operation", 422) from exc
                        raise
                    existing_request = (
                        await session.execute(
                            select(AgentRequest).where(
                                AgentRequest.agent_id == agent_id,
                                AgentRequest.external_id == requested["request_id"],
                            )
                        )
                    ).scalar_one_or_none()
                    if existing_request:
                        if (
                            existing_request.operation != requested["operation"]
                            or existing_request.arguments != arguments
                        ):
                            raise OperationError(
                                "Request ID already belongs to different arguments", 409
                            )
                    else:
                        request = AgentRequest(
                            id=new_id(),
                            agent_id=agent_id,
                            external_id=requested["request_id"],
                            operation=requested["operation"],
                            arguments=arguments,
                            message=requested.get("message"),
                        )
                        session.add(request)
                        await session.flush()
                        task_scope = requested.get("metadata", {}).get("task_id")
                        if (
                            task_scope
                            and request.operation in DIRECT_OPERATIONS | ARTIFACT_OPERATIONS
                        ):
                            await self.collaboration._task(session, task_scope, agent_id)
                            session.add(RequestContext(request_id=request.id, task_id=task_scope))
                            await session.flush()
                        if not await self._direct_request(session, request):
                            event = ("agent.request", {"agent_id": agent_id, "envelope": body})
                elif envelope.type == "agent.contact":
                    event = ("agent.contact", {"agent_id": agent_id, "envelope": body})
                else:
                    event = ("agent.extension", {"agent_id": agent_id, "envelope": body})
        await self.emit(
            "agent.message_received",
            {"agent_id": agent_id, "message_id": envelope.id, "type": envelope.type},
        )
        if event:
            await self.emit(*event)
        return {
            "ack": Envelope(
                type="ack", correlation_id=envelope.id, payload={"message_id": envelope.id}
            ).model_dump(mode="json"),
            "duplicate": False,
        }

    async def _direct_request(self, session: AsyncSession, request: AgentRequest) -> bool:
        """Resolve deterministic peer operations through the same validation and policy boundary."""
        if request.operation not in DIRECT_OPERATIONS | ARTIFACT_OPERATIONS:
            return False
        operation = self._policy(request.operation, request.arguments)
        if (
            request.operation not in TELEMETRY_OPERATIONS | ARTIFACT_CONTINUATIONS
            and self._approval_reason(operation, f"agent:{request.agent_id}", None)
        ):
            return False
        try:
            async with session.begin_nested():
                context = await session.get(RequestContext, request.id)
                validator = (
                    self.artifacts
                    if request.operation in ARTIFACT_OPERATIONS
                    else self.collaboration
                )
                await validator.validate_scope(
                    session,
                    request.operation,
                    request.arguments,
                    request.agent_id,
                    context.task_id if context else None,
                )
                result, _ = await self._core(
                    session, request.operation, request.arguments, f"agent:{request.agent_id}"
                )
            request.status, request.result = "approved", result
        except OperationError as exc:
            request.status, request.result = "failed", {"error": str(exc)}
        request.resolved_at = time.time()
        await self._enqueue(
            session,
            request.agent_id,
            Envelope(
                type="request.resolved",
                correlation_id=request.external_id,
                payload={
                    "request_id": request.external_id,
                    "status": request.status,
                    "result": request.result,
                },
            ),
        )
        session.add(
            AuditEvent(
                actor=f"agent:{request.agent_id}",
                action=request.operation,
                details={"request_id": request.id, "status": request.status},
            )
        )
        return True

    async def pending(self, agent_id: str) -> list[Envelope]:
        async with self.db.write_lock:
            async with self.db.sessions.begin() as session:
                await self._connection_agent(session, agent_id)
                messages = (
                    await session.scalars(
                        select(Message)
                        .where(
                            Message.agent_id == agent_id,
                            Message.direction == "outbox",
                            Message.acked.is_(False),
                            Message.expired.is_(False),
                        )
                        .order_by(Message.created_at)
                    )
                ).all()
                pending: list[Envelope] = []
                for message in messages:
                    if message.expires_at is not None and message.expires_at < time.time():
                        message.expired = True
                        if message.body.get("type") == "task.dispatch":
                            task = await session.get(Task, message.body["payload"]["task_id"])
                            if task and task.status == "queued":
                                task.status, task.updated_at = "expired", time.time()
                                await self.coordination.terminal(session, task)
                        session.add(
                            AuditEvent(
                                actor="system",
                                action="message.expired",
                                details={"agent_id": agent_id, "message_id": message.message_id},
                            )
                        )
                    else:
                        pending.append(Envelope.model_validate(message.body))
                return pending

    async def state(self) -> JsonObject:
        async with self.db.sessions() as session:
            return {
                "agents": [
                    self.agent_view(a)
                    for a in (
                        await session.scalars(select(Agent).order_by(Agent.created_at.desc()))
                    ).all()
                ],
                "tasks": [
                    self.task_view(t)
                    for t in (
                        await session.scalars(select(Task).order_by(Task.created_at.desc()))
                    ).all()
                ],
                "approvals": [
                    self.approval_view(a)
                    for a in (
                        await session.scalars(select(Approval).order_by(Approval.created_at.desc()))
                    ).all()
                ],
                "requests": [
                    self.request_view(r)
                    for r in (
                        await session.scalars(
                            select(AgentRequest).order_by(AgentRequest.created_at.desc())
                        )
                    ).all()
                ],
                "goals": [
                    goal_view(goal)
                    for goal in (
                        await session.scalars(select(Goal).order_by(Goal.created_at.desc()))
                    ).all()
                ],
                "task_links": [
                    task_link_view(link) for link in (await session.scalars(select(TaskLink))).all()
                ],
                "config": self.settings_manager.public(),
                "runtime": {
                    "config_revision": self.settings_manager.revision,
                    "pending_restart": self.settings_manager.pending_restart,
                    "pending_restart_values": self.settings_manager.pending_restart_values,
                    "last_config_error": self.settings_manager.last_error,
                    "protocol_version": "1.0",
                },
            }
