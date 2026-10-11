from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

import httpx
import pytest
from sqlalchemy import select, update
from test_ai_providers import SSE, chat_tool, text_response
from test_chat_supervisor import manager, wait_status

from chiliarch.ai import Provider
from chiliarch.chat import ConversationTitle, Supervisor
from chiliarch.config import ModelSettings
from chiliarch.database import Database
from chiliarch.service import OperationService

JSONObject = dict[str, Any]


def text_events(mode: str, content: str) -> list[JSONObject]:
    events = text_response(mode)
    if mode == "chat_completions":
        events[0]["choices"][0]["delta"]["content"] = content
    elif mode == "responses":
        events[0]["response"]["output"][0]["content"][0]["text"] = content
    else:
        events[2]["delta"]["text"] = content
    return events


class NamingUpstream:
    def __init__(self, mode: str = "chat_completions") -> None:
        self.mode = mode
        self.title = "服务器运行状态"
        self.answer = "已检查，服务器运行正常。"
        self.title_requests: list[JSONObject] = []
        self.chat_requests: list[JSONObject] = []
        self.title_started = asyncio.Event()
        self.all_titles_started = asyncio.Event()
        self.title_gate: asyncio.Event | None = None
        self.title_error = False
        self.title_tool = False
        self.chat_error = False
        self.inspect_first = False

    async def handle(self, request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        if body.get("tools"):
            self.chat_requests.append(body)
            if self.chat_error:
                return httpx.Response(400, json={"error": {"message": "isolated chat failure"}})
            if self.inspect_first and len(self.chat_requests) == 1:
                events = chat_tool()
            else:
                events = text_events(self.mode, self.answer)
        else:
            self.title_requests.append(body)
            self.title_started.set()
            if len(self.title_requests) == 10:
                self.all_titles_started.set()
            if self.title_gate is not None:
                await self.title_gate.wait()
            if self.title_error:
                return httpx.Response(
                    400, json={"error": {"message": "private provider diagnostic"}}
                )
            events = chat_tool() if self.title_tool else text_events(self.mode, self.title)
        return httpx.Response(
            200, headers={"content-type": "text/event-stream"}, stream=SSE(events)
        )

    def provider(self, config: ModelSettings) -> Provider:
        return Provider(
            config, http_client=httpx.AsyncClient(transport=httpx.MockTransport(self.handle))
        )


async def wait_naming(supervisor: Supervisor, conversation_id: str, status: str) -> None:
    async with asyncio.timeout(5):
        while True:
            async with supervisor.db.sessions() as session:
                naming = await session.get(ConversationTitle, conversation_id)
            if naming is not None and naming.status == status:
                return
            await asyncio.sleep(0.01)


@pytest.mark.parametrize("mode", ["chat_completions", "responses", "anthropic"])
async def test_first_completed_answer_is_named_without_blocking_chat(
    tmp_path: Path, mode: str
) -> None:
    db = Database(f"sqlite+aiosqlite:///{tmp_path / 'state.db'}")
    settings = manager()
    settings.update({"model": {"api_mode": mode, "anthropic_max_tokens": 4096}})
    upstream = NamingUpstream(mode)
    upstream.title_gate = asyncio.Event()
    service = OperationService(db, settings)
    supervisor = Supervisor(db, service, settings, provider_factory=upstream.provider)
    await db.initialize()
    await supervisor.start()
    try:
        conversation = await supervisor.create_conversation()
        sent = await supervisor.send_message(conversation["id"], "检查服务器状态")
        await wait_status(supervisor, sent["run_id"], "completed")
        await asyncio.wait_for(upstream.title_started.wait(), 5)
        before = await supervisor.get_conversation(conversation["id"])
        assert before["title"] == "新对话"
        assert before["messages"][-1]["content"] == upstream.answer
        assert before["events"][-1]["type"] == "completed"
        body = upstream.title_requests[0]
        assert "tools" not in body and "tool_choice" not in body
        messages = body.get("messages", body.get("input"))
        user = messages[-1]
        content = user["content"]
        if mode == "anthropic":
            content = content[0]["text"]
        assert json.loads(content) == {"question": "检查服务器状态", "answer": upstream.answer}
        assert body["model"] == settings.current.model.model
        upstream.title_gate.set()
        await wait_naming(supervisor, conversation["id"], "generated")
        after = await supervisor.get_conversation(conversation["id"])
        assert after["title"] == upstream.title
        assert after["messages"] == before["messages"]
        update_event = after["events"][-1]
        assert update_event["type"] == "conversation_updated"
        assert update_event["data"] == {
            "conversation_id": conversation["id"],
            "title": upstream.title,
            "source": "automatic",
        }
        assert update_event["seq"] > before["last_event_seq"]
        following = await supervisor.send_message(conversation["id"], "再检查一次")
        await wait_status(supervisor, following["run_id"], "completed")
        assert len(upstream.title_requests) == 1
    finally:
        await supervisor.stop()
        await db.close()


async def test_title_input_uses_only_final_visible_answer_and_bounded_user_text(
    tmp_path: Path,
) -> None:
    db = Database(f"sqlite+aiosqlite:///{tmp_path / 'state.db'}")
    settings = manager()
    upstream = NamingUpstream()
    upstream.inspect_first = True
    service = OperationService(db, settings)

    async def inspect(_: JSONObject) -> JSONObject:
        return {"diagnostic": "not part of title", "api_key": "private-credential"}

    service.register(
        "inspect",
        "Read diagnostic state",
        {
            "type": "object",
            "properties": {"agent_id": {"type": "string"}},
            "required": ["agent_id"],
            "additionalProperties": False,
        },
        inspect,
    )
    supervisor = Supervisor(db, service, settings, provider_factory=upstream.provider)
    await db.initialize()
    await supervisor.start()
    try:
        conversation = await supervisor.create_conversation()
        sent = await supervisor.send_message(conversation["id"], "问" * 9000)
        await wait_status(supervisor, sent["run_id"], "completed")
        await wait_naming(supervisor, conversation["id"], "generated")
        payload = json.loads(upstream.title_requests[0]["messages"][-1]["content"])
        assert payload == {"question": "问" * 8192, "answer": upstream.answer}
        snapshot = await supervisor.get_conversation(conversation["id"])
        assert snapshot["messages"][-1]["reasoning"] == "check state"
        assert snapshot["messages"][-1]["content"] == "Working " + upstream.answer
        assert any(event["type"] == "tool_end" for event in snapshot["events"])
    finally:
        await supervisor.stop()
        await db.close()


@pytest.mark.parametrize("action", ["rename", "delete"])
async def test_manual_change_while_naming_is_pending_cancels_title_request(
    tmp_path: Path, action: str
) -> None:
    db = Database(f"sqlite+aiosqlite:///{tmp_path / 'state.db'}")
    settings = manager()
    upstream = NamingUpstream()
    upstream.title_gate = asyncio.Event()
    service = OperationService(db, settings)
    supervisor = Supervisor(db, service, settings, provider_factory=upstream.provider)
    await db.initialize()
    await supervisor.start()
    try:
        conversation = await supervisor.create_conversation()
        sent = await supervisor.send_message(conversation["id"], "检查状态")
        await wait_status(supervisor, sent["run_id"], "completed")
        await asyncio.wait_for(upstream.title_started.wait(), 5)
        task = supervisor.title_tasks[conversation["id"]]
        if action == "rename":
            await service.execute(
                "rename_conversation",
                {"conversation_id": conversation["id"], "title": "  我指定的标题  "},
                actor="user",
            )
            snapshot = await supervisor.get_conversation(conversation["id"])
            assert snapshot["title"] == "我指定的标题"
            assert snapshot["events"][-1]["data"]["source"] == "manual"
            await wait_naming(supervisor, conversation["id"], "manual")
        else:
            await service.execute(
                "delete_conversation", {"conversation_id": conversation["id"]}, actor="user"
            )
            assert (await supervisor.list_conversations())["conversations"] == []
            async with db.sessions() as session:
                assert (await session.scalars(select(ConversationTitle))).all() == []
        assert task.cancelled()
        assert conversation["id"] not in supervisor.title_tasks
        upstream.title_gate.set()
    finally:
        await supervisor.stop()
        await db.close()


@pytest.mark.parametrize("custom", ["created", "renamed_to_default"])
async def test_explicit_custom_title_is_never_automatically_replaced(
    tmp_path: Path, custom: str
) -> None:
    db = Database(f"sqlite+aiosqlite:///{tmp_path / 'state.db'}")
    settings = manager()
    upstream = NamingUpstream()
    service = OperationService(db, settings)
    supervisor = Supervisor(db, service, settings, provider_factory=upstream.provider)
    await db.initialize()
    await supervisor.start()
    try:
        conversation = await supervisor.create_conversation(
            "我的会话" if custom == "created" else "新对话"
        )
        if custom == "renamed_to_default":
            await service.execute(
                "rename_conversation",
                {"conversation_id": conversation["id"], "title": "新对话"},
                actor="user",
            )
        sent = await supervisor.send_message(conversation["id"], "检查状态")
        await wait_status(supervisor, sent["run_id"], "completed")
        await wait_naming(supervisor, conversation["id"], "manual")
        assert upstream.title_requests == []
        snapshot = await supervisor.get_conversation(conversation["id"])
        assert snapshot["title"] == conversation["title"]
    finally:
        await supervisor.stop()
        await db.close()


@pytest.mark.parametrize("failure", ["provider", "unexpected_tool"])
async def test_naming_failure_preserves_answer_and_is_not_retried_after_restart(
    tmp_path: Path, failure: str
) -> None:
    db = Database(f"sqlite+aiosqlite:///{tmp_path / 'state.db'}")
    settings = manager()
    upstream = NamingUpstream()
    upstream.title_error = failure == "provider"
    upstream.title_tool = failure == "unexpected_tool"
    service = OperationService(db, settings)
    supervisor = Supervisor(db, service, settings, provider_factory=upstream.provider)
    await db.initialize()
    await supervisor.start()
    conversation = await supervisor.create_conversation()
    sent = await supervisor.send_message(conversation["id"], "检查状态")
    await wait_status(supervisor, sent["run_id"], "completed")
    await wait_naming(supervisor, conversation["id"], "failed")
    before = await supervisor.get_conversation(conversation["id"])
    assert before["title"] == "新对话"
    assert before["runs"][-1]["status"] == "completed" and before["runs"][-1]["error"] is None
    assert before["messages"][-1]["content"] == upstream.answer
    assert not any(event["type"] in {"error", "approval_required"} for event in before["events"])
    await supervisor.stop()
    restored = Supervisor(
        db, OperationService(db, settings), settings, provider_factory=upstream.provider
    )
    await restored.start()
    try:
        following = await restored.send_message(conversation["id"], "下一条消息")
        await wait_status(restored, following["run_id"], "completed")
        assert len(upstream.title_requests) == 1
        assert (await restored.get_conversation(conversation["id"]))["title"] == "新对话"
    finally:
        await restored.stop()
        await db.close()


async def test_first_failed_question_does_not_consume_automatic_naming_attempt(
    tmp_path: Path,
) -> None:
    db = Database(f"sqlite+aiosqlite:///{tmp_path / 'state.db'}")
    settings = manager()
    upstream = NamingUpstream()
    upstream.chat_error = True
    service = OperationService(db, settings)
    supervisor = Supervisor(db, service, settings, provider_factory=upstream.provider)
    await db.initialize()
    await supervisor.start()
    try:
        conversation = await supervisor.create_conversation()
        failed = await supervisor.send_message(conversation["id"], "失败的问答")
        await wait_status(supervisor, failed["run_id"], "failed")
        await wait_naming(supervisor, conversation["id"], "eligible")
        assert upstream.title_requests == []
        upstream.chat_error = False
        sent = await supervisor.send_message(conversation["id"], "成功的问答")
        await wait_status(supervisor, sent["run_id"], "completed")
        await wait_naming(supervisor, conversation["id"], "generated")
        assert json.loads(upstream.title_requests[0]["messages"][-1]["content"]) == {
            "question": "成功的问答",
            "answer": upstream.answer,
        }
    finally:
        await supervisor.stop()
        await db.close()


async def test_shutdown_or_crash_does_not_repeat_pending_naming_request(tmp_path: Path) -> None:
    db = Database(f"sqlite+aiosqlite:///{tmp_path / 'state.db'}")
    settings = manager()
    upstream = NamingUpstream()
    upstream.title_gate = asyncio.Event()
    supervisor = Supervisor(
        db, OperationService(db, settings), settings, provider_factory=upstream.provider
    )
    await db.initialize()
    await supervisor.start()
    conversation = await supervisor.create_conversation()
    sent = await supervisor.send_message(conversation["id"], "检查状态")
    await wait_status(supervisor, sent["run_id"], "completed")
    await asyncio.wait_for(upstream.title_started.wait(), 5)
    await supervisor.stop()
    await wait_naming(supervisor, conversation["id"], "interrupted")
    # Simulate a process killed before its cancellation cleanup reached storage.
    async with db.sessions.begin() as session:
        await session.execute(update(ConversationTitle).values(status="generating"))
    restored = Supervisor(
        db, OperationService(db, settings), settings, provider_factory=upstream.provider
    )
    await restored.start()
    try:
        await wait_naming(restored, conversation["id"], "interrupted")
        following = await restored.send_message(conversation["id"], "继续")
        await wait_status(restored, following["run_id"], "completed")
        assert len(upstream.title_requests) == 1
        assert (await restored.get_run(sent["run_id"]))["status"] == "completed"
    finally:
        await restored.stop()
        await db.close()


async def test_title_requests_can_overlap_without_a_platform_concurrency_cap(
    tmp_path: Path,
) -> None:
    db = Database(f"sqlite+aiosqlite:///{tmp_path / 'state.db'}")
    settings = manager()
    upstream = NamingUpstream()
    upstream.title_gate = asyncio.Event()
    supervisor = Supervisor(
        db, OperationService(db, settings), settings, provider_factory=upstream.provider
    )
    await db.initialize()
    await supervisor.start()
    try:
        ids: list[str] = []
        for index in range(10):
            conversation = await supervisor.create_conversation()
            ids.append(conversation["id"])
            sent = await supervisor.send_message(conversation["id"], f"检查 {index}")
            await wait_status(supervisor, sent["run_id"], "completed")
            task = supervisor.tasks.get(sent["run_id"])
            if task is not None:
                await task
        await asyncio.wait_for(upstream.all_titles_started.wait(), 5)
        assert len(upstream.title_requests) == 10
        assert not upstream.title_gate.is_set()
        upstream.title_gate.set()
        for conversation_id in ids:
            await wait_naming(supervisor, conversation_id, "generated")
        assert len(upstream.title_requests) == 10
    finally:
        await supervisor.stop()
        await db.close()
