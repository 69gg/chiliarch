"""Real loopback daemon RPC and independent outbound platform connections."""

from __future__ import annotations

import asyncio
from asyncio import sleep
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import select
from test_client_network import LiveServer, wait_for_task

from chiliarch.client import AgentClient
from chiliarch.daemon import run_daemon
from chiliarch.daemon_client import DaemonClient, RemoteMailbox
from chiliarch.daemon_models import DaemonConfiguration, DaemonError, private_write
from chiliarch.database import Message, Task

JsonObject = dict[str, Any]


class TemporaryDaemon:
    """Exercise the foreground entrypoint without installing or controlling services."""

    def __init__(self, directory: Path) -> None:
        self.directory = directory
        self.task: asyncio.Task[None] | None = None

    async def start(self) -> None:
        self.task = asyncio.create_task(run_daemon(self.directory))
        async with DaemonClient(self.directory) as client:
            async with asyncio.timeout(8):
                while True:
                    if self.task.done():
                        self.task.result()
                        pytest.fail("Temporary daemon exited before becoming healthy")
                    if (self.directory / "runtime.json").exists():
                        try:
                            assert (await client.health())["status"] == "ok"
                            return
                        except DaemonError:
                            pass  # The private runtime is written just before listen.
                    await sleep(0.05)

    async def stop(self) -> None:
        if self.task is None:
            return
        try:
            if not self.task.done():
                async with DaemonClient(self.directory) as client:
                    assert (await client.shutdown())["status"] == "stopping"
            await asyncio.wait_for(asyncio.shield(self.task), 8)
            assert not (self.directory / "runtime.json").exists()
        finally:
            if not self.task.done():
                self.task.cancel()
                await asyncio.gather(self.task, return_exceptions=True)
            self.task = None


@pytest.fixture
async def connected_machine(
    tmp_path: Path,
) -> AsyncIterator[tuple[LiveServer, TemporaryDaemon, list[RemoteMailbox]]]:
    platform_directory = tmp_path / "platform"
    platform_directory.mkdir()
    platform = LiveServer(platform_directory)
    await platform.start()
    directory = tmp_path / "local daemon"
    configuration = DaemonConfiguration(
        transport=platform.settings.transport,
        logs=platform.settings.logs.model_copy(update={"directory": directory / "logs"}),
    )
    private_write(directory / "daemon.json", configuration.model_dump(mode="json"))
    daemon = TemporaryDaemon(directory)
    mailboxes: list[RemoteMailbox] = []
    try:
        await daemon.start()
        for handle in ("codex-main", "claude-helper"):
            pairing = await platform.app.state.service.execute("create_pairing_code", {})
            state_path = tmp_path / (handle + ".sqlite")
            await AgentClient.pair(
                platform.url, pairing["code"], handle, "one-isolated-machine", state_path
            )
            async with DaemonClient(directory) as client:
                registered = await client.register(
                    state_path, handle=handle, preferred_transport="poll"
                )
                assert registered["handle"] == handle
                assert registered["connection"] == "owned_by_daemon"
            mailboxes.append(RemoteMailbox(directory, handle))
        yield platform, daemon, mailboxes
    finally:
        await asyncio.gather(*(mailbox.close() for mailbox in mailboxes))
        await daemon.stop()
        await platform.stop()


async def dispatch(platform: LiveServer, mailbox: RemoteMailbox, instruction: str) -> str:
    identity = await mailbox.status()
    response = await platform.app.state.service.execute(
        "dispatch_task", {"agent_id": identity["agent_id"], "instruction": instruction}
    )
    task_id: str = response["task"]["id"]
    await wait_mailbox_task(mailbox, task_id)
    return task_id


async def wait_mailbox_task(
    mailbox: RemoteMailbox,
    task_id: str,
    status: str | None = None,
    *,
    timeout_seconds: float = 5,
) -> JsonObject:
    # Short timers can fire early on Windows. Query counts do not measure elapsed
    # time; retain the actual deadline and avoid flooding loopback RPC with reads.
    async with asyncio.timeout(timeout_seconds):
        while True:
            tasks = (await mailbox.list_tasks())["tasks"]
            for task in tasks:
                if task["task_id"] == task_id and (status is None or task["status"] == status):
                    return task
            await sleep(0.05)


async def wait_mailbox_status(mailbox: RemoteMailbox, task_id: str, status: str) -> None:
    await wait_mailbox_task(mailbox, task_id, status)


async def wait_platform_progress(platform: LiveServer, task_id: str, progress: float) -> JsonObject:
    async with asyncio.timeout(5):
        while True:
            # Observe persisted progress without creating audit writes that
            # compete with the agent's outbound updates.
            async with platform.app.state.database.sessions() as session:
                task = await session.get(Task, task_id)
                if task is not None and task.status == "running" and task.progress == progress:
                    result: JsonObject = platform.app.state.service.task_view(task)
                    return result
            await sleep(0.05)


@pytest.mark.parametrize("status", [None, "running"])
async def test_mailbox_wait_observes_state_even_when_short_timers_fire_early(
    monkeypatch: pytest.MonkeyPatch, status: str | None
) -> None:
    mailbox = AsyncMock(spec=RemoteMailbox)
    checks = 0

    async def list_tasks() -> JsonObject:
        nonlocal checks
        checks += 1
        tasks = [] if checks <= 600 else [{"task_id": "target", "status": "running"}]
        return {"tasks": tasks}

    async def early_timer(delay: float) -> None:
        await asyncio.sleep(0)

    mailbox.list_tasks.side_effect = list_tasks
    monkeypatch.setattr("test_daemon_transport.sleep", early_timer)
    assert await wait_mailbox_task(mailbox, "target", status) == {
        "task_id": "target",
        "status": "running",
    }


async def test_mailbox_wait_still_times_out_for_missing_task() -> None:
    mailbox = AsyncMock(spec=RemoteMailbox)
    mailbox.list_tasks.return_value = {"tasks": []}
    with pytest.raises(TimeoutError):
        await wait_mailbox_task(mailbox, "missing", timeout_seconds=0.02)


async def test_daemon_routes_two_paired_identities_and_reports_over_real_poll(
    connected_machine: tuple[LiveServer, TemporaryDaemon, list[RemoteMailbox]],
) -> None:
    platform, daemon, (codex, claude) = connected_machine
    first_status, second_status = await asyncio.gather(codex.status(), claude.status())
    assert first_status["agent_id"] != second_status["agent_id"]
    assert first_status["selected_transport"] == second_status["selected_transport"] == "poll"
    first, second = await asyncio.gather(
        dispatch(platform, codex, "Codex-specific task"),
        dispatch(platform, claude, "Claude-specific task"),
    )
    assert [
        (task["task_id"], task["instruction"]) for task in (await codex.list_tasks())["tasks"]
    ] == [(first, "Codex-specific task")]
    assert [
        (task["task_id"], task["instruction"]) for task in (await claude.list_tasks())["tasks"]
    ] == [(second, "Claude-specific task")]
    assert await codex.claim_task(second) == {"status": "empty"}
    assert (await claude.list_tasks())["tasks"][0]["status"] == "queued"
    with pytest.raises(DaemonError, match="capability"):
        await codex.client.request(
            "POST",
            "/agents/claude-helper/mailbox",
            {"method": "claim_task", "arguments": {"task_id": second}},
        )
    async with DaemonClient(daemon.directory) as client:
        assert (await client.health())["agent_count"] == 2
        assert {entry["handle"] for entry in await client.agents()} == {
            "codex-main",
            "claude-helper",
        }
    claims = await asyncio.gather(codex.claim_task(first), claude.claim_task(second))
    assert [claim["status"] for claim in claims] == ["claimed", "claimed"]
    await asyncio.gather(
        codex.report_progress(first, 0.25, "Codex is working"),
        claude.report_progress(second, 0.75, "Claude is working"),
    )
    first_progress, second_progress = await asyncio.gather(
        wait_platform_progress(platform, first, 0.25),
        wait_platform_progress(platform, second, 0.75),
    )
    assert first_progress["agent_id"] == first_status["agent_id"]
    assert second_progress["agent_id"] == second_status["agent_id"]
    assert first_progress["progress"] == 0.25
    assert second_progress["progress"] == 0.75

    contact = await codex.send_message("Claude can review my work", task_id=first)
    async with asyncio.timeout(5):
        while True:
            async with platform.app.state.service.db.sessions() as session:
                received = await session.scalar(
                    select(Message).where(
                        Message.message_id == contact["message_id"], Message.direction == "inbox"
                    )
                )
                if received is not None:
                    assert received.agent_id == first_status["agent_id"]
                    assert received.body["type"] == "agent.contact"
                    assert received.body["payload"] == {
                        "message": "Claude can review my work",
                        "metadata": {"task_id": first},
                    }
                    break
            await sleep(0.05)

    await asyncio.gather(
        codex.complete_task(first, {"owner": "codex", "count": 1}),
        claude.complete_task(second, {"owner": "claude", "count": 2}),
    )
    completed = await asyncio.gather(
        wait_for_task(platform, first, "completed"), wait_for_task(platform, second, "completed")
    )
    assert [task["result"] for task in completed] == [
        {"owner": "codex", "count": 1},
        {"owner": "claude", "count": 2},
    ]
    assert platform.gate.attempts and set(platform.gate.attempts) == {"poll"}


async def test_cancel_and_unregister_one_identity_leave_other_agent_working(
    connected_machine: tuple[LiveServer, TemporaryDaemon, list[RemoteMailbox]],
) -> None:
    platform, daemon, (codex, claude) = connected_machine
    first, second = await asyncio.gather(
        dispatch(platform, codex, "Cancel only this task"),
        dispatch(platform, claude, "Continue this task"),
    )
    await asyncio.gather(codex.claim_task(first), claude.claim_task(second))
    await asyncio.gather(
        wait_for_task(platform, first, "running"), wait_for_task(platform, second, "running")
    )
    await platform.app.state.service.execute("cancel_task", {"task_id": first})
    await wait_mailbox_status(codex, first, "cancelling")
    with pytest.raises(DaemonError, match="cancelling"):
        await codex.complete_task(first, {"incorrect_success": True})
    await claude.report_progress(second, 0.6, "Unaffected by peer cancellation")
    assert (await wait_platform_progress(platform, second, 0.6))["progress"] == 0.6
    await codex.confirm_cancellation(first)
    await wait_for_task(platform, first, "cancelled")

    async with DaemonClient(daemon.directory) as client:
        removed = await client.unregister("codex-main")
        assert removed["state_preserved"] is True
        assert (await client.health())["agent_count"] == 1
        assert [entry["handle"] for entry in await client.agents()] == ["claude-helper"]
    with pytest.raises(DaemonError, match="not registered"):
        await codex.status()
    assert (await claude.status())["connection"] == "owned_by_daemon"
    await claude.complete_task(second, {"survived_peer_removal": True})
    assert (await wait_for_task(platform, second, "completed"))["result"] == {
        "survived_peer_removal": True
    }
    fresh = await dispatch(platform, claude, "New work after removing the peer")
    await claude.claim_task(fresh)
    await claude.complete_task(fresh, {"new_work": True})
    await wait_for_task(platform, fresh, "completed")


async def test_daemon_restart_restores_registrations_without_replaying_unknown_work(
    connected_machine: tuple[LiveServer, TemporaryDaemon, list[RemoteMailbox]],
) -> None:
    platform, daemon, (codex, claude) = connected_machine
    unknown = await dispatch(platform, codex, "An external side effect may have occurred")
    queued = await dispatch(platform, claude, "Still unclaimed work")
    await codex.claim_task(unknown)
    await wait_for_task(platform, unknown, "running")
    identities = [(await mailbox.status())["agent_id"] for mailbox in (codex, claude)]
    await daemon.stop()
    await daemon.start()
    async with DaemonClient(daemon.directory) as client:
        assert (await client.health())["agent_count"] == 2
    assert [(await mailbox.status())["agent_id"] for mailbox in (codex, claude)] == identities
    interrupted = await wait_for_task(platform, unknown, "interrupted")
    assert "unknown" in interrupted["error"]
    assert (await codex.list_tasks())["tasks"][0]["status"] == "interrupted"
    assert await codex.claim_task() == {"status": "empty"}
    with pytest.raises(DaemonError, match="already interrupted"):
        await codex.claim_task(unknown)
    assert (await claude.list_tasks())["tasks"][0]["status"] == "queued"
    assert (await claude.claim_task(queued))["status"] == "claimed"
    await claude.complete_task(queued, {"resumed_unclaimed_work": True})
    completed = await wait_for_task(platform, queued, "completed")
    assert completed["result"] == {"resumed_unclaimed_work": True}
