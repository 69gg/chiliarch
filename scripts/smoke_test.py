"""Run in an isolated installation of the actual wheel or source distribution."""

from __future__ import annotations

import asyncio
import importlib.resources
import json
import tempfile
from pathlib import Path

from fastapi.testclient import TestClient

from chiliarch import __version__
from chiliarch.adapters.mcp import create_mcp_server
from chiliarch.api import create_app
from chiliarch.auth import hash_password
from chiliarch.config import Settings
from chiliarch.protocol import Envelope, protocol_schema


def main() -> None:
    resources = importlib.resources.files("chiliarch")
    assert resources.joinpath("static/index.html").is_file()
    assert json.loads(resources.joinpath("protocol/schema.json").read_text()) == protocol_schema()
    assert Envelope(type="heartbeat").protocol_version == "1.0"
    with tempfile.TemporaryDirectory() as directory:
        state_path = Path(directory) / "agent.db"
        standalone_tools = asyncio.run(create_mcp_server(state_path).list_tools())
        mailbox_tools = asyncio.run(create_mcp_server(state_path, mode="mailbox").list_tools())
        assert {tool.name for tool in standalone_tools} - {tool.name for tool in mailbox_tools} == {
            "claim_task",
            "report_progress",
            "complete_task",
            "fail_task",
            "confirm_cancellation",
        }
        assert {tool.name for tool in mailbox_tools} == {
            "status",
            "list_tasks",
            "read_messages",
            "acknowledge_messages",
            "send_message",
            "request_operation",
            "get_request_result",
            "list_agents",
            "create_channel",
            "list_channels",
            "get_channel",
            "send_peer_message",
            "acknowledge_peer_message",
            "get_peer_message",
            "wait_for_tasks",
            "list_task_waits",
            "upload_artifact",
            "download_artifact",
            "list_artifacts",
            "get_artifact",
        }
        settings = Settings.model_validate(
            {
                "database": {"url": f"sqlite+aiosqlite:///{Path(directory) / 'smoke.db'}"},
                "logs": {"directory": directory},
                "auth": {"password_hash": hash_password("smoke-test-password")},
            }
        )
        with TestClient(create_app(config=settings)) as client:
            assert client.get("/").status_code == 200
            response = client.post("/api/auth/login", json={"password": "smoke-test-password"})
            assert response.status_code == 200
            assert client.get("/api/state").status_code == 200
            for operation, field in (
                ("list_channels", "channels"),
                ("list_task_waits", "waits"),
                ("list_artifacts", "artifacts"),
            ):
                queried = client.post(
                    "/api/operations/" + operation,
                    json={"arguments": {}},
                    headers={"X-CSRF-Token": response.json()["csrf_token"]},
                )
                assert queried.status_code == 200
                assert queried.json()[field] == []
    print(
        f"chiliarch {__version__}: installed package, UI, schema, MCP tools and authenticated API verified"
    )


if __name__ == "__main__":
    main()
