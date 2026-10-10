"""Two independent outbound agents collaborate without a supervisor model."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from contextlib import AsyncExitStack
from pathlib import Path
from typing import Any, Literal

import pytest
from test_client_network import LiveServer, wait_for_task

from chiliarch.adapters.mailbox import AgentMailbox
from chiliarch.adapters.runtime import outbound_client
from chiliarch.client import AgentClient

JsonObject = dict[str, Any]
Progress = Callable[[float, str], Awaitable[None]]


@pytest.mark.parametrize("transport", ["ws", "sse", "poll"])
async def test_discover_wake_reply_and_receipts_across_outbound_transports(
    tmp_path: Path, transport: Literal["ws", "sse", "poll"]
) -> None:
    platform = LiveServer(tmp_path)
    await platform.start()
    try:
        service = platform.app.state.service
        service.settings_manager.current.supervisor.mode = "yolo"
        states = {name: tmp_path / f"{name}.sqlite" for name in ("author", "reviewer")}
        identities: dict[str, str] = {}
        for name, state in states.items():
            pairing = await service.execute("create_pairing_code", {})
            paired = await AgentClient.pair(platform.url, pairing["code"], name, name, state)
            await paired.store.open()
            try:
                identities[name] = (await paired.store.identity())["agent_id"]
            finally:
                await paired.store.close()

        handled: list[str] = []
        reviewer: AgentClient

        async def review(payload: JsonObject, progress: Progress) -> JsonObject:
            message = payload["parameters"]["collaboration_message"]
            mailbox = AgentMailbox(
                reviewer.store, manual_tasks=False, managed_task=payload["task_id"]
            )
            handled.append(payload["task_id"])
            await progress(0.5, "Reviewing peer request")
            decision = await mailbox.send_peer_message(
                "Review complete: evidence checked",
                recipient_agent_id=message["sender_agent_id"],
                reply_to=message["id"],
                wait_seconds=5,
                client_request_id="review-result",
            )
            assert decision["status"] == "approved"
            acknowledged = await mailbox.acknowledge_peer_message(
                message["id"], "processed", wait_seconds=5
            )
            assert acknowledged["status"] == "approved"
            return {"reviewed": message["id"]}

        async with AsyncExitStack() as stack:
            author, _ = await stack.enter_async_context(
                outbound_client(
                    states["author"],
                    manual_tasks=True,
                    preferred=transport,
                    options=platform.settings.transport,
                )
            )
            reviewer, _ = await stack.enter_async_context(
                outbound_client(
                    states["reviewer"],
                    handler=review,
                    preferred=transport,
                    options=platform.settings.transport,
                )
            )
            # Exercise actual heartbeat generation, rather than inventing metadata shape.
            await service.receive(identities["reviewer"], await reviewer.heartbeat())
            mailbox = AgentMailbox(author.store)
            discovered = await mailbox.list_agents(wait_seconds=5)
            assert discovered["status"] == "approved"
            assert {row["id"] for row in discovered["result"]["agents"]} == set(identities.values())
            result = await mailbox.send_peer_message(
                "Please independently review this result",
                recipient_agent_id=identities["reviewer"],
                wake=True,
                wait_seconds=5,
                client_request_id="review-request",
            )
            assert result["status"] == "approved"
            task_id = result["result"]["wake_task_id"]
            completed = await wait_for_task(platform, task_id, "completed")
            assert completed["result"]["reviewed"] == result["result"]["message"]["id"]
            replay = await mailbox.send_peer_message(
                "Please independently review this result",
                recipient_agent_id=identities["reviewer"],
                wake=True,
                wait_seconds=5,
                client_request_id="review-request",
            )
            assert replay == result
            assert handled == [task_id]
            async with asyncio.timeout(5):
                while True:
                    received = await mailbox.read_messages()
                    replies = [
                        row
                        for row in received["messages"]
                        if row["type"] == "collaboration.message"
                    ]
                    if replies:
                        break
                    await asyncio.sleep(0.01)
            assert replies[0]["payload"]["sender_agent_id"] == identities["reviewer"]
            assert replies[0]["payload"]["reply_to"] == result["result"]["message"]["id"]
            receipt = await mailbox.get_peer_message(
                result["result"]["message"]["id"], wait_seconds=5
            )
            assert receipt["result"]["receipts"][0]["state"] == "processed"
            # File bytes use this same live outbound connection, including multi-chunk payloads.
            evidence = bytes(range(256)) * 600
            source, destination = tmp_path / "evidence.bin", tmp_path / "reviewed.bin"
            await asyncio.to_thread(source.write_bytes, evidence)
            uploaded = await mailbox.upload_artifact(
                str(source), [identities["reviewer"]], client_request_id="review-evidence"
            )
            assert uploaded["status"] == "completed"
            recipient = AgentMailbox(reviewer.store, manual_tasks=False)
            downloaded = await recipient.download_artifact(uploaded["upload_id"], str(destination))
            assert downloaded["status"] == "completed"
            assert await asyncio.to_thread(destination.read_bytes) == evidence
            # Generic mailbox reads must continue to expose metadata only.
            for item in (await recipient.read_messages())["messages"]:
                assert "data_base64" not in (item["payload"].get("result") or {})
            assert not platform.settings.model.model
    finally:
        await platform.stop()
