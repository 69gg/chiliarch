"""Managed continuation contracts use isolated fake native CLIs, never user agents."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any, Literal

import pytest

from chiliarch.adapters.runners import RunnerHandler, RunnerSettings
from chiliarch.client import AgentClient
from chiliarch.protocol import Envelope
from tests.test_agent_runners import claude_success, codex_success, fake_cli

JsonObject = dict[str, Any]
Kind = Literal["codex", "claude-code"]


@pytest.fixture(autouse=True)
def isolated_workspace_locks(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "chiliarch.adapters.runtime.default_daemon_directory", lambda: tmp_path / "daemon"
    )


def settings(tmp_path: Path, kind: Kind = "codex") -> RunnerSettings:
    events = codex_success() if kind == "codex" else claude_success()
    return RunnerSettings(
        kind=kind,
        workdir=tmp_path,
        executable=str(fake_cli(tmp_path, events)),
        terminate_grace_seconds=0.05,
    )


async def client_for(tmp_path: Path, configuration: RunnerSettings) -> AgentClient:
    state_path = tmp_path / "agent.db"
    client = AgentClient(
        "http://platform.example",
        "test-credential",
        state_path,
        handler=RunnerHandler(configuration, state_path),
    )
    await client.store.open()
    if not await client.store.identity():
        await client.store.save_identity(
            {"agent_id": "managed-agent", "server_url": client.server_url}
        )
    return client


def task(task_id: str, parent: str | None = None) -> Envelope:
    parameters: JsonObject = {}
    if parent is not None:
        parameters["chiliarch_context"] = {"continue_task_id": parent}
    return Envelope(
        type="task.dispatch",
        payload={
            "task_id": task_id,
            "instruction": "Coordinate the next step",
            "parameters": parameters,
        },
    )


async def run(client: AgentClient, command: Envelope) -> JsonObject:
    await client.store.accept(command)
    await client._execute(command)
    reports = [
        item.payload
        for item in await client.store.pending()
        if item.type == "task.status" and item.payload["task_id"] == command.payload["task_id"]
    ]
    return reports[-1]


def observed(tmp_path: Path) -> JsonObject:
    return json.loads((tmp_path / "observed.json").read_text(encoding="utf-8"))


@pytest.mark.parametrize("kind", ["codex", "claude-code"])
async def test_continuation_survives_client_restart_and_keeps_new_task_identity(
    tmp_path: Path, kind: Kind
) -> None:
    configuration = settings(tmp_path, kind)
    first = await client_for(tmp_path, configuration)
    try:
        original = await run(first, task("original"))
        assert original["status"] == "completed"
        initial = observed(tmp_path)
        initial_args = initial["argv"]
        assert "resume" not in initial_args and "--resume" not in initial_args
        if kind == "codex":
            assert initial["requests"][2]["method"] == "thread/start"
    finally:
        await first.store.close()
    restarted = await client_for(tmp_path, configuration)
    try:
        continued = await run(restarted, task("follow-up", "original"))
        assert continued["status"] == "completed"
        assert continued["result"]["continued_task_id"] == "original"
        assert continued["result"]["session_id"] == original["result"]["session_id"]
        execution = observed(tmp_path)
        args = execution["argv"]
        if kind == "codex":
            assert args == ["app-server"]
            resumed = execution["requests"][2]
            assert resumed["method"] == "thread/resume"
            assert resumed["params"]["threadId"] == original["result"]["session_id"]
            assert resumed["params"]["excludeTurns"] is True
            assert set(resumed["params"]) == {"cwd", "config", "threadId", "excludeTurns"}
            mcp_name, mcp = next(iter(resumed["params"]["config"].items()))
            assert mcp_name not in initial["requests"][2]["params"]["config"]
            mcp_name = mcp_name.removeprefix("mcp_servers.")
            mcp_args = mcp["args"]
        else:
            assert args[args.index("--resume") + 1] == original["result"]["session_id"]
            assert "--permission-mode" not in args
            assert "--strict-mcp-config" not in args
            mcp_name, mcp = next(iter(execution["mcp_config"]["mcpServers"].items()))
            assert mcp_name not in initial["mcp_config"]["mcpServers"]
            mcp_args = mcp["args"]
        assert mcp_args[-2:] == ["--managed-task", "follow-up"]
        assert f"Use the {mcp_name} MCP server's tools" in execution["stdin"]
        assert "Task ID: follow-up" in execution["stdin"]
        assert len(await restarted.store.tasks()) == 2
        assert (await restarted.store.tasks("original"))[0][0] == "completed"
    finally:
        await restarted.store.close()


@pytest.mark.parametrize("changed", ["identity", "server", "adapter", "workspace", "state"])
async def test_continuation_refuses_other_identity_adapter_workspace_or_unfinished_work(
    tmp_path: Path, changed: str
) -> None:
    configuration = settings(tmp_path)
    client = await client_for(tmp_path, configuration)
    try:
        assert (await run(client, task("original")))["status"] == "completed"
        before = observed(tmp_path)
        if changed in {"identity", "server"}:
            await client.store.save_identity(
                {"agent_id": "different-agent"}
                if changed == "identity"
                else {"server_url": "http://other-platform.example"}
            )
        elif changed == "adapter":
            configuration = configuration.model_copy(update={"kind": "claude-code"})
        elif changed == "workspace":
            other = tmp_path / "another-workspace"
            other.mkdir()
            configuration = configuration.model_copy(update={"workdir": other})
        else:
            await client.store.state("original", "running")
        client.handler = RunnerHandler(configuration, client.store.path)
        result = await run(client, task("forbidden-follow-up", "original"))
        assert result["status"] == "failed"
        assert "Continuation requires" in result["error"]
        assert observed(tmp_path) == before
    finally:
        await client.store.close()


async def test_continuation_refuses_unmanaged_history_and_raw_session_id(tmp_path: Path) -> None:
    client = await client_for(tmp_path, settings(tmp_path))
    try:
        await client.store.accept(task("manual"))
        await client.store.state("manual", "completed")
        result = await run(client, task("not-managed", "manual"))
        assert result["status"] == "failed"
        assert not (tmp_path / "observed.json").exists()
        command = task("fresh")
        command.payload["parameters"] = {"session_id": "another-users-session", "resume": True}
        result = await run(client, command)
        assert result["status"] == "completed"
        args = observed(tmp_path)["argv"]
        assert "resume" not in args and "another-users-session" not in args
        assert observed(tmp_path)["requests"][2]["method"] == "thread/start"
        assert "threadId" not in observed(tmp_path)["requests"][2]["params"]
    finally:
        await client.store.close()


async def test_continuation_requires_latest_completed_task_not_stale_ancestor(
    tmp_path: Path,
) -> None:
    client = await client_for(tmp_path, settings(tmp_path))
    try:
        assert (await run(client, task("first")))["status"] == "completed"
        assert (await run(client, task("second", "first")))["status"] == "completed"
        before = observed(tmp_path)
        assert (await run(client, task("stale", "first")))["status"] == "failed"
        assert observed(tmp_path) == before
        assert (await run(client, task("third", "second")))["status"] == "completed"
    finally:
        await client.store.close()


async def test_failed_continuation_cannot_be_bypassed_by_resuming_older_success(
    tmp_path: Path,
) -> None:
    configuration = settings(tmp_path)
    client = await client_for(tmp_path, configuration)
    try:
        assert (await run(client, task("first")))["status"] == "completed"
        fake_cli(tmp_path, [{"type": "error"}], exitcode=1)
        assert (await run(client, task("failed-turn", "first")))["status"] == "failed"
        fake_cli(tmp_path, codex_success())
        before = observed(tmp_path)
        for source in ("first", "failed-turn"):
            assert (await run(client, task(f"bypass-{source}", source)))["status"] == "failed"
            assert observed(tmp_path) == before
    finally:
        await client.store.close()


async def test_cancelled_continuation_stays_unresumable_after_restart(tmp_path: Path) -> None:
    configuration = settings(tmp_path)
    client = await client_for(tmp_path, configuration)
    running: asyncio.Task[None] | None = None
    try:
        assert (await run(client, task("first")))["status"] == "completed"
        fake_cli(tmp_path, codex_success(), hang=True)
        observation = tmp_path / "observed.json"
        observation.unlink()
        command = task("interrupted", "first")
        await client.store.accept(command)
        running = asyncio.create_task(client._execute(command))
        async with asyncio.timeout(10):
            while not observation.exists():
                if running.done():
                    running.result()
                await asyncio.sleep(0.01)
        assert "Task ID: interrupted" in observed(tmp_path)["stdin"]
        running.cancel()
        with pytest.raises(asyncio.CancelledError):
            await running
    finally:
        if running is not None:
            running.cancel()
            await asyncio.gather(running, return_exceptions=True)
        await client.store.close()
    fake_cli(tmp_path, codex_success())
    restarted = await client_for(tmp_path, configuration)
    try:
        before = observed(tmp_path)
        for source in ("first", "interrupted"):
            result = await run(restarted, task(f"unsafe-{source}", source))
            assert result["status"] == "failed"
            assert observed(tmp_path) == before
    finally:
        await restarted.store.close()


async def test_continuation_requires_native_confirmation_of_same_session(tmp_path: Path) -> None:
    client = await client_for(tmp_path, settings(tmp_path))
    try:
        assert (await run(client, task("first")))["status"] == "completed"
        events = codex_success()
        events[0]["thread_id"] = "unexpected-session"
        fake_cli(tmp_path, events)
        result = await run(client, task("changed-session", "first"))
        assert result["status"] == "failed"
        assert "resumed a different thread" in result["error"]
    finally:
        await client.store.close()


async def test_running_parent_is_rejected_without_waiting_for_workspace(tmp_path: Path) -> None:
    configuration = settings(tmp_path)
    fake_cli(tmp_path, codex_success(), hang=True)
    client = await client_for(tmp_path, configuration)
    active: asyncio.Task[None] | None = None
    try:
        command = task("running-parent")
        await client.store.accept(command)
        active = asyncio.create_task(client._execute(command))
        async with asyncio.timeout(10):
            while not await asyncio.to_thread((tmp_path / "observed.json").exists):
                if active.done():
                    active.result()
                await asyncio.sleep(0.01)
        before = observed(tmp_path)
        # Invoke the handler without the client's one-slot semaphore: its own
        # continuation check must reject an active parent instead of waiting.
        assert client.handler is not None

        async def progress(value: float, message: str) -> None:
            pass

        from chiliarch.adapters.runners import RunnerError

        with pytest.raises(RunnerError, match="Continuation requires"):
            async with asyncio.timeout(2):
                await client.handler(task("follow-up", "running-parent").payload, progress)
        assert not active.done()
        assert observed(tmp_path) == before
        assert (await client.store.tasks("running-parent"))[0][0] == "running"
    finally:
        if active is not None:
            active.cancel()
            await asyncio.gather(active, return_exceptions=True)
        await client.store.close()


async def test_runner_reports_resume_capability_and_workspace_identity(tmp_path: Path) -> None:
    client = await client_for(tmp_path, settings(tmp_path))
    try:
        metadata = (await client.heartbeat()).payload["metadata"]
        assert metadata["supports_resume"] is True
        assert metadata["notification_mode"] == "followup_task"
        assert metadata["workspace"] == str(tmp_path)
        assert metadata["workspace_id"]
        assert metadata["capacity"] == 1
    finally:
        await client.store.close()
