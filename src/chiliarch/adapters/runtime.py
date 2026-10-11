"""A single outbound connection owner; children exchange messages through SQLite."""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

from filelock import FileLock, Timeout

from chiliarch.client import AgentClient, RuntimeMetadata, TaskExecutionError, TaskHandler
from chiliarch.config import TransportSettings
from chiliarch.daemon_services import default_daemon_directory


class WorkspaceBlocked(TaskExecutionError):
    """A previous native execution did not confirm cleanup of its workspace."""


def workspace_lock_path(workdir: Path, lock_directory: Path | None = None) -> Path:
    canonical = workdir.expanduser().resolve()
    identity = canonical.stat()
    # Filesystem identity also handles case aliases on macOS and Windows, where
    # differently spelled paths can point at one directory.
    key_material = (
        f"{identity.st_dev}:{identity.st_ino}"
        if identity.st_ino
        else os.path.normcase(str(canonical))
    )
    key = hashlib.sha256(key_material.encode()).hexdigest()
    directory = lock_directory or default_daemon_directory().parent / "workspace-locks"
    return directory / (key + ".lock")


@dataclass
class WorkspaceLease:
    marker: Path
    workdir: Path

    async def begin_execution(self, task_id: str) -> None:
        """Persist uncertainty before native launch; a crash must not silently clear it."""

        def write() -> None:
            with self.marker.open("x", encoding="utf-8") as stream:
                json.dump(
                    {"workspace": str(self.workdir), "task_id": task_id, "owner_pid": os.getpid()},
                    stream,
                    ensure_ascii=False,
                )
                stream.flush()
                os.fsync(stream.fileno())

        await _finish_lock_operation(write)

    async def confirm_stopped(self) -> None:
        await _finish_lock_operation(lambda: self.marker.unlink(missing_ok=True))


async def recover_workspace(workdir: Path, *, confirm_stopped: bool = False) -> dict[str, Any]:
    """Inspect without writing; clear only after explicit human confirmation and a lock."""
    path = await asyncio.to_thread(workspace_lock_path, workdir)
    canonical = await asyncio.to_thread(lambda: workdir.expanduser().resolve())
    marker = path.with_suffix(".unverified.json")
    result: dict[str, Any] = {
        "workspace": str(canonical),
        "marker": str(marker),
        "unverified_execution": await asyncio.to_thread(marker.exists),
        "processes_checked": False,
        "cleared": False,
    }
    if not confirm_stopped or not path.parent.exists():
        return result
    lock = FileLock(path, timeout=0, thread_local=False)
    try:
        try:
            await _finish_lock_operation(lock.acquire)
        except Timeout:
            raise WorkspaceBlocked(
                "Workspace is still owned by a running Chiliarch worker"
            ) from None
        await _finish_lock_operation(lambda: marker.unlink(missing_ok=True))
        result.update(unverified_execution=False, cleared=True)
    finally:
        await _finish_lock_operation(lock.release)
    return result


async def _finish_lock_operation(operation: Callable[[], Any]) -> None:
    """Do not abandon a file-lock worker while it may still acquire/release ownership."""
    pending = asyncio.create_task(asyncio.to_thread(operation))
    interrupted = False
    while not pending.done():
        try:
            await asyncio.shield(pending)
        except asyncio.CancelledError:
            interrupted = True
        except Exception:
            break
    try:
        pending.result()
    finally:
        if interrupted:
            raise asyncio.CancelledError


@asynccontextmanager
async def workspace_owner(
    workdir: Path,
    *,
    lock_directory: Path | None = None,
    on_wait: Callable[[], Awaitable[None]] | None = None,
) -> AsyncIterator[WorkspaceLease]:
    """Serialize managed work in one canonical directory across identities/processes."""
    path = await asyncio.to_thread(workspace_lock_path, workdir, lock_directory)
    await asyncio.to_thread(path.parent.mkdir, parents=True, exist_ok=True)
    marker = path.with_suffix(".unverified.json")
    lock = FileLock(path, timeout=0, thread_local=False)
    notified = False
    try:
        while True:
            try:
                await _finish_lock_operation(lock.acquire)
                break
            except Timeout:
                if not notified and on_wait is not None:
                    await on_wait()
                    notified = True
                await asyncio.sleep(0.1)
        if await asyncio.to_thread(marker.exists):
            raise WorkspaceBlocked(
                "Workspace has an unverified native execution; inspect it before using "
                "chiliarch agent workspace-recover --confirm-stopped"
            )
        yield WorkspaceLease(marker, workdir)
    finally:
        # Leave pathname cleanup to filelock's platform backend. Unlinking it
        # ourselves would let Unix waiters acquire different inodes concurrently.
        await _finish_lock_operation(lock.release)


@asynccontextmanager
async def connection_owner(state_path: Path) -> AsyncIterator[None]:
    """Prevent a runner and an interactive MCP host from executing the same identity."""
    await asyncio.to_thread(state_path.parent.mkdir, parents=True, exist_ok=True)
    resolved = await asyncio.to_thread(state_path.resolve)
    lock = FileLock(str(resolved) + ".lock", timeout=0, thread_local=False)
    try:
        try:
            await _finish_lock_operation(lock.acquire)
        except Timeout as error:
            raise ValueError(
                "This agent state is already connected; use a separate paired identity"
            ) from error
        yield
    finally:
        await _finish_lock_operation(lock.release)


@asynccontextmanager
async def outbound_client(
    state_path: Path,
    *,
    handler: TaskHandler | None = None,
    manual_tasks: bool = False,
    preferred: Literal["ws", "sse", "poll"] = "ws",
    options: TransportSettings | None = None,
    max_concurrent_tasks: int = 1,
    runtime_metadata: RuntimeMetadata | None = None,
) -> AsyncIterator[tuple[AgentClient, asyncio.Task[None]]]:
    async with connection_owner(state_path):
        client = await AgentClient.from_state(
            state_path,
            handler,
            options=options,
            max_concurrent_tasks=max_concurrent_tasks,
            manual_tasks=manual_tasks,
            runtime_metadata=runtime_metadata,
        )
        task = asyncio.create_task(
            client.run(preferred=preferred), name="chiliarch.agent.connection"
        )
        ready = asyncio.create_task(client.ready.wait())
        body_failed = False
        try:
            done, _ = await asyncio.wait({task, ready}, return_when=asyncio.FIRST_COMPLETED)
            if task in done:
                task.result()
                raise RuntimeError("Outbound client stopped before its mailbox was ready")
            yield client, task
        except BaseException:
            body_failed = True
            raise
        finally:
            ready.cancel()
            client.stop()

            async def finish() -> None:
                # Let an in-flight HTTP POST receive its durable ACK before
                # closing SSE/long-poll readers. Immediate cancellation can
                # reset that socket halfway through the server's request body.
                done, _ = await asyncio.wait([task], timeout=client.options.poll_timeout_seconds)
                if not done:
                    task.cancel()
                # Cancellation still joins the client's existing resource and
                # native-worker cleanup before releasing connection ownership.
                await asyncio.gather(ready, task, return_exceptions=True)

            cleanup = asyncio.create_task(finish())
            interrupted = False
            while not cleanup.done():
                try:
                    await asyncio.shield(cleanup)
                except asyncio.CancelledError:
                    interrupted = True
            cleanup.result()
            if interrupted:
                raise asyncio.CancelledError
            if not body_failed and not task.cancelled():
                task.result()
