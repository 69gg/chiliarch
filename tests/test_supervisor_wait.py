"""Test observation follows durable run progress, without slow wall-clock sleeps."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest
from test_chat_supervisor import manager, wait_status

from chiliarch.chat import ChatRun, Supervisor
from chiliarch.database import Database
from chiliarch.service import OperationService


class ObservedCondition(asyncio.Condition):
    def __init__(self) -> None:
        super().__init__()
        self.waiting = asyncio.Event()

    async def wait(self) -> bool:
        self.waiting.set()
        return await super().wait()


@pytest.mark.parametrize("progress", ["current", "other", "spurious", "total"])
async def test_wait_status_renews_only_for_durable_progress_with_a_total_bound(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, progress: str
) -> None:
    db = Database(f"sqlite+aiosqlite:///{tmp_path / 'wait.sqlite'}")
    settings = manager()
    supervisor = Supervisor(db, OperationService(db, settings), settings)
    condition = ObservedCondition()
    supervisor.condition = condition
    await db.initialize()
    waiter: asyncio.Task[dict[str, Any]] | None = None
    try:
        conversation = await supervisor.create_conversation(title="Observer test")
        async with db.sessions.begin() as session:
            for run_id in ("current", "other"):
                session.add(
                    ChatRun(
                        id=run_id,
                        conversation_id=conversation["id"],
                        assistant_message_id="unused",
                        status="running",
                    )
                )
        waiter = asyncio.create_task(
            wait_status(supervisor, "current", "completed", total_timeout=9)
        )

        async def observed_wait() -> None:
            assert waiter is not None
            reached = asyncio.create_task(condition.waiting.wait())
            try:
                done, _ = await asyncio.wait(
                    [reached, waiter], timeout=3, return_when=asyncio.FIRST_COMPLETED
                )
                if waiter in done:
                    waiter.result()
                assert reached in done
                condition.waiting.clear()
            finally:
                reached.cancel()
                await asyncio.gather(reached, return_exceptions=True)

        await observed_wait()
        loop = asyncio.get_running_loop()
        real_time = loop.time
        advance = 0.0
        with monkeypatch.context() as clock:
            clock.setattr(loop, "time", lambda: real_time() + advance)
            for _ in range(2 if progress in {"current", "total"} else 1):
                advance += 4
                if progress == "spurious":
                    async with condition:
                        condition.notify_all()
                else:
                    await supervisor._event(
                        conversation["id"],
                        "other" if progress == "other" else "current",
                        "context_usage",
                        {"private_body": "must-not-appear-in-timeout"},
                    )
                await observed_wait()
            if progress == "current":
                async with db.sessions.begin() as session:
                    run = await session.get(ChatRun, "current")
                    assert run is not None
                    run.status = "completed"
                await supervisor._event(conversation["id"], "current", "completed", {})
                assert (await waiter)["status"] == "completed"
            else:
                advance += 2
                with pytest.raises(TimeoutError, match="status=running") as failed:
                    await waiter
                diagnostic = str(failed.value)
                assert "event_seq=" in diagnostic and "task_stack=" in diagnostic
                assert "must-not-appear-in-timeout" not in diagnostic
    finally:
        if waiter is not None:
            waiter.cancel()
            await asyncio.gather(waiter, return_exceptions=True)
        await db.close()
