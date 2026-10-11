"""Committed mailbox writes wake local senders without replacing durable polling."""

from __future__ import annotations

import asyncio
import sys
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

import pytest

from chiliarch.client import AgentClient, ClientStore
from chiliarch.config import TransportSettings
from chiliarch.protocol import Envelope


@asynccontextmanager
async def open_client(path: Path, *, retry_seconds: float = 30) -> AsyncIterator[AgentClient]:
    client = AgentClient(
        "http://127.0.0.1:1",
        "isolated-credential",
        path,
        manual_tasks=True,
        options=TransportSettings(
            retry_seconds=retry_seconds, heartbeat_seconds=60, offline_after_seconds=120
        ),
    )
    await client.store.open()
    try:
        yield client
    finally:
        client.stop()
        await client.store.close()


async def stop_writer(client: AgentClient, writer: asyncio.Task[None]) -> None:
    client.stop()
    try:
        await asyncio.wait_for(asyncio.shield(writer), 3)
    finally:
        writer.cancel()
        await asyncio.gather(writer, return_exceptions=True)


async def next_message(queue: asyncio.Queue[Envelope]) -> Envelope:
    return await asyncio.wait_for(queue.get(), 3)


async def test_committed_local_messages_wake_writer_without_early_retries_or_heartbeats(
    tmp_path: Path,
) -> None:
    async with open_client(tmp_path / "local.sqlite") as client:
        sent: asyncio.Queue[Envelope] = asyncio.Queue()
        writer = asyncio.create_task(client._writer(sent.put))
        try:
            assert (await next_message(sent)).type == "heartbeat"
            first = Envelope(type="agent.contact", payload={"message": "First"})
            second = Envelope(type="agent.contact", payload={"message": "Second"})
            await client.store.enqueue(first)
            assert (await next_message(sent)).id == first.id
            await client.store.enqueue(second)
            # First remains unacknowledged. A new commit must not bypass its
            # retry deadline or generate a heartbeat before the heartbeat clock.
            assert (await next_message(sent)).id == second.id
            assert [item.id for item in await client.store.pending()] == [first.id, second.id]
            await stop_writer(client, writer)
            assert sent.empty()
        finally:
            await stop_writer(client, writer)


@pytest.mark.parametrize("change", ["enqueue", "stop"])
async def test_change_between_outbox_snapshot_and_wait_is_not_lost(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, change: str
) -> None:
    async with open_client(tmp_path / "snapshot.sqlite") as client:
        snapshot_taken, release = asyncio.Event(), asyncio.Event()
        pending = client.store.pending

        async def blocked_snapshot() -> list[Envelope]:
            snapshot = await pending()
            if not snapshot_taken.is_set():
                snapshot_taken.set()
                await release.wait()
            return snapshot

        monkeypatch.setattr(client.store, "pending", blocked_snapshot)
        sent: asyncio.Queue[Envelope] = asyncio.Queue()
        writer = asyncio.create_task(client._writer(sent.put))
        try:
            await asyncio.wait_for(snapshot_taken.wait(), 3)
            message = Envelope(type="agent.contact", payload={"message": "After snapshot"})
            if change == "enqueue":
                await client.store.enqueue(message)
            else:
                client.stop()
            release.set()
            assert (await next_message(sent)).type == "heartbeat"
            if change == "enqueue":
                assert (await next_message(sent)).id == message.id
            await stop_writer(client, writer)
            assert sent.empty()
        finally:
            release.set()
            await stop_writer(client, writer)


async def test_external_process_write_is_discovered_by_polling(tmp_path: Path) -> None:
    async with open_client(tmp_path / "external.sqlite", retry_seconds=0.1) as client:
        sent: asyncio.Queue[Envelope] = asyncio.Queue()
        writer = asyncio.create_task(client._writer(sent.put))
        process: asyncio.subprocess.Process | None = None
        message = Envelope(type="agent.contact", payload={"message": "External mailbox"})
        try:
            assert (await next_message(sent)).type == "heartbeat"
            process = await asyncio.create_subprocess_exec(
                sys.executable,
                "-c",
                """
import asyncio
import sys
from pathlib import Path
from chiliarch.client import ClientStore
from chiliarch.protocol import Envelope

async def main() -> None:
    store = ClientStore(Path(sys.argv[1]))
    await store.open()
    try:
        await store.enqueue(Envelope.model_validate_json(sys.argv[2]))
    finally:
        await store.close()

asyncio.run(main())
""",
                str(client.store.path),
                message.canonical(),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, stderr = await asyncio.wait_for(process.communicate(), 10)
            assert process.returncode == 0, (stdout, stderr)
            assert not client.store.changed.is_set()
            assert (await next_message(sent)).id == message.id
            assert [item.id for item in await client.store.pending()] == [message.id]
        finally:
            if process is not None and process.returncode is None:
                process.kill()
                await process.communicate()
            await stop_writer(client, writer)


async def test_cancelled_commit_notifies_only_after_durable_write(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = ClientStore(tmp_path / "cancelled.sqlite")
    observer = ClientStore(store.path)
    await store.open()
    await observer.open()
    entered, release = asyncio.Event(), asyncio.Event()
    commit = store.db().commit

    async def blocked_commit() -> None:
        entered.set()
        await release.wait()
        await commit()

    monkeypatch.setattr(store.db(), "commit", blocked_commit)
    message = Envelope(type="agent.contact", payload={"message": "Durable during cancellation"})
    transaction = asyncio.create_task(store.enqueue(message))
    try:
        await asyncio.wait_for(entered.wait(), 3)
        assert not store.changed.is_set()
        assert await observer.pending() == []
        transaction.cancel()
        await asyncio.sleep(0)
        transaction.cancel()
        assert not store.changed.is_set()
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(transaction, 3)
        assert store.changed.is_set()
        assert [item.id for item in await observer.pending()] == [message.id]
        assert not store.db().in_transaction
    finally:
        release.set()
        await asyncio.gather(transaction, return_exceptions=True)
        await observer.close()
        await store.close()


async def test_rolled_back_transaction_does_not_publish_change(tmp_path: Path) -> None:
    store = ClientStore(tmp_path / "rollback.sqlite")
    await store.open()
    try:
        original = Envelope(type="agent.message", payload={"message": "Original"})
        assert await store.accept(original)
        store.changed.clear()
        conflicting = original.model_copy(update={"payload": {"message": "Conflicting"}})
        with pytest.raises(ValueError, match="conflicting content"):
            await store.accept(conflicting)
        assert not store.changed.is_set()
        assert not store.db().in_transaction
        assert [item.payload["message"] for _, item in await store.messages()] == ["Original"]
    finally:
        await store.close()
