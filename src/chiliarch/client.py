"""Outbound reference client; adapters supply a task handler, never a listening port."""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import json
import logging
import os
import platform
import random
import time
from collections.abc import AsyncIterator, Awaitable, Callable, Coroutine
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal
from urllib.parse import urlsplit, urlunsplit

import aiosqlite
import httpx
from websockets.asyncio.client import connect
from websockets.exceptions import ConnectionClosed, InvalidStatus

from chiliarch.config import TransportSettings
from chiliarch.delivery import DeliveryBackoff
from chiliarch.protocol import Envelope

JsonObject = dict[str, Any]
TaskHandler = Callable[[JsonObject, Callable[[float, str], Awaitable[None]]], Awaitable[Any]]
RepositoryHandler = Callable[[JsonObject], Awaitable[JsonObject]]
RuntimeMetadata = Callable[[], JsonObject]
Sender = Callable[[Envelope], Awaitable[None]]
logger = logging.getLogger("chiliarch.client")


class CredentialRejected(RuntimeError):
    """Authentication failures stop the client instead of endless downgrade loops."""


class ProtocolRejected(ValueError):
    """Permanent protocol failures require correcting the request or adapter."""


class TaskExecutionError(RuntimeError):
    """Trusted adapters may expose a bounded diagnostic without native secrets."""


class UpgradeTransport(Exception):
    pass


class ClientStore:
    """Durable inbox, outbox and task ledger shared by all three transports."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self.connection: aiosqlite.Connection | None = None
        self.lock = asyncio.Lock()
        self.changed = asyncio.Event()

    async def open(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.connection = await aiosqlite.connect(self.path)
        os.chmod(self.path, 0o600)
        await self.connection.executescript("""
            PRAGMA journal_mode=WAL;
            CREATE TABLE IF NOT EXISTS identity (key TEXT PRIMARY KEY, value TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS inbox (id TEXT PRIMARY KEY, fingerprint TEXT NOT NULL, body TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS outbox (id TEXT PRIMARY KEY, body TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS work (task_id TEXT PRIMARY KEY, state TEXT NOT NULL, body TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS work_results (task_id TEXT PRIMARY KEY, body TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS cancellations (task_id TEXT PRIMARY KEY, body TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS repository_work (
                command_id TEXT PRIMARY KEY, state TEXT NOT NULL, body TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS native_deliveries (
                task_id TEXT NOT NULL, message_id TEXT NOT NULL, request_id TEXT NOT NULL,
                state TEXT NOT NULL, PRIMARY KEY(task_id,message_id)
            );
            CREATE TABLE IF NOT EXISTS local_requests (
                request_id TEXT PRIMARY KEY, client_request_id TEXT UNIQUE,
                task_id TEXT, body TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS message_cursors (scope TEXT PRIMARY KEY, cursor INTEGER NOT NULL);
            INSERT OR IGNORE INTO local_requests(request_id,task_id,body)
                SELECT json_extract(body,'$.payload.request_id'),
                       json_extract(body,'$.payload.metadata.task_id'),body FROM outbox
                WHERE json_extract(body,'$.type')='agent.request';
        """)
        await self.connection.commit()

    def db(self) -> aiosqlite.Connection:
        if self.connection is None:
            raise RuntimeError("Client store is not open")
        return self.connection

    @staticmethod
    async def _settle(operation: Coroutine[Any, Any, None]) -> None:
        """SQLite's worker keeps executing even when its awaiting task is cancelled."""
        cleanup = asyncio.create_task(operation)
        interrupted = False
        while not cleanup.done():
            try:
                await asyncio.shield(cleanup)
            except asyncio.CancelledError:
                interrupted = True
        cleanup.result()
        if interrupted:
            raise asyncio.CancelledError

    @asynccontextmanager
    async def _transaction(self) -> AsyncIterator[None]:
        """Finish commit/rollback before releasing the connection to another owner."""
        async with self.lock:
            try:
                await self.db().execute("BEGIN IMMEDIATE")
                yield
                await self._settle(self._commit())
            except BaseException:
                await self._settle(self.db().rollback())
                raise

    async def _commit(self) -> None:
        await self.db().commit()
        # Keep notification inside the settled operation: cancellation can arrive
        # while SQLite commits, but a durable result must still wake the writer.
        self.changed.set()

    async def identity(self) -> dict[str, str]:
        async with self.lock:
            async with self.db().execute("SELECT key,value FROM identity") as cursor:
                return {str(row[0]): str(row[1]) for row in await cursor.fetchall()}

    async def save_identity(self, identity: dict[str, str]) -> None:
        async with self._transaction():
            await self.db().executemany(
                "INSERT OR REPLACE INTO identity VALUES (?,?)", list(identity.items())
            )

    async def enqueue(self, envelope: Envelope) -> None:
        if envelope.type == "agent.request":
            await self.enqueue_request(envelope)
            return
        async with self._transaction():
            await self.db().execute(
                "INSERT OR IGNORE INTO outbox VALUES (?,?)", (envelope.id, envelope.canonical())
            )

    async def enqueue_request(
        self, envelope: Envelope, client_request_id: str | None = None
    ) -> str:
        """Persist request identity and task scope even after its outbox ACK is removed."""
        if envelope.type != "agent.request":
            raise ValueError("Expected an agent request")
        if client_request_id is not None and (
            not client_request_id.strip() or len(client_request_id) > 128
        ):
            raise ValueError("client_request_id must contain 1 to 128 characters")
        async with self._transaction():
            if client_request_id is not None:
                async with self.db().execute(
                    "SELECT request_id,body FROM local_requests WHERE client_request_id=?",
                    (client_request_id,),
                ) as cursor:
                    existing = await cursor.fetchone()
                if existing:
                    original = Envelope.model_validate_json(existing[1])
                    old_payload = {k: v for k, v in original.payload.items() if k != "request_id"}
                    new_payload = {k: v for k, v in envelope.payload.items() if k != "request_id"}
                    if json.dumps(old_payload, sort_keys=True) != json.dumps(
                        new_payload, sort_keys=True
                    ):
                        raise ValueError("client_request_id already belongs to different arguments")
                    return str(existing[0])
            request_id = str(envelope.payload["request_id"])
            body = envelope.canonical()
            async with self.db().execute(
                "SELECT body FROM local_requests WHERE request_id=?", (request_id,)
            ) as cursor:
                existing_request = await cursor.fetchone()
            if existing_request and existing_request[0] != body:
                raise ValueError("The same request ID has conflicting content")
            await self.db().execute(
                "INSERT OR IGNORE INTO local_requests VALUES (?,?,?,?)",
                (
                    request_id,
                    client_request_id,
                    envelope.payload.get("metadata", {}).get("task_id"),
                    body,
                ),
            )
            await self.db().execute(
                "INSERT OR IGNORE INTO outbox VALUES (?,?)", (envelope.id, body)
            )
            return request_id

    async def request_belongs_to(self, request_id: str, task_id: str) -> bool:
        async with self.lock:
            async with self.db().execute(
                "SELECT 1 FROM local_requests WHERE request_id=? AND task_id=?",
                (request_id, task_id),
            ) as cursor:
                return await cursor.fetchone() is not None

    async def pending(self) -> list[Envelope]:
        async with self.lock:
            async with self.db().execute("SELECT body FROM outbox ORDER BY rowid") as cursor:
                return [Envelope.model_validate_json(row[0]) for row in await cursor.fetchall()]

    async def acknowledge(self, message_id: str) -> None:
        async with self._transaction():
            await self.db().execute("DELETE FROM outbox WHERE id=?", (message_id,))

    async def accept(self, envelope: Envelope) -> bool:
        body = envelope.canonical()
        digest = hashlib.sha256(body.encode()).hexdigest()
        async with self._transaction():
            async with self.db().execute(
                "SELECT fingerprint FROM inbox WHERE id=?", (envelope.id,)
            ) as cursor:
                existing = await cursor.fetchone()
            if existing:
                if existing[0] != digest:
                    raise ValueError("The same message ID has conflicting content")
                return False
            if envelope.type == "task.dispatch":
                task_id = envelope.payload["task_id"]
                async with self.db().execute(
                    "SELECT body FROM work WHERE task_id=?", (task_id,)
                ) as cursor:
                    work = await cursor.fetchone()
                if work and Envelope.model_validate_json(work[0]).payload != envelope.payload:
                    raise ValueError("The same task ID has conflicting instructions")
                async with self.db().execute(
                    "SELECT task_id FROM cancellations WHERE task_id=?", (task_id,)
                ) as cursor:
                    cancelled = await cursor.fetchone()
                await self.db().execute(
                    "INSERT OR IGNORE INTO work VALUES (?,?,?)",
                    (task_id, "cancelled" if cancelled else "queued", body),
                )
            elif envelope.type == "repository.command":
                command_id = envelope.payload["command_id"]
                async with self.db().execute(
                    "SELECT body FROM repository_work WHERE command_id=?", (command_id,)
                ) as cursor:
                    prior = await cursor.fetchone()
                if prior and Envelope.model_validate_json(prior[0]).payload != envelope.payload:
                    raise ValueError("The same repository command ID has conflicting arguments")
                await self.db().execute(
                    "INSERT OR IGNORE INTO repository_work VALUES (?,?,?)",
                    (command_id, "queued", body),
                )
            elif envelope.type == "task.cancel":
                task_id = envelope.payload["task_id"]
                await self.db().execute(
                    "INSERT OR IGNORE INTO cancellations VALUES (?,?)", (task_id, body)
                )
                async with self.db().execute(
                    "SELECT state FROM work WHERE task_id=?", (task_id,)
                ) as cursor:
                    work_state = await cursor.fetchone()
                if work_state is not None and work_state[0] == "running":
                    # ACK confirms durable cancellation intent, not process termination.
                    await self.db().execute(
                        "UPDATE work SET state='cancelling' WHERE task_id=?", (task_id,)
                    )
                elif work_state is None or work_state[0] == "queued":
                    await self.db().execute(
                        "UPDATE work SET state='cancelled' WHERE task_id=?", (task_id,)
                    )
                    report = Envelope(
                        type="task.status",
                        correlation_id=envelope.correlation_id or task_id,
                        payload={"task_id": task_id, "status": "cancelled"},
                    )
                    await self.db().execute(
                        "INSERT OR IGNORE INTO outbox VALUES (?,?)",
                        (report.id, report.canonical()),
                    )
            await self.db().execute("INSERT INTO inbox VALUES (?,?,?)", (envelope.id, digest, body))
        return True

    async def repository_work(self) -> list[Envelope]:
        async with self.lock:
            async with self.db().execute(
                "SELECT body FROM repository_work WHERE state IN ('queued','running') ORDER BY rowid"
            ) as cursor:
                return [Envelope.model_validate_json(row[0]) for row in await cursor.fetchall()]

    async def native_response(self, interaction_id: str, task_id: str) -> JsonObject | None:
        """Only a response for this native request/task may resume its local process."""
        async with self.lock:
            async with self.db().execute(
                "SELECT body FROM inbox WHERE json_extract(body,'$.type')='native.response' "
                "AND json_extract(body,'$.payload.interaction_id')=? "
                "AND json_extract(body,'$.payload.task_id')=? ORDER BY rowid DESC LIMIT 1",
                (interaction_id, task_id),
            ) as cursor:
                row = await cursor.fetchone()
                if row is None:
                    return None
                envelope = Envelope.model_validate_json(row[0])
                if envelope.expires_at and envelope.expires_at <= datetime.now(UTC):
                    return {**envelope.payload, "status": "expired", "answer": {}}
                return envelope.payload

    async def claim_native_delivery(self, task_id: str, message_id: str, request_id: str) -> bool:
        """Journal uncertain delivery before native stdin; an uncertain send is never repeated."""
        async with self._transaction():
            async with self.db().execute(
                "INSERT OR IGNORE INTO native_deliveries VALUES (?,?,?,'unknown')",
                (task_id, message_id, request_id),
            ) as cursor:
                return cursor.rowcount == 1

    async def complete_native_delivery(self, task_id: str, message_id: str) -> None:
        async with self._transaction():
            await self.db().execute(
                "UPDATE native_deliveries SET state='delivered' WHERE task_id=? AND message_id=?",
                (task_id, message_id),
            )

    async def native_deliveries(self, task_id: str) -> list[JsonObject]:
        async with self.lock:
            async with self.db().execute(
                "SELECT message_id,request_id,state FROM native_deliveries WHERE task_id=? ORDER BY rowid",
                (task_id,),
            ) as cursor:
                return [
                    {
                        "task_id": task_id,
                        "message_id": row[0],
                        "request_id": row[1],
                        "state": row[2],
                    }
                    for row in await cursor.fetchall()
                ]

    async def repository_transition(
        self, command_id: str, state: str, report: Envelope | None = None
    ) -> None:
        async with self._transaction():
            await self.db().execute(
                "UPDATE repository_work SET state=? WHERE command_id=?", (state, command_id)
            )
            if report is not None:
                await self.db().execute(
                    "INSERT OR IGNORE INTO outbox VALUES (?,?)", (report.id, report.canonical())
                )

    async def work(self, state: str) -> list[Envelope]:
        async with self.lock:
            async with self.db().execute("SELECT body FROM work WHERE state=?", (state,)) as cursor:
                return [Envelope.model_validate_json(row[0]) for row in await cursor.fetchall()]

    async def tasks(
        self, task_id: str | None = None, *, unfinished: bool = False
    ) -> list[tuple[str, Envelope]]:
        async with self.lock:
            query = "SELECT state,body FROM work"
            parameters: tuple[str, ...] = ()
            clauses: list[str] = []
            if task_id is not None:
                clauses.append("task_id=?")
                parameters = (task_id,)
            if unfinished:
                clauses.append("state IN ('queued','running','cancelling')")
            if clauses:
                query += " WHERE " + " AND ".join(clauses)
            query += " ORDER BY rowid"
            async with self.db().execute(query, parameters) as cursor:
                return [
                    (str(row[0]), Envelope.model_validate_json(row[1]))
                    for row in await cursor.fetchall()
                ]

    async def messages(
        self, after: int = 0, limit: int = 100, *, task_id: str | None = None
    ) -> list[tuple[int, Envelope]]:
        """Expose durable business replies with a local cursor, never transport ACKs."""
        if after < 0 or not 1 <= limit <= 1000:
            raise ValueError("Invalid message cursor or limit")
        query = """SELECT rowid,body FROM inbox WHERE rowid>?
            AND json_extract(body,'$.type') IN
                ('agent.message','request.resolved','task.cancel','collaboration.message')"""
        parameters: list[str | int] = [after]
        if task_id is not None:
            scopes = await self._peer_message_scopes(task_id)
            placeholders = ",".join("?" for _ in scopes)
            query += """ AND (
                json_extract(body,'$.payload.task_id')=? OR
                json_extract(body,'$.payload.metadata.task_id')=? OR
                (json_extract(body,'$.type')='request.resolved' AND EXISTS (
                    SELECT 1 FROM local_requests r
                    WHERE r.request_id=json_extract(inbox.body,'$.payload.request_id')
                    AND r.task_id=?))"""
            parameters.extend([task_id, task_id, task_id])
            query += (
                " OR (json_extract(body,'$.type')='collaboration.message' "
                f"AND json_extract(body,'$.payload.task_id') IN ({placeholders})))"
            )
            parameters.extend(scopes)
        query += " ORDER BY rowid LIMIT ?"
        parameters.append(limit)
        async with self.lock:
            async with self.db().execute(query, parameters) as cursor:
                return [
                    (int(row[0]), Envelope.model_validate_json(row[1]))
                    for row in await cursor.fetchall()
                ]

    async def _peer_message_scopes(self, task_id: str) -> list[str]:
        """A continuation inherits late peer replies to completed ancestors, not sibling work."""
        scopes = [task_id]
        async with self.lock:
            while True:
                async with self.db().execute(
                    "SELECT body FROM work WHERE task_id=?", (scopes[-1],)
                ) as cursor:
                    current = await cursor.fetchone()
                if current is None:
                    break
                command = Envelope.model_validate_json(current[0])
                context = command.payload.get("parameters", {}).get("chiliarch_context", {})
                parent = context.get("continue_task_id") if isinstance(context, dict) else None
                if not isinstance(parent, str) or parent in scopes:
                    break
                async with self.db().execute(
                    "SELECT state FROM work WHERE task_id=?", (parent,)
                ) as cursor:
                    previous = await cursor.fetchone()
                if previous is None or previous[0] != "completed":
                    break
                scopes.append(parent)
        return scopes

    async def message_cursor(self, task_id: str | None = None) -> int:
        async with self.lock:
            async with self.db().execute(
                "SELECT cursor FROM message_cursors WHERE scope=?",
                ("agent" if task_id is None else "task:" + task_id,),
            ) as cursor:
                row = await cursor.fetchone()
                return int(row[0]) if row else 0

    async def acknowledge_messages(self, cursor: int, task_id: str | None = None) -> int:
        """Advance a local consumer only after it explicitly confirms receiving the page."""
        if cursor < 0:
            raise ValueError("Invalid message cursor")
        if cursor:
            found = await self.messages(cursor - 1, 1, task_id=task_id)
            if not found or found[0][0] != cursor:
                raise ValueError("Message cursor does not belong to this mailbox")
        scope = "agent" if task_id is None else "task:" + task_id
        async with self._transaction():
            await self.db().execute(
                """INSERT INTO message_cursors VALUES (?,?) ON CONFLICT(scope)
                   DO UPDATE SET cursor=MAX(cursor,excluded.cursor)""",
                (scope, cursor),
            )
            async with self.db().execute(
                "SELECT cursor FROM message_cursors WHERE scope=?", (scope,)
            ) as result:
                row = await result.fetchone()
                assert row is not None
                return int(row[0])

    async def request_result(self, request_id: str) -> Envelope | None:
        async with self.lock:
            async with self.db().execute("SELECT body FROM inbox ORDER BY rowid DESC") as cursor:
                rows = await cursor.fetchall()
        for (body,) in rows:
            envelope = Envelope.model_validate_json(body)
            if envelope.type == "request.resolved" and envelope.payload["request_id"] == request_id:
                return envelope
        return None

    async def transition(
        self,
        task_id: str,
        expected: set[str],
        state: str,
        reports: list[Envelope],
        *,
        outcome: str | None = None,
    ) -> bool:
        """Atomically reserve/finish manual work across multiple MCP processes."""
        async with self._transaction():
            async with self.db().execute(
                "SELECT state FROM work WHERE task_id=?", (task_id,)
            ) as cursor:
                row = await cursor.fetchone()
            if row is None:
                raise ValueError("Task not found")
            if row[0] not in expected:
                if outcome is not None:
                    async with self.db().execute(
                        "SELECT body FROM work_results WHERE task_id=?", (task_id,)
                    ) as cursor:
                        saved = await cursor.fetchone()
                    if saved and saved[0] == outcome:
                        return False
                raise ValueError(f"Task cannot transition from {row[0]} to {state}")
            await self.db().execute("UPDATE work SET state=? WHERE task_id=?", (state, task_id))
            if outcome is not None:
                await self.db().execute("INSERT INTO work_results VALUES (?,?)", (task_id, outcome))
            for report in reports:
                await self.db().execute(
                    "INSERT OR IGNORE INTO outbox VALUES (?,?)",
                    (report.id, report.canonical()),
                )
            return True

    async def state(self, task_id: str, state: str, report: Envelope | None = None) -> None:
        async with self._transaction():
            await self.db().execute("UPDATE work SET state=? WHERE task_id=?", (state, task_id))
            if report:
                await self.db().execute(
                    "INSERT OR IGNORE INTO outbox VALUES (?,?)", (report.id, report.canonical())
                )

    async def close(self) -> None:
        if self.connection:
            await self.connection.close()
            self.connection = None


class AgentClient:
    def __init__(
        self,
        server_url: str,
        credential: str,
        state_path: Path,
        handler: TaskHandler | None = None,
        *,
        options: TransportSettings | None = None,
        max_concurrent_tasks: int = 1,
        manual_tasks: bool = False,
        runtime_metadata: RuntimeMetadata | None = None,
        repository_handler: RepositoryHandler | None = None,
    ) -> None:
        parsed = urlsplit(server_url)
        if (
            parsed.scheme not in {"http", "https"}
            or not parsed.netloc
            or parsed.username
            or parsed.password
            or parsed.query
            or parsed.fragment
        ):
            raise ValueError(
                "server_url must be an HTTP(S) URL without credentials, query or fragment"
            )
        if max_concurrent_tasks < 1:
            raise ValueError("max_concurrent_tasks must be positive")
        self.server_url = server_url.rstrip("/")
        self.credential = credential
        self.store = ClientStore(state_path)
        self.handler = handler
        self.manual_tasks = manual_tasks
        self.runtime_metadata = runtime_metadata
        self.repository_handler = repository_handler
        self.max_concurrent_tasks = max_concurrent_tasks
        self.options = options or TransportSettings()
        self.transport: str | None = None
        self.transport_history: list[str] = []
        self.stop_event = asyncio.Event()
        self.ready = asyncio.Event()
        self._workers: dict[str, asyncio.Task[None]] = {}
        self._repository_workers: dict[str, asyncio.Task[None]] = {}
        self._repository_semaphore = asyncio.Semaphore(1)
        self._cancelled_tasks: set[str] = set()
        self._semaphore = asyncio.Semaphore(max_concurrent_tasks)
        self._http: httpx.AsyncClient | None = None

    @classmethod
    async def pair(
        cls,
        server_url: str,
        code: str,
        name: str,
        machine: str,
        state_path: Path,
        *,
        capabilities: list[str] | None = None,
        handler: TaskHandler | None = None,
    ) -> AgentClient:
        async with httpx.AsyncClient() as http:
            response = await http.post(
                server_url.rstrip("/") + "/api/agent/pair",
                json={
                    "code": code,
                    "name": name,
                    "machine": machine,
                    "capabilities": capabilities or [],
                },
            )
            response.raise_for_status()
            identity = response.json()
        client = cls(server_url, str(identity["credential"]), state_path, handler)
        await client.store.open()
        try:
            await client.store.save_identity(
                {
                    "server_url": client.server_url,
                    "credential": client.credential,
                    "agent_id": str(identity["agent_id"]),
                    "name": name,
                    "machine": machine,
                }
            )
        finally:
            await client.store.close()
        return client

    @classmethod
    async def from_state(
        cls,
        state_path: Path,
        handler: TaskHandler | None = None,
        *,
        options: TransportSettings | None = None,
        max_concurrent_tasks: int = 1,
        manual_tasks: bool = False,
        runtime_metadata: RuntimeMetadata | None = None,
        repository_handler: RepositoryHandler | None = None,
    ) -> AgentClient:
        if not await asyncio.to_thread(state_path.exists):
            raise ValueError("Pair the client before running it")
        store = ClientStore(state_path)
        await store.open()
        try:
            identity = await store.identity()
        finally:
            await store.close()
        if "credential" not in identity or "server_url" not in identity:
            raise ValueError("Client state does not contain a paired identity")
        return cls(
            identity["server_url"],
            identity["credential"],
            state_path,
            handler,
            options=options,
            max_concurrent_tasks=max_concurrent_tasks,
            manual_tasks=manual_tasks,
            runtime_metadata=runtime_metadata,
            repository_handler=repository_handler,
        )

    async def execution_status(self) -> JsonObject:
        """Report observable execution state; a live connection is not native readiness."""
        tasks = await self.store.tasks(unfinished=True)
        active = [
            str(command.payload["task_id"])
            for state, command in tasks
            if state in {"running", "cancelling"}
        ]
        metadata: JsonObject = {
            "client": "chiliarch-reference",
            "execution_mode": "manual" if self.manual_tasks else "managed",
            "state": "busy" if active else "waiting_for_claim" if self.manual_tasks else "idle",
            "active_task_ids": active,
            "queued_tasks": sum(state == "queued" for state, _ in tasks),
            "capacity": self.max_concurrent_tasks if not self.manual_tasks else None,
            "adapter": None,
            "workspace": None,
            "supports_repository_commands": self.repository_handler is not None,
            "operating_system": platform.system().lower(),
        }
        if self.handler is None and not self.manual_tasks:
            metadata.update(state="blocked", blocked_reason="handler_unavailable", capacity=0)
        provider = self.runtime_metadata or getattr(self.handler, "runtime_metadata", None)
        if callable(provider):
            # Only a trusted local adapter supplies this optional callback. Runtime
            # metadata must never contain credentials or native authentication output.
            metadata.update(provider())
        return metadata

    async def heartbeat(self) -> Envelope:
        metadata = await self.execution_status()
        return Envelope(
            type="heartbeat", payload={"status": metadata["state"], "metadata": metadata}
        )

    def _status(self, command: Envelope, status: str, **fields: Any) -> Envelope:
        return Envelope(
            type="task.status",
            correlation_id=command.correlation_id or command.payload["task_id"],
            payload={"task_id": command.payload["task_id"], "status": status, **fields},
        )

    async def contact(
        self,
        message: str,
        metadata: JsonObject | None = None,
        *,
        correlation_id: str | None = None,
    ) -> str:
        envelope = Envelope(
            type="agent.contact",
            correlation_id=correlation_id,
            payload={"message": message, "metadata": metadata or {}},
        )
        await self.store.enqueue(envelope)
        return envelope.id

    async def request(self, operation: str, arguments: JsonObject, message: str = "") -> str:
        from uuid import uuid4

        request_id = str(uuid4())
        await self.store.enqueue(
            Envelope(
                type="agent.request",
                correlation_id=request_id,
                payload={
                    "request_id": request_id,
                    "operation": operation,
                    "arguments": arguments,
                    "message": message,
                },
            )
        )
        return request_id

    async def _accept(self, envelope: Envelope, sender: Sender) -> None:
        if envelope.type == "ack":
            await self.store.acknowledge(str(envelope.payload["message_id"]))
            return
        fresh = await self.store.accept(envelope)
        # Persistence precedes ACK. The transport cursor is never a business ACK.
        await sender(
            Envelope(type="ack", correlation_id=envelope.id, payload={"message_id": envelope.id})
        )
        if not fresh:
            return
        if envelope.type == "task.cancel":
            task_id = str(envelope.payload["task_id"])
            worker = self._workers.get(task_id)
            if worker:
                self._cancelled_tasks.add(task_id)
                worker.cancel()
        else:
            logger.info(
                "Agent received a message",
                extra={"event": envelope.type, "message_id": envelope.id},
            )

    async def _schedule(self) -> None:
        for command in await self.store.repository_work():
            command_id = str(command.payload["command_id"])
            if command_id not in self._repository_workers:
                self._repository_workers[command_id] = asyncio.create_task(
                    self._execute_repository(command), name=f"agent.repository.{command_id}"
                )
        if self.manual_tasks:
            return
        for command in await self.store.work("queued"):
            task_id = str(command.payload["task_id"])
            if task_id not in self._workers:
                self._workers[task_id] = asyncio.create_task(
                    self._execute(command), name=f"agent.task.{task_id}"
                )

    async def _execute_repository(self, command: Envelope) -> None:
        """Handlers must journal side effects: a restarted running command is reconciled."""
        command_id = str(command.payload["command_id"])
        try:
            async with self._repository_semaphore:
                await self.store.repository_transition(command_id, "running")
                if self.repository_handler is None:
                    payload = {
                        "command_id": command_id,
                        "status": "failed",
                        "result": {},
                        "error": "Repository execution is unavailable on this client",
                    }
                elif command.expires_at and command.expires_at <= datetime.now(UTC):
                    payload = {
                        "command_id": command_id,
                        "status": "unknown",
                        "result": {},
                        "error": "Command expired; reconcile before issuing further work",
                    }
                else:
                    try:
                        payload = await self.repository_handler(command.payload)
                        if payload.get("command_id") != command_id:
                            raise ValueError("Repository handler returned a different identity")
                        # Validate before committing the terminal state and result together.
                        Envelope(type="repository.result", payload=payload)
                    except asyncio.CancelledError:
                        raise
                    except Exception as error:
                        payload = {
                            "command_id": command_id,
                            "status": "unknown",
                            "result": {},
                            "error": f"Repository executor stopped ({type(error).__name__}); reconcile first",
                        }
                report = Envelope(
                    type="repository.result", correlation_id=command.id, payload=payload
                )
                await self.store.repository_transition(command_id, str(payload["status"]), report)
        finally:
            self._repository_workers.pop(command_id, None)

    async def _execute(self, command: Envelope) -> None:
        task_id = str(command.payload["task_id"])
        try:
            async with self._semaphore:
                if command.expires_at and command.expires_at <= datetime.now(UTC):
                    with contextlib.suppress(ValueError):
                        await self.store.transition(
                            task_id,
                            {"queued"},
                            "failed",
                            [
                                self._status(
                                    command, "failed", error="Command expired before execution"
                                )
                            ],
                        )
                    return
                try:
                    await self.store.transition(
                        task_id,
                        {"queued"},
                        "running",
                        [
                            self._status(command, "accepted"),
                            self._status(command, "running", progress=0),
                        ],
                    )
                except ValueError:
                    # Cancellation or another owner won the reservation; never execute.
                    return

                async def progress(value: float, message: str = "") -> None:
                    await self.store.transition(
                        task_id,
                        {"running"},
                        "running",
                        [self._status(command, "running", progress=value, message=message)],
                    )

                if self.handler is None:
                    raise RuntimeError("This reference client has no task handler installed")
                result = await self.handler(command.payload, progress)
                await self.store.transition(
                    task_id,
                    {"running"},
                    "completed",
                    [self._status(command, "completed", progress=1, result=result)],
                )
        except asyncio.CancelledError:
            if task_id in self._cancelled_tasks:
                with contextlib.suppress(ValueError):
                    await self.store.transition(
                        task_id,
                        {"cancelling"},
                        "cancelled",
                        [self._status(command, "cancelled")],
                    )
            raise
        except Exception as error:
            with contextlib.suppress(ValueError):
                await self.store.transition(
                    task_id,
                    {"running"},
                    "failed",
                    [
                        self._status(
                            command,
                            "failed",
                            error=str(error)
                            if isinstance(error, TaskExecutionError)
                            else f"Task handler failed: {type(error).__name__}",
                        )
                    ],
                )
            logger.exception("Agent task failed", extra={"task_id": task_id})
        finally:
            self._workers.pop(task_id, None)
            self._cancelled_tasks.discard(task_id)

    def http(self) -> httpx.AsyncClient:
        if self._http is None:
            raise RuntimeError("Client is not running")
        return self._http

    @staticmethod
    def _check(response: httpx.Response) -> None:
        if response.status_code in {401, 403}:
            raise CredentialRejected("Agent credential was rejected or revoked")
        if 400 <= response.status_code < 500 and response.status_code not in {404, 408, 429}:
            raise ProtocolRejected(f"Protocol message rejected (HTTP {response.status_code})")
        response.raise_for_status()

    async def _http_send(self, envelope: Envelope) -> None:
        response = await self.http().post(
            "/api/agent/messages", json=envelope.model_dump(mode="json")
        )
        self._check(response)
        acknowledgment = response.json().get("ack")
        if acknowledgment:
            await self.store.acknowledge(str(acknowledgment["payload"]["message_id"]))

    async def _writer(self, sender: Sender) -> None:
        delivery = DeliveryBackoff()
        heartbeat_at = 0.0
        while not self.stop_event.is_set():
            # Clear before reading, so a commit racing with the snapshot or its
            # delivery remains visible when we start waiting. Other processes
            # have independent events and are still discovered by timed polling.
            self.store.changed.clear()
            await self._schedule()
            for envelope in delivery.ready(
                await self.store.pending(),
                retry_seconds=self.options.retry_seconds,
                max_retry_seconds=self.options.offline_after_seconds,
            ):
                await sender(envelope)
            if time.monotonic() >= heartbeat_at:
                await sender(await self.heartbeat())
                heartbeat_at = time.monotonic() + self.options.heartbeat_seconds
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(
                    self.store.changed.wait(),
                    min(self.options.retry_seconds, self.options.heartbeat_seconds),
                )

    async def _duplex(self, reader: Awaitable[None], sender: Sender) -> None:
        receive = asyncio.ensure_future(reader)
        write = asyncio.create_task(self._writer(sender))
        try:
            done, _ = await asyncio.wait({receive, write}, return_when=asyncio.FIRST_COMPLETED)
            for task in done:
                task.result()
        finally:
            for task in (receive, write):
                task.cancel()
            await asyncio.gather(receive, write, return_exceptions=True)

    async def _ws(self) -> None:
        parsed = urlsplit(self.server_url)
        url = urlunsplit(
            (
                "wss" if parsed.scheme == "https" else "ws",
                parsed.netloc,
                parsed.path + "/api/agent/ws",
                "",
                "",
            )
        )
        async with connect(
            url,
            additional_headers={"Authorization": f"Bearer {self.credential}"},
            open_timeout=self.options.poll_timeout_seconds,
            close_timeout=self.options.heartbeat_seconds,
            max_size=self.options.max_message_bytes,
            ping_interval=self.options.heartbeat_seconds,
        ) as websocket:
            lock = asyncio.Lock()
            heartbeat_id: str | None = None
            heartbeat_sent_at = 0.0

            async def send(envelope: Envelope) -> None:
                nonlocal heartbeat_id, heartbeat_sent_at
                async with lock:
                    if envelope.type == "heartbeat":
                        if heartbeat_id is not None:
                            if (
                                time.monotonic() - heartbeat_sent_at
                                >= self.options.offline_after_seconds
                            ):
                                raise TimeoutError("WebSocket heartbeat was not acknowledged")
                            return
                        # Control traffic must not grow faster than the receiver
                        # can persist it and delay business messages indefinitely.
                        heartbeat_id = envelope.id
                        heartbeat_sent_at = time.monotonic()
                    await websocket.send(envelope.model_dump_json())

            async def read() -> None:
                nonlocal heartbeat_id
                async for message in websocket:
                    envelope = Envelope.model_validate_json(message)
                    if envelope.type == "ack" and envelope.payload["message_id"] == heartbeat_id:
                        # Heartbeats have no durable outbox row to delete.
                        heartbeat_id = None
                        continue
                    await self._accept(envelope, send)

            await self._duplex(read(), send)

    async def _sse(self) -> None:
        async def read() -> None:
            async with self.http().stream(
                "GET",
                "/api/agent/events",
                timeout=httpx.Timeout(
                    self.options.poll_timeout_seconds, read=self.options.offline_after_seconds
                ),
            ) as response:
                self._check(response)
                data: list[str] = []
                event = "message"
                async for line in response.aiter_lines():
                    if line.startswith("event:"):
                        event = line[6:].strip()
                    elif line.startswith("data:"):
                        data.append(line[5:].lstrip())
                        if (
                            sum(len(item.encode()) for item in data)
                            > self.options.max_message_bytes
                        ):
                            raise ValueError("SSE message exceeds max_message_bytes")
                    elif not line and data:
                        if event == "revoked":
                            raise CredentialRejected("Agent credential was revoked")
                        await self._accept(
                            Envelope.model_validate_json("\n".join(data)), self._http_send
                        )
                        data.clear()
                        event = "message"

        try:
            async with asyncio.timeout(self.options.upgrade_interval_seconds):
                await self._duplex(read(), self._http_send)
        except TimeoutError as error:
            raise UpgradeTransport from error

    async def _poll(self) -> None:
        async def read() -> None:
            while not self.stop_event.is_set():
                response = await self.http().get(
                    "/api/agent/poll",
                    params={"timeout": self.options.poll_timeout_seconds},
                    timeout=self.options.poll_timeout_seconds + self.options.heartbeat_seconds,
                )
                self._check(response)
                for body in response.json()["messages"]:
                    await self._accept(Envelope.model_validate(body), self._http_send)

        try:
            async with asyncio.timeout(self.options.upgrade_interval_seconds):
                await self._duplex(read(), self._http_send)
        except TimeoutError as error:
            raise UpgradeTransport from error

    async def run(self, *, preferred: Literal["ws", "sse", "poll"] = "ws") -> None:
        """Prefer WS, fall back on transport failure, stop on identity/protocol errors."""
        await self.store.open()
        transports = ("ws", "sse", "poll")
        index = transports.index(preferred)
        failures = 0
        try:
            abandoned_work = await self.store.work("running") + await self.store.work("cancelling")
            for abandoned in abandoned_work:
                await self.store.state(
                    str(abandoned.payload["task_id"]),
                    "interrupted",
                    self._status(
                        abandoned,
                        "interrupted",
                        error="Client stopped during execution; external effects are unknown",
                    ),
                )
            self.ready.set()
            async with httpx.AsyncClient(
                base_url=self.server_url,
                headers={"Authorization": f"Bearer {self.credential}"},
                timeout=self.options.poll_timeout_seconds,
            ) as http:
                self._http = http
                while not self.stop_event.is_set():
                    self.transport = transports[index]
                    self.transport_history.append(self.transport)
                    logger.info(
                        "Agent transport selected",
                        extra={"event": "client.transport", "transport": self.transport},
                    )
                    try:
                        await {"ws": self._ws, "sse": self._sse, "poll": self._poll}[
                            self.transport
                        ]()
                        failures = 0
                    except UpgradeTransport:
                        index, failures = 0, 0
                        continue
                    except InvalidStatus as error:
                        if error.response.status_code in {401, 403}:
                            # Some proxies block only WS. Confirm identity via outbound HTTP.
                            await self._http_send(await self.heartbeat())
                    except ConnectionClosed as error:
                        if error.rcvd and error.rcvd.code in {4001, 4401, 4403}:
                            raise CredentialRejected("Agent credential was revoked") from error
                        if error.rcvd and error.rcvd.code == 1008:
                            await self._http_send(await self.heartbeat())
                            raise ProtocolRejected(
                                "Server closed WS for a protocol violation"
                            ) from error
                    except (httpx.RequestError, httpx.HTTPStatusError, OSError, TimeoutError):
                        pass
                    if self.stop_event.is_set():
                        break
                    index = min(index + 1, len(transports) - 1)
                    failures += 1
                    delay = min(
                        self.options.reconnect_max_seconds,
                        self.options.reconnect_base_seconds * (2 ** min(failures - 1, 10)),
                    )
                    with contextlib.suppress(TimeoutError):
                        await asyncio.wait_for(
                            self.stop_event.wait(), random.uniform(delay / 2, delay)
                        )
        finally:
            self._http = None
            self.ready.clear()
            workers = [*self._workers.values(), *self._repository_workers.values()]
            for worker in workers:
                worker.cancel()
            await asyncio.gather(*workers, return_exceptions=True)
            await self.store.close()

    def stop(self) -> None:
        self.stop_event.set()
        self.store.changed.set()


async def simulated_task(
    payload: JsonObject, progress: Callable[[float, str], Awaitable[None]]
) -> JsonObject:
    """Protocol demonstration only; no shell, files, or vendor agents are operated."""
    steps = int(payload.get("parameters", {}).get("steps", 3))
    delay = float(payload.get("parameters", {}).get("delay", 0.1))
    if not 1 <= steps <= 100 or not 0 <= delay <= 60:
        raise ValueError("Simulator steps/delay exceed allowed bounds")
    for index in range(steps):
        await asyncio.sleep(delay)
        await progress((index + 1) / steps, "模拟任务执行中")
    return {"simulated": True, "instruction": payload["instruction"], "steps": steps}
