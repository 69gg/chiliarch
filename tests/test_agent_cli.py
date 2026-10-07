"""Agent-machine CLI contracts without invoking installed vendor agents."""

from __future__ import annotations

import asyncio
import json
import sys
import tomllib
from pathlib import Path
from typing import Any
from unittest.mock import Mock

import pytest

from chiliarch import cli
from chiliarch.adapters.cli import load_runner_settings
from chiliarch.client import ClientStore


async def create_identity(state_path: Path) -> dict[str, str]:
    identity = {
        "agent_id": "fixture-local-agent",
        "name": "fixture worker",
        "machine": "fixture-machine",
        "server_url": "http://127.0.0.1:1",
        "credential": "fixture-credential-never-public",
    }
    store = ClientStore(state_path)
    await store.open()
    try:
        await store.save_identity(identity)
    finally:
        await store.close()
    return identity


def command(arguments: list[str], monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sys, "argv", ["chiliarch", "agent", *arguments])
    cli.main()


@pytest.mark.parametrize("host", ["codex-app", "codex", "claude-code"])
@pytest.mark.parametrize("installed_entrypoint", [False, True])
def test_config_stdout_is_native_parseable_and_points_at_standalone_bridge(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    host: str,
    installed_entrypoint: bool,
) -> None:
    state_path = tmp_path / "identity with spaces 'quoted' 名字.sqlite"
    asyncio.run(create_identity(state_path))
    executable = str(tmp_path / "tool directory" / "chiliarch")
    which = Mock(return_value=executable if installed_entrypoint else None)
    monkeypatch.setattr("chiliarch.adapters.cli.shutil.which", which)
    before = set(tmp_path.rglob("*"))

    command(["config", "--host", host, "--state", str(state_path)], monkeypatch)

    captured = capsys.readouterr()
    assert not captured.err
    configuration: dict[str, Any] = (
        json.loads(captured.out) if host == "claude-code" else tomllib.loads(captured.out)
    )
    servers = configuration["mcpServers" if host == "claude-code" else "mcp_servers"]
    assert set(servers) == {"chiliarch"}
    entry = servers["chiliarch"]
    assert entry["command"] == (executable if installed_entrypoint else sys.executable)
    arguments = entry["args"]
    if not installed_entrypoint:
        assert arguments[:2] == ["-m", "chiliarch"]
        arguments = arguments[2:]
    parsed = cli.parser().parse_args(arguments)
    assert parsed.command == "agent"
    assert parsed.agent_command == "mcp"
    assert parsed.state == state_path.resolve()
    assert parsed.mode == "standalone"
    assert parsed.transport == "ws"
    assert parsed.managed_task is None
    if host == "claude-code":
        assert entry["type"] == "stdio"
    else:
        assert entry["startup_timeout_sec"] >= 1
        assert entry["tool_timeout_sec"] >= 30
    assert "fixture-credential-never-public" not in captured.out
    assert set(tmp_path.rglob("*")) == before
    which.assert_called_once_with("chiliarch")


@pytest.mark.parametrize("kind", ["codex", "claude-code"])
def test_relative_workdir_is_bound_to_local_settings_file_not_launch_cwd(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, kind: str
) -> None:
    settings_directory = tmp_path / "config"
    workspace = settings_directory / "workspace"
    unrelated = tmp_path / "unrelated launch directory"
    workspace.mkdir(parents=True)
    unrelated.mkdir()
    settings_path = settings_directory / "local-agent.toml"
    settings_path.write_text(
        f'kind = "{kind}"\nworkdir = "workspace"\ntimeout_seconds = 40\n',
        encoding="utf-8",
    )
    monkeypatch.chdir(tmp_path)
    first = load_runner_settings(Path("config/local-agent.toml"))
    monkeypatch.chdir(unrelated)
    second = load_runner_settings(settings_path)
    assert first == second
    assert first.workdir == workspace.resolve()
    assert first.timeout_seconds == 40
    assert first.codex_sandbox == "read-only"
    assert first.claude_permission_mode == "plan"


def test_doctor_reports_only_public_identity_without_native_or_network_calls(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    state_path = tmp_path / "agent.sqlite"
    identity = asyncio.run(create_identity(state_path))
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    settings_path = tmp_path / "local-agent.toml"
    settings_path.write_text(
        'kind = "codex"\nworkdir = "workspace"\nexecutable = "fixture-native"\n',
        encoding="utf-8",
    )
    which = Mock(side_effect=lambda executable: str(tmp_path / executable))
    monkeypatch.setattr("chiliarch.adapters.cli.shutil.which", which)
    native = Mock(side_effect=AssertionError("doctor must not invoke native programs"))
    network = Mock(side_effect=AssertionError("doctor must not connect to a service"))
    monkeypatch.setattr("subprocess.Popen", native)
    monkeypatch.setattr("subprocess.run", native)
    monkeypatch.setattr("asyncio.create_subprocess_exec", native)
    monkeypatch.setattr("httpx.AsyncClient", network)

    command(["doctor", "--state", str(state_path), "--settings", str(settings_path)], monkeypatch)

    captured = capsys.readouterr()
    report = json.loads(captured.out)
    assert report["paired"] is True
    assert report["identity"] == {
        key: identity[key] for key in ("agent_id", "name", "machine", "server_url")
    }
    assert report["runner"] == {
        "kind": "codex",
        "workdir": str(workspace),
        "executable": str(tmp_path / "fixture-native"),
        "permission": "read-only",
    }
    assert identity["credential"] not in captured.out + captured.err
    native.assert_not_called()
    network.assert_not_called()


def test_doctor_missing_state_stays_unpaired_without_creating_database(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    missing = tmp_path / "not-paired.sqlite"
    command(["doctor", "--state", str(missing)], monkeypatch)
    report = json.loads(capsys.readouterr().out)
    assert report["paired"] is False
    assert "identity" not in report
    assert not missing.exists()


def test_pair_refuses_existing_state_before_prompt_or_http_and_preserves_data(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    state_path = tmp_path / "existing-state.sqlite"
    original = b"existing credential and task ledger must be preserved"
    state_path.write_bytes(original)
    network = Mock(side_effect=AssertionError("must reject before creating HTTP client"))
    prompt = Mock(side_effect=AssertionError("must reject before asking for a new code"))
    monkeypatch.setattr("httpx.AsyncClient", network)
    monkeypatch.setattr("chiliarch.adapters.cli.getpass.getpass", prompt)

    with pytest.raises(SystemExit, match="State already exists"):
        command(
            [
                "pair",
                "--server",
                "http://127.0.0.1:1",
                "--name",
                "new identity",
                "--adapter",
                "codex-app",
                "--state",
                str(state_path),
            ],
            monkeypatch,
        )

    network.assert_not_called()
    prompt.assert_not_called()
    assert state_path.read_bytes() == original
