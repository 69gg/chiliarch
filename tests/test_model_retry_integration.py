from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import httpx
import pytest
from sqlalchemy import select
from test_ai_providers import SSE, chat
from test_chat_supervisor import Upstream, manager, register_change, wait_status
from test_chat_titles import NamingUpstream, wait_naming

from chiliarch.chat import ChatRun, Supervisor
from chiliarch.database import Database, OperationRecord
from chiliarch.service import OperationService

JSONObject = dict[str, Any]


class RetriedToolUpstream(Upstream):
    def __init__(self) -> None:
        super().__init__()
        self.attempts: list[JSONObject] = []

    async def handle(self, request: httpx.Request) -> httpx.Response:
        self.attempts.append(json.loads(request.content))
        if len(self.attempts) == 1:
            return httpx.Response(
                200,
                headers={"content-type": "text/event-stream"},
                stream=SSE(
                    [
                        chat(
                            {
                                "tool_calls": [
                                    {
                                        "index": 0,
                                        "id": "failed-call",
                                        "function": {
                                            "name": "change_state",
                                            "arguments": '{"value":99}',
                                        },
                                    }
                                ]
                            }
                        ),
                        {
                            "type": "error",
                            "error": {"type": "server_error", "message": "temporarily unavailable"},
                        },
                    ]
                ),
            )
        return await super().handle(request)


class RetriedNamingUpstream(NamingUpstream):
    async def handle(self, request: httpx.Request) -> httpx.Response:
        body: JSONObject = json.loads(request.content)
        if not body.get("tools") and not self.title_requests:
            self.title_requests.append(body)
            return httpx.Response(
                200,
                headers={"content-type": "text/event-stream"},
                stream=SSE(
                    [
                        {
                            "type": "error",
                            "error": {
                                "type": "overloaded_error",
                                "message": "temporarily unavailable",
                            },
                        }
                    ]
                ),
            )
        return await super().handle(request)


@pytest.fixture
def retry_delays(monkeypatch: pytest.MonkeyPatch) -> list[float]:
    delays: list[float] = []

    async def no_wait(delay: float) -> None:
        delays.append(delay)

    monkeypatch.setattr("chiliarch.ai.providers.sleep", no_wait)
    return delays


async def test_stream_api_error_retries_same_run_and_executes_only_successful_tool(
    tmp_path: Path, retry_delays: list[float]
) -> None:
    db = Database(f"sqlite+aiosqlite:///{tmp_path / 'state.db'}")
    settings = manager("yolo")
    settings.update({"model": {"retries": 1}})
    service = OperationService(db, settings)
    effects: list[int] = []
    register_change(service, effects)
    upstream = RetriedToolUpstream()
    supervisor = Supervisor(db, service, settings, provider_factory=upstream.provider)
    await db.initialize()
    await supervisor.start()
    try:
        conversation = await supervisor.create_conversation("手动命名的重试测试")
        sent = await supervisor.send_message(conversation["id"], "change state")
        completed = await wait_status(supervisor, sent["run_id"], "completed")
        snapshot = await supervisor.get_conversation(conversation["id"])
        assert completed["error"] is None
        assert effects == [7]
        assert len(upstream.attempts) == 3
        assert upstream.attempts[0] == upstream.attempts[1]
        assert len(upstream.requests) == 2
        assert upstream.title_requests == []
        assert len(retry_delays) == 1
        assert len(snapshot["runs"]) == 1
        assert snapshot["runs"][0]["id"] == sent["run_id"]
        assert snapshot["messages"][-1]["content"] == "Working Done"
        events = snapshot["events"]
        assert sum(event["type"] == "run_started" for event in events) == 1
        assert sum(event["type"] == "completed" for event in events) == 1
        assert not any(event["type"] in {"failed", "error", "interrupted"} for event in events)
        starts = [event for event in events if event["type"] == "tool_start"]
        results = [event for event in events if event["type"] == "tool_end"]
        assert len(starts) == len(results) == 1
        assert starts[0]["data"]["arguments"] == {"value": 7}
        assert results[0]["data"]["result"] == {"status": "changed", "value": 7}
        tool_messages = [
            message for message in upstream.attempts[-1]["messages"] if message["role"] == "tool"
        ]
        assert len(tool_messages) == 1
        assert tool_messages[0]["tool_call_id"] == "call-1"
        async with db.sessions() as session:
            runs = (await session.scalars(select(ChatRun))).all()
            operations = (
                await session.scalars(
                    select(OperationRecord).where(OperationRecord.name == "change_state")
                )
            ).all()
        assert len(runs) == 1 and runs[0].status == "completed"
        assert len(operations) == 1
        assert operations[0].status == "completed"
        assert operations[0].result == {"status": "changed", "value": 7}
    finally:
        await supervisor.stop()
        await db.close()


@pytest.mark.parametrize("mode", ["chat_completions", "responses", "anthropic"])
async def test_automatic_naming_retries_stream_api_error_without_repeating_main_answer(
    tmp_path: Path, retry_delays: list[float], mode: str
) -> None:
    db = Database(f"sqlite+aiosqlite:///{tmp_path / 'state.db'}")
    settings = manager()
    settings.update({"model": {"api_mode": mode, "retries": 1, "anthropic_max_tokens": 4096}})
    service = OperationService(db, settings)
    upstream = RetriedNamingUpstream(mode)
    supervisor = Supervisor(db, service, settings, provider_factory=upstream.provider)
    await db.initialize()
    await supervisor.start()
    try:
        conversation = await supervisor.create_conversation()
        sent = await supervisor.send_message(conversation["id"], "检查服务器状态")
        await wait_status(supervisor, sent["run_id"], "completed")
        await wait_naming(supervisor, conversation["id"], "generated")
        snapshot = await supervisor.get_conversation(conversation["id"])
        assert snapshot["title"] == upstream.title
        assert snapshot["messages"][-1]["content"] == upstream.answer
        assert len(snapshot["messages"]) == 2
        assert len(snapshot["runs"]) == 1
        assert snapshot["runs"][0]["status"] == "completed"
        assert len(upstream.chat_requests) == 1
        assert len(upstream.title_requests) == 2
        assert upstream.title_requests[0] == upstream.title_requests[1]
        assert len(retry_delays) == 1
        events = snapshot["events"]
        assert sum(event["type"] == "completed" for event in events) == 1
        assert sum(event["type"] == "conversation_updated" for event in events) == 1
        assert not any(event["type"] in {"failed", "error", "interrupted"} for event in events)
        assert (
            "".join(event["data"]["delta"] for event in events if event["type"] == "text_delta")
            == upstream.answer
        )
    finally:
        await supervisor.stop()
        await db.close()
