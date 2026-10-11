"""Persistent, private daemon configuration and fixed local API contracts."""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from chiliarch.config import LogSettings, TransportSettings

DAEMON_API_VERSION = 1


class DaemonError(ValueError):
    """The local daemon cannot safely perform the requested action."""


class AgentRegistration(BaseModel):
    model_config = ConfigDict(extra="forbid")

    handle: str = Field(pattern=r"^[A-Za-z0-9_.-]{1,128}$")
    state_path: Path
    runner_settings: Path | None = None
    max_concurrent_tasks: int = Field(default=1, ge=1, le=64)
    preferred_transport: Literal["ws", "sse", "poll"] = "ws"
    token: str


class DaemonConfiguration(BaseModel):
    model_config = ConfigDict(extra="forbid")

    format_version: Literal[1] = 1
    port: int = Field(default=0, ge=0, le=65535)
    startup_timeout_seconds: float = Field(default=20, ge=1, le=120)
    graceful_shutdown_seconds: int = Field(default=15, gt=0)
    logs: LogSettings = Field(default_factory=LogSettings)
    transport: TransportSettings = Field(default_factory=TransportSettings)
    agents: list[AgentRegistration] = Field(default_factory=list)


class RegistrationRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    state_path: Path
    handle: str | None = Field(default=None, pattern=r"^[A-Za-z0-9_.-]{1,128}$")
    runner_settings: Path | None = None
    max_concurrent_tasks: int = Field(default=1, ge=1, le=64)
    preferred_transport: Literal["ws", "sse", "poll"] = "ws"


class MailboxRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    method: Literal[
        "status",
        "list_tasks",
        "read_messages",
        "acknowledge_messages",
        "send_message",
        "request_operation",
        "get_request_result",
        "list_agents",
        "create_channel",
        "list_channels",
        "get_channel",
        "send_peer_message",
        "acknowledge_peer_message",
        "get_peer_message",
        "wait_for_tasks",
        "list_task_waits",
        "upload_artifact",
        "download_artifact",
        "claim_task",
        "report_progress",
        "complete_task",
        "fail_task",
        "confirm_cancellation",
    ]
    arguments: dict[str, Any] = Field(default_factory=dict)


def private_write(path: Path, payload: dict[str, Any]) -> None:
    """Atomically replace private JSON, including on Windows where open renames fail."""
    path.parent.mkdir(parents=True, exist_ok=True)
    os.chmod(path.parent, 0o700)
    descriptor, temporary = tempfile.mkstemp(dir=path.parent, prefix=path.name + ".")
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            json.dump(payload, stream, ensure_ascii=False, indent=2, allow_nan=False)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.chmod(temporary, 0o600)
        os.replace(temporary, path)
    finally:
        Path(temporary).unlink(missing_ok=True)


def read_configuration(directory: Path) -> DaemonConfiguration:
    path = directory / "daemon.json"
    if path.exists():
        configuration = DaemonConfiguration.model_validate_json(path.read_text(encoding="utf-8"))
    else:
        configuration = DaemonConfiguration(logs=LogSettings(directory=directory / "logs"))
    if not configuration.logs.directory.is_absolute():
        configuration.logs.directory = directory / configuration.logs.directory
    handles = [entry.handle for entry in configuration.agents]
    states = [entry.state_path.resolve() for entry in configuration.agents]
    if len(set(handles)) != len(handles) or len(set(states)) != len(states):
        raise DaemonError("Daemon configuration contains duplicate agent handles or states")
    return configuration
