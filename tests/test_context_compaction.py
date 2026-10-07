"""SDK-backed tests of active windows, durable history and tool side effects."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

import httpx
import pytest
from sqlalchemy import delete, select
from test_ai_providers import SSE, anthropic_tool, chat, responses_tool
from test_chat_supervisor import manager, register_change, wait_status
from test_chat_titles import text_events

from chiliarch.ai import ModelTurn, Provider
from chiliarch.ai.context import compact_context, context_size, usage_state
from chiliarch.chat import ChatContext, ChatMessage, ChatRun, Supervisor
from chiliarch.config import ModelSettings, SettingsManager
from chiliarch.database import Database
from chiliarch.service import OperationService

JSON = dict[str, Any]
WINDOW = 20000


def with_usage(events: list[JSON], mode: str, input_tokens: int, output_tokens: int) -> list[JSON]:
    usage = {"input_tokens": input_tokens, "output_tokens": output_tokens}
    if mode == "chat_completions":
        events.append(
            {
                **chat({}),
                "choices": [],
                "usage": {
                    "prompt_tokens": input_tokens,
                    "completion_tokens": output_tokens,
                    "total_tokens": input_tokens + output_tokens,
                },
            }
        )
    elif mode == "responses":
        events[-1]["response"]["usage"] = {**usage, "total_tokens": input_tokens + output_tokens}
    else:
        events[0]["message"]["usage"] = {"input_tokens": input_tokens, "output_tokens": 0}
        events[-2]["usage"] = {"output_tokens": output_tokens}
    return events


def change_events(mode: str) -> list[JSON]:
    if mode == "chat_completions":
        return [
            chat({"content": "先保存状态。"}),
            chat(
                {
                    "tool_calls": [
                        {
                            "index": 0,
                            "id": "call-1",
                            "type": "function",
                            "function": {"name": "change_state", "arguments": '{"value":7}'},
                        }
                    ]
                },
                "tool_calls",
            ),
        ]
    if mode == "responses":
        events = responses_tool()
        call = events[-1]["response"]["output"][-1]
        call.update({"name": "change_state", "arguments": '{"value":7}'})
        return events
    events = anthropic_tool()
    for event in events:
        if event["type"] == "content_block_start" and event["content_block"]["type"] == "tool_use":
            event["content_block"]["name"] = "change_state"
        if event["type"] == "content_block_delta" and event["delta"]["type"] == "input_json_delta":
            event["delta"]["partial_json"] = (
                '{"value":' if event["delta"]["partial_json"].startswith("{") else "7}"
            )
    return events


class CompactionUpstream:
    def __init__(self, mode: str = "chat_completions", *, tool_first: bool = False) -> None:
        self.mode, self.tool_first = mode, tool_first
        self.business: list[JSON] = []
        self.summaries: list[JSON] = []
        self.business_usage: list[tuple[int, int]] = []
        self.summary = (
            "目标：保存状态并继续核对。change_state 已完成时不重复；未审批操作必须等待用户许可。"
        )
        self.failure: str | None = None
        self.summary_gate: asyncio.Event | None = None
        self.summary_started = asyncio.Event()
        self.business_gate: asyncio.Event | None = None
        self.all_business_started = asyncio.Event()

    async def handle(self, request: httpx.Request) -> httpx.Response:
        body: JSON = json.loads(request.content)
        if body.get("tools"):
            self.business.append(body)
            if len(self.business) == 10:
                self.all_business_started.set()
            if self.business_gate is not None:
                await self.business_gate.wait()
            events = (
                change_events(self.mode)
                if self.tool_first and len(self.business) == 1
                else text_events(self.mode, "核对完成。")
            )
            usage = (
                self.business_usage[len(self.business) - 1]
                if len(self.business) <= len(self.business_usage)
                else (0, 0)
            )
            events = with_usage(events, self.mode, *usage)
        else:
            self.summaries.append(body)
            self.summary_started.set()
            if self.summary_gate is not None:
                await self.summary_gate.wait()
            if self.failure == "http":
                return httpx.Response(
                    400, json={"error": {"message": "private-error-with-credential"}}
                )
            events = (
                change_events(self.mode)
                if self.failure == "tool"
                else text_events(self.mode, "" if self.failure == "empty" else self.summary)
            )
        return httpx.Response(
            200, headers={"content-type": "text/event-stream"}, stream=SSE(events)
        )

    def provider(self, config: ModelSettings) -> Provider:
        return Provider(
            config, http_client=httpx.AsyncClient(transport=httpx.MockTransport(self.handle))
        )


async def setup(
    tmp_path: Path, upstream: CompactionUpstream, mode: str = "ask"
) -> tuple[Database, SettingsManager, OperationService, Supervisor]:
    db = Database(f"sqlite+aiosqlite:///{tmp_path / 'context.db'}")
    settings = manager(mode=mode)
    settings.update({"model": {"api_mode": upstream.mode, "context_window": WINDOW}})
    service = OperationService(db, settings)
    supervisor = Supervisor(db, service, settings, provider_factory=upstream.provider)
    await db.initialize()
    await supervisor.start()
    return db, settings, service, supervisor


def assert_complete_tool_pairs(history: list[JSON], mode: str) -> None:
    if mode == "chat_completions":
        calls = [call["id"] for message in history for call in message.get("tool_calls", [])]
        results = [message["tool_call_id"] for message in history if message["role"] == "tool"]
    elif mode == "responses":
        calls = [item["call_id"] for item in history if item.get("type") == "function_call"]
        results = [
            item["call_id"] for item in history if item.get("type") == "function_call_output"
        ]
    else:
        blocks = [block for message in history for block in message["content"]]
        calls = [block["id"] for block in blocks if block["type"] == "tool_use"]
        results = [block["tool_use_id"] for block in blocks if block["type"] == "tool_result"]
    assert calls == results == ["call-1"]


@pytest.mark.parametrize("mode", ["chat_completions", "responses", "anthropic"])
async def test_compaction_waits_for_approval_preserves_native_pairs_and_survives_restart(
    tmp_path: Path, mode: str
) -> None:
    upstream = CompactionUpstream(mode, tool_first=True)
    upstream.business_usage = [(17950, 50)]
    db, settings, service, supervisor = await setup(tmp_path, upstream)
    effects: list[int] = []
    register_change(service, effects)
    conversation = await supervisor.create_conversation(title="接续任务")
    try:
        sent = await supervisor.send_message(conversation["id"], "保存状态并继续核对")
        waiting = await wait_status(supervisor, sent["run_id"], "awaiting_approval")
        assert effects == [] and upstream.summaries == []
        await service.execute(
            "approve_operation", {"approval_id": waiting["pending_approval_id"]}, actor="user"
        )
        await wait_status(supervisor, sent["run_id"], "completed")
        assert effects == [7] and len(upstream.summaries) == 1
        summary_body = upstream.summaries[0]
        assert not summary_body.get("tools")
        assert "signed-value" not in json.dumps(summary_body) and "opaque" not in json.dumps(
            summary_body
        )
        model_history = upstream.business[1]["input" if mode == "responses" else "messages"]
        assert_complete_tool_pairs(model_history, mode)
        async with db.sessions() as session:
            run = await session.get(ChatRun, sent["run_id"])
            assert run is not None and run.history[0]["compacted"]
            assistant = await session.get(ChatMessage, run.assistant_message_id)
            assert assistant is not None
            original_turns = assistant.transcript["turns"]
        assert [turn["role"] for turn in original_turns] == ["assistant", "tool", "assistant"]
        assert not any(turn.get("compacted") for turn in original_turns)
        snapshot = await supervisor.get_conversation(conversation["id"])
        events = snapshot["events"]
        assert next(event["seq"] for event in events if event["type"] == "tool_end") < next(
            event["seq"] for event in events if event["type"] == "context_compacting"
        )
        assert len([event for event in events if event["type"] == "approval_required"]) == 1
        await supervisor.stop()
        await db.close()
        # Reopen SQLite, not just the same Python objects.
        db = Database(f"sqlite+aiosqlite:///{tmp_path / 'context.db'}")
        service = OperationService(db, settings)
        register_change(service, effects)
        supervisor = Supervisor(db, service, settings, provider_factory=upstream.provider)
        await db.initialize()
        await supervisor.start()
        following = await supervisor.send_message(conversation["id"], "继续，不重复已完成操作")
        await wait_status(supervisor, following["run_id"], "completed")
        assert effects == [7]
        assert_complete_tool_pairs(
            upstream.business[-1]["input" if mode == "responses" else "messages"], mode
        )
        snapshot = await supervisor.get_conversation(conversation["id"])
        assert [message["role"] for message in snapshot["messages"]] == [
            "user",
            "assistant",
            "user",
            "assistant",
        ]
        await service.execute(
            "delete_conversation", {"conversation_id": conversation["id"]}, actor="user"
        )
        async with db.sessions() as session:
            assert (await session.scalars(select(ChatContext))).all() == []
    finally:
        await supervisor.stop()
        await db.close()


async def test_large_legacy_history_and_no_usage_are_compacted_in_bounded_batches(
    tmp_path: Path,
) -> None:
    upstream = CompactionUpstream()
    db, _, _, supervisor = await setup(tmp_path, upstream)
    try:
        conversation = await supervisor.create_conversation(title="历史记录")
        async with db.sessions.begin() as session:
            for index in range(240):
                role = "user" if index % 2 == 0 else "assistant"
                content = f"历史记录 {index}：" + "中文资料" * 100
                session.add(
                    ChatMessage(
                        conversation_id=conversation["id"],
                        role=role,
                        content=content,
                        transcript={"role": role, "content": content},
                    )
                )
        sent = await supervisor.send_message(conversation["id"], "继续核对，保留所有已有任务的结论")
        await wait_status(supervisor, sent["run_id"], "completed")
        assert len(upstream.summaries) > 1
        records: dict[int, dict[int, str]] = {}
        for body in upstream.summaries:
            fragments = body["messages"][-1]["content"].split(
                "\nConversation record fragments:\n", 1
            )[1]
            for line in fragments.splitlines():
                fragment = json.loads(line)
                records.setdefault(fragment["record"], {})[fragment["part"]] = fragment["data"]
        restored = [
            json.loads("".join(parts[index] for index in sorted(parts)))
            for _, parts in sorted(records.items())
        ]
        assert len(restored) == 241
        assert [record["content"] for record in restored[:-1]] == [
            f"历史记录 {index}：" + "中文资料" * 100 for index in range(240)
        ]
        assert restored[-1]["content"] == "继续核对，保留所有已有任务的结论"
        provider = upstream.provider(supervisor.settings_manager.snapshot().model)
        try:
            for body in upstream.summaries:
                request_messages = body["messages"][1:]
                assert (
                    provider.estimate_tokens(request_messages, [], body["messages"][0]["content"])
                    < WINDOW * 0.9
                )
        finally:
            await provider.close()
        original_question = upstream.business[0]["messages"]
        assert any(
            message.get("content") == "继续核对，保留所有已有任务的结论"
            for message in original_question
        )
        assert len(original_question) < 240
        snapshot = await supervisor.get_conversation(conversation["id"])
        assert len(snapshot["messages"]) == 242
        assert any(event["type"] == "context_compacted" for event in snapshot["events"])
    finally:
        await supervisor.stop()
        await db.close()


@pytest.mark.parametrize("tokens", [17999, 18000])
async def test_exact_ninety_percent_triggers_post_turn_compaction(
    tmp_path: Path, tokens: int
) -> None:
    upstream = CompactionUpstream()
    upstream.business_usage = [(tokens - 50, 50)]
    db, _, _, supervisor = await setup(tmp_path, upstream)
    try:
        conversation = await supervisor.create_conversation(title="阈值")
        sent = await supervisor.send_message(conversation["id"], "检查")
        await wait_status(supervisor, sent["run_id"], "completed")
        assert len(upstream.summaries) == (1 if tokens == 18000 else 0)
        snapshot = await supervisor.get_conversation(conversation["id"])
        assert snapshot["messages"][-1]["content"] == "核对完成。"
    finally:
        await supervisor.stop()
        await db.close()


@pytest.mark.parametrize("failure", ["empty", "tool", "http", "oversized"])
async def test_failed_compaction_keeps_original_context_and_never_executes_summary_tools(
    tmp_path: Path, failure: str
) -> None:
    upstream = CompactionUpstream(tool_first=True)
    upstream.failure = failure
    if failure == "oversized":
        upstream.summary = "无法压缩的重复内容" * 12000
    upstream.business_usage = [(17950, 50)]
    db, _, service, supervisor = await setup(tmp_path, upstream, mode="yolo")
    effects: list[int] = []
    register_change(service, effects)
    try:
        conversation = await supervisor.create_conversation(title="失败保留")
        sent = await supervisor.send_message(conversation["id"], "检查并保存")
        result = await wait_status(supervisor, sent["run_id"], "failed")
        assert effects == [7]
        async with db.sessions() as session:
            run = await session.get(ChatRun, sent["run_id"])
            assert run is not None and not any(message.get("compacted") for message in run.history)
            assert [message["role"] for message in run.history] == ["user", "assistant", "tool"]
        snapshot = await supervisor.get_conversation(conversation["id"])
        assert any(event["type"] == "context_compaction_failed" for event in snapshot["events"])
        assert "private-error-with-credential" not in json.dumps(snapshot)
        assert len(upstream.business) == 1 and result["error"]
    finally:
        await supervisor.stop()
        await db.close()


async def test_cancellation_during_compaction_commits_no_partial_summary(tmp_path: Path) -> None:
    upstream = CompactionUpstream()
    upstream.business_usage = [(17950, 50)]
    upstream.summary_gate = asyncio.Event()
    db, _, _, supervisor = await setup(tmp_path, upstream)
    try:
        conversation = await supervisor.create_conversation(title="取消压缩")
        sent = await supervisor.send_message(conversation["id"], "检查")
        await asyncio.wait_for(upstream.summary_started.wait(), 5)
        await supervisor.cancel_run(sent["run_id"])
        await wait_status(supervisor, sent["run_id"], "cancelled")
        async with db.sessions() as session:
            run = await session.get(ChatRun, sent["run_id"])
            assert run is not None and not any(message.get("compacted") for message in run.history)
    finally:
        upstream.summary_gate.set()
        await supervisor.stop()
        await db.close()


async def test_reported_usage_is_a_current_window_and_prefix_changes_invalidate_it() -> None:
    upstream = CompactionUpstream()
    provider = upstream.provider(ModelSettings(model="test"))
    tools: list[JSON] = []
    history = [{"role": "user", "content": "原始问题"}, {"role": "assistant", "content": "回答"}]
    try:
        state = usage_state(
            provider,
            history,
            tools,
            "system",
            ModelTurn(usage={"input_tokens": 139, "output_tokens": 11}),
        )
        assert context_size(provider, history, tools, "system", state).tokens == 150
        appended = [*history, {"role": "user", "content": "新的问题"}]
        assert context_size(provider, appended, tools, "system", state).tokens > 150
        new_state = usage_state(
            provider,
            appended,
            tools,
            "system",
            ModelTurn(usage={"input_tokens": 70, "output_tokens": 10}),
        )
        assert context_size(provider, appended, tools, "system", new_state).tokens == 80
        changed = context_size(provider, appended, tools, "updated system", new_state)
        assert changed.estimated and changed.tokens == provider.estimate_tokens(
            appended, tools, "updated system"
        )
    finally:
        await provider.close()


async def test_compaction_records_mask_secret_arguments_and_exclude_native_replay() -> None:
    upstream = CompactionUpstream()
    provider = upstream.provider(ModelSettings(model="test", context_window=WINDOW))
    history: list[JSON] = [
        {"role": "user", "content": "更新配置后核对状态"},
        {
            "role": "assistant",
            "content": "更新模型设置",
            "tool_calls": [
                {
                    "id": "config-call",
                    "name": "update_config",
                    "arguments": {"patch": {"model": {"api_key": "secret-must-not-be-summarized"}}},
                }
            ],
            "native": {"items": [{"signature": "private-native-value"}]},
        },
        {"role": "tool", "tool_call_id": "config-call", "content": '{"api_key":"********"}'},
    ]
    try:
        result = await compact_context(provider, history, [], "system", WINDOW, 18000)
        assert result.history[0]["compacted"]
        fragments = upstream.summaries[0]["messages"][-1]["content"].split(
            "\nConversation record fragments:\n", 1
        )[1]
        records = [json.loads(json.loads(fragment)["data"]) for fragment in fragments.splitlines()]
        assert records[1]["tool_calls"][0]["arguments"]["patch"]["model"]["api_key"] == "********"
        assert "native" not in records[1]
    finally:
        await provider.close()


async def test_more_than_eight_concurrent_runs_are_accepted_but_one_per_conversation(
    tmp_path: Path,
) -> None:
    upstream = CompactionUpstream()
    upstream.business_gate = asyncio.Event()
    db, _, _, supervisor = await setup(tmp_path, upstream)
    try:
        conversations = [
            await supervisor.create_conversation(title=f"并行 {index}") for index in range(10)
        ]
        runs = [
            await supervisor.send_message(conversation["id"], "检查")
            for conversation in conversations
        ]
        await asyncio.wait_for(upstream.all_business_started.wait(), 5)
        assert len(upstream.business) == 10
        with pytest.raises(ValueError, match="already has an active run"):
            await supervisor.send_message(conversations[0]["id"], "不能重入")
        upstream.business_gate.set()
        for run in runs:
            await wait_status(supervisor, run["run_id"], "completed")
    finally:
        upstream.business_gate.set()
        await supervisor.stop()
        await db.close()


async def test_more_than_twenty_tool_rounds_continue_with_distinct_operation_keys(
    tmp_path: Path,
) -> None:
    rounds: list[int] = []
    upstream = CompactionUpstream()

    async def handle(request: httpx.Request) -> httpx.Response:
        body: JSON = json.loads(request.content)
        upstream.business.append(body)
        index = len(upstream.business) - 1
        events = (
            [
                chat(
                    {
                        "tool_calls": [
                            {
                                "index": 0,
                                "id": f"round-{index}",
                                "type": "function",
                                "function": {
                                    "name": "inspect_round",
                                    "arguments": json.dumps({"round": index}),
                                },
                            }
                        ]
                    },
                    "tool_calls",
                )
            ]
            if index < 25
            else [chat({"content": "核对完成。"}, "stop")]
        )
        return httpx.Response(
            200, headers={"content-type": "text/event-stream"}, stream=SSE(events)
        )

    def provider(config: ModelSettings) -> Provider:
        return Provider(
            config, http_client=httpx.AsyncClient(transport=httpx.MockTransport(handle))
        )

    db, settings, service, supervisor = await setup(tmp_path, upstream)
    supervisor.provider_factory = provider

    async def inspect(arguments: JSON) -> JSON:
        rounds.append(arguments["round"])
        return {"status": "checked", "round": arguments["round"]}

    service.register(
        "inspect_round",
        "Read the next observation",
        {
            "type": "object",
            "properties": {"round": {"type": "integer"}},
            "required": ["round"],
            "additionalProperties": False,
        },
        inspect,
    )
    try:
        conversation = await supervisor.create_conversation(title="连续轮次")
        sent = await supervisor.send_message(conversation["id"], "连续核对")
        await wait_status(supervisor, sent["run_id"], "completed")
        assert rounds == list(range(25))
        assert len(upstream.business) == 26
    finally:
        await supervisor.stop()
        await db.close()


async def test_legacy_approval_without_token_row_can_resume(tmp_path: Path) -> None:
    upstream = CompactionUpstream(tool_first=True)
    db, settings, service, supervisor = await setup(tmp_path, upstream)
    effects: list[int] = []
    register_change(service, effects)
    conversation = await supervisor.create_conversation(title="旧审批")
    sent = await supervisor.send_message(conversation["id"], "保存")
    waiting = await wait_status(supervisor, sent["run_id"], "awaiting_approval")
    await supervisor.stop()
    async with db.sessions.begin() as session:
        await session.execute(delete(ChatContext))
        run = await session.get(ChatRun, sent["run_id"])
        assert run is not None
        assistant = await session.get(ChatMessage, run.assistant_message_id)
        assert assistant is not None
        assistant.transcript = {"turns": []}
    service = OperationService(db, settings)
    register_change(service, effects)
    supervisor = Supervisor(db, service, settings, provider_factory=upstream.provider)
    await supervisor.start()
    try:
        await service.execute(
            "approve_operation", {"approval_id": waiting["pending_approval_id"]}, actor="user"
        )
        await wait_status(supervisor, sent["run_id"], "completed")
        assert effects == [7]
        async with db.sessions() as session:
            run = await session.get(ChatRun, sent["run_id"])
            assert run is not None
            assistant = await session.get(ChatMessage, run.assistant_message_id)
            assert assistant is not None
            assert [turn["role"] for turn in assistant.transcript["turns"]] == [
                "assistant",
                "tool",
                "assistant",
            ]
    finally:
        await supervisor.stop()
        await db.close()
