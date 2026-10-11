"""Real TCP tests: server failures drive the reference client's transport fallback."""

from __future__ import annotations

import asyncio
import contextlib
import json
import socket
import sys
from collections.abc import AsyncIterator, Awaitable, Callable
from pathlib import Path
from typing import Any

import httpx
import pytest
import uvicorn
from fastapi import FastAPI
from fastapi.responses import JSONResponse
from starlette.types import Receive, Scope, Send
from websockets.asyncio.client import connect

from chiliarch.api import create_app
from chiliarch.auth import hash_password
from chiliarch.client import AgentClient, ClientStore, CredentialRejected
from chiliarch.config import Settings
from chiliarch.protocol import Envelope

JsonObject = dict[str, Any]


def pending_task_stacks() -> str:
    """Capture coroutine locations before teardown cancellation changes the evidence."""
    descriptions: list[str] = []
    for task in sorted(asyncio.all_tasks(), key=lambda item: item.get_name()):
        locations: list[str] = []
        waiting: Any = task.get_coro()
        while waiting is not None:
            frame = getattr(waiting, "cr_frame", getattr(waiting, "gi_frame", None))
            if frame is not None:
                locations.append(
                    f"{frame.f_code.co_filename}:{frame.f_lineno} ({frame.f_code.co_name})"
                )
            waiting = getattr(waiting, "cr_await", getattr(waiting, "gi_yieldfrom", None))
        descriptions.append(f"{task.get_name()}: " + " -> ".join(locations))
    return "\n".join(descriptions)


class TransportGate:
    """A real ASGI gateway rejects transport handshakes, while POST remains usable."""

    def __init__(self, app: FastAPI) -> None:
        self.app = app
        self.blocked: set[str] = set()
        self.attempts: list[str] = []
        self.drop_acks = False
        self.dropped_acks = 0
        self.lifespan_task: asyncio.Task[None] | None = None

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] == "lifespan":
            self.lifespan_task = asyncio.current_task()
            try:
                await self.app(scope, receive, send)
            finally:
                self.lifespan_task = None
            return
        path = scope.get("path", "")
        transport = {
            "/api/agent/ws": "ws",
            "/api/agent/events": "sse",
            "/api/agent/poll": "poll",
        }.get(path)
        if transport:
            self.attempts.append(transport)
        if transport in self.blocked:
            if scope["type"] == "websocket":
                await send({"type": "websocket.close", "code": 1008})
            else:
                await JSONResponse(
                    {"detail": "Transport blocked by test gateway"}, status_code=503
                )(scope, receive, send)
            return
        if scope["type"] == "websocket":

            async def filtered_receive() -> Any:
                while True:
                    message = await receive()
                    if self.drop_acks and message["type"] == "websocket.receive":
                        body = json.loads(message.get("text", "{}"))
                        if body.get("type") == "ack":
                            self.dropped_acks += 1
                            continue
                    return message

            await self.app(scope, filtered_receive, send)
        else:
            await self.app(scope, receive, send)


class LiveServer:
    def __init__(self, directory: Path) -> None:
        self.settings = Settings.model_validate(
            {
                "database": {"url": f"sqlite+aiosqlite:///{directory}/platform.db"},
                "auth": {"password_hash": hash_password("network-password")},
                "logs": {"directory": str(directory / "logs")},
                "transport": {
                    "heartbeat_seconds": 0.2,
                    "offline_after_seconds": 5,
                    "retry_seconds": 0.03,
                    "poll_timeout_seconds": 2,
                    "reconnect_base_seconds": 0.02,
                    "reconnect_max_seconds": 0.05,
                    "upgrade_interval_seconds": 60,
                },
            }
        )
        self.port = 0
        self.app = create_app(config=self.settings)
        self.gate = TransportGate(self.app)
        self.server: uvicorn.Server | None = None
        self.task: asyncio.Task[None] | None = None

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    async def start(self) -> None:
        listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        listener.bind(("127.0.0.1", self.port))
        self.port = int(listener.getsockname()[1])
        config = uvicorn.Config(
            self.gate,
            host="127.0.0.1",
            port=self.port,
            log_level="warning",
            access_log=False,
            timeout_graceful_shutdown=1,
        )
        self.server = uvicorn.Server(config)
        self.task = asyncio.create_task(self.server.serve(sockets=[listener]))
        try:
            # Cold SQLite schema creation on native Windows runners can exceed
            # five seconds. This only bounds fixture startup, not transport latency.
            async with asyncio.timeout(20):
                while not self.server.started:
                    if self.task.done():
                        self.task.result()
                        raise RuntimeError("Test platform stopped before becoming ready")
                    await asyncio.sleep(0.01)
        except BaseException:
            tasks = [self.task]
            if self.gate.lifespan_task:
                tasks.append(self.gate.lifespan_task)
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            listener.close()
            self.task = None
            raise

    async def stop(self) -> None:
        if self.server and self.task:
            server = self.server
            server_task = self.task
            server.should_exit = True

            def stalled(phase: str) -> TimeoutError:
                listeners = [
                    {
                        "serving": listener.is_serving(),
                        "active_count": getattr(listener, "_active_count", None),
                        "clients": len(getattr(listener, "_clients", ())),
                    }
                    for listener in server.servers
                ]
                diagnostic = (
                    f"Test platform shutdown stalled during {phase}: "
                    f"connections={len(server.server_state.connections)}, listeners={listeners}\n"
                    + pending_task_stacks()
                )
                # A failing finalizer may itself be blocked. Emit evidence now,
                # without waiting for pytest to print the captured failure.
                print(diagnostic, file=sys.__stderr__, flush=True)
                return TimeoutError(diagnostic)

            try:
                # wait() leaves the blocked task intact long enough to capture
                # its actual cleanup phase, rather than only a CancelledError.
                done, _ = await asyncio.wait([server_task], timeout=5)
                if not done:
                    raise stalled("graceful shutdown")
                await server_task
            finally:
                deadline = asyncio.get_running_loop().time() + 5
                phase = "request cleanup"
                owned: set[asyncio.Task[Any]] = {server_task}

                async def join_phase(tasks: list[asyncio.Task[Any]], name: str) -> None:
                    nonlocal phase
                    phase = name
                    owned.update(tasks)
                    if not tasks:
                        return
                    remaining = max(0, deadline - asyncio.get_running_loop().time())
                    done, pending = await asyncio.wait(tasks, timeout=min(5, remaining))
                    if pending:
                        error = stalled(name)
                        for task in pending:
                            task.cancel()
                        raise error
                    await asyncio.gather(*done, return_exceptions=True)

                async def join_owned_tasks() -> None:
                    for listener in server.servers:
                        listener.close()
                    for connection in tuple(server.server_state.connections):
                        connection.transport.close()
                    # Stop requests before the lifespan disposes their database
                    # and other resources, including when Uvicorn never reached
                    # its own graceful shutdown routine.
                    tasks = [server_task, *server.server_state.tasks]
                    for task in tasks:
                        if not task.done():
                            task.cancel()
                    await join_phase(tasks, "request cleanup")
                    lifespan = self.gate.lifespan_task
                    if lifespan is not None:
                        if not lifespan.done():
                            lifespan.cancel()
                        await join_phase([lifespan], "lifespan cleanup")
                    await join_phase(
                        [
                            asyncio.create_task(listener.wait_closed())
                            for listener in server.servers
                        ],
                        "listener cleanup",
                    )

                cleanup = asyncio.create_task(join_owned_tasks())
                cancelled = False
                while not cleanup.done():
                    remaining = deadline - asyncio.get_running_loop().time()
                    if remaining <= 0:
                        error = stalled(phase)
                        cleanup.cancel()
                        for task in owned:
                            if not task.done():
                                task.cancel()
                        raise error
                    try:
                        # wait() does not cancel cleanup when this task is
                        # cancelled; repeated cancellation never resets its budget.
                        await asyncio.wait([cleanup], timeout=remaining)
                    except asyncio.CancelledError:
                        cancelled = True
                cleanup.result()
                self.task = None
                if cancelled:
                    raise asyncio.CancelledError

    async def restart(self) -> None:
        blocked = self.gate.blocked
        await self.stop()
        self.app = create_app(config=self.settings)
        self.gate = TransportGate(self.app)
        self.gate.blocked = blocked
        await self.start()

    async def agent(
        self,
        state_path: Path,
        handler: Callable[[JsonObject, Callable[[float, str], Awaitable[None]]], Awaitable[Any]],
    ) -> tuple[AgentClient, str]:
        pairing = await self.app.state.service.execute("create_pairing_code", {})
        client = await AgentClient.pair(
            self.url, pairing["code"], "demo", "laptop", state_path, handler=handler
        )
        client.options = self.settings.transport
        identity_store = ClientStore(state_path)
        await identity_store.open()
        identity = await identity_store.identity()
        await identity_store.close()
        return client, identity["agent_id"]


@pytest.fixture
async def live(tmp_path: Path) -> AsyncIterator[LiveServer]:
    server = LiveServer(tmp_path)
    await server.start()
    try:
        yield server
    finally:
        await server.stop()


async def test_cancelled_platform_startup_releases_lifespan_and_listener(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    server = LiveServer(tmp_path)
    entered = asyncio.Event()
    closed = asyncio.Event()
    original_close = server.app.state.database.close

    async def initialize() -> None:
        entered.set()
        await asyncio.Event().wait()

    async def close() -> None:
        await original_close()
        closed.set()

    monkeypatch.setattr(server.app.state.database, "initialize", initialize)
    monkeypatch.setattr(server.app.state.database, "close", close)
    startup = asyncio.create_task(server.start())
    try:
        await asyncio.wait_for(entered.wait(), 5)
        startup.cancel()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(startup, 5)
        assert closed.is_set()
        assert server.task is None
        assert server.gate.lifespan_task is None
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
            listener.bind(("127.0.0.1", server.port))
    finally:
        startup.cancel()
        await asyncio.gather(startup, return_exceptions=True)


@pytest.mark.parametrize("cancel_stop", [True, False], ids=["cancelled", "timed-out"])
async def test_interrupted_platform_shutdown_joins_lifespan_and_releases_listener(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, cancel_stop: bool
) -> None:
    platform = LiveServer(tmp_path)
    entered = asyncio.Event()
    closed = asyncio.Event()
    request_entered = asyncio.Event()
    request_closed = asyncio.Event()
    cleanup_order: list[str] = []
    original_app = platform.gate.app
    original_close = platform.app.state.database.close

    async def hold_request(scope: Scope, receive: Receive, send: Send) -> None:
        if scope.get("path") == "/test-blocked-request":
            request_entered.set()
            try:
                await asyncio.Event().wait()
            finally:
                cleanup_order.append("request")
                request_closed.set()
        else:
            await original_app(scope, receive, send)

    async def close() -> None:
        await original_close()
        cleanup_order.append("database")
        closed.set()

    async def hold_shutdown(sockets: list[socket.socket] | None = None) -> None:
        entered.set()
        await asyncio.Event().wait()

    monkeypatch.setattr(platform.gate, "app", hold_request)
    monkeypatch.setattr(platform.app.state.database, "close", close)
    await platform.start()
    assert platform.server is not None
    monkeypatch.setattr(platform.server, "shutdown", hold_shutdown)
    maintenance = platform.app.state.service._maintenance_task
    sweeper = platform.app.state.supervisor.sweeper
    server_task = platform.task
    async with httpx.AsyncClient(timeout=None, trust_env=False) as client:
        request = asyncio.create_task(client.get(platform.url + "/test-blocked-request"))
        shutdown: asyncio.Task[None] | None = None
        try:
            async with asyncio.timeout(8):
                await request_entered.wait()
                requests = tuple(platform.server.server_state.tasks)
                assert requests
                shutdown = asyncio.create_task(platform.stop())
                await entered.wait()
                if cancel_stop:
                    shutdown.cancel()
                    with pytest.raises(asyncio.CancelledError):
                        await shutdown
                else:
                    with pytest.raises(TimeoutError) as error:
                        await shutdown
                    # Capture nested coroutine locations before cancellation.
                    assert "hold_shutdown" in str(error.value)
                    assert "hold_request" in str(error.value)
                with pytest.raises(httpx.TransportError):
                    await request
            assert request_closed.is_set() and closed.is_set()
            assert cleanup_order == ["request", "database"]
            assert all(task.done() for task in requests)
            assert not platform.server.server_state.tasks
            assert not platform.server.server_state.connections
            assert maintenance.done()
            assert sweeper.done()
            assert server_task is not None and server_task.done()
            assert platform.task is None
            assert platform.gate.lifespan_task is None
            assert all(not listener.is_serving() for listener in platform.server.servers)
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
                # Match startup's reuse option: the accepted connection may
                # legitimately remain in TCP TIME_WAIT after both ends close.
                listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
                listener.bind(("127.0.0.1", platform.port))
        finally:
            request.cancel()
            await asyncio.gather(request, return_exceptions=True)
            if shutdown is not None:
                shutdown.cancel()
                await asyncio.gather(shutdown, return_exceptions=True)
            else:
                await platform.stop()


async def test_stalled_listener_cleanup_fails_with_immediate_diagnostics(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capfd: pytest.CaptureFixture[str]
) -> None:
    platform = LiveServer(tmp_path)
    await platform.start()
    assert platform.server is not None
    original_shutdown = platform.server.shutdown
    entered = asyncio.Event()
    released = asyncio.Event()
    cancelled = asyncio.Event()

    async def stalled_listener() -> None:
        entered.set()
        try:
            await released.wait()
        finally:
            cancelled.set()

    async def shutdown_then_stall(sockets: list[socket.socket] | None = None) -> None:
        await original_shutdown(sockets)
        for listener in platform.server.servers:
            monkeypatch.setattr(listener, "wait_closed", stalled_listener)

    monkeypatch.setattr(platform.server, "shutdown", shutdown_then_stall)
    shutdown = asyncio.create_task(platform.stop())
    try:
        await asyncio.wait_for(entered.wait(), 5)
        # A repeated caller cancellation must not extend the cleanup deadline.
        shutdown.cancel()
        await asyncio.sleep(0)
        shutdown.cancel()
        done, pending = await asyncio.wait([shutdown], timeout=6)
        assert done and not pending, "Cleanup must fail instead of shielding forever"
        with pytest.raises(TimeoutError, match="listener cleanup"):
            shutdown.result()
        diagnostic = capfd.readouterr().err
        assert "listener cleanup" in diagnostic
        assert "stalled_listener" in diagnostic
        assert "connections=0" in diagnostic
        assert "'serving': False" in diagnostic
        await asyncio.wait_for(cancelled.wait(), 1)
    finally:
        # Release the simulated block even if a regression breaks the deadline;
        # the test's own failure path must not leave an unjoinable task behind.
        released.set()
        shutdown.cancel()
        done, pending = await asyncio.wait([shutdown], timeout=5)
        assert not pending
        await asyncio.gather(*done, return_exceptions=True)


async def wait_for_task(live: LiveServer, task_id: str, status: str) -> JsonObject:
    async with asyncio.timeout(8):
        while True:
            task: JsonObject = (
                await live.app.state.service.execute("get_task", {"task_id": task_id})
            )["task"]
            if task["status"] == status:
                return task
            await asyncio.sleep(0.02)


async def stop_client(client: AgentClient, runner: asyncio.Task[None]) -> None:
    client.stop()
    with contextlib.suppress(CredentialRejected):
        async with asyncio.timeout(5):
            await runner


@pytest.mark.parametrize(
    "blocked,expected", [(set(), "ws"), ({"ws"}, "sse"), ({"ws", "sse"}, "poll")]
)
async def test_outbound_client_completes_tasks_through_real_fallback(
    live: LiveServer, tmp_path: Path, blocked: set[str], expected: str
) -> None:
    calls: list[str] = []

    async def handler(
        payload: JsonObject, progress: Callable[[float, str], Awaitable[None]]
    ) -> JsonObject:
        calls.append(payload["task_id"])
        await progress(0.5, "working")
        return {"answer": 42}

    live.gate.blocked = blocked
    client, agent_id = await live.agent(tmp_path / "client.db", handler)
    task = (
        await live.app.state.service.execute(
            "dispatch_task", {"agent_id": agent_id, "instruction": "Inspect"}
        )
    )["task"]
    runner = asyncio.create_task(client.run())
    try:
        completed = await wait_for_task(live, task["id"], "completed")
        assert completed["result"] == {"answer": 42}
        assert calls == [task["id"]]
        assert client.transport == expected
        assert (
            client.transport_history
            == ["ws", "sse", "poll"][: ["ws", "sse", "poll"].index(expected) + 1]
        )
        assert set(live.gate.attempts) >= blocked | {expected}
        agent = (await live.app.state.service.execute("get_agent", {"agent_id": agent_id}))["agent"]
        assert agent["transport"] == expected
        assert agent["online"]
        assert await live.app.state.service.pending(agent_id) == []
    finally:
        await stop_client(client, runner)


async def test_server_queue_survives_full_app_restart(live: LiveServer, tmp_path: Path) -> None:
    calls: list[str] = []

    async def handler(payload: JsonObject, _: Callable[[float, str], Awaitable[None]]) -> str:
        calls.append(payload["task_id"])
        return "done"

    client, agent_id = await live.agent(tmp_path / "durable-client.db", handler)
    task = (
        await live.app.state.service.execute(
            "dispatch_task", {"agent_id": agent_id, "instruction": "Persist"}
        )
    )["task"]
    original = (await live.app.state.service.pending(agent_id))[0]
    await live.restart()
    recovered = (await live.app.state.service.pending(agent_id))[0]
    assert recovered.canonical() == original.canonical()
    runner = asyncio.create_task(client.run())
    try:
        await wait_for_task(live, task["id"], "completed")
        assert calls == [task["id"]]
    finally:
        await stop_client(client, runner)


async def test_unacknowledged_message_crosses_ws_and_poll_with_same_identity(
    live: LiveServer, tmp_path: Path
) -> None:
    async def handler(_: JsonObject, __: Callable[[float, str], Awaitable[None]]) -> None:
        return None

    client, agent_id = await live.agent(tmp_path / "identity-client.db", handler)
    await live.app.state.service.execute(
        "dispatch_task", {"agent_id": agent_id, "instruction": "Inspect"}
    )
    async with connect(
        live.url.replace("http://", "ws://") + "/api/agent/ws",
        additional_headers={"Authorization": f"Bearer {client.credential}"},
    ) as websocket:
        received = Envelope.model_validate_json(await websocket.recv())
    async with httpx.AsyncClient(
        base_url=live.url, headers={"Authorization": f"Bearer {client.credential}"}
    ) as http:
        response = await http.get("/api/agent/poll", params={"timeout": 0})
        assert response.status_code == 200
        assert (
            Envelope.model_validate(response.json()["messages"][0]).canonical()
            == received.canonical()
        )
        ack = Envelope(type="ack", payload={"message_id": received.id})
        assert (await http.post("/api/agent/messages", json=ack.model_dump(mode="json"))).json()[
            "ack"
        ] is None
        assert (await http.get("/api/agent/poll", params={"timeout": 0})).json()["messages"] == []


async def test_sse_credential_revocation_terminates_client(
    live: LiveServer, tmp_path: Path
) -> None:
    async def handler(_: JsonObject, __: Callable[[float, str], Awaitable[None]]) -> None:
        return None

    live.gate.blocked = {"ws"}
    client, agent_id = await live.agent(tmp_path / "revoke-client.db", handler)
    runner = asyncio.create_task(client.run())
    async with asyncio.timeout(5):
        while not (await live.app.state.service.execute("get_agent", {"agent_id": agent_id}))[  # noqa: ASYNC110
            "agent"
        ]["online"]:
            await asyncio.sleep(0.02)
        while "sse" not in live.gate.attempts:  # noqa: ASYNC110
            await asyncio.sleep(0.01)
    await live.app.state.service.execute("revoke_agent", {"agent_id": agent_id})
    with pytest.raises(CredentialRejected):
        async with asyncio.timeout(3):
            await runner
    assert runner.done()


async def test_client_running_ledger_recovers_as_interrupted_without_replaying_handler(
    live: LiveServer, tmp_path: Path
) -> None:
    calls: list[str] = []

    async def handler(payload: JsonObject, _: Callable[[float, str], Awaitable[None]]) -> None:
        calls.append(payload["task_id"])

    state_path = tmp_path / "interrupted-client.db"
    client, agent_id = await live.agent(state_path, handler)
    task = (
        await live.app.state.service.execute(
            "dispatch_task", {"agent_id": agent_id, "instruction": "External side effect"}
        )
    )["task"]
    command = (await live.app.state.service.pending(agent_id))[0]
    store = ClientStore(state_path)
    await store.open()
    await store.accept(command)
    await store.state(task["id"], "running", client._status(command, "running"))
    await store.close()
    restored = await AgentClient.from_state(state_path, handler, options=live.settings.transport)
    runner = asyncio.create_task(restored.run())
    try:
        interrupted = await wait_for_task(live, task["id"], "interrupted")
        assert interrupted["error"]
        assert calls == []
        assert await live.app.state.service.pending(agent_id) == []
    finally:
        await stop_client(restored, runner)


async def test_lost_ws_acks_retransmit_without_reexecuting_handler(
    live: LiveServer, tmp_path: Path
) -> None:
    release = asyncio.Event()
    calls: list[str] = []

    async def handler(payload: JsonObject, _: Callable[[float, str], Awaitable[None]]) -> str:
        calls.append(payload["task_id"])
        await release.wait()
        return "completed once"

    live.gate.drop_acks = True
    client, agent_id = await live.agent(tmp_path / "lost-ack-client.db", handler)
    task = (
        await live.app.state.service.execute(
            "dispatch_task", {"agent_id": agent_id, "instruction": "Single side effect"}
        )
    )["task"]
    original = (await live.app.state.service.pending(agent_id))[0]
    runner = asyncio.create_task(client.run())
    try:
        async with asyncio.timeout(5):
            while live.gate.dropped_acks < 3 or not calls:  # noqa: ASYNC110
                await asyncio.sleep(0.01)
        assert calls == [task["id"]]
        assert (await live.app.state.service.pending(agent_id))[0].id == original.id
        live.gate.drop_acks = False
        release.set()
        await wait_for_task(live, task["id"], "completed")
        assert calls == [task["id"]]
        assert await live.app.state.service.pending(agent_id) == []
    finally:
        release.set()
        await stop_client(client, runner)


async def test_sse_without_agent_heartbeats_becomes_offline(
    live: LiveServer, tmp_path: Path
) -> None:
    async def handler(_: JsonObject, __: Callable[[float, str], Awaitable[None]]) -> None:
        return None

    client, agent_id = await live.agent(tmp_path / "quiet-client.db", handler)
    async with httpx.AsyncClient(
        base_url=live.url, headers={"Authorization": f"Bearer {client.credential}"}
    ) as http:
        async with http.stream("GET", "/api/agent/events") as response:
            assert response.status_code == 200
            lines = response.aiter_lines()
            assert await anext(lines) == ": chiliarch connected"
            assert (await live.app.state.service.execute("get_agent", {"agent_id": agent_id}))[
                "agent"
            ]["online"]
            await asyncio.sleep(live.settings.transport.offline_after_seconds + 0.1)
            assert not (await live.app.state.service.execute("get_agent", {"agent_id": agent_id}))[
                "agent"
            ]["online"]
            assert response.is_closed is False


async def test_permanent_http_protocol_error_stops_client_without_fallback_loop(
    live: LiveServer, tmp_path: Path
) -> None:
    async def handler(_: JsonObject, __: Callable[[float, str], Awaitable[None]]) -> None:
        return None

    client, _ = await live.agent(tmp_path / "invalid-client.db", handler)
    await client.store.open()
    await client.request("operation_that_does_not_exist", {})
    await client.store.close()
    runner = asyncio.create_task(client.run(preferred="poll"))
    try:
        with pytest.raises(ValueError):
            async with asyncio.timeout(3):
                await runner
        assert client.transport_history == ["poll"]
    finally:
        if not runner.done():
            client.stop()
            runner.cancel()
            await asyncio.gather(runner, return_exceptions=True)
