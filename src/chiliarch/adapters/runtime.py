"""A single outbound connection owner; children exchange messages through SQLite."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Literal

from filelock import FileLock, Timeout

from chiliarch.client import AgentClient, TaskHandler
from chiliarch.config import TransportSettings


@asynccontextmanager
async def connection_owner(state_path: Path) -> AsyncIterator[None]:
    """Prevent a runner and an interactive MCP host from executing the same identity."""
    await asyncio.to_thread(state_path.parent.mkdir, parents=True, exist_ok=True)
    resolved = await asyncio.to_thread(state_path.resolve)
    lock = FileLock(str(resolved) + ".lock", timeout=0, thread_local=False)
    try:
        await asyncio.to_thread(lock.acquire)
    except Timeout as error:
        raise ValueError(
            "This agent state is already connected; use a separate paired identity"
        ) from error
    try:
        yield
    finally:
        await asyncio.to_thread(lock.release)


@asynccontextmanager
async def outbound_client(
    state_path: Path,
    *,
    handler: TaskHandler | None = None,
    manual_tasks: bool = False,
    preferred: Literal["ws", "sse", "poll"] = "ws",
    options: TransportSettings | None = None,
    max_concurrent_tasks: int = 1,
) -> AsyncIterator[tuple[AgentClient, asyncio.Task[None]]]:
    async with connection_owner(state_path):
        client = await AgentClient.from_state(
            state_path,
            handler,
            options=options,
            max_concurrent_tasks=max_concurrent_tasks,
            manual_tasks=manual_tasks,
        )
        task = asyncio.create_task(
            client.run(preferred=preferred), name="chiliarch.agent.connection"
        )
        ready = asyncio.create_task(client.ready.wait())
        try:
            done, _ = await asyncio.wait({task, ready}, return_when=asyncio.FIRST_COMPLETED)
            if task in done:
                task.result()
                raise RuntimeError("Outbound client stopped before its mailbox was ready")
            yield client, task
        finally:
            ready.cancel()
            client.stop()
            # Interrupt a pending long poll/stream instead of delaying stdio shutdown.
            task.cancel()
            await asyncio.gather(ready, task, return_exceptions=True)
