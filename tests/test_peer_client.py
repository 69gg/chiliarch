"""Peer tools use persistent, task-scoped mailboxes without invoking native agents."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest
from test_daemon import daemon as daemon
from test_daemon import paired_state

from chiliarch.adapters.mailbox import AgentMailbox
from chiliarch.client import AgentClient, ClientStore
from chiliarch.daemon_client import DaemonClient, RemoteMailbox
from chiliarch.protocol import Envelope


def peer_message(task_id: str | None, text: str = "A peer reply") -> Envelope:
    return Envelope(
        type="collaboration.message",
        payload={
            "message_id": "peer-message-" + (task_id or "general"),
            "sender_agent_id": "peer",
            "sender_role": "agent",
            "recipient_agent_id": "self",
            "task_id": task_id,
            "message": text,
            "sequence": 1,
        },
    )


async def running_task(store: ClientStore, task_id: str) -> None:
    await store.accept(
        Envelope(type="task.dispatch", payload={"task_id": task_id, "instruction": "fixture"})
    )
    await store.state(task_id, "running")


async def test_request_retry_survives_ack_restart_and_concurrent_mcp_bridges(
    tmp_path: Path,
) -> None:
    path = tmp_path / "identity.sqlite"
    first, second = ClientStore(path), ClientStore(path)
    await first.open()
    await second.open()
    try:
        await running_task(first, "task-a")
        a = AgentMailbox(first, manual_tasks=False, managed_task="task-a")
        b = AgentMailbox(second, manual_tasks=False, managed_task="task-a")
        kwargs: dict[str, Any] = {
            "message": "Can you review this?",
            "recipient_agent_id": "peer",
            "wait_seconds": 0,
            "client_request_id": "review-1",
        }
        one, two = await asyncio.gather(
            a.send_peer_message(**kwargs), b.send_peer_message(**kwargs)
        )
        assert one["request_id"] == two["request_id"]
        queued = await first.pending()
        assert len(queued) == 1
        assert queued[0].payload["arguments"]["task_id"] == "task-a"
        assert queued[0].payload["metadata"]["task_id"] == "task-a"
        await first.acknowledge(queued[0].id)
        assert await first.pending() == []
        await first.close()
        await first.open()
        retry = await a.send_peer_message(**kwargs)
        assert retry["request_id"] == one["request_id"]
        assert await first.pending() == []
        with pytest.raises(ValueError, match="different arguments"):
            await a.send_peer_message(**{**kwargs, "message": "Different side effect"})
        await first.accept(
            Envelope(
                type="request.resolved",
                payload={
                    "request_id": one["request_id"],
                    "status": "approved",
                    "result": {"message_id": "peer-result"},
                },
            )
        )
        done = await a.send_peer_message(**kwargs)
        assert done["result"] == {"message_id": "peer-result"}
        assert await first.pending() == []
        messages = await a.read_messages()
        assert [m["payload"]["request_id"] for m in messages["messages"]] == [one["request_id"]]
    finally:
        await first.close()
        await second.close()


async def test_task_scoped_inbox_and_explicit_cursor_survive_restart(tmp_path: Path) -> None:
    store = ClientStore(tmp_path / "inbox.sqlite")
    await store.open()
    try:
        await running_task(store, "a")
        await running_task(store, "b")
        a = AgentMailbox(store, manual_tasks=False, managed_task="a")
        b = AgentMailbox(store, manual_tasks=False, managed_task="b")
        request_a = await a.list_agents(wait_seconds=0)
        request_b = await b.list_agents(wait_seconds=0)
        for item in await store.pending():
            await store.acknowledge(item.id)
        incoming = [peer_message("b"), peer_message("a"), peer_message(None)]
        for envelope in incoming:
            assert await store.accept(envelope)
            assert not await store.accept(envelope)
        for request in (request_b, request_a):
            await store.accept(
                Envelope(
                    type="request.resolved",
                    payload={"request_id": request["request_id"], "status": "approved"},
                )
            )
        await store.accept(
            Envelope(
                type="agent.message", payload={"message": "B only", "metadata": {"task_id": "b"}}
            )
        )
        await store.accept(Envelope(type="task.cancel", payload={"task_id": "b"}))
        page = await a.read_messages(after=None)
        assert len(page["messages"]) == 2
        assert page["messages"][0]["payload"]["task_id"] == "a"
        assert page["messages"][1]["payload"]["request_id"] == request_a["request_id"]
        # A lost tool response can be read again; only explicit acknowledgment consumes it.
        assert await a.read_messages(after=None) == page
        await store.close()
        await store.open()
        assert await a.read_messages(after=None) == page
        with pytest.raises(ValueError, match="another managed task"):
            await a.read_messages(task_id="b")
        with pytest.raises(ValueError, match="does not belong"):
            await a.get_request_result(request_b["request_id"])
        b_page = await b.read_messages()
        with pytest.raises(ValueError, match="does not belong"):
            await a.acknowledge_messages(b_page["cursor"])
        await a.acknowledge_messages(page["cursor"])
        await store.close()
        await store.open()
        assert (await a.read_messages(after=None))["messages"] == []
        assert await a.read_messages(after=0) == page
        assert len((await b.read_messages(after=None))["messages"]) == 4
        assert len((await AgentMailbox(store).read_messages())["messages"]) == 7
    finally:
        await store.close()


async def test_transport_ack_occurs_after_peer_message_is_durable(tmp_path: Path) -> None:
    client = AgentClient("http://127.0.0.1:1", "fixture", tmp_path / "transport.sqlite")
    await client.store.open()
    acknowledgments: list[Envelope] = []

    async def sender(ack: Envelope) -> None:
        assert (await client.store.messages())[0][1].type == "collaboration.message"
        acknowledgments.append(ack)

    message = peer_message(None)
    try:
        await client._accept(message, sender)
        await client._accept(message, sender)
        assert len(acknowledgments) == 2
        assert {ack.payload["message_id"] for ack in acknowledgments} == {message.id}
        assert len(await client.store.messages()) == 1
        # Transport receipt is not a read/processed receipt and does not invoke any handler.
        assert await client.store.pending() == []
        assert client._workers == {}
    finally:
        await client.store.close()


async def test_continuation_reads_and_acknowledges_ancestor_peer_reply_only(tmp_path: Path) -> None:
    store = ClientStore(tmp_path / "continuations.sqlite")
    await store.open()
    try:
        await running_task(store, "ancestor")
        await store.state("ancestor", "completed")
        await running_task(store, "sibling")
        await store.accept(
            Envelope(
                type="task.dispatch",
                payload={
                    "task_id": "child",
                    "instruction": "Continue completed work",
                    "parameters": {"chiliarch_context": {"continue_task_id": "ancestor"}},
                },
            )
        )
        await store.state("child", "running")
        await store.accept(peer_message("sibling"))
        await store.accept(peer_message("ancestor", "Late reply to the old task"))
        await store.accept(Envelope(type="task.cancel", payload={"task_id": "ancestor"}))
        current = AgentMailbox(store, manual_tasks=False, managed_task="child")
        page = await current.read_messages(after=None)
        assert len(page["messages"]) == 1
        assert page["messages"][0]["payload"]["task_id"] == "ancestor"
        assert page["messages"][0]["type"] == "collaboration.message"
        await current.acknowledge_messages(page["cursor"])
        await store.close()
        await store.open()
        assert (await current.read_messages(after=None))["messages"] == []
        assert (await current.read_messages(after=0))["messages"] == page["messages"]
        # A malformed/untrusted continuation cannot inherit still-running work.
        await store.accept(
            Envelope(
                type="task.dispatch",
                payload={
                    "task_id": "invalid",
                    "instruction": "Not a completed continuation",
                    "parameters": {"chiliarch_context": {"continue_task_id": "sibling"}},
                },
            )
        )
        await store.state("invalid", "running")
        invalid = AgentMailbox(store, manual_tasks=False, managed_task="invalid")
        assert (await invalid.read_messages())["messages"] == []
    finally:
        await store.close()


async def test_daemon_proxy_routes_peer_tools_to_correct_identity(
    daemon: tuple[Path, dict[Path, AgentClient]],
) -> None:
    directory, clients = daemon
    path = directory / "peer.db"
    await paired_state(path, "self")
    async with DaemonClient(directory) as admin:
        await admin.register(path, handle="self")
    proxy = RemoteMailbox(directory, "self")
    try:
        store = clients[path].store
        await running_task(store, "current")
        sent = await proxy.send_peer_message(
            "Check the patch",
            recipient_agent_id="reviewer",
            task_id="current",
            recipient_task_id="review",
            wait_seconds=0,
            client_request_id="review-patch",
        )
        duplicate = await proxy.send_peer_message(
            "Check the patch",
            recipient_agent_id="reviewer",
            task_id="current",
            recipient_task_id="review",
            wait_seconds=0,
            client_request_id="review-patch",
        )
        assert sent["request_id"] == duplicate["request_id"]
        request = (await store.pending())[0]
        assert request.payload["operation"] == "send_peer_message"
        assert request.payload["arguments"]["recipient_task_id"] == "review"
        assert request.payload["metadata"] == {"task_id": "current"}
        await store.accept(peer_message("current"))
        result = await proxy.read_messages(after=None, task_id="current")
        assert result["messages"][0]["payload"]["message"] == "A peer reply"
        await proxy.acknowledge_messages(result["cursor"], task_id="current")
        assert (await proxy.read_messages(after=None, task_id="current"))["messages"] == []
        await proxy.acknowledge_peer_message("peer-message-current", "read", wait_seconds=0)
        receipt = (await store.pending())[-1]
        assert receipt.payload["operation"] == "acknowledge_peer_message"
        assert receipt.payload["arguments"] == {
            "message_id": "peer-message-current",
            "state": "read",
        }
        await proxy.wait_for_tasks([], task_id="current", wait_seconds=0)
        assert (await store.pending())[-1].payload["arguments"]["waiting_for"] == []
    finally:
        await proxy.close()
