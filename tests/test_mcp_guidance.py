"""Exercise the inbox and receipt workflow advertised to real MCP clients."""

from __future__ import annotations

import asyncio
from pathlib import Path

from mcp import ClientSession
from mcp.client.stdio import stdio_client
from test_agent_mcp import JsonObject, parameters, payload
from test_client_network import LiveServer
from test_peer_client import peer_message, running_task

from chiliarch.adapters.mailbox import AgentMailbox
from chiliarch.client import AgentClient, ClientStore


async def receive_peer(session: ClientSession, message_id: str) -> JsonObject:
    async with asyncio.timeout(15):
        while True:
            page = payload(await session.call_tool("read_messages", {"after": None}))
            if any(item["payload"].get("message_id") == message_id for item in page["messages"]):
                return page
            await asyncio.sleep(0.05)


async def receipt_state(platform: LiveServer, message_id: str, expected: str) -> None:
    async with asyncio.timeout(15):
        while True:
            view = await platform.app.state.service.execute(
                "get_peer_message", {"message_id": message_id}
            )
            if view["receipts"][0]["state"] == expected:
                return
            await asyncio.sleep(0.05)


async def test_stdio_inbox_cursor_is_distinct_from_peer_read_and_processed_receipts(
    tmp_path: Path,
) -> None:
    platform = LiveServer(tmp_path)
    await platform.start()
    state_path = tmp_path / "reader.sqlite"
    try:
        service = platform.app.state.service
        service.settings_manager.current.supervisor.mode = "yolo"
        pairing = await service.execute("create_pairing_code", {})
        paired = await AgentClient.pair(
            platform.url, pairing["code"], "reader", "isolated", state_path
        )
        await paired.store.open()
        try:
            identity = await paired.store.identity()
        finally:
            await paired.store.close()

        async with stdio_client(parameters(state_path, "standalone")) as streams:
            async with ClientSession(*streams) as session:
                async with asyncio.timeout(45):
                    await session.initialize()
                    first = await service.execute(
                        "send_peer_message",
                        {"recipient_agent_id": identity["agent_id"], "message": "Review this"},
                    )
                    first_id = first["message"]["id"]
                    page = await receive_peer(session, first_id)
                    assert [item["payload"]["message_id"] for item in page["messages"]] == [
                        first_id
                    ]
                    # A lost read response is safe to retry. Delivery and reading do not
                    # advance the explicit local cursor or report application handling.
                    assert (
                        payload(await session.call_tool("read_messages", {"after": None})) == page
                    )
                    await receipt_state(platform, first_id, "delivered")
                    saved = payload(
                        await session.call_tool("acknowledge_messages", {"cursor": page["cursor"]})
                    )
                    assert saved["cursor"] == page["cursor"]
                    assert not payload(await session.call_tool("read_messages", {"after": None}))[
                        "messages"
                    ]
                    view = await service.execute("get_peer_message", {"message_id": first_id})
                    assert view["receipts"][0]["state"] == "delivered"

                    for state in ("read", "processed"):
                        arguments = {
                            "message_id": first_id,
                            "state": state,
                            "client_request_id": f"receipt-{state}",
                            "wait_seconds": 10,
                        }
                        result = payload(
                            await session.call_tool("acknowledge_peer_message", arguments)
                        )
                        assert result["status"] == "approved"
                        assert (
                            payload(await session.call_tool("acknowledge_peer_message", arguments))
                            == result
                        )
                        await receipt_state(platform, first_id, state)

                    # Read the request resolutions too, then save the next incremental position.
                    receipts = payload(await session.call_tool("read_messages", {"after": None}))
                    assert len(receipts["messages"]) == 2
                    assert {item["type"] for item in receipts["messages"]} == {"request.resolved"}
                    await session.call_tool("acknowledge_messages", {"cursor": receipts["cursor"]})
                    second = await service.execute(
                        "send_peer_message",
                        {"recipient_agent_id": identity["agent_id"], "message": "New follow-up"},
                    )
                    second_id = second["message"]["id"]
                    later = await receive_peer(session, second_id)
                    assert [item["payload"]["message_id"] for item in later["messages"]] == [
                        second_id
                    ]
                    assert later["cursor"] > receipts["cursor"]
                    replay = payload(await session.call_tool("read_messages", {"after": 0}))
                    assert [
                        item["payload"]["message_id"]
                        for item in replay["messages"]
                        if item["type"] == "collaboration.message"
                    ] == [first_id, second_id]
    finally:
        await platform.stop()


async def test_stdio_managed_inbox_cursor_survives_reconnect_without_crossing_task_scope(
    tmp_path: Path,
) -> None:
    state_path = tmp_path / "managed-reader.sqlite"
    store = ClientStore(state_path)
    await store.open()
    try:
        await store.save_identity(
            {
                "agent_id": "self",
                "server_url": "http://127.0.0.1:1",
                "credential": "isolated-test-credential",
            }
        )
        for task_id in ("current", "sibling"):
            await running_task(store, task_id)
        first, sibling = peer_message("current"), peer_message("sibling")
        for envelope in (first, sibling, peer_message(None)):
            assert await store.accept(envelope)
        sibling_page = await AgentMailbox(store, managed_task="sibling").read_messages()

        async with stdio_client(
            parameters(state_path, "mailbox", "--managed-task", "current")
        ) as streams:
            async with ClientSession(*streams) as session:
                async with asyncio.timeout(15):
                    await session.initialize()
                    page = payload(await session.call_tool("read_messages", {"after": None}))
                    assert [item["id"] for item in page["messages"]] == [first.id]
                    assert (
                        payload(await session.call_tool("read_messages", {"after": None})) == page
                    )
                    assert (
                        await session.call_tool("read_messages", {"task_id": "sibling"})
                    ).isError
                    assert (
                        await session.call_tool(
                            "acknowledge_messages", {"cursor": sibling_page["cursor"]}
                        )
                    ).isError
                    assert (
                        payload(await session.call_tool("read_messages", {"after": None})) == page
                    )
                    await session.call_tool("acknowledge_messages", {"cursor": page["cursor"]})

        later = peer_message("current", "Arrived between sessions")
        await store.accept(later)
        async with stdio_client(
            parameters(state_path, "mailbox", "--managed-task", "current")
        ) as streams:
            async with ClientSession(*streams) as session:
                async with asyncio.timeout(15):
                    await session.initialize()
                    resumed = payload(await session.call_tool("read_messages", {"after": None}))
                    assert [item["id"] for item in resumed["messages"]] == [later.id]
                    replay = payload(await session.call_tool("read_messages", {"after": 0}))
                    assert [item["id"] for item in replay["messages"]] == [first.id, later.id]
                    # Advancing one task's inbox never consumes a sibling's messages.
                    assert (
                        await AgentMailbox(store, managed_task="sibling").read_messages()
                        == sibling_page
                    )
    finally:
        await store.close()
