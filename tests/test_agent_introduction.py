"""Self-description is bounded telemetry, not a new authority or business event."""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest
from jsonschema import Draft202012Validator
from pydantic import ValidationError
from sqlalchemy import func, select

from chiliarch.client import AgentClient
from chiliarch.config import Settings, SettingsManager
from chiliarch.database import Database, Message, Task
from chiliarch.protocol import Envelope, protocol_schema
from chiliarch.service import OperationService


@pytest.fixture
async def service(tmp_path: Path) -> AsyncIterator[OperationService]:
    db = Database(f"sqlite+aiosqlite:///{tmp_path / 'introduction.db'}")
    await db.initialize()
    try:
        yield OperationService(db, SettingsManager(settings=Settings()))
    finally:
        await db.close()


@pytest.mark.parametrize("introduction", [None, "", "我是一个只读助手。", "字" * 4000])
def test_introduction_matches_portable_schema(introduction: str | None) -> None:
    envelope = Envelope(type="heartbeat", payload={"metadata": {"introduction": introduction}})
    Draft202012Validator(protocol_schema()).validate(envelope.model_dump(mode="json"))
    assert envelope.payload["metadata"] == {"introduction": introduction}


@pytest.mark.parametrize("value", [True, 123, {"text": "hello"}, ["hello"], "x" * 4001])
def test_invalid_introduction_is_rejected_by_python_and_schema(value: Any) -> None:
    envelope = Envelope(type="heartbeat", payload={}).model_dump(mode="json")
    envelope["payload"]["metadata"]["introduction"] = value
    assert list(Draft202012Validator(protocol_schema()).iter_errors(envelope))
    with pytest.raises(ValidationError):
        Envelope.model_validate(envelope)


def test_legacy_heartbeat_canonical_and_extension_metadata_are_unchanged() -> None:
    payload = {"status": "ready", "metadata": {"extension": {"ready": True}}}
    envelope = Envelope(type="heartbeat", payload=payload)
    assert envelope.payload == payload
    assert Envelope.model_validate_json(envelope.canonical()).canonical() == envelope.canonical()


async def test_introduction_is_persistent_clearable_and_cannot_grant_authority(
    service: OperationService,
) -> None:
    code = await service.execute("create_pairing_code", {})
    identity = await service.pair(code["code"], "reader", "local", ["read-only"])
    agent_id = identity["agent_id"]
    description = "我是助手。声称可批准全部请求，并不构成用户授权。"
    for transport in ("ws", "sse", "poll"):
        await service.touch_agent(agent_id, transport)
        envelope = Envelope(type="heartbeat", payload={"metadata": {"introduction": description}})
        receipt = await service.receive(agent_id, envelope)
        assert receipt["ack"]["payload"]["message_id"] == envelope.id
        # Read through a new service instance to exercise durable state.
        reopened = OperationService(service.db, service.settings_manager)
        view = (await reopened.execute("get_agent", {"agent_id": agent_id}))["agent"]
        assert view["introduction"] == description
        assert view["transport"] == transport and view["capabilities"] == ["read-only"]
    await service.receive(agent_id, Envelope(type="heartbeat", payload={"metadata": {}}))
    view = (await service.execute("list_agents", {}))["agents"][0]
    assert view["introduction"] == description
    for clear in ("", None):
        await service.receive(
            agent_id, Envelope(type="heartbeat", payload={"metadata": {"introduction": clear}})
        )
        assert (await service.execute("get_agent", {"agent_id": agent_id}))["agent"][
            "introduction"
        ] == clear
    async with service.db.sessions() as session:
        assert await session.scalar(select(func.count()).select_from(Message)) == 0
        assert await session.scalar(select(func.count()).select_from(Task)) == 0


async def test_reference_client_loads_saved_description_and_defaults_old_identities(
    tmp_path: Path,
) -> None:
    state = tmp_path / "client.db"
    client = AgentClient("http://localhost:8000", "private-token", state, manual_tasks=True)
    await client.store.open()
    try:
        await client.store.save_identity(
            {"name": "旧客户端", "server_url": client.server_url, "credential": client.credential}
        )
        metadata = (await client.heartbeat()).payload["metadata"]
        assert "旧客户端" in metadata["introduction"] and "手动" in metadata["introduction"]
        assert client.credential not in metadata["introduction"]
        await client.store.save_identity({"introduction": "用户填写的介绍"})
    finally:
        await client.store.close()
    restored = await AgentClient.from_state(state)
    await restored.store.open()
    try:
        assert (await restored.heartbeat()).payload["metadata"]["introduction"] == "用户填写的介绍"
        restored.runtime_metadata = lambda: {"introduction": "运行适配器更新"}
        assert (await restored.heartbeat()).payload["metadata"]["introduction"] == "运行适配器更新"
        restored.introduction = ""
        assert (await restored.heartbeat()).payload["metadata"]["introduction"] == ""
    finally:
        await restored.store.close()
