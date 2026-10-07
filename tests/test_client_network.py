"""Real TCP tests: server failures drive the reference client's transport fallback."""

from __future__ import annotations

import asyncio
import contextlib
import json
import socket
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


class TransportGate:
    """A real ASGI gateway rejects transport handshakes, while POST remains usable."""

    def __init__(self, app: FastAPI) -> None:
        self.app = app
        self.blocked: set[str] = set()
        self.attempts: list[str] = []
        self.drop_acks = False
        self.dropped_acks = 0

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
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
                    "heartbeat_seconds": 0.05,
                    "offline_after_seconds": 0.5,
                    "retry_seconds": 0.03,
                    "poll_timeout_seconds": 0.15,
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
        async with asyncio.timeout(5):
            while not self.server.started:
                if self.task.done():
                    self.task.result()
                await asyncio.sleep(0.01)

    async def stop(self) -> None:
        if self.server and self.task:
            self.server.should_exit = True
            async with asyncio.timeout(5):
                await self.task
            self.task = None

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
        async with asyncio.timeout(3):
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
