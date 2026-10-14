"""Repository commands share durable delivery without consuming native task slots."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from jsonschema import Draft202012Validator
from pydantic import ValidationError

from chiliarch.client import AgentClient, ClientStore
from chiliarch.protocol import Envelope, protocol_schema


def command(command_id: str = "repository-1") -> Envelope:
    return Envelope(
        type="repository.command",
        payload={"command_id": command_id, "action": "inspect_repository", "arguments": {}},
    )


@pytest.mark.parametrize(
    "kind,payload",
    [
        ("repository.command", {"command_id": "c", "action": "inspect_repository"}),
        ("repository.result", {"command_id": "c", "status": "unknown"}),
    ],
)
def test_repository_protocol_is_portable(kind: str, payload: dict[str, Any]) -> None:
    envelope = Envelope(type=kind, payload=payload)
    Draft202012Validator(protocol_schema()).validate(envelope.model_dump(mode="json"))
    invalid = envelope.model_dump(mode="json")
    invalid["payload"]["command_id"] = ""
    assert list(Draft202012Validator(protocol_schema()).iter_errors(invalid))
    with pytest.raises(ValidationError):
        Envelope.model_validate(invalid)


async def test_repository_identity_survives_reopen_and_rejects_conflicts(tmp_path: Path) -> None:
    store = ClientStore(tmp_path / "agent.sqlite")
    incoming = command()
    await store.open()
    try:
        assert await store.accept(incoming)
        await store.repository_transition(incoming.payload["command_id"], "running")
    finally:
        await store.close()
    await store.open()
    try:
        assert [item.id for item in await store.repository_work()] == [incoming.id]
        assert not await store.accept(incoming)
        duplicate = command()
        assert await store.accept(duplicate)
        assert len(await store.repository_work()) == 1
        conflicting = command()
        conflicting.payload["arguments"] = {"branch": "different"}
        with pytest.raises(ValueError, match="conflicting arguments"):
            await store.accept(conflicting)
        report = Envelope(
            type="repository.result", payload={"command_id": "repository-1", "status": "unknown"}
        )
        await store.repository_transition("repository-1", "unknown", report)
        assert await store.repository_work() == []
        assert [item.id for item in await store.pending()] == [report.id]
    finally:
        await store.close()


async def test_repository_ack_before_execution_and_manual_client_can_process(
    tmp_path: Path,
) -> None:
    calls: list[dict[str, Any]] = []

    async def execute(payload: dict[str, Any]) -> dict[str, Any]:
        calls.append(payload)
        return {"command_id": payload["command_id"], "status": "completed", "result": {"ok": True}}

    client = AgentClient(
        "http://platform.test",
        "token",
        tmp_path / "agent.sqlite",
        manual_tasks=True,
        repository_handler=execute,
    )
    incoming = command()

    async def sender(envelope: Envelope) -> None:
        assert envelope.type == "ack"
        assert envelope.payload["message_id"] == incoming.id
        assert calls == []
        assert len(await client.store.repository_work()) == 1

    await client.store.open()
    try:
        await client._accept(incoming, sender)
        await client._schedule()
        await asyncio.gather(*client._repository_workers.values())
        await client._schedule()
        assert len(calls) == 1
        reports = await client.store.pending()
        assert reports[0].type == "repository.result"
        assert reports[0].payload["result"] == {"ok": True}
        assert (await client.execution_status())["supports_repository_commands"]
    finally:
        await client.store.close()


async def test_crashed_executor_reports_unknown_without_retry(tmp_path: Path) -> None:
    calls = 0

    async def execute(payload: dict[str, Any]) -> dict[str, Any]:
        nonlocal calls
        calls += 1
        raise OSError("private credentials must never appear in diagnostics")

    client = AgentClient(
        "http://platform.test", "token", tmp_path / "agent.sqlite", repository_handler=execute
    )
    await client.store.open()
    try:
        await client.store.accept(command())
        await client._schedule()
        await asyncio.gather(*client._repository_workers.values())
        await client._schedule()
        assert calls == 1
        report = (await client.store.pending())[0]
        assert report.payload["status"] == "unknown"
        assert report.payload["error"] == "Repository executor stopped (OSError); reconcile first"
    finally:
        await client.store.close()


async def test_native_steering_claims_remain_unknown_across_restart(tmp_path: Path) -> None:
    store = ClientStore(tmp_path / "steer.db")
    await store.open()
    assert await store.claim_native_delivery("task-a", "message", "rpc-1")
    await store.close()
    await store.open()
    try:
        assert not await store.claim_native_delivery("task-a", "message", "rpc-2")
        assert (await store.native_deliveries("task-a"))[0]["state"] == "unknown"
        await store.complete_native_delivery("task-a", "message")
        assert (await store.native_deliveries("task-a"))[0]["state"] == "delivered"
        assert await store.native_deliveries("task-b") == []
        response = Envelope(
            type="native.response",
            payload={
                "interaction_id": "request",
                "task_id": "task-a",
                "status": "answered",
                "answer": {"choice": "decline"},
            },
        )
        await store.accept(response)
        assert await store.native_response("request", "task-b") is None
        assert await store.native_response("other", "task-a") is None
        assert (await store.native_response("request", "task-a")) == response.payload
        expired = response.model_copy(
            update={
                "id": "expired-answer",
                "expires_at": datetime.now(UTC) - timedelta(seconds=1),
                "payload": {**response.payload, "answer": {"choice": "accept"}},
            }
        )
        await store.accept(expired)
        assert await store.native_response("request", "task-a") == {
            **response.payload,
            "status": "expired",
            "answer": {},
        }
    finally:
        await store.close()
