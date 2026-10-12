"""Real bytes cross agent identities through persistent protocol requests and SQLite."""

from __future__ import annotations

import asyncio
import base64
import hashlib
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import func, select
from test_peer_service import agent, request, running
from test_peer_service import service as service

from chiliarch.adapters.artifacts import public_result
from chiliarch.adapters.mailbox import AgentMailbox
from chiliarch.artifacts import Artifact, ArtifactChunk
from chiliarch.client import ClientStore
from chiliarch.database import AgentRequest
from chiliarch.protocol import Envelope
from chiliarch.service import OperationError, OperationService

JsonObject = dict[str, Any]


class ConnectedMailbox(AgentMailbox):
    """Test the real persistent mailbox + server through a controllable transport pump."""

    def __init__(self, store: ClientStore, service: OperationService, agent_id: str) -> None:
        super().__init__(store)
        self.service, self.agent_id = service, agent_id
        self.block_chunk: int | None = None
        self.tamper_download = False

    async def request_operation(
        self,
        operation: str,
        arguments: JsonObject,
        message: str = "",
        wait_seconds: float = 0,
        client_request_id: str | None = None,
        task_id: str | None = None,
    ) -> JsonObject:
        result = await super().request_operation(
            operation, arguments, message, 0, client_request_id, task_id
        )
        if (
            operation in {"put_artifact_chunk", "read_artifact_chunk"}
            and self.block_chunk is not None
        ):
            if arguments["index"] >= self.block_chunk:
                return result
        for envelope in await self.store.pending():
            await self.service.receive(self.agent_id, envelope)
            await self.store.acknowledge(envelope.id)
        for envelope in await self.service.pending(self.agent_id):
            await self.store.accept(envelope)
            await self.service.receive(
                self.agent_id, Envelope(type="ack", payload={"message_id": envelope.id})
            )
        response = await self.get_request_result(result["request_id"])
        if self.tamper_download and operation == "read_artifact_chunk":
            data = base64.b64decode(response["result"]["data_base64"])
            data = bytes([data[0] ^ 1]) + data[1:]
            response["result"]["data_base64"] = base64.b64encode(data).decode()
            response["result"]["sha256"] = hashlib.sha256(data).hexdigest()
        return response


async def mailbox(service: OperationService, directory: Path, name: str) -> ConnectedMailbox:
    identifier = await agent(service, name)
    store = ClientStore(directory / (name + ".sqlite"))
    await store.open()
    return ConnectedMailbox(store, service, identifier)


async def test_file_roundtrip_resumes_after_restart_and_never_exposes_bytes(
    service: OperationService,
    tmp_path: Path,
) -> None:
    service.settings_manager.current.artifacts.chunk_bytes = 17
    source = tmp_path / "report.bin"
    data = bytes(range(256)) + b"shared evidence"
    await asyncio.to_thread(source.write_bytes, data)
    a, b = await mailbox(service, tmp_path, "A"), await mailbox(service, tmp_path, "B")
    try:
        a.block_chunk = 1
        partial = await a.upload_artifact(str(source), [b.agent_id], client_request_id="report-1")
        assert partial["status"] == "pending"
        assert partial["uploaded_chunks"] == 1
        async with service.db.sessions() as session:
            assert await session.scalar(select(func.count()).select_from(Artifact)) == 1
            assert await session.scalar(select(func.count()).select_from(ArtifactChunk)) == 1
        await a.store.close()
        await a.store.open()
        a.block_chunk = None
        done = await a.upload_artifact(str(source), [b.agent_id], client_request_id="report-1")
        assert done["status"] == "completed"
        assert done["upload_id"] == partial["upload_id"]
        again = await a.upload_artifact(str(source), [b.agent_id], client_request_id="report-1")
        assert again["artifact"]["id"] == done["artifact"]["id"]
        destination = tmp_path / "downloaded.bin"
        b.block_chunk = 2
        pending = await b.download_artifact(done["upload_id"], str(destination))
        assert pending["status"] == "pending"
        assert pending["downloaded_bytes"] == 34
        assert not await asyncio.to_thread(destination.exists)
        await b.store.close()
        await b.store.open()
        b.block_chunk = None
        downloaded = await b.download_artifact(done["upload_id"], str(destination))
        assert downloaded["status"] == "completed"
        assert await asyncio.to_thread(destination.read_bytes) == data
        assert downloaded["sha256"] == hashlib.sha256(data).hexdigest()
        inbox = await b.read_messages()
        for message in inbox["messages"]:
            assert "data_base64" not in (message["payload"].get("result") or {})
        raw = next(
            e
            for _, e in await b.store.messages()
            if isinstance(e.payload.get("result"), dict) and "data_base64" in e.payload["result"]
        )
        assert "data_base64" not in public_result(raw.payload)["result"]
        with pytest.raises(ValueError, match="already exists"):
            await b.download_artifact(done["upload_id"], str(destination))
        assert await asyncio.to_thread(destination.read_bytes) == data
    finally:
        await a.store.close()
        await b.store.close()


async def test_ask_approves_manifest_once_then_only_owner_can_supply_exact_bytes(
    service: OperationService,
    tmp_path: Path,
) -> None:
    a, b, outsider = await agent(service, "A"), await agent(service, "B"), await agent(service, "C")
    service.settings_manager.current.supervisor.mode = "ask"
    payload = b"evidence"
    queued = await request(
        service,
        a,
        "begin_artifact_upload",
        {
            "name": "report.txt",
            "size": len(payload),
            "sha256": hashlib.sha256(payload).hexdigest(),
            "members": [b],
        },
    )
    assert queued["status"] == "pending"
    async with service.db.sessions() as session:
        assert await session.scalar(select(func.count()).select_from(Artifact)) == 0
    await service.execute(
        "resolve_agent_request", {"request_id": queued["id"], "decision": "approve"}
    )
    async with service.db.sessions() as session:
        record = await session.get(AgentRequest, queued["id"])
        assert record is not None
        upload_id = record.result["upload_id"]
    args = {"upload_id": upload_id, "index": 0, "data_base64": base64.b64encode(payload).decode()}
    assert (await request(service, b, "put_artifact_chunk", args))["status"] == "failed"
    assert (await request(service, a, "put_artifact_chunk", {**args, "data_base64": "QQ=="}))[
        "status"
    ] == "failed"
    assert (await request(service, a, "put_artifact_chunk", args))["status"] == "approved"
    assert (await request(service, a, "put_artifact_chunk", args))["result"]["duplicate"] is True
    assert (await request(service, a, "finish_artifact_upload", {"upload_id": upload_id}))[
        "status"
    ] == "approved"
    result = await request(
        service, b, "read_artifact_chunk", {"artifact_id": upload_id, "index": 0}
    )
    delivered = next(
        item
        for item in await service.pending(b)
        if item.payload.get("request_id") == result["external_id"]
    )
    assert base64.b64decode(delivered.payload["result"]["data_base64"]) == payload
    assert result["result"] == delivered.payload["result"]
    assert (
        await request(
            service, outsider, "read_artifact_chunk", {"artifact_id": upload_id, "index": 0}
        )
    )["status"] == "failed"
    assert (await request(service, outsider, "list_artifacts", {}))["result"]["artifacts"] == []
    altered = base64.b64encode(b"EVIDENCE").decode()
    assert (await request(service, a, "put_artifact_chunk", {**args, "data_base64": altered}))[
        "status"
    ] == "failed"
    assert (await request(service, a, "get_artifact", {"artifact_id": upload_id}))["result"][
        "artifact"
    ]["members"] == [b, a]


async def test_hash_failure_never_publishes_and_other_task_cannot_modify_upload(
    service: OperationService,
    tmp_path: Path,
) -> None:
    a = await agent(service, "A")
    first, second = await running(service, a), await running(service, a)
    begin = await request(
        service,
        a,
        "begin_artifact_upload",
        {
            "name": "check.bin",
            "size": 3,
            "sha256": hashlib.sha256(b"abc").hexdigest(),
            "task_id": first,
        },
        task_id=first,
    )
    upload_id = begin["result"]["upload_id"]
    args = {"upload_id": upload_id, "index": 0, "data_base64": base64.b64encode(b"bad").decode()}
    assert (await request(service, a, "put_artifact_chunk", args, task_id=second))[
        "status"
    ] == "failed"
    assert (await request(service, a, "put_artifact_chunk", args, task_id=first))[
        "status"
    ] == "approved"
    assert (
        await request(service, a, "finish_artifact_upload", {"upload_id": upload_id}, task_id=first)
    )["status"] == "failed"
    assert (await request(service, a, "get_artifact", {"artifact_id": upload_id}))["result"][
        "artifact"
    ]["state"] == "uploading"
    assert (
        await request(service, a, "read_artifact_chunk", {"artifact_id": upload_id, "index": 0})
    )["status"] == "failed"


async def test_download_checks_whole_manifest_even_if_chunk_checksum_is_forged(
    service: OperationService,
    tmp_path: Path,
) -> None:
    path, destination = tmp_path / "source", tmp_path / "target"
    await asyncio.to_thread(path.write_bytes, b"source bytes")
    a, b = await mailbox(service, tmp_path, "A"), await mailbox(service, tmp_path, "B")
    try:
        result = await a.upload_artifact(str(path), [b.agent_id])
        b.tamper_download = True
        with pytest.raises(ValueError, match="SHA256"):
            await b.download_artifact(result["upload_id"], str(destination))
        assert not await asyncio.to_thread(destination.exists)
    finally:
        await a.store.close()
        await b.store.close()


async def test_storage_quota_is_reserved_and_explicit_discard_releases_it(
    service: OperationService,
) -> None:
    service.settings_manager.current.artifacts.max_total_bytes = 8
    service.settings_manager.current.artifacts.max_artifact_bytes = 6
    args = {"name": "one", "size": 5, "sha256": hashlib.sha256(b"12345").hexdigest()}
    first = await service.execute("begin_artifact_upload", args)
    with pytest.raises(OperationError, match="quota"):
        await service.execute("begin_artifact_upload", args)
    with pytest.raises(OperationError, match="file size"):
        await service.execute("begin_artifact_upload", {**args, "size": 7})
    await service.execute("discard_artifact_upload", {"upload_id": first["upload_id"]})
    second = await service.execute("begin_artifact_upload", args)
    assert second["upload_id"] != first["upload_id"]


async def test_empty_file_roundtrip_and_owner_deletion_uses_policy(
    service: OperationService,
    tmp_path: Path,
) -> None:
    source, destination = tmp_path / "empty.txt", tmp_path / "download-empty.txt"
    await asyncio.to_thread(source.write_bytes, b"")
    a, b = await mailbox(service, tmp_path, "A"), await mailbox(service, tmp_path, "B")
    try:
        result = await a.upload_artifact(str(source), [b.agent_id])
        assert (await b.download_artifact(result["upload_id"], str(destination)))[
            "status"
        ] == "completed"
        assert await asyncio.to_thread(destination.read_bytes) == b""
        service.settings_manager.current.supervisor.mode = "auto"
        pending = await request(
            service, a.agent_id, "delete_artifact", {"artifact_id": result["upload_id"]}
        )
        assert pending["status"] == "pending"
        await service.execute(
            "resolve_agent_request", {"request_id": pending["id"], "decision": "approve"}
        )
        assert (
            await request(service, b.agent_id, "get_artifact", {"artifact_id": result["upload_id"]})
        )["status"] == "failed"
    finally:
        await a.store.close()
        await b.store.close()


async def test_high_level_upload_waits_for_approval_without_sending_file_bytes(
    service: OperationService,
    tmp_path: Path,
) -> None:
    service.settings_manager.current.supervisor.mode = "ask"
    source = tmp_path / "approval.txt"
    await asyncio.to_thread(source.write_bytes, b"approved content")
    sender = await mailbox(service, tmp_path, "A")
    try:
        pending = await sender.upload_artifact(str(source), [], client_request_id="approval")
        assert pending["status"] == "pending"
        async with service.db.sessions() as session:
            assert await session.scalar(select(func.count()).select_from(ArtifactChunk)) == 0
            record = await session.scalar(
                select(AgentRequest).where(
                    AgentRequest.agent_id == sender.agent_id,
                    AgentRequest.external_id == pending["request_id"],
                )
            )
            assert record is not None
            request_id = record.id
        await service.execute(
            "resolve_agent_request", {"request_id": request_id, "decision": "approve"}
        )
        result = await sender.upload_artifact(str(source), [], client_request_id="approval")
        assert result["status"] == "completed"
        assert result["artifact"]["uri"] == "chiliarch:artifact:" + result["upload_id"]
    finally:
        await sender.store.close()


async def test_chunks_adapt_to_transport_budget_and_limit_checks_precede_file_hash(
    service: OperationService,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service.settings_manager.current.transport.max_message_bytes = 4096
    source = tmp_path / "bounded.bin"
    await asyncio.to_thread(source.write_bytes, b"data" * 1200)
    sender = await mailbox(service, tmp_path, "A")
    try:
        result = await sender.upload_artifact(str(source), [])
        assert result["status"] == "completed"
        assert (
            result["artifact"]["chunk_bytes"]
            < service.settings_manager.current.artifacts.chunk_bytes
        )
        async with service.db.sessions() as session:
            assert await session.scalar(select(func.count()).select_from(ArtifactChunk)) > 1
        service.settings_manager.current.artifacts.max_artifact_bytes = 1

        def no_hash(path: Path) -> tuple[int, str]:
            pytest.fail("Over-limit file must be rejected before full local hashing")

        monkeypatch.setattr("chiliarch.adapters.artifacts._manifest", no_hash)
        with pytest.raises(ValueError, match="size limit"):
            await sender.upload_artifact(str(source), [])
    finally:
        await sender.store.close()
