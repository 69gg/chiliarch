"""Observed readiness and real process/workspace ownership, without vendor agents."""

from __future__ import annotations

import asyncio
import contextlib
import json
import sys
import threading
from pathlib import Path
from typing import Any

import pytest
from filelock import FileLock
from test_agent_runners import codex_success, fake_cli

from chiliarch.adapters.mailbox import AgentMailbox
from chiliarch.adapters.processes import ProcessTree
from chiliarch.adapters.runners import RunnerHandler, RunnerSettings
from chiliarch.adapters.runtime import (
    WorkspaceBlocked,
    recover_workspace,
    workspace_lock_path,
    workspace_owner,
)
from chiliarch.client import AgentClient
from chiliarch.protocol import Envelope

JsonObject = dict[str, Any]


def command(task_id: str) -> Envelope:
    return Envelope(
        type="task.dispatch", payload={"task_id": task_id, "instruction": "Inspect this workspace"}
    )


async def test_manual_heartbeat_distinguishes_waiting_from_claimed_work(tmp_path: Path) -> None:
    client = AgentClient(
        "http://localhost:8000", "fixture", tmp_path / "manual.db", manual_tasks=True
    )
    await client.store.open()
    try:
        await client.store.accept(command("manual-task"))
        heartbeat = await client.heartbeat()
        assert heartbeat.payload["status"] == "waiting_for_claim"
        metadata = heartbeat.payload["metadata"]
        assert metadata["execution_mode"] == "manual"
        assert metadata["queued_tasks"] == 1
        assert metadata["active_task_ids"] == []
        assert metadata["capacity"] is None
        mailbox = AgentMailbox(client.store)
        await mailbox.claim_task("manual-task")
        active = (await client.heartbeat()).payload["metadata"]
        assert active["state"] == "busy"
        assert active["active_task_ids"] == ["manual-task"]
        await mailbox.complete_task("manual-task", {"checked": True})
        assert (await client.heartbeat()).payload["status"] == "waiting_for_claim"
    finally:
        await client.store.close()


async def test_writer_emits_dynamic_metadata_and_managed_task_ledger(tmp_path: Path) -> None:
    running = asyncio.Event()
    release = asyncio.Event()

    async def handler(payload: JsonObject, progress: Any) -> JsonObject:
        running.set()
        await release.wait()
        return {"complete": True}

    client = AgentClient(
        "http://localhost:8000",
        "fixture",
        tmp_path / "managed.db",
        handler,
        max_concurrent_tasks=2,
        runtime_metadata=lambda: {"adapter": "fixture", "workspace": str(tmp_path)},
    )
    await client.store.open()
    try:
        assert (await client.heartbeat()).payload["status"] == "idle"
        await client.store.accept(command("managed-task"))
        await client._schedule()
        await asyncio.wait_for(running.wait(), 5)
        reports: list[Envelope] = []

        async def sender(envelope: Envelope) -> None:
            reports.append(envelope)
            if envelope.type == "heartbeat":
                client.stop()

        await client._writer(sender)
        metadata = next(item for item in reports if item.type == "heartbeat").payload["metadata"]
        assert metadata["state"] == "busy"
        assert metadata["active_task_ids"] == ["managed-task"]
        assert metadata["execution_mode"] == "managed"
        assert metadata["capacity"] == 2
        assert metadata["adapter"] == "fixture"
        assert metadata["workspace"] == str(tmp_path)
        assert "credential" not in metadata
        workers = list(client._workers.values())
        release.set()
        await asyncio.gather(*workers)
        assert (await client.heartbeat()).payload["status"] == "idle"
        assert (await client.store.tasks("managed-task"))[0][0] == "completed"
    finally:
        release.set()
        await asyncio.gather(*list(client._workers.values()), return_exceptions=True)
        await client.store.close()


async def test_missing_handler_is_blocked_without_claiming_native_readiness(tmp_path: Path) -> None:
    client = AgentClient("http://localhost:8000", "fixture", tmp_path / "empty.db")
    await client.store.open()
    try:
        metadata = (await client.heartbeat()).payload["metadata"]
        assert metadata["state"] == "blocked"
        assert metadata["blocked_reason"] == "handler_unavailable"
        assert metadata["capacity"] == 0
    finally:
        await client.store.close()


WORKSPACE_PROCESS = """
import asyncio
import sys
from pathlib import Path
from chiliarch.adapters.runtime import workspace_owner

async def main() -> None:
    async def waiting() -> None:
        print("waiting", flush=True)
    async with workspace_owner(Path(sys.argv[1]), lock_directory=Path(sys.argv[2]), on_wait=waiting) as lease:
        if len(sys.argv) > 3:
            await lease.begin_execution("crash-test")
        print("acquired", flush=True)
        await asyncio.to_thread(sys.stdin.readline)
        if len(sys.argv) > 3:
            await lease.confirm_stopped()
    print("released", flush=True)

asyncio.run(main())
"""


async def line(process: asyncio.subprocess.Process) -> str:
    assert process.stdout is not None
    return (await asyncio.wait_for(process.stdout.readline(), 10)).decode().strip()


async def release_process(process: asyncio.subprocess.Process) -> None:
    assert process.stdin is not None
    process.stdin.write(b"release\n")
    await process.stdin.drain()
    assert await line(process) == "released"
    assert await asyncio.wait_for(process.wait(), 10) == 0


@pytest.mark.parametrize("same_workspace", [True, False])
async def test_workspace_ownership_across_processes(tmp_path: Path, same_workspace: bool) -> None:
    workspace = tmp_path / "work"
    workspace.mkdir()
    other = workspace if same_workspace else tmp_path / "other"
    other.mkdir(exist_ok=True)
    processes: list[asyncio.subprocess.Process] = []
    try:
        for directory in (workspace, other):
            process = await asyncio.create_subprocess_exec(
                sys.executable,
                "-c",
                WORKSPACE_PROCESS,
                str(directory),
                str(tmp_path / "locks"),
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            processes.append(process)
            expected = "waiting" if same_workspace and len(processes) == 2 else "acquired"
            assert await line(process) == expected
        # For different workspaces both processes acquired ownership before either
        # releases it. Shared workspaces explicitly report contention instead.
        await release_process(processes[0])
        if same_workspace:
            assert await line(processes[1]) == "acquired"
        await release_process(processes[1])
        assert not list(workspace.glob("*.lock"))
    finally:
        for process in processes:
            if process.returncode is None:
                process.kill()
            await process.wait()


async def test_cancelled_workspace_waiter_never_acquires_or_releases_another_owner(
    tmp_path: Path,
) -> None:
    waiting = asyncio.Event()
    acquired = asyncio.Event()

    async def notify() -> None:
        waiting.set()

    async def waiter() -> None:
        async with workspace_owner(tmp_path, lock_directory=tmp_path / "locks", on_wait=notify):
            acquired.set()

    async with workspace_owner(tmp_path, lock_directory=tmp_path / "locks"):
        task = asyncio.create_task(waiter())
        await asyncio.wait_for(waiting.wait(), 5)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert not acquired.is_set()
        waiting.clear()
        second = asyncio.create_task(waiter())
        await asyncio.wait_for(waiting.wait(), 5)
        assert not acquired.is_set()
    await asyncio.wait_for(second, 5)
    assert acquired.is_set()


async def test_repeated_cancellation_waits_for_inflight_lock_acquisition_and_releases_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    entered = threading.Event()
    release = threading.Event()
    acquired = asyncio.Event()

    class DelayedLock(FileLock):
        def acquire(self, *args: Any, **kwargs: Any) -> Any:
            entered.set()
            release.wait(10)
            return super().acquire(*args, **kwargs)

    monkeypatch.setattr("chiliarch.adapters.runtime.FileLock", DelayedLock)

    async def owner() -> None:
        async with workspace_owner(tmp_path, lock_directory=tmp_path / "locks"):
            acquired.set()

    task = asyncio.create_task(owner())
    try:
        assert await asyncio.to_thread(entered.wait, 5)
        task.cancel()
        await asyncio.sleep(0)
        task.cancel()
        await asyncio.sleep(0)
        assert not task.done()
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, 5)
        assert not acquired.is_set()
        # An independent lock object can immediately enter after cancelled acquire.
        await asyncio.wait_for(owner(), 5)
        assert acquired.is_set()
    finally:
        release.set()
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


async def test_runner_contention_reports_blocked_and_cancel_does_not_start_native_process(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        "chiliarch.adapters.runtime.default_daemon_directory", lambda: tmp_path / "daemon"
    )
    workspace = tmp_path / "work"
    workspace.mkdir()
    first_binary = tmp_path / "first"
    first_binary.mkdir()
    second_binary = tmp_path / "second"
    second_binary.mkdir()
    handlers = [
        RunnerHandler(
            RunnerSettings(
                kind="codex",
                workdir=workspace,
                executable=str(fake_cli(directory, codex_success(), hang=index == 0)),
                terminate_grace_seconds=0.05,
            ),
            tmp_path / f"identity-{index}.db",
        )
        for index, directory in enumerate((first_binary, second_binary))
    ]
    blocked = asyncio.Event()

    async def progress(value: float, message: str) -> None:
        if handlers[1].runtime_metadata()["blocked_task_ids"] == ["second"]:
            blocked.set()

    first = asyncio.create_task(handlers[0](command("first").payload, progress))
    second: asyncio.Task[JsonObject] | None = None
    try:
        async with asyncio.timeout(10):
            while not (workspace / "observed.json").exists():
                if first.done():
                    first.result()
                await asyncio.sleep(0.01)
        second = asyncio.create_task(handlers[1](command("second").payload, progress))
        await asyncio.wait_for(blocked.wait(), 5)
        metadata = handlers[1].runtime_metadata()
        assert metadata["blocked_reason"] == "workspace_busy"
        assert metadata["blocked_task_ids"] == ["second"]
        second.cancel()
        with pytest.raises(asyncio.CancelledError):
            await second
        observed = json.loads((workspace / "observed.json").read_text())
        assert "Task ID: first" in observed["stdin"]
        assert handlers[1].runtime_metadata()["blocked_task_ids"] == []
        first.cancel()
        with pytest.raises(asyncio.CancelledError):
            await first
        result = await asyncio.wait_for(handlers[1](command("third").payload, progress), 10)
        assert result["session_id"] == "native-codex-session"
        observed = json.loads((workspace / "observed.json").read_text())
        assert "Task ID: third" in observed["stdin"]
    finally:
        for task in (first, second):
            if task is not None:
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await task


@pytest.mark.parametrize("cleanup_error", [PermissionError, asyncio.CancelledError])
async def test_cleanup_failure_isolates_workspace_until_explicit_recovery(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, cleanup_error: type[BaseException]
) -> None:
    monkeypatch.setattr(
        "chiliarch.adapters.runtime.default_daemon_directory", lambda: tmp_path / "daemon"
    )
    workspace = tmp_path / "work"
    workspace.mkdir()
    settings = RunnerSettings(
        kind="codex",
        workdir=workspace,
        executable=str(fake_cli(tmp_path, codex_success(), hang=True)),
        terminate_grace_seconds=0.05,
    )
    original_cleanup = ProcessTree.terminate
    trees: list[ProcessTree] = []

    async def cannot_confirm(tree: ProcessTree, grace_seconds: float) -> None:
        trees.append(tree)
        raise cleanup_error("private native diagnostic must not become platform result")

    monkeypatch.setattr(ProcessTree, "terminate", cannot_confirm)

    async def progress(value: float, message: str) -> None:
        pass

    first = RunnerHandler(settings, tmp_path / "first.db")
    task = asyncio.create_task(first(command("first").payload, progress))
    try:
        async with asyncio.timeout(10):
            while not (workspace / "observed.json").exists():
                if task.done():
                    task.result()
                await asyncio.sleep(0.01)
        task.cancel()
        with pytest.raises(RuntimeError, match="cleanup could not be confirmed"):
            await task
        assert trees and trees[0].process.returncode is None
        second = RunnerHandler(settings, tmp_path / "second.db")
        assert second.runtime_metadata()["blocked_reason"] == "workspace_unverified"
        with pytest.raises(WorkspaceBlocked):
            await second(command("second").payload, progress)
        observed = json.loads((workspace / "observed.json").read_text())
        assert "Task ID: first" in observed["stdin"]
        inspection = await recover_workspace(workspace)
        assert inspection["unverified_execution"] is True
        assert inspection["processes_checked"] is False
        marker = Path(inspection["marker"])
        assert await asyncio.to_thread(marker.exists)
        await original_cleanup(trees[0], 0.05)
        monkeypatch.setattr(ProcessTree, "terminate", original_cleanup)
        assert (await recover_workspace(workspace, confirm_stopped=True))["cleared"] is True
        assert second.runtime_metadata()["blocked_reason"] is None
        fake_cli(tmp_path, codex_success())
        result = await asyncio.wait_for(second(command("recovered").payload, progress), 10)
        assert result["session_id"] == "native-codex-session"
        observed = json.loads((workspace / "observed.json").read_text())
        assert "Task ID: recovered" in observed["stdin"]
        assert not await asyncio.to_thread(marker.exists)
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        for tree in trees:
            await original_cleanup(tree, 0.05)


async def test_process_crash_leaves_workspace_isolated(tmp_path: Path) -> None:
    locks = tmp_path / "locks"
    process = await asyncio.create_subprocess_exec(
        sys.executable,
        "-c",
        WORKSPACE_PROCESS,
        str(tmp_path),
        str(locks),
        "mark-execution",
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        assert await line(process) == "acquired"
        process.kill()
        await asyncio.wait_for(process.wait(), 10)
        with pytest.raises(WorkspaceBlocked):
            async with workspace_owner(tmp_path, lock_directory=locks):
                pytest.fail("A crashed runner must not silently release workspace isolation")
        assert workspace_lock_path(tmp_path, locks).with_suffix(".unverified.json").exists()
    finally:
        if process.returncode is None:
            process.kill()
        await process.wait()


async def test_recovery_refuses_a_workspace_still_owned_by_a_worker(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        "chiliarch.adapters.runtime.default_daemon_directory", lambda: tmp_path / "daemon"
    )
    async with workspace_owner(tmp_path) as lease:
        await lease.begin_execution("active")
        with pytest.raises(WorkspaceBlocked, match="still owned"):
            await recover_workspace(tmp_path, confirm_stopped=True)
        assert lease.marker.exists()
        await lease.confirm_stopped()


def test_workspace_recovery_cli_defaults_to_read_only_and_requires_explicit_confirmation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    from chiliarch import cli

    monkeypatch.setattr(
        "chiliarch.adapters.runtime.default_daemon_directory", lambda: tmp_path / "daemon"
    )

    def run(*options: str) -> JsonObject:
        monkeypatch.setattr(
            sys,
            "argv",
            ["chiliarch", "agent", "workspace-recover", "--workdir", str(tmp_path), *options],
        )
        cli.main()
        return json.loads(capsys.readouterr().out)

    assert run()["unverified_execution"] is False
    assert not (tmp_path / "workspace-locks").exists()

    async def leave_marker() -> None:
        async with workspace_owner(tmp_path) as lease:
            await lease.begin_execution("stopped-but-unverified")

    asyncio.run(leave_marker())
    marker = workspace_lock_path(tmp_path).with_suffix(".unverified.json")
    before = marker.read_bytes()
    assert run()["unverified_execution"] is True
    assert marker.read_bytes() == before
    result = run("--confirm-stopped")
    assert result["cleared"] is True
    assert result["processes_checked"] is False
    assert not marker.exists()

    async def execute_after_recovery() -> None:
        async with asyncio.timeout(5):
            async with workspace_owner(tmp_path) as lease:
                await lease.begin_execution("after-recovery")
                with pytest.raises(WorkspaceBlocked, match="still owned"):
                    await recover_workspace(tmp_path, confirm_stopped=True)
                assert await asyncio.to_thread(lease.marker.exists)
                await lease.confirm_stopped()

    asyncio.run(execute_after_recovery())
    assert run()["unverified_execution"] is False


async def test_renamed_directory_keeps_the_same_physical_workspace_lock(tmp_path: Path) -> None:
    workspace = tmp_path / "original"
    workspace.mkdir()
    renamed = tmp_path / "renamed"
    waiting = asyncio.Event()

    async def notify() -> None:
        waiting.set()

    async def contender() -> None:
        async with workspace_owner(renamed, lock_directory=tmp_path / "locks", on_wait=notify):
            pass

    async with workspace_owner(workspace, lock_directory=tmp_path / "locks"):
        workspace.rename(renamed)
        task = asyncio.create_task(contender())
        await asyncio.wait_for(waiting.wait(), 5)
        assert not task.done()
    await asyncio.wait_for(task, 5)
