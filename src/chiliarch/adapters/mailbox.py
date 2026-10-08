"""Durable local task mailbox used by interactive MCP hosts and managed runners."""

from __future__ import annotations

import asyncio
import json
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import uuid4

from chiliarch.client import ClientStore
from chiliarch.protocol import Envelope

JsonObject = dict[str, Any]


class AgentMailbox:
    def __init__(
        self, store: ClientStore, *, manual_tasks: bool = True, managed_task: str | None = None
    ) -> None:
        self.store = store
        self.manual_tasks = manual_tasks
        self.managed_task = managed_task

    @staticmethod
    def _wait(value: float) -> None:
        if not 0 <= value <= 30:
            raise ValueError("wait_seconds must be between 0 and 30")

    @staticmethod
    def _report(command: Envelope, status: str, **fields: Any) -> Envelope:
        return Envelope(
            type="task.status",
            correlation_id=command.correlation_id or command.payload["task_id"],
            payload={"task_id": command.payload["task_id"], "status": status, **fields},
        )

    async def _task(self, task_id: str) -> tuple[str, Envelope]:
        tasks = await self.store.tasks(task_id)
        if not tasks:
            raise ValueError("Task not found in this agent's mailbox")
        return tasks[0]

    async def status(self) -> JsonObject:
        identity = await self.store.identity()
        # Credentials never become MCP results or model context.
        return {
            "agent_id": identity.get("agent_id"),
            "name": identity.get("name"),
            "machine": identity.get("machine"),
            "server_url": identity.get("server_url"),
            "mode": "standalone" if self.manual_tasks else "mailbox",
            "managed_task": self.managed_task,
            "pending_outbox": len(await self.store.pending()),
            "connection": "owned_by_host" if self.manual_tasks else "owned_by_runner",
        }

    async def list_tasks(self, status: str | None = None) -> JsonObject:
        return {
            "tasks": [
                {
                    **command.payload,
                    "status": state,
                    "expires_at": command.expires_at.isoformat() if command.expires_at else None,
                }
                for state, command in await self.store.tasks()
                if (status is None or state == status)
                and (self.managed_task is None or command.payload["task_id"] == self.managed_task)
            ]
        }

    async def read_messages(self, after: int = 0, limit: int = 100) -> JsonObject:
        messages = await self.store.messages(after, limit)
        return {
            "messages": [
                {"seq": seq, **envelope.model_dump(mode="json")} for seq, envelope in messages
            ],
            "cursor": messages[-1][0] if messages else after,
        }

    async def claim_task(self, task_id: str | None = None, wait_seconds: float = 0) -> JsonObject:
        if not self.manual_tasks:
            raise ValueError("Managed runner owns task execution")
        self._wait(wait_seconds)
        deadline = time.monotonic() + wait_seconds
        while True:
            for state, command in await self.store.tasks(task_id):
                if state != "queued":
                    if task_id is not None:
                        raise ValueError(f"Task is already {state}; do not execute it again")
                    continue
                candidate = str(command.payload["task_id"])
                if command.expires_at and command.expires_at <= datetime.now(UTC):
                    try:
                        await self.store.transition(
                            candidate,
                            {"queued"},
                            "failed",
                            [
                                self._report(
                                    command, "failed", error="Command expired before execution"
                                )
                            ],
                        )
                    except ValueError:
                        continue
                    continue
                try:
                    await self.store.transition(
                        candidate,
                        {"queued"},
                        "running",
                        [
                            self._report(command, "accepted"),
                            self._report(command, "running", progress=0),
                        ],
                    )
                except ValueError:
                    if task_id is not None:
                        raise
                    continue
                return {"status": "claimed", "task": command.payload}
            if time.monotonic() >= deadline:
                return {"status": "empty"}
            await asyncio.sleep(min(0.1, max(0, deadline - time.monotonic())))

    async def report_progress(self, task_id: str, progress: float, message: str = "") -> JsonObject:
        if not self.manual_tasks:
            raise ValueError("Managed runner owns task progress")
        _, command = await self._task(task_id)
        report = self._report(command, "running", progress=progress, message=message)
        await self.store.transition(task_id, {"running"}, "running", [report])
        return {"status": "queued", "message_id": report.id}

    async def _finish(self, task_id: str, status: str, **fields: Any) -> JsonObject:
        if not self.manual_tasks:
            raise ValueError("Managed runner owns task completion")
        _, command = await self._task(task_id)
        report = self._report(command, status, **fields)
        outcome = json.dumps(report.payload, sort_keys=True, ensure_ascii=False, allow_nan=False)
        changed = await self.store.transition(
            task_id,
            {"cancelling"} if status == "cancelled" else {"running"},
            status,
            [report],
            outcome=outcome,
        )
        return {"status": status, "delivery": "queued", "duplicate": not changed}

    async def complete_task(self, task_id: str, result: JsonObject) -> JsonObject:
        return await self._finish(task_id, "completed", progress=1, result=result)

    async def fail_task(self, task_id: str, error: str) -> JsonObject:
        if not error.strip():
            raise ValueError("Provide a reason for the failed task")
        return await self._finish(task_id, "failed", error=error)

    async def confirm_cancellation(self, task_id: str) -> JsonObject:
        """Confirm that the interactive host has actually stopped cancelled work."""
        return await self._finish(task_id, "cancelled")

    async def send_message(
        self, message: str, task_id: str | None = None, correlation_id: str | None = None
    ) -> JsonObject:
        if self.managed_task is not None:
            if task_id is not None and task_id != self.managed_task:
                raise ValueError("Cannot report on another managed task")
            task_id = self.managed_task
        if task_id is not None:
            state, _ = await self._task(task_id)
            if self.managed_task is not None and state != "running":
                raise ValueError("Managed task is no longer running")
        envelope = Envelope(
            type="agent.contact",
            correlation_id=correlation_id,
            payload={"message": message, "metadata": {"task_id": task_id} if task_id else {}},
        )
        await self.store.enqueue(envelope)
        return {"status": "queued", "message_id": envelope.id}

    async def request_operation(
        self, operation: str, arguments: JsonObject, message: str = "", wait_seconds: float = 0
    ) -> JsonObject:
        self._wait(wait_seconds)
        if self.managed_task is not None:
            state, _ = await self._task(self.managed_task)
            if state != "running":
                raise ValueError("Managed task is no longer running")
        request_id = str(uuid4())
        envelope = Envelope(
            type="agent.request",
            correlation_id=request_id,
            payload={
                "request_id": request_id,
                "operation": operation,
                "arguments": arguments,
                "message": message,
                **({"metadata": {"task_id": self.managed_task}} if self.managed_task else {}),
            },
        )
        await self.store.enqueue(envelope)
        return await self.get_request_result(request_id, wait_seconds)

    async def get_request_result(self, request_id: str, wait_seconds: float = 0) -> JsonObject:
        self._wait(wait_seconds)
        deadline = time.monotonic() + wait_seconds
        while True:
            # A response remains durable after its transport ACK and after restarts.
            envelope = await self.store.request_result(request_id)
            if envelope is not None:
                return {"request_id": request_id, **envelope.payload}
            if time.monotonic() >= deadline:
                return {"status": "pending", "request_id": request_id}
            await asyncio.sleep(min(0.1, max(0, deadline - time.monotonic())))

    async def list_agents(self, wait_seconds: float = 10) -> JsonObject:
        return await self.request_operation(
            "list_agents", {}, "Discover agents for coordination", wait_seconds
        )


async def open_mailbox(state_path: Path, *, managed_task: str | None = None) -> AgentMailbox:
    if not await asyncio.to_thread(state_path.is_file):
        raise ValueError("Pair the agent before opening its mailbox")
    store = ClientStore(state_path)
    await store.open()
    try:
        identity = await store.identity()
        if not identity.get("credential") or not identity.get("server_url"):
            raise ValueError("Mailbox has no paired identity")
        mailbox = AgentMailbox(store, manual_tasks=False, managed_task=managed_task)
        if managed_task is not None:
            state, _ = await mailbox._task(managed_task)
            if state != "running":
                raise ValueError("Managed task is not running")
        return mailbox
    except BaseException:
        await store.close()
        raise
