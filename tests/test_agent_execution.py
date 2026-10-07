"""Cancellation intent must not become a confirmed stop before handler cleanup."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

from chiliarch.client import AgentClient
from chiliarch.protocol import Envelope


async def test_cancelled_status_waits_for_execution_cleanup(tmp_path: Path) -> None:
    started = asyncio.Event()
    cleaning = asyncio.Event()
    cleaned = asyncio.Event()
    acknowledgments: list[Envelope] = []

    async def handler(payload: dict[str, Any], progress: Any) -> None:
        started.set()
        try:
            await asyncio.Event().wait()
        finally:
            cleaning.set()
            await cleaned.wait()

    async def acknowledge(envelope: Envelope) -> None:
        acknowledgments.append(envelope)

    client = AgentClient(
        "http://unused.invalid", "fixture-credential", tmp_path / "agent.db", handler
    )
    await client.store.open()
    command = Envelope(type="task.dispatch", payload={"task_id": "work", "instruction": "Inspect"})
    try:
        await client.store.accept(command)
        await client._schedule()
        async with asyncio.timeout(3):
            await started.wait()
            cancellation = Envelope(type="task.cancel", payload={"task_id": "work"})
            execution = client._workers["work"]
            await client._accept(cancellation, acknowledge)
            await cleaning.wait()
            assert acknowledgments[-1].payload["message_id"] == cancellation.id
            assert (await client.store.tasks("work"))[0][0] == "cancelling"
            assert not any(
                report.type == "task.status" and report.payload["status"] == "cancelled"
                for report in await client.store.pending()
            )
            cleaned.set()
            await asyncio.gather(execution, return_exceptions=True)
        assert (await client.store.tasks("work"))[0][0] == "cancelled"
        terminal = [
            report
            for report in await client.store.pending()
            if report.type == "task.status" and report.payload["status"] == "cancelled"
        ]
        assert len(terminal) == 1
    finally:
        cleaned.set()
        for task in list(client._workers.values()):
            task.cancel()
        await asyncio.gather(*list(client._workers.values()), return_exceptions=True)
        await client.store.close()


async def test_automatic_reservation_never_executes_persistently_cancelled_task(
    tmp_path: Path,
) -> None:
    calls: list[str] = []

    async def handler(payload: dict[str, Any], progress: Any) -> None:
        calls.append(payload["instruction"])

    client = AgentClient(
        "http://unused.invalid", "fixture-credential", tmp_path / "agent.db", handler
    )
    await client.store.open()
    command = Envelope(type="task.dispatch", payload={"task_id": "work", "instruction": "Inspect"})
    try:
        await client.store.accept(command)
        await client.store.accept(Envelope(type="task.cancel", payload={"task_id": "work"}))
        # A stale scheduling snapshot still cannot win the atomic reservation.
        await client._execute(command)
        assert calls == []
        assert (await client.store.tasks("work"))[0][0] == "cancelled"
        assert not await client.store.work("failed")
        assert not await client.store.work("completed")
    finally:
        await client.store.close()
