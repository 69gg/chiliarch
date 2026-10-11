"""Model-visible tool contracts retain guidance across each provider's wire format."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import httpx
import pytest
from jsonschema import Draft202012Validator
from test_ai_providers import SSE, text_response
from test_peer_service import agent, running
from test_peer_service import service as service

from chiliarch.ai import ModelTurn, Provider
from chiliarch.chat import Supervisor
from chiliarch.client import ClientStore
from chiliarch.config import ModelSettings
from chiliarch.protocol import Envelope
from chiliarch.service import OperationError, OperationService

JSONObject = dict[str, Any]


def assert_described_properties(schema: JSONObject) -> None:
    """Check the served schema, including nested references, never Python source text."""
    for definition in schema.get("$defs", {}).values():
        assert_described_properties(definition)
    for name, field in schema.get("properties", {}).items():
        assert isinstance(field.get("description"), str), name
        assert field["description"].strip(), name
        assert_described_properties(field)


@pytest.mark.parametrize("mode", ["chat_completions", "responses", "anthropic"])
async def test_complete_runtime_guidance_reaches_provider_requests(
    service: OperationService, mode: str
) -> None:
    supervisor = Supervisor(service.db, service, service.settings_manager)
    tools = supervisor.tool_schemas()
    original = {tool["name"]: tool for tool in service.schemas()}
    bodies: list[JSONObject] = []
    for tool in tools:
        assert tool["description"].strip()
        Draft202012Validator.check_schema(tool["parameters"])
        assert_described_properties(tool["parameters"])
        assert "_risk" not in original[tool["name"]]["parameters"]["properties"]
        assert ("_risk" in tool["parameters"]["properties"]) is tool["mutating"]

    async def handle(request: httpx.Request) -> httpx.Response:
        bodies.append(json.loads(request.content))
        return httpx.Response(
            200,
            headers={"content-type": "text/event-stream"},
            stream=SSE(text_response(mode)),
        )

    provider = Provider(
        ModelSettings.model_validate(
            {"api_mode": mode, "model": "test", "anthropic_max_tokens": 4096}
        ),
        http_client=httpx.AsyncClient(transport=httpx.MockTransport(handle)),
    )
    try:
        turn = ModelTurn()
        async for _ in provider.stream(
            [{"role": "user", "content": "Inspect collaboration tools."}], tools, "system", turn
        ):
            pass
        assert turn.content == "Done"
        assert len(bodies) == 1
        received = [
            item["function"] if mode == "chat_completions" else item for item in bodies[0]["tools"]
        ]
        expected = {tool["name"]: tool for tool in tools}
        assert {tool["name"] for tool in received} == set(expected)
        parameter_key = "input_schema" if mode == "anthropic" else "parameters"
        for tool in received:
            assert tool["description"] == expected[tool["name"]]["description"]
            assert tool[parameter_key] == expected[tool["name"]]["parameters"]
            assert "risk" not in tool and "mutating" not in tool
    finally:
        await provider.close()


async def test_described_contact_reply_targets_only_the_original_task_inbox(
    service: OperationService, tmp_path: Path
) -> None:
    recipient = await agent(service, "recipient")
    first_task = await running(service, recipient)
    second_task = await running(service, recipient)
    contact = Envelope(
        type="agent.contact",
        payload={"message": "Need clarification", "metadata": {"task_id": first_task}},
    )
    await service.receive(recipient, contact)
    parameters = {
        "agent_id": recipient,
        "message": "Use the existing interface.",
        "correlation_id": contact.id,
        "metadata": {"task_id": first_task},
    }
    schema = next(item for item in service.schemas() if item["name"] == "send_agent_message")
    Draft202012Validator(schema["parameters"]).validate(parameters)
    result = await service.execute("send_agent_message", parameters, actor="supervisor")
    assert result["status"] == "queued"
    store = ClientStore(tmp_path / "recipient.sqlite")
    await store.open()
    try:
        for envelope in await service.pending(recipient):
            await store.accept(envelope)
        replies = await store.messages(task_id=first_task)
        assert len(replies) == 1
        assert replies[0][1].id == result["message_id"]
        assert replies[0][1].correlation_id == contact.id
        assert replies[0][1].payload["message"] == parameters["message"]
        assert await store.messages(task_id=second_task) == []
    finally:
        await store.close()


async def test_described_approval_wait_has_no_side_effect_until_user_decision(
    service: OperationService,
) -> None:
    recipient = await agent(service, "recipient")
    service.settings_manager.current.supervisor.mode = "ask"
    supervisor = Supervisor(service.db, service, service.settings_manager)
    schemas = {item["name"]: item for item in supervisor.tool_schemas()}
    args = {"agent_id": recipient, "instruction": "Inspect the workspace."}
    Draft202012Validator(schemas["dispatch_task"]["parameters"]).validate(args)
    pending = await service.execute("dispatch_task", args, actor="supervisor")
    assert pending["status"] == "awaiting_approval"
    assert (await service.execute("list_tasks", {}))["tasks"] == []
    assert await service.pending(recipient) == []
    decision = {"approval_id": pending["approval_id"]}
    for name in ("approve_operation", "reject_operation"):
        Draft202012Validator(schemas[name]["parameters"]).validate(decision)
        with pytest.raises(OperationError) as refused:
            await service.execute(name, decision, actor="supervisor")
        assert refused.value.status_code == 403
    await service.execute("approve_operation", decision, actor="user")
    tasks = (await service.execute("list_tasks", {}))["tasks"]
    assert len(tasks) == 1
    assert tasks[0]["status"] == "queued"
    assert [message.type for message in await service.pending(recipient)] == ["task.dispatch"]
