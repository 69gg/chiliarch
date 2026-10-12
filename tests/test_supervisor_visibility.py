"""The supervisor receives actual business values while audit trails remain redacted."""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from copy import deepcopy
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import func, select
from test_chat_supervisor import Upstream, manager, wait_status
from test_peer_service import agent as add_peer
from test_peer_service import request as submit_peer_request

from chiliarch.chat import AgentInbox, ChatMessage, Supervisor
from chiliarch.config import SettingsManager
from chiliarch.database import AuditEvent, Database, PairingCode
from chiliarch.logging import JsonFormatter
from chiliarch.protocol import Envelope
from chiliarch.service import OperationError, OperationService

JSONObject = dict[str, Any]


@asynccontextmanager
async def system(
    directory: Path,
    upstream: Upstream,
    mode: str = "yolo",
    *,
    start: bool = True,
) -> AsyncIterator[tuple[Database, SettingsManager, OperationService, Supervisor]]:
    db = Database(f"sqlite+aiosqlite:///{directory / 'state.db'}")
    settings = manager(mode)
    service = OperationService(db, settings)
    supervisor = Supervisor(db, service, settings, provider_factory=upstream.provider)
    await db.initialize()
    if start:
        await supervisor.start()
    try:
        yield db, settings, service, supervisor
    finally:
        await supervisor.stop()
        await db.close()


def model_result(upstream: Upstream) -> JSONObject:
    tool_message = upstream.requests[1]["messages"][-1]
    assert tool_message["role"] == "tool"
    return json.loads(tool_message["content"])


async def query(supervisor: Supervisor) -> JSONObject:
    conversation = await supervisor.create_conversation()
    run = await supervisor.send_message(conversation["id"], "Inspect the requested platform data")
    await wait_status(supervisor, run["run_id"], "completed")
    return await supervisor.get_conversation(conversation["id"])


@pytest.mark.parametrize("mode", ["ask", "auto", "yolo"])
async def test_model_receives_redeemable_pairing_code_without_duplicate_creation(
    tmp_path: Path, mode: str
) -> None:
    upstream = Upstream("create_pairing_code", {"ttl_seconds": 300})
    async with system(tmp_path, upstream, mode) as (db, _, service, supervisor):
        conversation = await supervisor.create_conversation()
        run = await supervisor.send_message(conversation["id"], "Create a code for my agent")
        if mode != "yolo":
            pending = await wait_status(supervisor, run["run_id"], "awaiting_approval")
            async with db.sessions() as session:
                assert await session.scalar(select(func.count()).select_from(PairingCode)) == 0
            assert len(upstream.requests) == 1
            with pytest.raises(OperationError) as forbidden:
                await service.execute(
                    "approve_operation",
                    {"approval_id": pending["pending_approval_id"]},
                    actor="supervisor",
                )
            assert forbidden.value.status_code == 403
            approval = await service.execute(
                "approve_operation",
                {"approval_id": pending["pending_approval_id"]},
                actor="user",
            )
            repeated = await service.execute(
                "approve_operation",
                {"approval_id": pending["pending_approval_id"]},
                actor="user",
            )
            assert repeated == approval
        await wait_status(supervisor, run["run_id"], "completed")
        snapshot = await supervisor.get_conversation(conversation["id"])
        ended = next(event for event in snapshot["events"] if event["type"] == "tool_end")
        result = model_result(upstream)
        assert result == ended["data"]["result"]
        assert result["code"] and result["code"] != "********"
        replayed = await service.execute(
            "create_pairing_code",
            {"ttl_seconds": 300},
            actor="supervisor",
            idempotency_key=ended["data"]["key"],
        )
        assert replayed == result
        identity = await service.pair(result["code"], "test agent", "test machine", [])
        authenticated = await service.authenticate_agent(identity["credential"])
        assert authenticated.id == identity["agent_id"]
        with pytest.raises(OperationError) as reused:
            await service.pair(result["code"], "another agent", "test machine", [])
        assert reused.value.status_code == 401
        async with db.sessions() as session:
            assert await session.scalar(select(func.count()).select_from(PairingCode)) == 1
            audit = (
                await session.scalars(
                    select(AuditEvent).where(
                        AuditEvent.action
                        == ("create_pairing_code" if mode == "yolo" else "approve_operation")
                    )
                )
            ).all()
        assert audit
        audited_result = audit[0].details["result"]
        if mode != "yolo":
            audited_result = audited_result["approval"]["result"]
        assert audited_result["code"] == "********"


async def test_original_tool_arguments_approval_results_and_history_reach_model(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    arguments: JSONObject = {
        "api_key": "visible-api-key",
        "password": "visible-password",
        "headers": {"Authorization": "Bearer visible-header", "X-Scope": "visible-scope"},
        "metadata": {"nested": [{"credential": "visible-credential", "secret": "visible-secret"}]},
    }
    expected = {"status": "saved", **arguments}
    upstream = Upstream("save_business_data", arguments)
    executed: list[JSONObject] = []
    with caplog.at_level(logging.INFO, logger="chiliarch"):
        async with system(tmp_path, upstream, "ask") as (db, _, service, supervisor):

            async def save(values: JSONObject) -> JSONObject:
                executed.append(deepcopy(values))
                return {"status": "saved", **values}

            service.register(
                "save_business_data",
                "Save the supplied values unchanged",
                {
                    "type": "object",
                    "properties": {
                        "api_key": {"type": "string"},
                        "password": {"type": "string"},
                        "headers": {"type": "object"},
                        "metadata": {"type": "object"},
                    },
                    "required": list(arguments),
                    "additionalProperties": False,
                },
                save,
                mutating=True,
            )
            conversation = await supervisor.create_conversation()
            run = await supervisor.send_message(conversation["id"], "Save the business data")
            pending = await wait_status(supervisor, run["run_id"], "awaiting_approval")
            assert executed == []
            snapshot = await supervisor.get_conversation(conversation["id"])
            events = [
                event
                for event in snapshot["events"]
                if event["type"] in {"tool_start", "approval_required"}
            ]
            assert {event["type"] for event in events} == {"tool_start", "approval_required"}
            assert all(event["data"]["arguments"] == arguments for event in events)
            approvals = (await service.execute("list_approvals", {}, actor="supervisor"))[
                "approvals"
            ]
            assert approvals[0]["arguments"] == arguments
            await service.execute(
                "approve_operation", {"approval_id": pending["pending_approval_id"]}, actor="user"
            )
            await wait_status(supervisor, run["run_id"], "completed")
            assert executed == [arguments]
            assert model_result(upstream) == expected
            snapshot = await supervisor.get_conversation(conversation["id"])
            assert [
                event["data"]["result"]
                for event in snapshot["events"]
                if event["type"] == "tool_end"
            ] == [expected]
            following = await supervisor.send_message(conversation["id"], "What did you save?")
            await wait_status(supervisor, following["run_id"], "completed")
            tool_history = [
                item for item in upstream.requests[-1]["messages"] if item["role"] == "tool"
            ]
            assert json.loads(tool_history[0]["content"]) == expected
            async with db.sessions() as session:
                audit_rows = (await session.scalars(select(AuditEvent))).all()
            awaiting = next(
                row for row in audit_rows if row.action == "operation.awaiting_approval"
            )
            assert awaiting.details["arguments"]["api_key"] == "********"
            assert awaiting.details["arguments"]["password"] == "********"
            assert awaiting.details["arguments"]["headers"] == {
                "Authorization": "********",
                "X-Scope": "********",
            }
            assert awaiting.details["arguments"]["metadata"] == {
                "nested": [{"credential": "********", "secret": "********"}]
            }
            approved = next(row for row in audit_rows if row.action == "approve_operation")
            assert approved.details["result"]["approval"]["result"]["api_key"] == "********"
    operational_records = [
        record for record in caplog.records if record.name.startswith("chiliarch.")
    ]
    assert operational_records
    formatted = [json.loads(JsonFormatter().format(record)) for record in operational_records]
    for record in formatted:
        assert record.get("arguments") is None and record.get("result") is None


@pytest.mark.parametrize("operation", ["get_config", "update_config", "get_platform_state"])
async def test_model_reads_full_effective_configuration_while_user_configuration_stays_masked(
    tmp_path: Path, operation: str
) -> None:
    initial: JSONObject = {
        "model": {
            "api_key": "configured-api-key",
            "headers": {"X-Tenant": "configured-header"},
            "request_params": {"metadata": {"api_key": "nested-configured-key"}},
        },
        "auth": {"password_hash": "stored-password-hash"},
    }
    arguments: JSONObject = {}
    if operation == "update_config":
        arguments = {"patch": {"model": {"api_key": "updated-api-key"}}}
    upstream = Upstream(operation, arguments)
    # Upstream's default arguments are for change_state; an empty query really
    # has an empty argument object rather than that helper's fallback payload.
    upstream.arguments = arguments
    async with system(tmp_path, upstream) as (_, settings, service, supervisor):
        settings.update(initial)
        if operation == "get_platform_state":
            settings.update({"server": {"port": 8123}})
        snapshot = await query(supervisor)
        result = model_result(upstream)
        assert result["config"]["model"]["api_key"] == (
            "updated-api-key" if operation == "update_config" else "configured-api-key"
        )
        assert result["config"]["model"]["headers"] == {"X-Tenant": "configured-header"}
        assert result["config"]["model"]["request_params"] == initial["model"]["request_params"]
        assert result["config"]["auth"]["password_hash"] == "stored-password-hash"
        assert result["config"] == next(
            event["data"]["result"]["config"]
            for event in snapshot["events"]
            if event["type"] == "tool_end"
        )
        public = (await service.execute("get_config", {}, actor="user"))["config"]
        assert public["model"]["api_key"] == public["auth"]["password_hash"] == "********"
        assert public["model"]["headers"] == {"X-Tenant": "********"}
        assert public["model"]["request_params"]["metadata"]["api_key"] == "********"
        if operation == "get_platform_state":
            assert result["config"]["server"]["port"] == 8000
            assert result["runtime"]["config_revision"] == settings.revision
            assert result["runtime"]["pending_restart"] == ["server.port"]
            assert result["runtime"]["pending_restart_values"] == {"server.port": 8123}
            assert result["runtime"]["protocol_version"] == "1.0"


@pytest.mark.parametrize("operation", ["list_agents", "list_agent_requests", "list_approvals"])
async def test_stored_agent_metadata_request_arguments_and_results_reach_model_unchanged(
    tmp_path: Path, operation: str
) -> None:
    upstream = Upstream(operation)
    upstream.arguments = {}
    async with system(tmp_path, upstream, start=False) as (_, settings, service, supervisor):
        pairing = await service.execute("create_pairing_code", {})
        identity = await service.pair(pairing["code"], "demo", "machine", [])
        metadata = {
            "credential": "business-credential",
            "nested": {"password": "business-password"},
        }
        await service.receive(
            identity["agent_id"], Envelope(type="heartbeat", payload={"metadata": metadata})
        )
        sent: JSONObject = {"metadata": metadata, "password": "business-result-password"}

        async def echo(values: JSONObject) -> JSONObject:
            return deepcopy(values)

        service.register(
            "business_echo",
            "Return the exact business data",
            {
                "type": "object",
                "properties": {"metadata": {"type": "object"}, "password": {"type": "string"}},
                "required": ["metadata", "password"],
                "additionalProperties": False,
            },
            echo,
            mutating=True,
        )
        await service.receive(
            identity["agent_id"],
            Envelope(
                type="agent.request",
                payload={
                    "request_id": "business-request",
                    "operation": "business_echo",
                    "arguments": sent,
                },
            ),
        )
        request = (await service.execute("list_agent_requests", {}))["requests"][0]
        settings.update({"supervisor": {"mode": "ask"}})
        pending = await service.execute(
            "business_echo", sent, actor="supervisor", idempotency_key="business"
        )
        await service.execute(
            "approve_operation", {"approval_id": pending["approval_id"]}, actor="user"
        )
        await service.execute(
            "resolve_agent_request",
            {"request_id": request["id"], "decision": "approve"},
            actor="user",
        )
        await query(supervisor)
        result = model_result(upstream)
        if operation == "list_agents":
            assert result["agents"][0]["metadata"] == {**metadata, "status": "ready"}
        elif operation == "list_agent_requests":
            resolved = result["requests"][0]
            assert resolved["arguments"] == sent and resolved["status"] == "approved"
            assert resolved["result"] == sent
        else:
            approved = result["approvals"][0]
            assert approved["arguments"] == sent and approved["status"] == "approved"
            assert approved["result"] == sent


async def test_agent_contact_metadata_is_available_to_model_and_durable_conversation_event(
    tmp_path: Path,
) -> None:
    upstream = Upstream("list_agents")
    upstream.arguments = {}
    async with system(tmp_path, upstream) as (db, _, service, supervisor):
        pairing = await service.execute("create_pairing_code", {})
        identity = await service.pair(pairing["code"], "demo", "machine", [])
        payload: JSONObject = {
            "message": "Please inspect this original business data",
            "metadata": {"api_key": "contact-api-key", "nested": {"password": "contact-password"}},
        }
        envelope = Envelope(type="agent.contact", payload=payload)
        await service.receive(identity["agent_id"], envelope)
        async with asyncio.timeout(5):
            async with supervisor.condition:
                while True:
                    async with db.sessions() as session:
                        inbox = await session.scalar(select(AgentInbox))
                    if inbox is not None and inbox.run_id:
                        break
                    await supervisor.condition.wait()
        await wait_status(supervisor, inbox.run_id, "completed")
        assert inbox.conversation_id is not None
        snapshot = await supervisor.get_conversation(inbox.conversation_id)
        contact = next(event for event in snapshot["events"] if event["type"] == "agent_contact")
        assert contact["data"]["content"] == payload
        model_content = upstream.requests[0]["messages"][-1]["content"]
        incoming = json.loads(model_content.split("\n", 1)[1])
        assert incoming["envelope"]["payload"] == payload


async def test_supervisor_tool_set_includes_every_management_operation_and_run_status(
    tmp_path: Path,
) -> None:
    upstream = Upstream("list_agents")
    upstream.arguments = {}
    async with system(tmp_path, upstream) as (_, _, service, supervisor):
        names = {schema["name"] for schema in service.schemas()}
        assert names == set(service.operations)
        assert {schema["name"] for schema in supervisor.tool_schemas()} == names
        assert {
            "put_artifact_chunk",
            "read_artifact_chunk",
            "get_platform_state",
            "get_chat_run",
        } <= names
        prior = await query(supervisor)
        prior_id = prior["runs"][0]["id"]
        upstream.name = "get_chat_run"
        upstream.arguments = {"run_id": prior_id}
        upstream.requests.clear()
        await query(supervisor)
        assert model_result(upstream) == await supervisor.get_run(prior_id)


@pytest.mark.parametrize(
    ("operation", "arguments", "error", "status_code"),
    [
        ("get_agent", {"agent_id": "missing-agent"}, "Agent not found", 404),
        (
            "get_conversation",
            {"conversation_id": "missing-conversation"},
            "Conversation not found",
            None,
        ),
        ("get_chat_run", {"run_id": "missing-run"}, "Run not found", None),
    ],
)
async def test_tool_failure_keeps_actionable_original_error_for_model(
    tmp_path: Path,
    operation: str,
    arguments: JSONObject,
    error: str,
    status_code: int | None,
) -> None:
    upstream = Upstream(operation, arguments)
    async with system(tmp_path, upstream) as (_, _, _, supervisor):
        snapshot = await query(supervisor)
        result = model_result(upstream)
        assert result["status"] == "error" and result["error"] == error
        if status_code is not None:
            assert result["status_code"] == status_code
        ended = next(event for event in snapshot["events"] if event["type"] == "tool_end")
        assert ended["data"]["result"] == result


async def test_supervisor_binary_transfer_preserves_bytes_and_existing_authorization_limits(
    tmp_path: Path,
) -> None:
    data = b"Original bytes for the supervisor\x00\xff"
    encoded = base64.b64encode(data).decode("ascii")
    upstream = Upstream("read_artifact_chunk")
    async with system(tmp_path, upstream) as (_, settings, service, supervisor):
        settings.current.artifacts.max_artifact_bytes = len(data)
        settings.current.artifacts.max_total_bytes = len(data)
        manifest: JSONObject = {
            "name": "evidence.bin",
            "size": len(data),
            "sha256": hashlib.sha256(data).hexdigest(),
        }
        upload = await service.execute("begin_artifact_upload", manifest, actor="supervisor")
        upload_id = upload["upload_id"]
        with pytest.raises(OperationError) as unauthorized_owner:
            await service.execute(
                "put_artifact_chunk",
                {"upload_id": upload_id, "index": 0, "data_base64": encoded},
                actor="user",
            )
        assert unauthorized_owner.value.status_code == 403
        await service.execute(
            "put_artifact_chunk",
            {"upload_id": upload_id, "index": 0, "data_base64": encoded},
            actor="supervisor",
        )
        await service.execute(
            "finish_artifact_upload", {"upload_id": upload_id}, actor="supervisor"
        )
        upstream.arguments = {"artifact_id": upload_id, "index": 0}
        snapshot = await query(supervisor)
        result = model_result(upstream)
        assert base64.b64decode(result["data_base64"], validate=True) == data
        ended = next(event for event in snapshot["events"] if event["type"] == "tool_end")
        assert ended["data"]["result"] == result
        with pytest.raises(OperationError) as exhausted:
            await service.execute("begin_artifact_upload", manifest, actor="supervisor")
        assert exhausted.value.status_code == 409
        with pytest.raises(OperationError) as oversized:
            await service.execute(
                "begin_artifact_upload", {**manifest, "size": len(data) + 1}, actor="supervisor"
            )
        assert oversized.value.status_code == 413
        with pytest.raises(OperationError) as unknown_member:
            await service.execute(
                "begin_artifact_upload",
                {**manifest, "size": 0, "members": ["missing-agent"]},
                actor="supervisor",
            )
        assert unknown_member.value.status_code == 404
        with pytest.raises(OperationError) as unauthenticated:
            await service.receive(
                "unregistered-agent", Envelope(type="agent.contact", payload={"message": "hello"})
            )
        assert unauthenticated.value.status_code == 401
        with pytest.raises(OperationError) as agent_management:
            await service.execute("get_config", {}, actor="agent:unregistered-agent")
        assert agent_management.value.status_code == 403


async def test_direct_agent_discovery_masks_other_agent_metadata_without_limiting_supervisor(
    tmp_path: Path,
) -> None:
    upstream = Upstream("list_agents")
    async with system(tmp_path, upstream, start=False) as (_, _, service, _):
        requester = await add_peer(service, "requester")
        peer = await add_peer(service, "peer")
        metadata: JSONObject = {
            "workspace": "public-workspace",
            "api_key": "private-peer-api-key",
            "nested": {
                "password": "private-peer-password",
                "credential": "private-peer-credential",
            },
        }
        await service.receive(peer, Envelope(type="heartbeat", payload={"metadata": metadata}))
        discovered = await submit_peer_request(service, requester, "list_agents", {})
        assert discovered["status"] == "approved"
        visible_peer = next(item for item in discovered["result"]["agents"] if item["id"] == peer)
        assert visible_peer["metadata"] == {
            "workspace": "public-workspace",
            "api_key": "********",
            "nested": {"password": "********", "credential": "********"},
            "status": "ready",
        }
        reply = next(
            envelope
            for envelope in await service.pending(requester)
            if envelope.correlation_id == discovered["external_id"]
        )
        assert reply.payload["result"] == discovered["result"]
        complete = (await service.execute("list_agents", {}, actor="supervisor"))["agents"]
        assert next(item for item in complete if item["id"] == peer)["metadata"] == {
            **metadata,
            "status": "ready",
        }


@pytest.mark.parametrize("operation", ["get_config", "get_platform_state", "update_config"])
async def test_resolved_agent_configuration_request_stays_masked_under_supervisor_approval(
    tmp_path: Path, operation: str
) -> None:
    upstream = Upstream("list_agents")
    async with system(tmp_path, upstream, start=False) as (_, settings, service, _):
        requester = await add_peer(service, "requester")
        peer = await add_peer(service, "peer")
        settings.update(
            {
                "model": {
                    "api_key": "private-platform-api-key",
                    "headers": {"X-Tenant": "private-platform-header"},
                    "request_params": {"nested": {"credential": "private-provider-credential"}},
                },
                "auth": {"password_hash": "private-platform-password-hash"},
            }
        )
        await service.receive(
            peer,
            Envelope(type="heartbeat", payload={"metadata": {"password": "private-peer-password"}}),
        )
        arguments: JSONObject = {}
        if operation == "update_config":
            arguments = {"patch": {"model": {"api_key": "agent-requested-api-key"}}}
        requested = await submit_peer_request(service, requester, operation, arguments)
        assert requested["status"] == "pending"
        approved = await service.execute(
            "resolve_agent_request",
            {"request_id": requested["id"], "decision": "approve"},
            actor="supervisor",
        )
        result = approved["request"]["result"]
        assert approved["request"]["status"] == "approved"
        assert result["config"]["model"]["api_key"] == "********"
        assert result["config"]["model"]["headers"] == {"X-Tenant": "********"}
        assert result["config"]["model"]["request_params"] == {"nested": {"credential": "********"}}
        assert result["config"]["auth"]["password_hash"] == "********"
        reply = next(
            envelope
            for envelope in await service.pending(requester)
            if envelope.correlation_id == requested["external_id"]
        )
        assert reply.payload["result"] == result
        raw = await service.execute("get_config", {}, actor="supervisor")
        assert raw["config"]["model"]["api_key"] == (
            "agent-requested-api-key"
            if operation == "update_config"
            else "private-platform-api-key"
        )
        assert raw["config"]["model"]["headers"] == {"X-Tenant": "private-platform-header"}
        assert raw["config"]["auth"]["password_hash"] == "private-platform-password-hash"
        raw_state = await service.execute("get_platform_state", {}, actor="supervisor")
        assert raw_state["config"] == raw["config"]
        assert (
            next(item for item in raw_state["agents"] if item["id"] == peer)["metadata"]["password"]
            == "private-peer-password"
        )
        if operation == "get_platform_state":
            assert (
                next(item for item in result["agents"] if item["id"] == peer)["metadata"][
                    "password"
                ]
                == "********"
            )


@pytest.mark.parametrize("mode", ["ask", "auto", "yolo"])
async def test_lower_agent_cannot_request_private_conversation_but_user_and_supervisor_read_original(
    tmp_path: Path, mode: str
) -> None:
    upstream = Upstream("list_agents")
    async with system(tmp_path, upstream, mode, start=False) as (db, _, service, supervisor):
        requester = await add_peer(service, "requester")
        conversation = await supervisor.create_conversation("Private conversation")
        original_content = json.dumps(
            {"api_key": "private-conversation-api-key", "password": "private-conversation-password"}
        )
        async with db.sessions.begin() as session:
            session.add(
                ChatMessage(
                    conversation_id=conversation["id"], role="user", content=original_content
                )
            )
        envelope = Envelope(
            type="agent.request",
            payload={
                "request_id": "read-private-conversation",
                "operation": "get_conversation",
                "arguments": {"conversation_id": conversation["id"]},
            },
        )
        with pytest.raises(OperationError) as forbidden:
            await service.receive(requester, envelope)
        assert forbidden.value.status_code == 403
        assert await service.pending(requester) == []
        assert (await service.execute("list_agent_requests", {}, actor="supervisor"))[
            "requests"
        ] == []
        expected = await supervisor.get_conversation(conversation["id"])
        for actor in ("user", "supervisor"):
            full = await service.execute(
                "get_conversation", {"conversation_id": conversation["id"]}, actor=actor
            )
            assert full == expected
            assert full["messages"][0]["content"] == original_content
        assert upstream.requests == []


@pytest.mark.parametrize("continuation", ["resolve_agent_request", "approve_operation"])
async def test_pending_extension_request_cannot_execute_after_agent_request_capability_is_removed(
    tmp_path: Path, continuation: str
) -> None:
    upstream = Upstream("list_agents")
    mode = "ask" if continuation == "approve_operation" else "yolo"
    async with system(tmp_path, upstream, mode, start=False) as (_, _, service, _):
        requester = await add_peer(service, "requester")
        effects: list[str] = []

        async def private_payload(arguments: JSONObject) -> JSONObject:
            effects.append(arguments["record_id"])
            return {"content": "private-conversation-fulltext", "api_key": "private-extension-key"}

        service.register(
            "read_private_payload",
            "Read a private payload with an observable access effect",
            {
                "type": "object",
                "properties": {"record_id": {"type": "string"}},
                "required": ["record_id"],
                "additionalProperties": False,
            },
            private_payload,
            mutating=True,
        )
        assert service.operations["read_private_payload"].agent_requestable
        requested = await submit_peer_request(
            service, requester, "read_private_payload", {"record_id": "private-record"}
        )
        assert requested["status"] == "pending"
        arguments: JSONObject = {"request_id": requested["id"], "decision": "approve"}
        if continuation == "approve_operation":
            waiting = await service.execute("resolve_agent_request", arguments, actor="supervisor")
            assert waiting["status"] == "awaiting_approval"
            arguments = {"approval_id": waiting["approval_id"]}
        assert effects == []
        service.operations["read_private_payload"].agent_requestable = False
        with pytest.raises(OperationError) as forbidden:
            await service.execute(
                continuation,
                arguments,
                actor="user" if continuation == "approve_operation" else "supervisor",
            )
        assert forbidden.value.status_code == 403
        assert effects == [] and await service.pending(requester) == []
        requests = (await service.execute("list_agent_requests", {}, actor="supervisor"))[
            "requests"
        ]
        preserved = next(item for item in requests if item["id"] == requested["id"])
        assert preserved["status"] == "pending" and preserved["result"] is None
        if continuation == "approve_operation":
            approvals = (await service.execute("list_approvals", {}, actor="supervisor"))[
                "approvals"
            ]
            approval = next(item for item in approvals if item["id"] == arguments["approval_id"])
            assert approval["status"] == "pending" and approval["result"] is None
        assert upstream.requests == []


@pytest.mark.parametrize("operation", ["get_agent", "list_agent_requests", "list_approvals"])
async def test_resolved_agent_management_views_do_not_inherit_supervisor_secret_visibility(
    tmp_path: Path, operation: str
) -> None:
    upstream = Upstream("list_agents")
    async with system(tmp_path, upstream, start=False) as (_, settings, service, _):
        requester = await add_peer(service, "requester")
        peer = await add_peer(service, "peer")
        await service.receive(
            peer,
            Envelope(type="heartbeat", payload={"metadata": {"api_key": "private-peer-api-key"}}),
        )
        private_request = await submit_peer_request(
            service,
            peer,
            "update_config",
            {"patch": {"model": {"api_key": "private-request-api-key"}}},
        )
        settings.update({"supervisor": {"mode": "ask"}})
        private_approval = await service.execute(
            "update_config",
            {"patch": {"model": {"api_key": "private-approval-api-key"}}},
            actor="supervisor",
        )
        assert private_approval["status"] == "awaiting_approval"
        settings.update({"supervisor": {"mode": "yolo"}})
        arguments: JSONObject = {"agent_id": peer} if operation == "get_agent" else {}
        requested = await submit_peer_request(service, requester, operation, arguments)
        approved = await service.execute(
            "resolve_agent_request",
            {"request_id": requested["id"], "decision": "approve"},
            actor="supervisor",
        )
        assert approved["request"]["status"] == "approved"
        result = approved["request"]["result"]
        raw = await service.execute(operation, arguments, actor="supervisor")
        if operation == "get_agent":
            assert result["agent"]["metadata"]["api_key"] == "********"
            assert raw["agent"]["metadata"]["api_key"] == "private-peer-api-key"
        elif operation == "list_agent_requests":
            visible = next(
                item for item in result["requests"] if item["id"] == private_request["id"]
            )
            original = next(item for item in raw["requests"] if item["id"] == private_request["id"])
            assert visible["arguments"]["patch"]["model"]["api_key"] == "********"
            assert original["arguments"]["patch"]["model"]["api_key"] == "private-request-api-key"
        else:
            visible = next(
                item
                for item in result["approvals"]
                if item["id"] == private_approval["approval_id"]
            )
            original = next(
                item for item in raw["approvals"] if item["id"] == private_approval["approval_id"]
            )
            assert visible["arguments"]["patch"]["model"]["api_key"] == "********"
            assert original["arguments"]["patch"]["model"]["api_key"] == "private-approval-api-key"
        reply = next(
            envelope
            for envelope in await service.pending(requester)
            if envelope.correlation_id == requested["external_id"]
        )
        assert reply.payload["result"] == result
