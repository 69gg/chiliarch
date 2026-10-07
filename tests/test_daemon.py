"""Real local control requests with durable independent agent state and no vendor calls."""

from __future__ import annotations

import asyncio
import json
import secrets
import subprocess
import sys
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import httpx
import pytest
import pytest_asyncio
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from test_agent_mcp import COORDINATION_TOOLS, EXECUTION_TOOLS, payload

from chiliarch import __version__
from chiliarch.adapters.runtime import connection_owner
from chiliarch.client import AgentClient, ClientStore
from chiliarch.daemon import AgentSession, DaemonRuntime, run_daemon
from chiliarch.daemon_client import DaemonClient, RemoteMailbox, ensure_daemon, stop_daemon
from chiliarch.daemon_models import (
    AgentRegistration,
    DaemonError,
    private_write,
    read_configuration,
)
from chiliarch.daemon_services import ServiceStatus
from chiliarch.protocol import Envelope

WORKSPACE = Path(__file__).resolve().parents[1]


async def paired_state(path: Path, name: str) -> None:
    store = ClientStore(path)
    await store.open()
    await store.save_identity(
        {
            "agent_id": name,
            "name": name,
            "machine": "fixture",
            "server_url": "http://127.0.0.1:1",
            "credential": "private-" + name,
        }
    )
    await store.close()


async def test_repeated_cancellation_of_shutdown_waits_for_owned_session_cleanup(
    tmp_path: Path,
) -> None:
    session = AgentSession(
        AgentRegistration(handle="worker", state_path=tmp_path / "worker.db", token="fixture"),
        DaemonRuntime(tmp_path),
    )
    running, cleaning, release, closed = (asyncio.Event() for _ in range(4))

    async def worker() -> None:
        try:
            running.set()
            await asyncio.Event().wait()
        finally:
            cleaning.set()
            await release.wait()
            closed.set()

    session.task = asyncio.create_task(worker())
    await running.wait()
    stopping = asyncio.create_task(session.stop())
    await cleaning.wait()
    stopping.cancel()
    await asyncio.sleep(0)
    stopping.cancel()
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await stopping
    assert closed.is_set()
    assert session.task.done()


@pytest_asyncio.fixture
async def daemon(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> AsyncIterator[tuple[Path, dict[Path, AgentClient]]]:
    clients: dict[Path, AgentClient] = {}

    @asynccontextmanager
    async def local_outbound(
        state_path: Path, **options: Any
    ) -> AsyncIterator[tuple[AgentClient, asyncio.Task[None]]]:
        async with connection_owner(state_path):
            client = AgentClient(
                "http://127.0.0.1:1", "fixture", state_path, manual_tasks=options["manual_tasks"]
            )
            await client.store.open()
            client.transport = "poll"
            clients[state_path] = client
            task = asyncio.create_task(asyncio.Event().wait())
            try:
                yield client, task
            finally:
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
                await client.store.close()

    monkeypatch.setattr("chiliarch.daemon.outbound_client", local_outbound)
    # Probe real loopback HTTP while proving proxy environment cannot intercept it.
    monkeypatch.setenv("HTTP_PROXY", "http://127.0.0.1:1")
    directory = tmp_path / "user daemon 名字"
    task = asyncio.create_task(run_daemon(directory))
    try:
        async with DaemonClient(directory) as client:
            async with asyncio.timeout(5):
                while True:
                    if task.done():
                        task.result()
                    try:
                        await client.health()
                        break
                    except DaemonError:
                        await asyncio.sleep(0.01)
        yield directory, clients
    finally:
        if not task.done():
            async with DaemonClient(directory) as client:
                await client.shutdown()
        await asyncio.wait_for(task, 5)
        assert not (directory / "runtime.json").exists()


async def test_two_agents_have_separate_capabilities_tasks_and_upstream_queues(
    daemon: tuple[Path, dict[Path, AgentClient]],
) -> None:
    directory, clients = daemon
    states = [directory / "alpha.db", directory / "beta.db"]
    async with DaemonClient(directory) as admin:
        for index, path in enumerate(states):
            await paired_state(path, ["alpha", "beta"][index])
            await admin.register(path)
        alpha, beta = RemoteMailbox(directory, "alpha"), RemoteMailbox(directory, "beta")
        try:
            for index, path in enumerate(states):
                envelope = Envelope(
                    type="task.dispatch",
                    payload={"task_id": f"task-{index}", "instruction": "local fixture"},
                )
                await clients[path].store.accept(envelope)
            assert [task["task_id"] for task in (await alpha.list_tasks())["tasks"]] == ["task-0"]
            assert [task["task_id"] for task in (await beta.list_tasks())["tasks"]] == ["task-1"]
            assert (await alpha.claim_task("task-1"))["status"] == "empty"
            assert (await alpha.claim_task("task-0"))["status"] == "claimed"
            await alpha.report_progress("task-0", 0.5, "actual progress")
            result = {"files": ["fixture.txt"]}
            assert (await alpha.complete_task("task-0", result))["duplicate"] is False
            assert (await alpha.complete_task("task-0", result))["duplicate"] is True
            await beta.send_message("coordinate with alpha")
            assert [
                message.payload.get("message")
                for message in await clients[states[1]].store.pending()
            ] == ["coordinate with alpha"]
            assert all(
                message.type == "task.status"
                for message in await clients[states[0]].store.pending()
            )
            runtime = json.loads((directory / "runtime.json").read_text())
            async with httpx.AsyncClient(
                base_url=runtime["url"],
                trust_env=False,
                headers={"Authorization": "Bearer " + alpha.client.token},
            ) as scoped:
                assert (await scoped.get("/agents")).status_code == 401
                assert (await scoped.post("/shutdown")).status_code == 401
                assert (
                    await scoped.post("/agents/beta/mailbox", json={"method": "status"})
                ).status_code == 401
            public = json.dumps(await admin.agents())
            assert "private-alpha" not in public and alpha.client.token not in public
        finally:
            await alpha.close()
            await beta.close()


async def test_registration_is_idempotent_rejects_collision_and_preserves_other_agents(
    daemon: tuple[Path, dict[Path, AgentClient]],
) -> None:
    directory, clients = daemon
    first, second = directory / "first.db", directory / "second.db"
    await paired_state(first, "first")
    await paired_state(second, "second")
    async with DaemonClient(directory) as admin:
        await admin.register(first, handle="worker")
        original = clients[first]
        await admin.register(first, handle="worker")
        assert clients[first] is original
        with pytest.raises(DaemonError):
            await admin.register(second, handle="worker")
        with pytest.raises(DaemonError):
            await admin.register(first, handle="different")
        await admin.register(second)
        old = RemoteMailbox(directory, "worker")
        try:
            await admin.unregister("worker")
            assert first.exists()
            assert [entry["handle"] for entry in await admin.agents()] == ["second"]
            assert clients[second].store.connection is not None
            assert original.store.connection is None
            await admin.register(first, handle="worker")
            with pytest.raises(DaemonError):
                await old.status()
            config = read_configuration(directory)
            assert {entry.handle for entry in config.agents} == {"worker", "second"}
        finally:
            await old.close()


async def test_control_rejects_browser_origin_dns_rebinding_and_unlisted_methods(
    daemon: tuple[Path, dict[Path, AgentClient]],
) -> None:
    directory, _ = daemon
    state = directory / "worker.db"
    await paired_state(state, "worker")
    async with DaemonClient(directory) as admin:
        await admin.register(state)
    runtime = json.loads((directory / "runtime.json").read_text())
    remote = RemoteMailbox(directory, "worker")
    try:
        async with httpx.AsyncClient(base_url=runtime["url"], trust_env=False) as http:
            assert (
                await http.get("/health", headers={"Origin": "https://evil.invalid"})
            ).status_code == 403
            assert (await http.get("/health", headers={"Host": "evil.invalid"})).status_code == 400
            assert (await http.get("/health")).status_code == 401
            headers = {"Authorization": "Bearer " + remote.client.token}
            assert (
                await http.post(
                    "/agents/worker/mailbox", headers=headers, json={"method": "_finish"}
                )
            ).status_code == 422
            assert (
                await http.post(
                    "/agents/worker/mailbox",
                    headers=headers,
                    json={"method": "status", "arguments": {"unexpected": 1}},
                )
            ).status_code == 400
        with pytest.raises(ValueError):
            RemoteMailbox(directory, "../escape")
    finally:
        await remote.close()


async def test_same_identity_cannot_be_connected_by_legacy_and_daemon(
    daemon: tuple[Path, dict[Path, AgentClient]],
) -> None:
    directory, _ = daemon
    state = directory / "owned.db"
    await paired_state(state, "owned")
    async with connection_owner(state), DaemonClient(directory) as admin:
        with pytest.raises(DaemonError):
            await admin.register(state)
        assert await admin.agents() == []
        assert read_configuration(directory).agents == []


async def test_actual_stdio_mcp_uses_shared_daemon_without_owning_a_connection(
    daemon: tuple[Path, dict[Path, AgentClient]],
) -> None:
    directory, clients = daemon
    state = directory / "stdio.db"
    await paired_state(state, "stdio")
    async with DaemonClient(directory) as admin:
        await admin.register(state)
    parameters = StdioServerParameters(
        command=sys.executable,
        args=[
            "-m",
            "chiliarch",
            "agent",
            "mcp",
            "--agent",
            "stdio",
            "--daemon-directory",
            str(directory),
        ],
        cwd=str(WORKSPACE),
    )
    owner = clients[state]
    async with asyncio.timeout(30), stdio_client(parameters) as streams:
        async with ClientSession(*streams) as session:
            await session.initialize()
            assert {
                tool.name for tool in (await session.list_tools()).tools
            } == COORDINATION_TOOLS | EXECUTION_TOOLS
            status = payload(await session.call_tool("status"))
            assert status["agent_id"] == "stdio" and status["mode"] == "daemon"
            assert status["connection"] == "owned_by_daemon"
            report = payload(
                await session.call_tool("send_message", {"message": "actual stdio report"})
            )
            assert report["status"] == "queued"
            assert [message.payload["message"] for message in await owner.store.pending()] == [
                "actual stdio report"
            ]
    # Closing one host does not close the shared connection or release its identity.
    assert owner.store.connection is not None
    async with DaemonClient(directory) as admin:
        assert (await admin.health())["agent_count"] == 1


async def test_managed_identity_does_not_allow_interactive_task_claims(
    daemon: tuple[Path, dict[Path, AgentClient]],
) -> None:
    directory, _ = daemon
    state = directory / "managed.db"
    settings = directory / "runner.toml"
    settings.write_text(f'kind = "codex"\nworkdir = {json.dumps(str(directory))}\n')
    await paired_state(state, "managed")
    async with DaemonClient(directory) as admin:
        assert (await admin.register(state, runner_settings=settings))["mode"] == "managed"
    mailbox = RemoteMailbox(directory, "managed")
    try:
        with pytest.raises(DaemonError, match="Managed runner"):
            await mailbox.claim_task()
    finally:
        await mailbox.close()


class UnavailableService:
    installs: int = 0

    def __init__(self, directory: Path) -> None:
        self.directory = directory

    def status(self) -> ServiceStatus:
        return ServiceStatus("fixture", False, False, False, False, "no native user manager")

    def install(self) -> ServiceStatus:
        type(self).installs += 1
        raise AssertionError("An unavailable manager must never be installed")


class DisabledService(UnavailableService):
    def status(self) -> ServiceStatus:
        return ServiceStatus("fixture", True, True, False, False)

    def start(self) -> ServiceStatus:
        raise AssertionError("Disabled login tasks must use manual background startup")


async def test_explicit_stop_can_authenticate_an_old_version_and_wait_for_cleanup(
    daemon: tuple[Path, dict[Path, AgentClient]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    directory, _ = daemon
    runtime_file = directory / "runtime.json"
    runtime = json.loads(runtime_file.read_text())
    runtime["version"] = "old-installation"
    private_write(runtime_file, runtime)
    monkeypatch.setattr("chiliarch.daemon_client.ServiceManager", UnavailableService)
    async with DaemonClient(directory) as client:
        with pytest.raises(DaemonError, match="version"):
            await client.health()
    assert (await stop_daemon(directory))["status"] == "stopped"
    assert not runtime_file.exists()


@pytest.mark.parametrize("manager", [UnavailableService, DisabledService])
async def test_concurrent_ensure_starts_once_and_preserves_autostart_preference(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    manager: type[UnavailableService],
) -> None:
    monkeypatch.setattr("chiliarch.daemon_client.ServiceManager", manager)
    started: list[list[str]] = []

    async def health(directory: Path) -> dict[str, Any] | None:
        return {"status": "ok", "directory": str(directory)} if started else None

    def spawn(command: list[str], **options: Any) -> Any:
        started.append(command)
        assert options["cwd"] == tmp_path
        return type("Child", (), {"poll": lambda self: None})()

    monkeypatch.setattr("chiliarch.daemon_client._health", health)
    monkeypatch.setattr("chiliarch.daemon_client._spawn_background", spawn)
    results = await asyncio.gather(ensure_daemon(tmp_path), ensure_daemon(tmp_path))
    assert len(started) == 1
    assert sorted(result["reused"] for result in results) == [False, True]
    assert all(result["autostart"]["enabled"] is False for result in results)
    assert started[0][-2:] == ["--directory", str(tmp_path)]


async def test_first_install_without_native_manager_starts_real_detached_daemon_once(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from chiliarch.daemon_client import _spawn_background

    monkeypatch.setattr("chiliarch.daemon_client.ServiceManager", UnavailableService)
    children: list[subprocess.Popen[bytes]] = []

    def spawn(command: list[str], **options: Any) -> subprocess.Popen[bytes]:
        child = _spawn_background(command, **options)
        children.append(child)
        return child

    monkeypatch.setattr("chiliarch.daemon_client._spawn_background", spawn)
    directory = tmp_path / "fresh detached directory 名字"
    try:
        first = await ensure_daemon(directory, autostart=False)
        second = await ensure_daemon(directory, autostart=False)
        assert first["status"] == second["status"] == "ok"
        assert first["reused"] is False and second["reused"] is True
        assert first["pid"] == second["pid"] == children[0].pid
        assert first["autostart"]["enabled"] is False
        assert len(children) == 1
    finally:
        if children:
            await stop_daemon(directory)
            assert await asyncio.to_thread(children[0].wait, timeout=5) == 0


async def test_incompatible_runtime_never_starts_a_replacement(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("chiliarch.daemon_client.ServiceManager", UnavailableService)
    private_write(
        tmp_path / "runtime.json",
        {"version": "99.0.0", "url": "http://127.0.0.1:1", "token": secrets.token_urlsafe(32)},
    )
    with pytest.raises(DaemonError, match="version"):
        await ensure_daemon(tmp_path)


@pytest.mark.parametrize(
    "url",
    [
        "http://example.invalid:42",
        "https://127.0.0.1:42",
        "http://user:secret@127.0.0.1:42",
        "http://127.0.0.1:42/path",
    ],
)
async def test_local_client_cannot_follow_a_nonlocal_runtime_endpoint(
    tmp_path: Path, url: str
) -> None:
    private_write(
        tmp_path / "runtime.json", {"version": __version__, "url": url, "token": "private"}
    )
    async with DaemonClient(tmp_path) as client:
        with pytest.raises(DaemonError, match="loopback"):
            await client.health()
