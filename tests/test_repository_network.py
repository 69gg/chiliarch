"""A repository result travels over exactly the same outbound fallback transports."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest
from test_client_network import LiveServer, stop_client
from test_client_network import live as live


@pytest.mark.parametrize(
    "blocked,transport", [(set(), "ws"), ({"ws"}, "sse"), ({"ws", "sse"}, "poll")]
)
async def test_repository_inspection_over_outbound_transports(
    live: LiveServer, tmp_path: Path, blocked: set[str], transport: str
) -> None:
    calls: list[str] = []

    async def unused_handler(payload: dict[str, Any], progress: Any) -> None:
        raise AssertionError("Repository inspection must not start a model or native task")

    async def execute(payload: dict[str, Any]) -> dict[str, Any]:
        calls.append(payload["command_id"])
        return {
            "command_id": payload["command_id"],
            "status": "completed",
            "result": {
                "repository_id": "42",
                "account": "local-user",
                "push_authentication": "unverified",
            },
        }

    service = live.app.state.service
    live.gate.blocked = blocked
    client, agent_id = await live.agent(tmp_path / "repository.db", unused_handler)
    client.manual_tasks = True
    client.repository_handler = execute
    runner = asyncio.create_task(client.run())
    try:
        async with asyncio.timeout(10):
            while True:
                agent = (await service.execute("get_agent", {"agent_id": agent_id}))["agent"]
                if agent["metadata"].get("supports_repository_commands"):
                    break
                await asyncio.sleep(0.02)
            project = (
                await service.execute(
                    "create_project",
                    {"name": "Demo", "repository": "owner/demo", "target_branch": "trunk"},
                )
            )["project"]
            await service.execute(
                "bind_project_agent", {"project_id": project["id"], "agent_id": agent_id}
            )
            queued = await service.execute(
                "inspect_project_repository", {"project_id": project["id"], "agent_id": agent_id}
            )
            command_id = queued["command"]["id"]
            while True:
                result = await service.execute("get_project_command", {"command_id": command_id})
                if result["command"]["status"] == "completed":
                    break
                await asyncio.sleep(0.02)
            assert result["command"]["result"]["account"] == "local-user"
            assert calls == [command_id]
            assert client.transport == transport
            assert (await service.execute("get_project", {"project_id": project["id"]}))["project"][
                "repository_id"
            ] == "42"
    finally:
        await stop_client(client, runner)
