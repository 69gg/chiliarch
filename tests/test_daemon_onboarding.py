"""Public onboarding commands never invoke real services or vendor agents in tests."""

from __future__ import annotations

import asyncio
import json
import sys
import tomllib
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, Mock

import pytest

from chiliarch import cli
from chiliarch.adapters.runtime import connection_owner
from chiliarch.client import ClientStore
from chiliarch.daemon_models import DaemonError

JsonObject = dict[str, Any]


def command(arguments: list[str], monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sys, "argv", ["chiliarch", "agent", *arguments])
    cli.main()


@pytest.mark.parametrize("host", ["codex-app", "codex", "claude-code", "opencode", "opencode-v1"])
def test_daemon_configuration_is_native_parseable_uses_handle_and_leaves_disk_unchanged(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    host: str,
) -> None:
    directory = tmp_path / "守护 directory with spaces"
    directory.mkdir()
    python = str(tmp_path / "tool directory" / "chiliarch")
    monkeypatch.setattr("chiliarch.adapters.cli.shutil.which", lambda _: python)
    command(
        ["config", "--host", host, "--agent", "worker-one", "--daemon-directory", str(directory)],
        monkeypatch,
    )
    output = capsys.readouterr()
    configuration: JsonObject = (
        tomllib.loads(output.out) if host in {"codex-app", "codex"} else json.loads(output.out)
    )
    if host in {"opencode", "opencode-v1"}:
        servers = configuration["mcp"]["servers"] if host == "opencode" else configuration["mcp"]
        entry = servers["chiliarch"]
        assert entry["command"][0] == python
        arguments = entry["command"][1:]
        assert entry["type"] == "local"
        if host == "opencode":
            assert entry["disabled"] is False
            assert entry["codemode"] is False
        else:
            assert entry["enabled"] is True
    else:
        entry = configuration["mcpServers" if host == "claude-code" else "mcp_servers"]["chiliarch"]
        assert entry["command"] == python
        arguments = entry["args"]
    parsed = cli.parser().parse_args(arguments)
    assert parsed.command == "agent" and parsed.agent_command == "mcp"
    assert parsed.mode == "daemon"
    assert parsed.agent == "worker-one"
    assert parsed.daemon_directory == directory.resolve()
    assert parsed.state is None
    assert list(directory.iterdir()) == []
    assert not output.err


@pytest.mark.parametrize(
    "arguments",
    [
        ["config", "--host", "codex", "--state", "fixture.db", "--agent", "worker"],
        ["mcp", "--mode", "daemon", "--state", "fixture.db"],
        ["mcp", "--mode", "standalone", "--agent", "worker"],
        ["config", "--host", "codex", "--mode", "mailbox", "--agent", "worker"],
    ],
)
def test_conflicting_bridge_targets_fail_before_starting_mcp_or_editing_state(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, arguments: list[str]
) -> None:
    monkeypatch.chdir(tmp_path)
    with pytest.raises(SystemExit):
        command(arguments, monkeypatch)
    assert list(tmp_path.iterdir()) == []


def test_daemon_mcp_routes_to_existing_local_identity_without_an_outbound_client(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    directory = tmp_path / "daemon"
    run = Mock()
    server = Mock(run=run)
    create = Mock(return_value=server)
    logging = Mock()
    close_logging = Mock()
    outbound = Mock(side_effect=AssertionError("MCP bridge must not own a platform connection"))
    monkeypatch.setattr("chiliarch.adapters.mcp.create_mcp_server", create)
    monkeypatch.setattr("chiliarch.adapters.cli.configure_logging", logging)
    monkeypatch.setattr("chiliarch.adapters.cli.close_logging", close_logging)
    monkeypatch.setattr("chiliarch.adapters.cli.outbound_client", outbound)
    command(["mcp", "--agent", "worker-one", "--daemon-directory", str(directory)], monkeypatch)
    create.assert_called_once_with(
        mode="daemon",
        daemon_directory=directory.resolve(),
        agent_handle="worker-one",
        preferred="ws",
        managed_task=None,
    )
    run.assert_called_once_with(transport="stdio")
    logging.assert_called_once()
    close_logging.assert_called_once()
    outbound.assert_not_called()
    assert not directory.exists()


def pair_arguments(state_path: Path, directory: Path) -> list[str]:
    return [
        "pair",
        "--server",
        "http://platform.invalid",
        "--name",
        "Development machine",
        "--adapter",
        "codex-app",
        "--state",
        str(state_path),
        "--workdir",
        str(state_path.parent),
        "--agent",
        "app-main",
        "--daemon-directory",
        str(directory),
    ]


@pytest.mark.parametrize(
    "adapter", ["codex-app", "codex", "claude-code", "opencode", "opencode-v1"]
)
@pytest.mark.parametrize("mode", ["managed", "prompt", "settings", "manual"])
def test_pair_checks_health_before_private_prompt_and_registers_after_state_is_durable(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    adapter: str,
    mode: str,
) -> None:
    state_path = tmp_path / "identity.db"
    directory = tmp_path / "daemon"
    order: list[str] = []
    existing: str | None = None
    if mode == "settings":
        kind = "codex" if adapter == "codex-app" else adapter
        existing = f'kind = "{kind}"\nworkdir = "."\ntimeout_seconds = 60\n'
        state_path.with_name(state_path.name + ".runner.toml").write_text(
            existing, encoding="utf-8"
        )

    async def ensure(path: Path | None = None) -> JsonObject:
        assert path == directory
        assert not state_path.exists()
        order.append("health")
        return {"status": "ok"}

    def prompt(_: str) -> str:
        assert order == ["health"]
        order.append("prompt")
        return "private-one-time-code"

    async def pair(
        server: str, code: str, name: str, machine: str, path: Path, **kwargs: Any
    ) -> None:
        assert order == ["health", "prompt"]
        assert code == "private-one-time-code"
        assert ("chiliarch.task_workdir" in kwargs["capabilities"]) == (mode != "manual")
        assert f"adapter.{adapter}" in kwargs["capabilities"]
        order.append("pair")
        store = ClientStore(path)
        await store.open()
        try:
            await store.save_identity(
                {
                    "agent_id": "platform-agent",
                    "credential": "private-long-term-credential",
                    "server_url": server,
                    "name": name,
                    "machine": machine,
                }
            )
        finally:
            await store.close()

    class LocalDaemon:
        def __init__(self, path: Path | None) -> None:
            assert path == directory

        async def __aenter__(self) -> LocalDaemon:
            return self

        async def __aexit__(self, *_: object) -> None:
            pass

        async def agents(self) -> list[JsonObject]:
            assert order == ["health"]
            return []

        async def register(
            self, path: Path, *, handle: str | None = None, runner_settings: Path | None = None
        ) -> JsonObject:
            assert order == ["health", "prompt", "pair"]
            assert path == state_path.resolve()
            assert handle == "app-main"
            if mode != "manual":
                assert runner_settings == state_path.with_name(state_path.name + ".runner.toml")
                raw = await asyncio.to_thread(runner_settings.read_text, encoding="utf-8")
                settings = tomllib.loads(raw)
                assert settings["kind"] == ("codex" if adapter == "codex-app" else adapter)
                if mode == "settings":
                    assert raw == existing
                    assert settings["timeout_seconds"] == 60
                else:
                    assert settings["workdir"] == str(await asyncio.to_thread(tmp_path.resolve))
            else:
                assert runner_settings is None
                assert not state_path.with_name(state_path.name + ".runner.toml").exists()
            # The connection ownership lock must be released before the daemon
            # begins owning this identity; pairing must also have closed its DB.
            async with connection_owner(path):
                store = ClientStore(path)
                await store.open()
                try:
                    identity = await store.identity()
                    assert identity["credential"] == "private-long-term-credential"
                finally:
                    await store.close()
            order.append("register")
            return {"handle": handle}

    monkeypatch.setattr("chiliarch.daemon_client.ensure_daemon", ensure)
    monkeypatch.setattr("chiliarch.daemon_client.DaemonClient", LocalDaemon)
    monkeypatch.setattr("chiliarch.adapters.cli.getpass.getpass", prompt)
    monkeypatch.setattr("chiliarch.adapters.cli.AgentClient.pair", pair)
    directory_prompt = Mock(return_value=str(tmp_path))
    monkeypatch.setattr("builtins.input", directory_prompt)
    arguments = pair_arguments(state_path, directory)
    arguments[arguments.index("--adapter") + 1] = adapter
    if mode in {"manual", "prompt"}:
        index = arguments.index("--workdir")
        del arguments[index : index + 2]
    if mode == "manual":
        arguments.extend(["--mode", "manual"])
    if mode == "settings":
        index = arguments.index("--workdir")
        arguments[index : index + 2] = [
            "--settings",
            str(state_path.with_name(state_path.name + ".runner.toml")),
        ]
    command(arguments, monkeypatch)
    assert directory_prompt.call_count == (1 if mode == "prompt" else 0)
    output = capsys.readouterr()
    assert order == ["health", "prompt", "pair", "register"]
    assert "app-main" in output.out
    assert "private-one-time-code" not in output.out + output.err
    assert "private-long-term-credential" not in output.out + output.err


@pytest.mark.parametrize("failure", ["missing_directory", "wrong_kind", "existing_settings"])
@pytest.mark.parametrize("adapter", ["codex-app", "opencode", "opencode-v1"])
def test_invalid_managed_setup_does_not_consume_pairing_code(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, failure: str, adapter: str
) -> None:
    state_path = tmp_path / "identity.db"
    arguments = pair_arguments(state_path, tmp_path / "daemon")
    arguments[arguments.index("--adapter") + 1] = adapter
    settings_path = state_path.with_name(state_path.name + ".runner.toml")
    other_kind = {"opencode": "opencode-v1", "opencode-v1": "opencode"}.get(adapter, "claude-code")
    original = f'kind = "{other_kind}"\nworkdir = "."\n'
    if failure == "missing_directory":
        arguments[arguments.index("--workdir") + 1] = str(tmp_path / "absent")
    else:
        settings_path.write_text(original, encoding="utf-8")
        if failure == "wrong_kind":
            index = arguments.index("--workdir")
            arguments[index : index + 2] = ["--settings", str(settings_path)]
    pair = AsyncMock()
    prompt = Mock(side_effect=AssertionError("Must validate local setup before asking for a code"))
    monkeypatch.setattr("chiliarch.daemon_client.ensure_daemon", AsyncMock())
    daemon = AsyncMock()
    daemon.agents.return_value = []
    daemon.__aenter__.return_value = daemon
    monkeypatch.setattr("chiliarch.daemon_client.DaemonClient", Mock(return_value=daemon))
    monkeypatch.setattr("chiliarch.adapters.cli.AgentClient.pair", pair)
    monkeypatch.setattr("chiliarch.adapters.cli.getpass.getpass", prompt)
    with pytest.raises(SystemExit):
        command(arguments, monkeypatch)
    pair.assert_not_awaited()
    prompt.assert_not_called()
    assert not state_path.exists()
    if failure != "missing_directory":
        assert settings_path.read_text(encoding="utf-8") == original


@pytest.mark.parametrize("handle", ["", "path/to/agent", "有中文", "a" * 129])
def test_invalid_local_handle_fails_before_daemon_start_or_pairing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, handle: str
) -> None:
    state_path = tmp_path / "identity.db"
    arguments = pair_arguments(state_path, tmp_path / "daemon")
    arguments[arguments.index("--agent") + 1] = handle
    ensure = AsyncMock()
    pair = AsyncMock()
    monkeypatch.setattr("chiliarch.daemon_client.ensure_daemon", ensure)
    monkeypatch.setattr("chiliarch.adapters.cli.AgentClient.pair", pair)
    with pytest.raises(SystemExit):
        command(arguments, monkeypatch)
    ensure.assert_not_awaited()
    pair.assert_not_awaited()
    assert not state_path.exists()


def test_occupied_handle_does_not_consume_code_or_replace_another_identity(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    state_path = tmp_path / "new.db"
    existing = tmp_path / "existing.db"
    existing.write_bytes(b"existing credential ledger")
    daemon = AsyncMock()
    daemon.__aenter__.return_value = daemon
    daemon.agents.return_value = [{"handle": "app-main", "state_path": str(existing)}]
    pair = AsyncMock()
    prompt = Mock()
    monkeypatch.setattr("chiliarch.daemon_client.ensure_daemon", AsyncMock())
    monkeypatch.setattr("chiliarch.daemon_client.DaemonClient", Mock(return_value=daemon))
    monkeypatch.setattr("chiliarch.adapters.cli.AgentClient.pair", pair)
    monkeypatch.setattr("chiliarch.adapters.cli.getpass.getpass", prompt)
    with pytest.raises(SystemExit, match="already registered"):
        command(pair_arguments(state_path, tmp_path / "daemon"), monkeypatch)
    pair.assert_not_awaited()
    prompt.assert_not_called()
    assert not state_path.exists()
    assert existing.read_bytes() == b"existing credential ledger"


@pytest.mark.parametrize("failure", [None, "settings", "registration"])
def test_pairing_preserves_credentials_and_explains_partial_setup_recovery(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, failure: str | None
) -> None:
    # A state file ending in .runner.toml must remain SQLite, not be replaced by TOML.
    state_path = tmp_path / "identity.runner.toml"
    settings_path = state_path.with_name(state_path.name + ".runner.toml")
    daemon = AsyncMock()
    daemon.__aenter__.return_value = daemon
    daemon.agents.return_value = []
    daemon.register.return_value = {"handle": "app-main"}
    if failure == "registration":
        daemon.register.side_effect = DaemonError("daemon unavailable")

    async def pair(
        server: str, code: str, name: str, machine: str, path: Path, **kwargs: Any
    ) -> None:
        store = ClientStore(path)
        await store.open()
        try:
            await store.save_identity(
                {"credential": "persisted-private-credential", "agent_id": "platform-agent"}
            )
        finally:
            await store.close()

    async def saved_identity() -> JsonObject:
        store = ClientStore(state_path)
        await store.open()
        try:
            return await store.identity()
        finally:
            await store.close()

    monkeypatch.setattr("chiliarch.daemon_client.ensure_daemon", AsyncMock())
    monkeypatch.setattr("chiliarch.daemon_client.DaemonClient", Mock(return_value=daemon))
    monkeypatch.setattr("chiliarch.adapters.cli.AgentClient.pair", pair)
    monkeypatch.setattr("chiliarch.adapters.cli.getpass.getpass", lambda _: "one-time-code")
    if failure == "settings":
        monkeypatch.setattr(
            "chiliarch.config.atomic_write_toml", Mock(side_effect=OSError("disk full"))
        )
    arguments = pair_arguments(state_path, tmp_path / "daemon")
    if failure:
        with pytest.raises(SystemExit, match="不要重新配对") as error:
            command(arguments, monkeypatch)
        message = str(error.value)
        assert str(state_path) in message
        assert str(settings_path) in message
        assert "persisted-private-credential" not in message
        assert "one-time-code" not in message
    else:
        command(arguments, monkeypatch)
    assert asyncio.run(saved_identity())["credential"] == "persisted-private-credential"
    if failure != "settings":
        assert tomllib.loads(settings_path.read_text())["kind"] == "codex"
        daemon.register.assert_awaited_once_with(
            state_path, handle="app-main", runner_settings=settings_path
        )
    else:
        assert not settings_path.exists()
        daemon.register.assert_not_awaited()


def test_unhealthy_daemon_blocks_pair_before_consuming_registration_code(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    state_path = tmp_path / "identity.db"
    healthy = AsyncMock(side_effect=DaemonError("Local daemon version is incompatible"))
    pair = AsyncMock(side_effect=AssertionError("must not exchange the one-time code"))
    monkeypatch.setattr("chiliarch.daemon_client.ensure_daemon", healthy)
    monkeypatch.setattr("chiliarch.adapters.cli.AgentClient.pair", pair)
    monkeypatch.setattr(
        "chiliarch.adapters.cli.getpass.getpass",
        lambda _: pytest.fail("must not ask for a code before daemon health succeeds"),
    )
    with pytest.raises(SystemExit, match="incompatible"):
        command(pair_arguments(state_path, tmp_path / "daemon"), monkeypatch)
    healthy.assert_awaited_once_with(tmp_path / "daemon")
    pair.assert_not_called()
    assert not state_path.exists()


@pytest.mark.parametrize("available", [True, False])
def test_doctor_is_read_only_checks_health_and_only_the_requested_registration(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    available: bool,
) -> None:
    directory = tmp_path / "daemon"
    ensure = AsyncMock(side_effect=AssertionError("doctor must never install or start services"))
    monkeypatch.setattr("chiliarch.daemon_client.ensure_daemon", ensure)
    registrations = [
        {"handle": "worker-one", "mode": "manual", "connection": "running"},
        {"handle": "other-worker", "mode": "managed", "connection": "running"},
    ]

    class LocalDaemon:
        def __init__(self, path: Path | None) -> None:
            assert path == directory

        async def __aenter__(self) -> LocalDaemon:
            return self

        async def __aexit__(self, *_: object) -> None:
            pass

        async def health(self) -> JsonObject:
            if not available:
                raise DaemonError("Local daemon unavailable")
            return {"status": "ok", "agent_count": 2}

        async def agents(self) -> list[JsonObject]:
            return registrations

    monkeypatch.setattr("chiliarch.daemon_client.DaemonClient", LocalDaemon)
    command(["doctor", "--agent", "worker-one", "--daemon-directory", str(directory)], monkeypatch)
    report = json.loads(capsys.readouterr().out)
    assert report["registered"] is available
    if available:
        assert report["daemon"] == {"status": "ok", "agent_count": 2}
        assert report["agents"] == registrations[:1]
    else:
        assert report["daemon"]["status"] == "unavailable"
    ensure.assert_not_called()
    assert not directory.exists()
