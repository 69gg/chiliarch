"""Production server configuration bounds HTTP draining without bypassing cleanup."""

from __future__ import annotations

import asyncio
import socket
import sys
from pathlib import Path
from typing import Any

import httpx
import pytest
import uvicorn
from pydantic import BaseModel, ValidationError
from starlette.types import Receive, Scope, Send

from chiliarch import cli
from chiliarch.auth import hash_password
from chiliarch.config import ServerSettings, SettingsManager
from chiliarch.daemon import run_daemon
from chiliarch.daemon_client import DaemonClient
from chiliarch.daemon_models import DaemonConfiguration, private_write


@pytest.mark.parametrize("settings_model", [ServerSettings, DaemonConfiguration])
@pytest.mark.parametrize("invalid", [0, -1])
def test_graceful_shutdown_requires_positive_seconds(
    settings_model: type[BaseModel], invalid: int
) -> None:
    assert settings_model().model_dump()["graceful_shutdown_seconds"] == 15
    with pytest.raises(ValidationError):
        settings_model.model_validate({"graceful_shutdown_seconds": invalid})


def test_platform_shutdown_limit_is_saved_but_requires_restart(tmp_path: Path) -> None:
    manager = SettingsManager(tmp_path / "chiliarch.toml")
    result = manager.update({"server": {"graceful_shutdown_seconds": 8}})
    assert manager.current.server.graceful_shutdown_seconds == 15
    assert manager.saved.server.graceful_shutdown_seconds == 8
    assert result["pending_restart"] == ["server.graceful_shutdown_seconds"]
    assert manager.pending_restart_values == {"server.graceful_shutdown_seconds": 8}
    assert manager.public()["server"]["graceful_shutdown_seconds"] == 15
    assert SettingsManager(manager.path).current.server.graceful_shutdown_seconds == 8
    manager.update({"server": {"graceful_shutdown_seconds": 15}})
    assert manager.pending_restart == []


async def wait_started(server: uvicorn.Server, task: asyncio.Task[None]) -> None:
    async with asyncio.timeout(20):
        while not server.started:
            if task.done():
                task.result()
                raise AssertionError("Server stopped during startup")
            await asyncio.sleep(0.01)


def track_lifespan(config: uvicorn.Config, owned: set[asyncio.Task[Any]]) -> None:
    application = config.app

    async def tracked(scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] == "lifespan":
            task = asyncio.current_task()
            assert task is not None
            owned.add(task)
        await application(scope, receive, send)

    config.app = tracked


async def cancel_and_join(tasks: list[asyncio.Task[Any]]) -> None:
    if not tasks:
        return
    for task in tasks:
        if not task.done():
            task.cancel()
    done, pending = await asyncio.wait(tasks, timeout=5)
    await asyncio.gather(*done, return_exceptions=True)
    assert not pending, f"Owned tasks did not stop: {[task.get_name() for task in pending]}"


async def finish_server(
    server: uvicorn.Server, task: asyncio.Task[None], lifespans: set[asyncio.Task[Any]]
) -> None:
    server.should_exit = True
    await asyncio.wait([task], timeout=5)
    server.force_exit = True
    for listener in getattr(server, "servers", ()):
        listener.close()
    for connection in tuple(server.server_state.connections):
        connection.transport.close()
    # Do not dispose application resources while an HTTP task still uses them.
    await cancel_and_join([task, *server.server_state.tasks])
    await cancel_and_join(list(lifespans))


async def test_platform_cli_stops_while_sse_client_continues_reading(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config_path = tmp_path / "chiliarch.toml"
    manager = SettingsManager(config_path)
    manager.update(
        {
            "auth": {"password_hash": hash_password("shutdown-password")},
            "server": {"graceful_shutdown_seconds": 1},
            "transport": {"retry_seconds": 0.03},
        }
    )
    configurations: list[uvicorn.Config] = []

    def configure(app: Any, **options: Any) -> None:
        # Exercise the actual CLI entry point, then run its exact configuration
        # on a test-owned socket so shutdown can be requested in this event loop.
        configurations.append(uvicorn.Config(app, **options))

    monkeypatch.setattr(uvicorn, "run", configure)
    monkeypatch.setattr(sys, "argv", ["chiliarch", "--config", str(config_path), "serve"])
    cli.main()
    assert len(configurations) == 1
    configuration = configurations[0]
    assert configuration.timeout_graceful_shutdown == 1
    application = configuration.app
    lifespans: set[asyncio.Task[Any]] = set()
    track_lifespan(configuration, lifespans)
    server = uvicorn.Server(configuration)
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    listener.bind(("127.0.0.1", 0))
    url = f"http://127.0.0.1:{listener.getsockname()[1]}"
    task = asyncio.create_task(server.serve(sockets=[listener]))
    try:
        await wait_started(server, task)
        pairing = await application.state.service.execute("create_pairing_code", {})
        async with httpx.AsyncClient(base_url=url, trust_env=False) as http:
            paired = await http.post(
                "/api/agent/pair",
                json={"code": pairing["code"], "name": "shutdown", "machine": "fixture"},
            )
            assert paired.status_code == 200
            credential = paired.json()["credential"]
            async with http.stream(
                "GET", "/api/agent/events", headers={"Authorization": f"Bearer {credential}"}
            ) as response:
                assert response.status_code == 200
                heartbeat = asyncio.Event()
                disconnected = asyncio.Event()

                async def consume() -> None:
                    try:
                        async for line in response.aiter_lines():
                            if line == ": heartbeat":
                                heartbeat.set()
                    except httpx.TransportError:
                        # Uvicorn ends an unfinished SSE response at its deadline.
                        pass
                    finally:
                        disconnected.set()

                reading = asyncio.create_task(consume())
                try:
                    await asyncio.wait_for(heartbeat.wait(), 5)
                    assert not disconnected.is_set()
                    assert server.server_state.tasks
                    server.should_exit = True
                    done, pending = await asyncio.wait([task], timeout=5)
                    assert done and not pending, "An active SSE stream must not prevent shutdown"
                    task.result()
                    await asyncio.wait_for(disconnected.wait(), 5)
                    assert not server.server_state.tasks
                    assert application.state.supervisor.closed
                    assert application.state.service._maintenance_task is None
                finally:
                    await cancel_and_join([reading])
    finally:
        try:
            await finish_server(server, task, lifespans)
        finally:
            listener.close()


async def test_daemon_entry_uses_its_own_http_shutdown_configuration(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    directory = tmp_path / "daemon"
    private_write(directory / "daemon.json", {"graceful_shutdown_seconds": 2})
    original_server = uvicorn.Server
    servers: list[uvicorn.Server] = []
    lifespans: set[asyncio.Task[Any]] = set()
    configured = asyncio.Event()

    def create_server(config: uvicorn.Config) -> uvicorn.Server:
        track_lifespan(config, lifespans)
        server = original_server(config)
        servers.append(server)
        configured.set()
        return server

    monkeypatch.setattr("chiliarch.daemon.uvicorn.Server", create_server)
    task = asyncio.create_task(run_daemon(directory))
    try:
        await asyncio.wait_for(configured.wait(), 5)
        await wait_started(servers[0], task)
        assert servers[0].config.timeout_graceful_shutdown == 2
        async with DaemonClient(directory) as client:
            assert (await client.health())["status"] == "ok"
            await client.shutdown()
        await asyncio.wait_for(task, 5)
        assert not (directory / "runtime.json").exists()
    finally:
        if servers:
            await finish_server(servers[0], task, lifespans)
        else:
            await cancel_and_join([task])
