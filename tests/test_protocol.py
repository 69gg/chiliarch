from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator
from pydantic import ValidationError

from chiliarch.protocol import Envelope, export_schema, protocol_schema


@pytest.mark.parametrize(
    "message_type,payload",
    [
        ("heartbeat", {}),
        ("ack", {"message_id": "m1"}),
        ("task.dispatch", {"task_id": "t1", "instruction": "Inspect"}),
        ("task.status", {"task_id": "t1", "status": "interrupted"}),
        ("agent.contact", {"message": "Help"}),
        ("myplugin.event", {"data": [1, 2]}),
    ],
)
def test_protocol_roundtrip_and_portable_schema(
    message_type: str, payload: dict[str, object]
) -> None:
    envelope = Envelope(type=message_type, payload=payload)
    Draft202012Validator(protocol_schema()).validate(envelope.model_dump(mode="json"))
    assert (
        Envelope.model_validate_json(envelope.model_dump_json()).canonical() == envelope.canonical()
    )


def test_invalid_typed_payload_fails_python_and_foreign_schema() -> None:
    envelope = Envelope(
        type="task.dispatch", payload={"task_id": "t", "instruction": "Test"}
    ).model_dump(mode="json")
    envelope["payload"] = {"task_id": "t"}
    assert list(Draft202012Validator(protocol_schema()).iter_errors(envelope))
    with pytest.raises(ValidationError):
        Envelope.model_validate(envelope)
    with pytest.raises(ValidationError):
        Envelope(type="task.unknown", payload={})
    with pytest.raises(ValidationError):
        Envelope(type="heartbeat", timestamp=datetime(2026, 1, 1), payload={})


def test_schema_export_is_valid_and_matches_runtime(tmp_path: Path) -> None:
    target = tmp_path / "schema.json"
    export_schema(target)
    exported = json.loads(target.read_text())
    Draft202012Validator.check_schema(exported)
    assert exported == protocol_schema()
