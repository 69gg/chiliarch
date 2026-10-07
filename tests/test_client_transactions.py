"""A cancelled transport cannot leave partial writes in the shared mailbox store."""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Coroutine
from pathlib import Path
from typing import Any

import aiosqlite
import pytest

from chiliarch.client import ClientStore
from chiliarch.protocol import Envelope


@pytest.mark.parametrize("operation", ["accept", "acknowledge", "enqueue"])
async def test_cancelled_write_rolls_back_before_next_mailbox_transaction(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, operation: str
) -> None:
    store = ClientStore(tmp_path / "client.sqlite")
    await store.open()
    command = Envelope(type="task.dispatch", payload={"task_id": "worker", "instruction": "Run"})
    outgoing = Envelope(type="agent.contact", payload={"message": "Report"})
    await store.accept(command)
    await store.enqueue(outgoing)
    written = asyncio.Event()
    execute = store.db().execute

    async def interrupt_write(sql: str, parameters: Any) -> aiosqlite.Cursor:
        cursor = await execute(sql, parameters)
        written.set()
        await asyncio.Event().wait()
        return cursor

    def interrupted_execute(sql: str, parameters: Any = ()) -> Any:
        target = {
            "accept": "INSERT INTO inbox",
            "acknowledge": "DELETE FROM outbox",
            "enqueue": "INSERT OR IGNORE INTO outbox",
        }[operation]
        if sql.startswith(target):
            return interrupt_write(sql, parameters)
        return execute(sql, parameters)

    incoming = Envelope(type="agent.message", payload={"message": "Reply"})
    pending = Envelope(type="agent.contact", payload={"message": "Pending"})
    operations: dict[str, Callable[[], Coroutine[Any, Any, Any]]] = {
        "accept": lambda: store.accept(incoming),
        "acknowledge": lambda: store.acknowledge(outgoing.id),
        "enqueue": lambda: store.enqueue(pending),
    }
    try:
        monkeypatch.setattr(store.db(), "execute", interrupted_execute)
        task = asyncio.create_task(operations[operation]())
        async with asyncio.timeout(3):
            await written.wait()
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
        monkeypatch.setattr(store.db(), "execute", execute)
        assert not store.db().in_transaction
        assert [item.id for item in await store.pending()] == [outgoing.id]
        assert await store.messages() == []
        assert await store.transition("worker", {"queued"}, "running", [])
        assert [(state, item.payload["task_id"]) for state, item in await store.tasks()] == [
            ("running", "worker")
        ]
    finally:
        await store.close()


async def test_cancelled_commit_finishes_before_outbox_becomes_visible(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = ClientStore(tmp_path / "client.sqlite")
    await store.open()
    committing, release = asyncio.Event(), asyncio.Event()
    original = store.db().commit

    async def blocked_commit() -> None:
        committing.set()
        await release.wait()
        await original()

    message = Envelope(type="agent.contact", payload={"message": "Persist before upload"})
    writer = asyncio.create_task(store.enqueue(message))
    reader: asyncio.Task[list[Envelope]] | None = None
    try:
        monkeypatch.setattr(store.db(), "commit", blocked_commit)
        async with asyncio.timeout(3):
            await committing.wait()
            writer.cancel()
            await asyncio.sleep(0)
            writer.cancel()
            reader = asyncio.create_task(store.pending())
            await asyncio.sleep(0)
            assert not writer.done()
            assert not reader.done()
            release.set()
            with pytest.raises(asyncio.CancelledError):
                await writer
            assert [entry.id for entry in await reader] == [message.id]
        assert not store.db().in_transaction
        # Durable receive may have committed during cancellation; reconnecting
        # retries the same ID instead of inventing another upstream contact.
        await store.enqueue(message)
        assert [entry.id for entry in await store.pending()] == [message.id]
    finally:
        release.set()
        await asyncio.gather(
            writer, *([reader] if reader is not None else []), return_exceptions=True
        )
        await store.close()


async def test_repeated_cancellation_waits_for_rollback_before_reusing_connection(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = ClientStore(tmp_path / "client.sqlite")
    await store.open()
    rolling_back, release = asyncio.Event(), asyncio.Event()
    rollback = store.db().rollback
    execute = store.db().execute

    async def blocked_rollback() -> None:
        rolling_back.set()
        await release.wait()
        await rollback()

    async def failed_write(sql: str, parameters: Any) -> aiosqlite.Cursor:
        await execute(sql, parameters)
        raise OSError("Injected write failure")

    def fail_insert(sql: str, parameters: Any = ()) -> Any:
        return (
            failed_write(sql, parameters) if sql.startswith("INSERT") else execute(sql, parameters)
        )

    monkeypatch.setattr(store.db(), "rollback", blocked_rollback)
    monkeypatch.setattr(store.db(), "execute", fail_insert)
    writer = asyncio.create_task(
        store.enqueue(Envelope(type="agent.contact", payload={"message": "Rollback"}))
    )
    try:
        async with asyncio.timeout(3):
            await rolling_back.wait()
            writer.cancel()
            await asyncio.sleep(0)
            writer.cancel()
            assert store.lock.locked()
            release.set()
            with pytest.raises(asyncio.CancelledError):
                await writer
        monkeypatch.setattr(store.db(), "execute", execute)
        assert not store.db().in_transaction
        assert await store.pending() == []
        command = Envelope(type="task.dispatch", payload={"task_id": "new", "instruction": "Run"})
        assert await store.accept(command)
        assert await store.transition("new", {"queued"}, "running", [])
    finally:
        release.set()
        await asyncio.gather(writer, return_exceptions=True)
        await store.close()
