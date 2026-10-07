"""Durable interactive-agent work and outbound bridge behavior."""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Literal

import pytest
from test_client_network import LiveServer, wait_for_task

from chiliarch.adapters.mailbox import AgentMailbox, open_mailbox
from chiliarch.adapters.runtime import connection_owner, outbound_client
from chiliarch.client import AgentClient, ClientStore
from chiliarch.protocol import Envelope

JsonObject = dict[str, Any]


@pytest.fixture
async def store(tmp_path: Path) -> AsyncIterator[ClientStore]:
    opened = ClientStore(tmp_path / "agent.sqlite")
    await opened.open()
    await opened.save_identity(
        {
            "agent_id": "local-agent",
            "credential": "credential-must-never-be-public",
            "server_url": "http://127.0.0.1:1",
            "name": "test-agent",
            "machine": "isolated-machine",
        }
    )
    try:
        yield opened
    finally:
        await opened.close()


def command(task_id: str = "task-one", *, expired: bool = False) -> Envelope:
    return Envelope(
        type="task.dispatch",
        correlation_id=f"dispatch:{task_id}",
        expires_at=datetime.now(UTC) - timedelta(seconds=1) if expired else None,
        payload={"task_id": task_id, "instruction": f"Do real work for {task_id}"},
    )


async def discard(_: Envelope) -> None:
    pass


async def wait_until_received(mailbox: AgentMailbox, task_id: str) -> None:
    async with asyncio.timeout(5):
        while not (await mailbox.store.tasks(task_id)):  # noqa: ASYNC110 -- poll durable SQLite, not an in-process event
            await asyncio.sleep(0.01)


@pytest.mark.parametrize("transport", ["ws", "sse", "poll"])
async def test_manual_outbound_only_queues_until_explicit_claim(
    tmp_path: Path, transport: Literal["ws", "sse", "poll"]
) -> None:
    platform = LiveServer(tmp_path)
    await platform.start()
    state_path = tmp_path / "agent.sqlite"
    try:
        pairing = await platform.app.state.service.execute("create_pairing_code", {})
        await AgentClient.pair(platform.url, pairing["code"], "manual", "isolated", state_path)
        async with outbound_client(
            state_path,
            manual_tasks=True,
            preferred=transport,
            options=platform.settings.transport,
        ) as (client, _runner):
            identity = await client.store.identity()
            dispatch = await platform.app.state.service.execute(
                "dispatch_task",
                {
                    "agent_id": identity["agent_id"],
                    "instruction": "Manual agent must explicitly accept",
                },
            )
            task_id = dispatch["task"]["id"]
            mailbox = AgentMailbox(client.store)
            await wait_until_received(mailbox, task_id)
            # A scheduling tick would fail unhandled work in the automatic client.
            await client._schedule()
            assert (await mailbox.list_tasks())["tasks"][0]["status"] == "queued"
            assert not client._workers
            reports = [item for item in await client.store.pending() if item.type == "task.status"]
            assert not reports
            task = (await platform.app.state.service.execute("get_task", {"task_id": task_id}))[
                "task"
            ]
            assert task["status"] in {"queued", "delivered"}

            claimed = await mailbox.claim_task(task_id)
            assert claimed["status"] == "claimed"
            await mailbox.report_progress(task_id, 0.5, "Half completed")
            await mailbox.complete_task(task_id, {"actual_result": "done"})
            completed = await wait_for_task(platform, task_id, "completed")
            assert completed["result"] == {"actual_result": "done"}
            assert completed["progress"] == 1
    finally:
        await platform.stop()


async def test_claim_is_atomic_across_distinct_store_connections(store: ClientStore) -> None:
    await store.accept(command())
    other = ClientStore(store.path)
    await other.open()
    try:
        first, second = AgentMailbox(store), AgentMailbox(other)
        claims = await asyncio.gather(first.claim_task(), second.claim_task())
        assert sorted(item["status"] for item in claims) == ["claimed", "empty"]
        assert (await store.tasks())[0][0] == "running"
        reports = await store.pending()
        assert [item.payload["status"] for item in reports] == ["accepted", "running"]
        with pytest.raises(ValueError, match="already running"):
            await second.claim_task("task-one")
        assert len(await store.pending()) == 2
    finally:
        await other.close()


async def test_progress_and_completion_obey_claim_and_idempotent_outcome(
    store: ClientStore,
) -> None:
    await store.accept(command())
    mailbox = AgentMailbox(store)
    with pytest.raises(ValueError, match="from queued"):
        await mailbox.report_progress("task-one", 0.2)
    with pytest.raises(ValueError, match="from queued"):
        await mailbox.complete_task("task-one", {"count": 1})
    assert not await store.pending()
    await mailbox.claim_task("task-one")
    with pytest.raises(ValueError, match="from running"):
        await mailbox.confirm_cancellation("task-one")
    progress = await mailbox.report_progress("task-one", 0.5, "Real progress")
    assert progress["status"] == "queued"
    assert (await store.pending())[-1].payload["progress"] == 0.5
    first = await mailbox.complete_task("task-one", {"count": 1, "ok": True})
    queue_size = len(await store.pending())
    retry = await mailbox.complete_task("task-one", {"ok": True, "count": 1})
    assert first == {"status": "completed", "delivery": "queued", "duplicate": False}
    assert retry == {"status": "completed", "delivery": "queued", "duplicate": True}
    assert len(await store.pending()) == queue_size
    with pytest.raises(ValueError, match="from completed"):
        await mailbox.complete_task("task-one", {"count": 2})
    with pytest.raises(ValueError, match="from completed"):
        await mailbox.report_progress("task-one", 1)
    assert (await store.tasks())[0][0] == "completed"


async def test_expired_command_is_failed_without_being_claimed(store: ClientStore) -> None:
    await store.accept(command(expired=True))
    mailbox = AgentMailbox(store)
    assert await mailbox.claim_task() == {"status": "empty"}
    assert (await store.tasks())[0][0] == "failed"
    reports = await store.pending()
    assert len(reports) == 1
    assert reports[0].payload["status"] == "failed"
    assert "expired" in reports[0].payload["error"]


@pytest.mark.parametrize("before_claim", [False, True])
async def test_cancel_prevents_completion_and_progress(
    store: ClientStore, before_claim: bool
) -> None:
    await store.accept(command())
    mailbox = AgentMailbox(store)
    if not before_claim:
        await mailbox.claim_task("task-one")
    client = AgentClient("http://127.0.0.1:1", "unused", store.path, manual_tasks=True)
    client.store = store
    cancellation = Envelope(type="task.cancel", payload={"task_id": "task-one"})
    await client._accept(cancellation, discard)
    expected = "cancelled" if before_claim else "cancelling"
    assert (await store.tasks())[0][0] == expected
    with pytest.raises(ValueError, match=expected):
        await mailbox.complete_task("task-one", {"false_success": True})
    with pytest.raises(ValueError, match=expected):
        await mailbox.report_progress("task-one", 0.7)
    assert not any(item.payload.get("status") == "completed" for item in await store.pending())
    if not before_claim:
        assert not any(item.payload.get("status") == "cancelled" for item in await store.pending())
        confirmed = await mailbox.confirm_cancellation("task-one")
        assert confirmed["status"] == "cancelled"
        assert (await store.tasks())[0][0] == "cancelled"
        before = await store.pending()
        await mailbox.confirm_cancellation("task-one")
        assert await store.pending() == before


async def test_manual_running_cancel_ack_is_not_execution_stopped(tmp_path: Path) -> None:
    platform = LiveServer(tmp_path)
    await platform.start()
    state_path = tmp_path / "cancelling-agent.sqlite"
    try:
        pairing = await platform.app.state.service.execute("create_pairing_code", {})
        await AgentClient.pair(platform.url, pairing["code"], "manual", "isolated", state_path)
        async with outbound_client(
            state_path, manual_tasks=True, options=platform.settings.transport
        ) as (client, _runner):
            identity = await client.store.identity()
            dispatch = await platform.app.state.service.execute(
                "dispatch_task",
                {
                    "agent_id": identity["agent_id"],
                    "instruction": "Manual work needs an explicit stop confirmation",
                },
            )
            task_id = dispatch["task"]["id"]
            mailbox = AgentMailbox(client.store)
            await wait_until_received(mailbox, task_id)
            await mailbox.claim_task(task_id)
            await wait_for_task(platform, task_id, "running")
            await platform.app.state.service.execute("cancel_task", {"task_id": task_id})
            async with asyncio.timeout(5):
                while (await client.store.tasks(task_id))[0][0] != "cancelling":  # noqa: ASYNC110 -- observe durable state
                    await asyncio.sleep(0.01)
                # A transport ACK only confirms reception. Ensure the cancellation
                # is no longer in the server queue before checking business state.
                while any(  # noqa: ASYNC110 -- observe transport ACK persisted by server
                    item.type == "task.cancel"
                    for item in await platform.app.state.service.pending(identity["agent_id"])
                ):
                    await asyncio.sleep(0.01)
            task = (await platform.app.state.service.execute("get_task", {"task_id": task_id}))[
                "task"
            ]
            assert task["status"] == "cancellation_requested"
            assert not any(
                item.payload.get("status") == "cancelled" for item in await client.store.pending()
            )
            await mailbox.confirm_cancellation(task_id)
            cancelled = await wait_for_task(platform, task_id, "cancelled")
            assert cancelled["status"] == "cancelled"
    finally:
        await platform.stop()


async def test_late_cancel_does_not_replace_completed_result(store: ClientStore) -> None:
    await store.accept(command())
    mailbox = AgentMailbox(store)
    await mailbox.claim_task()
    await mailbox.complete_task("task-one", {"actual": "finished"})
    before = await store.pending()
    client = AgentClient("http://127.0.0.1:1", "unused", store.path, manual_tasks=True)
    client.store = store
    cancellation = Envelope(type="task.cancel", payload={"task_id": "task-one"})
    await client._accept(cancellation, discard)
    assert (await store.tasks())[0][0] == "completed"
    assert await store.pending() == before
    assert (await mailbox.read_messages())["messages"][0]["type"] == "task.cancel"


async def test_replies_survive_ack_duplicate_delivery_and_reopen(store: ClientStore) -> None:
    mailbox = AgentMailbox(store)
    pending = await mailbox.request_operation("list_agents", {}, "Who can collaborate?")
    request_id = pending["request_id"]
    request = (await store.pending())[0]
    assert pending == {"status": "pending", "request_id": request_id}
    client = AgentClient("http://127.0.0.1:1", "unused", store.path, manual_tasks=True)
    client.store = store
    sent: list[Envelope] = []

    async def acknowledge(envelope: Envelope) -> None:
        sent.append(envelope)

    await client._accept(Envelope(type="ack", payload={"message_id": request.id}), acknowledge)
    assert (await mailbox.get_request_result(request_id))["status"] == "pending"
    reply = Envelope(
        type="request.resolved",
        correlation_id=request_id,
        payload={"request_id": request_id, "status": "approved", "result": {"agents": ["worker"]}},
    )
    message = Envelope(type="agent.message", payload={"message": "Please coordinate with worker"})
    await client._accept(reply, acknowledge)
    await client._accept(message, acknowledge)
    await client._accept(reply, acknowledge)
    assert [item.payload["message_id"] for item in sent] == [reply.id, message.id, reply.id]
    assert not await store.pending()
    page = await mailbox.read_messages(limit=1)
    assert [item["id"] for item in page["messages"]] == [reply.id]
    later = await mailbox.read_messages(after=page["cursor"])
    assert [item["id"] for item in later["messages"]] == [message.id]
    await store.close()
    await store.open()
    assert (await mailbox.get_request_result(request_id))["result"] == {"agents": ["worker"]}
    assert [item["id"] for item in (await mailbox.read_messages())["messages"]] == [
        reply.id,
        message.id,
    ]
    assert (await mailbox.read_messages(after=later["cursor"]))["messages"] == []


@pytest.mark.parametrize("cancel_first", [False, True])
async def test_cancellation_is_durable_before_ack_and_survives_reopening(
    store: ClientStore, cancel_first: bool
) -> None:
    cancellation = Envelope(type="task.cancel", payload={"task_id": "task-one"})
    incoming = [cancellation, command()] if cancel_first else [command(), cancellation]
    for envelope in incoming:
        assert await store.accept(envelope)
    # Simulate a process crash after durable receive and before AgentClient._accept
    # can send a transport ACK or perform any follow-up business callback.
    await store.close()
    await store.open()
    assert (await store.tasks("task-one"))[0][0] == "cancelled"
    assert await AgentMailbox(store).claim_task() == {"status": "empty"}
    statuses = [
        item.payload["status"] for item in await store.pending() if item.type == "task.status"
    ]
    assert "cancelled" in statuses
    assert "accepted" not in statuses


async def test_running_cancellation_crash_restart_is_interrupted_and_never_reclaimed(
    store: ClientStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    await store.accept(command())
    await AgentMailbox(store).claim_task()
    assert await store.accept(Envelope(type="task.cancel", payload={"task_id": "task-one"}))
    await store.close()
    await store.open()
    assert (await store.tasks("task-one"))[0][0] == "cancelling"
    assert not any(item.payload.get("status") == "cancelled" for item in await store.pending())
    await store.close()

    async def parked_transport(client: AgentClient) -> None:
        await client.stop_event.wait()

    monkeypatch.setattr(AgentClient, "_ws", parked_transport)
    async with outbound_client(store.path, manual_tasks=True) as (client, _runner):
        assert (await client.store.tasks("task-one"))[0][0] == "interrupted"
        assert await AgentMailbox(client.store).claim_task() == {"status": "empty"}
        reports = [item for item in await client.store.pending() if item.type == "task.status"]
        assert reports[-1].payload["status"] == "interrupted"
        assert not any(item.payload["status"] == "cancelled" for item in reports)


async def test_offline_queue_is_explicit_and_identity_does_not_expose_credentials(
    store: ClientStore,
) -> None:
    mailbox = AgentMailbox(store)
    message = await mailbox.send_message("I am offline; this is queued")
    request = await mailbox.list_agents(wait_seconds=0)
    assert message["status"] == "queued"
    assert request["status"] == "pending"
    status = await mailbox.status()
    assert status["pending_outbox"] == 2
    assert status["agent_id"] == "local-agent"
    assert "credential" not in status
    assert "credential-must-never-be-public" not in json.dumps(status)


async def test_connection_owner_is_exclusive_but_mailbox_is_network_free(
    store: ClientStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def forbidden_run(*args: Any, **kwargs: Any) -> None:
        raise AssertionError("Mailbox mode must not create an outbound client")

    monkeypatch.setattr(AgentClient, "run", forbidden_run)
    async with connection_owner(store.path):
        with pytest.raises(ValueError, match="already connected"):
            async with connection_owner(store.path):
                pytest.fail("A second owner acquired the same identity")
        opened = await open_mailbox(store.path)
        try:
            assert (await opened.status())["connection"] == "owned_by_runner"
            assert (await opened.send_message("Mailbox does not own networking"))[
                "status"
            ] == "queued"
        finally:
            await opened.store.close()
    async with connection_owner(store.path):
        assert (await store.identity())["agent_id"] == "local-agent"


async def test_managed_mailbox_is_bound_to_one_currently_running_task(store: ClientStore) -> None:
    await store.accept(command("managed"))
    await store.accept(command("other"))
    with pytest.raises(ValueError, match="not running"):
        await open_mailbox(store.path, managed_task="managed")
    await AgentMailbox(store).claim_task("managed")
    opened = await open_mailbox(store.path, managed_task="managed")
    try:
        assert [item["task_id"] for item in (await opened.list_tasks())["tasks"]] == ["managed"]
        with pytest.raises(ValueError, match="another managed task"):
            await opened.send_message("Wrong task", task_id="other")
        with pytest.raises(ValueError, match="owns task"):
            await opened.complete_task("managed", {"wrong_owner": True})
        queued = await opened.send_message("Real report for managed")
        message = next(item for item in await store.pending() if item.id == queued["message_id"])
        assert message.payload["metadata"] == {"task_id": "managed"}
        await store.transition("managed", {"running"}, "cancelled", [])
        with pytest.raises(ValueError, match="no longer running"):
            await opened.send_message("Too late")
    finally:
        await opened.store.close()


@pytest.mark.parametrize("value", [-0.1, 1.1])
async def test_progress_validation_has_no_side_effects(store: ClientStore, value: float) -> None:
    await store.accept(command())
    mailbox = AgentMailbox(store)
    await mailbox.claim_task()
    queued = await store.pending()
    with pytest.raises(ValueError):
        await mailbox.report_progress("task-one", value)
    assert await store.pending() == queued
    assert (await store.tasks())[0][0] == "running"
