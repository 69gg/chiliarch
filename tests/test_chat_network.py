"""Real TCP verifies provider -> persistent run -> authenticated UI streaming."""

from __future__ import annotations

import asyncio
import json
import socket
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import httpx
import pytest
import uvicorn
from fastapi import FastAPI, Request
from fastapi.responses import StreamingResponse
from sqlalchemy import update
from test_ai_providers import chat
from test_chat_supervisor import wait_status
from test_client_network import LiveServer

from chiliarch.database import UserSession


@asynccontextmanager
async def upstream_server(app: FastAPI) -> AsyncIterator[str]:
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.bind(("127.0.0.1", 0))
    port = listener.getsockname()[1]
    server = uvicorn.Server(
        uvicorn.Config(app, log_level="warning", access_log=False, timeout_graceful_shutdown=1)
    )
    task = asyncio.create_task(server.serve(sockets=[listener]))
    try:
        async with asyncio.timeout(5):
            while not server.started:
                if task.done():
                    task.result()
                await asyncio.sleep(0.01)
        yield f"http://127.0.0.1:{port}"
    finally:
        server.should_exit = True
        await asyncio.wait_for(task, 5)
        listener.close()


async def test_real_http_stream_survives_browser_disconnect_and_resumes_cursor(
    tmp_path: Path,
) -> None:
    model = FastAPI()
    release = asyncio.Event()
    bodies: list[dict[str, Any]] = []

    @model.post("/v1/chat/completions")
    async def complete(request: Request) -> StreamingResponse:
        bodies.append(await request.json())

        async def events() -> AsyncIterator[str]:
            yield "data: " + json.dumps(chat({"content": "第一段"})) + "\n\n"
            await release.wait()
            yield "data: " + json.dumps(chat({"content": "第二段"}, "stop")) + "\n\n"
            yield "data: [DONE]\n\n"

        return StreamingResponse(events(), media_type="text/event-stream")

    platform = LiveServer(tmp_path)
    async with upstream_server(model) as url:
        await platform.start()
        try:
            platform.app.state.settings_manager.update(
                {
                    "model": {
                        "model": "network-model",
                        "base_url": url + "/v1",
                        "api_key": "network-key",
                    }
                }
            )
            async with httpx.AsyncClient(base_url=platform.url) as browser:
                assert (await browser.get("/api/chat/conversations")).status_code == 401
                login = await browser.post("/api/auth/login", json={"password": "network-password"})
                browser.headers["X-CSRF-Token"] = login.json()["csrf_token"]
                created = await browser.post("/api/chat/conversations", json={"title": "网络验收"})
                assert created.status_code == 200
                conversation_id = created.json()["id"]
                sent = await browser.post(
                    f"/api/chat/conversations/{conversation_id}/messages", json={"content": "开始"}
                )
                assert sent.status_code == 200
                run_id = sent.json()["run_id"]
                path = f"/api/chat/conversations/{conversation_id}/events"
                async with asyncio.timeout(5):
                    async with browser.stream("GET", path) as response:
                        assert response.status_code == 200
                        async for line in response.aiter_lines():
                            if line.startswith("data: "):
                                event = json.loads(line.removeprefix("data: "))
                                if event["type"] == "text_delta":
                                    break
                assert event["data"]["delta"] == "第一段" and not release.is_set()
                first_seq = event["seq"]
                # Closing the UI connection leaves the model run alive.
                assert (await platform.app.state.supervisor.get_run(run_id))["status"] == "running"
                release.set()
                await wait_status(platform.app.state.supervisor, run_id, "completed")
                snapshot = (await browser.get(f"/api/chat/conversations/{conversation_id}")).json()
                assert snapshot["messages"][-1]["content"] == "第一段第二段"
                received: list[dict[str, Any]] = []
                async with asyncio.timeout(5):
                    async with browser.stream(
                        "GET", path, headers={"Last-Event-ID": str(first_seq)}
                    ) as response:
                        async for line in response.aiter_lines():
                            if line.startswith("data: "):
                                event = json.loads(line.removeprefix("data: "))
                                received.append(event)
                                if event["type"] == "completed":
                                    break
                assert [
                    event["data"]["delta"] for event in received if event["type"] == "text_delta"
                ] == ["第二段"]
                assert bodies[0]["model"] == "network-model" and bodies[0]["stream"] is True
                assert bodies[0]["messages"][-1] == {"role": "user", "content": "开始"}
        finally:
            release.set()
            await platform.stop()


@pytest.mark.parametrize("action", ["logout", "change_password", "expire"])
async def test_existing_chat_stream_stops_when_user_session_is_invalidated(
    tmp_path: Path, action: str
) -> None:
    platform = LiveServer(tmp_path)
    await platform.start()
    try:
        async with httpx.AsyncClient(base_url=platform.url) as browser:
            login = await browser.post("/api/auth/login", json={"password": "network-password"})
            browser.headers["X-CSRF-Token"] = login.json()["csrf_token"]
            created = await browser.post("/api/chat/conversations", json={"title": "会话失效"})
            conversation_id = created.json()["id"]
            async with browser.stream(
                "GET", f"/api/chat/conversations/{conversation_id}/events"
            ) as response:
                assert response.status_code == 200
                if action == "logout":
                    assert (await browser.post("/api/auth/logout")).status_code == 200
                elif action == "change_password":
                    changed = await browser.post(
                        "/api/operations/change_password",
                        json={"arguments": {"password": "replacement-password"}},
                    )
                    assert changed.status_code == 200
                else:
                    async with platform.app.state.database.sessions.begin() as session:
                        await session.execute(update(UserSession).values(expires_at=0))
                await platform.app.state.supervisor._event(
                    conversation_id, None, "completed", {"status": "completed"}
                )
                async with asyncio.timeout(3):
                    leaked = [
                        json.loads(line.removeprefix("data: "))
                        async for line in response.aiter_lines()
                        if line.startswith("data: ")
                    ]
                assert leaked == []
                assert (await browser.get("/api/chat/conversations")).status_code == 401
    finally:
        await platform.stop()
