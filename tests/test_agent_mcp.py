"""Official MCP client negotiates and calls the actual stdio bridge CLI."""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path
from typing import Any

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from mcp.types import CallToolResult
from test_client_network import LiveServer, wait_for_task

from chiliarch.adapters.mailbox import AgentMailbox
from chiliarch.client import AgentClient, ClientStore
from chiliarch.protocol import Envelope

COORDINATION_TOOLS = {
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
EXECUTION_TOOLS = {
    "claim_task",
    "report_progress",
    "complete_task",
    "fail_task",
    "confirm_cancellation",
}
JsonObject = dict[str, Any]


def parameters(state_path: Path, mode: str, *extra: str) -> StdioServerParameters:
    return StdioServerParameters(
        command=sys.executable,
        args=[
            "-m",
            "chiliarch",
            "agent",
            "mcp",
            "--state",
            str(state_path),
            "--mode",
            mode,
            *extra,
        ],
        cwd=str(Path(__file__).resolve().parents[1]),
    )


def payload(result: CallToolResult) -> JsonObject:
    assert not result.isError, result.content
    if result.structuredContent is not None:
        return result.structuredContent
    text = next(item.text for item in result.content if item.type == "text")
    decoded: JsonObject = json.loads(text)
    return decoded


async def test_standalone_stdio_exposes_full_tools_and_executes_explicitly_claimed_work(
    tmp_path: Path,
) -> None:
    platform = LiveServer(tmp_path)
    await platform.start()
    state_path = tmp_path / "paired-agent.sqlite"
    try:
        pairing = await platform.app.state.service.execute("create_pairing_code", {})
        await AgentClient.pair(platform.url, pairing["code"], "mcp-test", "isolated", state_path)
        store = ClientStore(state_path)
        await store.open()
        identity = await store.identity()
        await store.close()
        async with stdio_client(parameters(state_path, "standalone")) as streams:
            async with ClientSession(*streams) as session:
                # Standalone CLI uses the production retry interval (5 seconds),
                # unlike the in-process TCP fixture's shortened transport timers.
                async with asyncio.timeout(30):
                    initialized = await session.initialize()
                    assert initialized.serverInfo.name == "chiliarch"
                    assert {
                        tool.name for tool in (await session.list_tools()).tools
                    } == COORDINATION_TOOLS | EXECUTION_TOOLS
                    status = payload(await session.call_tool("status"))
                    assert status["agent_id"] == identity["agent_id"]
                    assert status["mode"] == "standalone"
                    assert status["connection_task"] == "running"
                    assert identity["credential"] not in json.dumps(status)

                    dispatch = await platform.app.state.service.execute(
                        "dispatch_task",
                        {
                            "agent_id": identity["agent_id"],
                            "instruction": "Inspect without invoking real agents",
                        },
                    )
                    task_id = dispatch["task"]["id"]
                    while True:
                        listed = payload(await session.call_tool("list_tasks"))
                        if listed["tasks"]:
                            break
                        await asyncio.sleep(0.01)
                    assert listed["tasks"][0]["status"] == "queued"
                    claimed = payload(await session.call_tool("claim_task", {"task_id": task_id}))
                    assert claimed["status"] == "claimed"
                    completed = payload(
                        await session.call_tool(
                            "complete_task", {"task_id": task_id, "result": {"checked": True}}
                        )
                    )
                    assert completed["duplicate"] is False
                    retry = payload(
                        await session.call_tool(
                            "complete_task", {"task_id": task_id, "result": {"checked": True}}
                        )
                    )
                    assert retry["duplicate"] is True
                    task = await wait_for_task(platform, task_id, "completed")
                    assert task["result"] == {"checked": True}

                    dispatch = await platform.app.state.service.execute(
                        "dispatch_task",
                        {
                            "agent_id": identity["agent_id"],
                            "instruction": "Cancel manual work only after explicit stop confirmation",
                        },
                    )
                    cancelled_id = dispatch["task"]["id"]
                    while True:
                        tasks = payload(await session.call_tool("list_tasks"))["tasks"]
                        if any(item["task_id"] == cancelled_id for item in tasks):
                            break
                        await asyncio.sleep(0.01)
                    await session.call_tool("claim_task", {"task_id": cancelled_id})
                    await wait_for_task(platform, cancelled_id, "running")
                    await platform.app.state.service.execute(
                        "cancel_task", {"task_id": cancelled_id}
                    )
                    while True:
                        tasks = payload(await session.call_tool("list_tasks"))["tasks"]
                        if any(
                            item["task_id"] == cancelled_id and item["status"] == "cancelling"
                            for item in tasks
                        ):
                            break
                        await asyncio.sleep(0.01)
                    task = (
                        await platform.app.state.service.execute(
                            "get_task", {"task_id": cancelled_id}
                        )
                    )["task"]
                    assert task["status"] == "cancellation_requested"
                    confirmed = payload(
                        await session.call_tool("confirm_cancellation", {"task_id": cancelled_id})
                    )
                    assert confirmed["status"] == "cancelled"
                    await wait_for_task(platform, cancelled_id, "cancelled")
                    request = payload(await session.call_tool("list_agents", {"wait_seconds": 10}))
                    assert request["status"] == "approved"
                    later = payload(
                        await session.call_tool(
                            "get_request_result", {"request_id": request["request_id"]}
                        )
                    )
                    assert later["status"] == "approved"
                    assert any(
                        item["id"] == identity["agent_id"] for item in later["result"]["agents"]
                    )
    finally:
        await platform.stop()


async def test_managed_stdio_mailbox_exposes_only_coordination_and_task_bound_reports(
    tmp_path: Path,
) -> None:
    state_path = tmp_path / "offline-agent.sqlite"
    store = ClientStore(state_path)
    await store.open()
    await store.save_identity(
        {
            "agent_id": "offline-agent",
            "server_url": "http://127.0.0.1:1",
            "credential": "private-test-credential",
        }
    )
    await store.accept(
        Envelope(
            type="task.dispatch",
            payload={"task_id": "managed", "instruction": "Already owned by parent"},
        )
    )
    await store.accept(
        Envelope(
            type="task.dispatch", payload={"task_id": "other", "instruction": "Not this child"}
        )
    )
    await AgentMailbox(store).claim_task("managed")
    try:
        async with stdio_client(
            parameters(state_path, "mailbox", "--managed-task", "managed")
        ) as streams:
            async with ClientSession(*streams) as session:
                async with asyncio.timeout(10):
                    await session.initialize()
                    tools = {tool.name for tool in (await session.list_tools()).tools}
                    assert tools == COORDINATION_TOOLS
                    assert not tools & EXECUTION_TOOLS
                    status = payload(await session.call_tool("status"))
                    assert status["connection"] == "owned_by_runner"
                    assert status["managed_task"] == "managed"
                    assert "private-test-credential" not in json.dumps(status)
                    listed = payload(await session.call_tool("list_tasks"))
                    assert [item["task_id"] for item in listed["tasks"]] == ["managed"]
                    before = len(await store.pending())
                    error = await session.call_tool(
                        "send_message", {"message": "Invalid report", "task_id": "other"}
                    )
                    assert error.isError
                    assert len(await store.pending()) == before
                    sent = payload(
                        await session.call_tool(
                            "send_message", {"message": "Real coordination request"}
                        )
                    )
                    assert sent["status"] == "queued"
                    report = next(
                        item for item in await store.pending() if item.id == sent["message_id"]
                    )
                    assert report.payload["metadata"] == {"task_id": "managed"}
                    request = payload(
                        await session.call_tool(
                            "request_operation",
                            {"operation": "list_agents", "arguments": {}, "wait_seconds": 0},
                        )
                    )
                    assert request["status"] == "pending"
                    peer = payload(
                        await session.call_tool(
                            "send_peer_message",
                            {
                                "message": "Review this result",
                                "recipient_agent_id": "peer",
                                "wait_seconds": 0,
                                "client_request_id": "first-review",
                            },
                        )
                    )
                    outgoing = next(
                        item
                        for item in await store.pending()
                        if item.payload.get("request_id") == peer["request_id"]
                    )
                    assert outgoing.payload["arguments"]["task_id"] == "managed"
                    assert outgoing.payload["metadata"]["task_id"] == "managed"
                    await store.transition("managed", {"running"}, "cancelled", [])
                    error = await session.call_tool("send_message", {"message": "Late report"})
                    assert error.isError
    finally:
        await store.close()
