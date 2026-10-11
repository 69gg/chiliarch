"""Transport-independent Chiliarch 1.0 message protocol.

This module intentionally imports no server, database, or model-provider code.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated, Any, Literal
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, model_validator

PROTOCOL_VERSION: Literal["1.0"] = "1.0"
JsonObject = dict[str, Any]
TaskWorkdir = Annotated[
    str,
    Field(
        min_length=1,
        max_length=4096,
        pattern=r"^(?:/|[a-zA-Z]:[\\/]|\\\\)[^\x00-\x1f]*$",
        # JSON Schema regex engines may match $ before a final newline.
        json_schema_extra={"not": {"pattern": r"[\x00-\x1f]"}},
        description="Absolute directory on the agent machine; omitted uses its local default.",
    ),
]


class Payload(BaseModel):
    model_config = ConfigDict(extra="forbid")


class HeartbeatPayload(Payload):
    status: str = "ready"
    metadata: JsonObject = Field(default_factory=dict)


class AckPayload(Payload):
    message_id: str = Field(min_length=1, max_length=128)


class DispatchPayload(Payload):
    task_id: str = Field(min_length=1)
    instruction: str = Field(min_length=1)
    parameters: JsonObject = Field(default_factory=dict)
    workdir: TaskWorkdir | None = None


class CancelPayload(Payload):
    task_id: str = Field(min_length=1)
    reason: str = "User requested cancellation"


class TaskStatusPayload(Payload):
    task_id: str = Field(min_length=1)
    status: Literal["accepted", "running", "completed", "failed", "cancelled", "interrupted"]
    progress: float | None = Field(default=None, ge=0, le=1)
    result: Any = None
    error: str | None = None
    message: str | None = None


class AgentRequestPayload(Payload):
    request_id: str = Field(min_length=1, max_length=128)
    operation: str = Field(min_length=1)
    arguments: JsonObject = Field(default_factory=dict)
    message: str | None = None
    metadata: JsonObject = Field(default_factory=dict)


class AgentContactPayload(Payload):
    message: str = Field(min_length=1)
    metadata: JsonObject = Field(default_factory=dict)


class AgentMessagePayload(Payload):
    message: str = Field(min_length=1)
    metadata: JsonObject = Field(default_factory=dict)


class RequestResolvedPayload(Payload):
    request_id: str = Field(min_length=1)
    status: Literal["approved", "rejected", "failed"]
    result: Any = None
    error: str | None = None


class CollaborationMessagePayload(Payload):
    message_id: str = Field(min_length=1)
    sender_agent_id: str | None = None
    sender_role: Literal["agent", "user", "supervisor"]
    recipient_agent_id: str = Field(min_length=1)
    channel_id: str | None = None
    task_id: str | None = None
    source_task_id: str | None = None
    message: str = Field(min_length=1)
    reply_to: str | None = None
    sequence: int = Field(ge=1)
    wake: bool = False


PAYLOAD_TYPES: dict[str, type[Payload]] = {
    "heartbeat": HeartbeatPayload,
    "ack": AckPayload,
    "task.dispatch": DispatchPayload,
    "task.cancel": CancelPayload,
    "task.status": TaskStatusPayload,
    "agent.request": AgentRequestPayload,
    "agent.contact": AgentContactPayload,
    "agent.message": AgentMessagePayload,
    "request.resolved": RequestResolvedPayload,
    "collaboration.message": CollaborationMessagePayload,
}
AGENT_MESSAGE_TYPES = frozenset(
    {"heartbeat", "ack", "task.status", "agent.request", "agent.contact"}
)
SERVER_MESSAGE_TYPES = frozenset(
    {"task.dispatch", "task.cancel", "request.resolved", "agent.message", "collaboration.message"}
)


class Envelope(BaseModel):
    """One durable message identity, preserved across retries and transports."""

    model_config = ConfigDict(extra="forbid")
    id: str = Field(default_factory=lambda: str(uuid4()), min_length=1, max_length=128)
    type: str = Field(min_length=1, max_length=128)
    correlation_id: str | None = Field(default=None, min_length=1, max_length=128)
    protocol_version: Literal["1.0"] = PROTOCOL_VERSION
    timestamp: datetime = Field(default_factory=lambda: datetime.now(UTC))
    expires_at: datetime | None = None
    payload: JsonObject = Field(default_factory=dict)

    @model_validator(mode="after")
    def validate_payload(self) -> Envelope:
        if self.timestamp.tzinfo is None:
            raise ValueError("timestamp must include a timezone")
        if self.expires_at is not None and self.expires_at.tzinfo is None:
            raise ValueError("expires_at must include a timezone")
        payload_type = PAYLOAD_TYPES.get(self.type)
        if payload_type is not None:
            legacy_request = self.type == "agent.request" and "metadata" not in self.payload
            self.payload = payload_type.model_validate(self.payload).model_dump(mode="json")
            if self.type == "task.dispatch" and self.payload.get("workdir") is None:
                # Preserve old durable message fingerprints and old-client compatibility.
                self.payload.pop("workdir", None)
            if legacy_request:
                # Keep pre-upgrade request fingerprints stable across redelivery.
                self.payload.pop("metadata", None)
        elif "." not in self.type or self.type.split(".", 1)[0] in {"task", "agent", "request"}:
            raise ValueError("extension message types require a non-reserved namespace")
        return self

    def canonical(self) -> str:
        return json.dumps(self.model_dump(mode="json"), sort_keys=True, separators=(",", ":"))


def protocol_schema() -> JsonObject:
    """Return a portable draft-2020-12 schema with type-specific payloads."""
    schema = Envelope.model_json_schema()
    definitions: JsonObject = schema.setdefault("$defs", {})
    variants: list[JsonObject] = []
    for message_type, payload_type in PAYLOAD_TYPES.items():
        payload_schema = payload_type.model_json_schema(ref_template="#/$defs/{model}")
        definitions.update(payload_schema.pop("$defs", {}))
        definitions[payload_type.__name__] = payload_schema
        variants.append(
            {
                "properties": {
                    "type": {"const": message_type},
                    "payload": {"$ref": f"#/$defs/{payload_type.__name__}"},
                },
                "required": ["type", "payload"],
            }
        )
    variants.append(
        {
            "properties": {
                "type": {
                    "pattern": r"^(?!task\.|agent\.|request\.)[^.]+\..+$",
                    "not": {"enum": list(PAYLOAD_TYPES)},
                },
                "payload": {"type": "object"},
            },
            "required": ["type", "payload"],
        }
    )
    schema["oneOf"] = variants
    schema["$schema"] = "https://json-schema.org/draft/2020-12/schema"
    schema["$id"] = "urn:chiliarch:protocol:1.0"
    return schema


def export_schema(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(protocol_schema(), indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )


__all__ = [
    "PROTOCOL_VERSION",
    "Envelope",
    "HeartbeatPayload",
    "AckPayload",
    "DispatchPayload",
    "TaskWorkdir",
    "CancelPayload",
    "TaskStatusPayload",
    "AgentRequestPayload",
    "AgentContactPayload",
    "AgentMessagePayload",
    "RequestResolvedPayload",
    "CollaborationMessagePayload",
    "AGENT_MESSAGE_TYPES",
    "SERVER_MESSAGE_TYPES",
    "protocol_schema",
    "export_schema",
]
