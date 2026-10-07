"""Registration failure must not orphan leases or switch uncommitted policies."""

from __future__ import annotations

import asyncio
import json
import socket
from pathlib import Path
from typing import Any

import pytest
from filelock import FileLock, Timeout

from chiliarch import __version__
from chiliarch.adapters.mailbox import AgentMailbox
from chiliarch.adapters.runtime import connection_owner
from chiliarch.client import AgentClient, ClientStore
from chiliarch.daemon import AgentSession, DaemonRuntime
from chiliarch.daemon_client import stop_daemon
from chiliarch.daemon_models import DaemonError, RegistrationRequest, private_write
from chiliarch.daemon_services import ServiceStatus


async def paired_state(path: Path) -> None:
    store = ClientStore(path)
    await store.open()
    try:
        await store.save_identity(
            {
                "agent_id": "persistence-fixture",
                "credential": "persistence-fixture-only",
                "server_url": "http://never-contacted.invalid",
                "name": "persistence fixture",
                "machine": "test-only",
            }
        )
    finally:
        await store.close()


def isolated_sessions(monkeypatch: pytest.MonkeyPatch) -> list[AgentSession]:
    """Real state/lease ownership, without opening a platform connection."""
    created: list[AgentSession] = []

    async def run(session: AgentSession) -> None:
        created.append(session)
        try:
            async with connection_owner(session.registration.state_path):
                client = AgentClient(
                    "http://never-contacted.invalid",
                    "fixture-only",
                    session.registration.state_path,
                    manual_tasks=True,
                )
                await client.store.open()
                client.transport = session.registration.preferred_transport
                session.client = client
                session.mailbox = AgentMailbox(client.store)
                session.ready.set_result(None)
                try:
                    await asyncio.Event().wait()
                finally:
                    await client.store.close()
        except asyncio.CancelledError:
            raise
        except Exception:
            if not session.ready.done():
                session.ready.set_exception(DaemonError("Fixture could not acquire its lease"))
        finally:
            session.client = None
            session.mailbox = None
            if not session.ready.done():
                session.ready.cancel()

    monkeypatch.setattr(AgentSession, "run", run)
    return created


async def close_all(runtime: DaemonRuntime, created: list[AgentSession]) -> None:
    await runtime.close()
    # This fallback makes a failed regression test safe as well: it must never
    # leave the deliberately detected orphan fixture holding a lease.
    await asyncio.gather(*(session.stop() for session in created))


@pytest.mark.parametrize("failed_write", ["capability", "configuration"])
async def test_failed_new_registration_releases_session_and_leaves_persistent_state_empty(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, failed_write: str
) -> None:
    created = isolated_sessions(monkeypatch)
    state_path = tmp_path / "identity.db"
    await paired_state(state_path)
    runtime = DaemonRuntime(tmp_path / "daemon")
    runtime.configuration.startup_timeout_seconds = 1
    await runtime.start()
    before = (runtime.directory / "daemon.json").read_bytes()
    injected = False

    def write(path: Path, payload: dict[str, Any]) -> None:
        nonlocal injected
        target = (
            path == runtime.connection_file("worker")
            if failed_write == "capability"
            else path.name == "daemon.json"
        )
        if target and not injected:
            injected = True
            raise OSError("Injected persistence failure")
        private_write(path, payload)

    monkeypatch.setattr("chiliarch.daemon.private_write", write)
    try:
        async with asyncio.timeout(3):
            with pytest.raises((OSError, DaemonError)):
                await runtime.register(RegistrationRequest(state_path=state_path, handle="worker"))
        assert injected
        assert runtime.sessions == {}
        assert runtime.configuration.agents == []
        assert json.loads((runtime.directory / "daemon.json").read_bytes()) == json.loads(before)
        assert not runtime.connection_file("worker").exists()
        assert created and all(
            session.task is not None and session.task.done() for session in created
        )
        async with connection_owner(state_path):
            pass
        await runtime.close()
        assert all(session.task is not None and session.task.done() for session in created)
    finally:
        await close_all(runtime, created)


@pytest.mark.parametrize("failed_write", ["capability", "configuration"])
async def test_failed_policy_update_restores_previous_registration_and_usable_capability(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, failed_write: str
) -> None:
    created = isolated_sessions(monkeypatch)
    state_path = tmp_path / "identity.db"
    await paired_state(state_path)
    runtime = DaemonRuntime(tmp_path / "daemon")
    runtime.configuration.startup_timeout_seconds = 1
    await runtime.start()
    await runtime.register(RegistrationRequest(state_path=state_path, handle="worker"))
    original = runtime.configuration.agents[0].model_copy(deep=True)
    disk = (runtime.directory / "daemon.json").read_bytes()
    capability = runtime.connection_file("worker").read_bytes()
    injected = False

    def write(path: Path, payload: dict[str, Any]) -> None:
        nonlocal injected
        target = (
            path == runtime.connection_file("worker")
            if failed_write == "capability"
            else path.name == "daemon.json"
        )
        if target and not injected:
            injected = True
            raise OSError("Injected persistence failure")
        private_write(path, payload)

    monkeypatch.setattr("chiliarch.daemon.private_write", write)
    try:
        async with asyncio.timeout(3):
            with pytest.raises((OSError, DaemonError)):
                await runtime.register(
                    RegistrationRequest(
                        state_path=state_path,
                        handle="worker",
                        preferred_transport="poll",
                        max_concurrent_tasks=2,
                    )
                )
        assert injected
        assert runtime.configuration.agents == [original]
        assert json.loads((runtime.directory / "daemon.json").read_bytes()) == json.loads(disk)
        assert json.loads(runtime.connection_file("worker").read_bytes()) == json.loads(capability)
        session = runtime.sessions["worker"]
        assert session.registration == original
        assert session.client is not None and session.client.transport == "ws"
        assert session.mailbox is not None
        assert (await session.mailbox.status())["agent_id"] == "persistence-fixture"
        active = {entry for entry in created if entry.task is not None and not entry.task.done()}
        assert active == {session}
        await runtime.close()
        assert all(entry.task is not None and entry.task.done() for entry in created)
        async with connection_owner(state_path):
            pass
    finally:
        await close_all(runtime, created)


async def test_failed_unregister_restores_identity_connection_and_persistent_registration(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    created = isolated_sessions(monkeypatch)
    state_path = tmp_path / "identity.db"
    await paired_state(state_path)
    runtime = DaemonRuntime(tmp_path / "daemon")
    await runtime.start()
    await runtime.register(RegistrationRequest(state_path=state_path, handle="worker"))
    original = runtime.configuration.agents[0].model_copy(deep=True)
    disk = (runtime.directory / "daemon.json").read_bytes()
    capability = runtime.connection_file("worker").read_bytes()

    def write(path: Path, payload: dict[str, Any]) -> None:
        if path.name == "daemon.json":
            raise OSError("Injected unregister persistence failure")
        private_write(path, payload)

    monkeypatch.setattr("chiliarch.daemon.private_write", write)
    try:
        with pytest.raises((OSError, DaemonError)):
            await runtime.unregister("worker")
        assert runtime.configuration.agents == [original]
        assert (runtime.directory / "daemon.json").read_bytes() == disk
        assert runtime.connection_file("worker").read_bytes() == capability
        session = runtime.sessions["worker"]
        assert session.registration == original and session.mailbox is not None
        assert (await session.mailbox.status())["agent_id"] == "persistence-fixture"
        assert {entry for entry in created if entry.task is not None and not entry.task.done()} == {
            session
        }
    finally:
        await close_all(runtime, created)


async def test_unreachable_daemon_with_owned_lease_cannot_be_reported_stopped(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    class UnavailableManager:
        def __init__(self, directory: Path) -> None:
            self.directory = directory

        def status(self) -> ServiceStatus:
            return ServiceStatus("fixture", False, False, False, False)

    monkeypatch.setattr("chiliarch.daemon_client.ServiceManager", UnavailableManager)
    owner = FileLock(str(tmp_path / "daemon.lock"), timeout=0, thread_local=False)
    await asyncio.to_thread(owner.acquire)
    try:
        # Reserve a loopback port without listening; no real service can take it
        # between choosing the port and the actual HTTP health request.
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as closed_endpoint:
            closed_endpoint.bind(("127.0.0.1", 0))
            private_write(
                tmp_path / "runtime.json",
                {
                    "url": f"http://127.0.0.1:{closed_endpoint.getsockname()[1]}",
                    "version": __version__,
                    "token": "closed-endpoint-fixture",
                },
            )
            async with asyncio.timeout(3):
                with pytest.raises(DaemonError):
                    await stop_daemon(tmp_path)
        assert owner.is_locked
        assert (tmp_path / "runtime.json").exists()
        competitor = FileLock(str(tmp_path / "daemon.lock"), timeout=0, thread_local=False)
        with pytest.raises(Timeout):
            await asyncio.to_thread(competitor.acquire)
    finally:
        await asyncio.to_thread(owner.release)
