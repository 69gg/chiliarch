"""Exercise the public setup CLI and persist validated, portable configuration."""

from __future__ import annotations

import sys
import threading
import tomllib
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any
from unittest.mock import Mock

import pytest
from test_paths import tool_environment

from chiliarch import cli
from chiliarch.auth import verify_password
from chiliarch.config import Settings, SettingsManager


def answers(monkeypatch: pytest.MonkeyPatch, values: list[str]) -> list[str]:
    iterator = iter(values)
    prompts: list[str] = []

    def reply(prompt: str) -> str:
        prompts.append(prompt)
        return next(iterator)

    monkeypatch.setattr("builtins.input", reply)
    return prompts


def password_input(monkeypatch: pytest.MonkeyPatch, key: str = "") -> str:
    password = "private-initial-password"
    values = iter([password, password, key])
    monkeypatch.setattr("chiliarch.cli.getpass.getpass", lambda _: next(values))
    return password


def read_config(path: Path) -> dict[str, Any]:
    with path.open("rb") as stream:
        return tomllib.load(stream)


def test_tool_init_then_serve_from_another_directory_uses_same_configuration(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _, state = tool_environment(tmp_path, monkeypatch)
    monkeypatch.chdir(tmp_path)
    password = password_input(monkeypatch)
    answers(monkeypatch, [""] * 6)
    monkeypatch.setattr(sys, "argv", ["chiliarch", "init"])
    cli.main()
    config = state.parent / "server/chiliarch.toml"
    saved = read_config(config)
    assert saved["server"]["host"] == "127.0.0.1"
    assert saved["server"]["port"] == 8000
    assert saved["supervisor"]["mode"] == "auto"
    assert saved["logs"]["level"] == "INFO"
    assert verify_password(password, saved["auth"]["password_hash"])
    assert not (tmp_path / "chiliarch.toml").exists()
    output = capsys.readouterr().out
    assert str(config) in output
    assert password not in output

    elsewhere = tmp_path / "another cwd"
    elsewhere.mkdir()
    monkeypatch.chdir(elsewhere)
    run = Mock()
    monkeypatch.setattr("uvicorn.run", run)
    monkeypatch.setattr(sys, "argv", ["chiliarch", "serve"])
    cli.main()
    app = run.call_args.args[0]
    manager: SettingsManager = app.state.settings_manager
    assert manager.path == config
    assert manager.current.database.url == "sqlite+aiosqlite:///" + str(
        config.parent / "data/chiliarch.db"
    )
    assert manager.current.logs.directory == config.parent / "logs"
    assert run.call_args.kwargs["host"] == "127.0.0.1"
    assert run.call_args.kwargs["port"] == 8000


@pytest.mark.parametrize("mode", ["chat_completions", "responses", "anthropic"])
def test_wizard_persists_custom_network_model_and_policy_without_echoing_secrets(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    mode: str,
) -> None:
    path = tmp_path / "private directory" / "settings.toml"
    key = "secret-api-key-never-echo"
    password = password_input(monkeypatch, key)
    values = [
        "0.0.0.0",
        "9123",
        "https://chiliarch.example.test/",
        "ask",
        "warning",
        "yes",
        mode,
        "my-model-id",
        "https://models.example.test/v1/",
    ]
    if mode == "anthropic":
        values.extend(["", "0", "-1", "many", "12345"])
    values.append("64000")
    prompts = answers(monkeypatch, values)
    monkeypatch.setattr(sys, "argv", ["chiliarch", "init", "--config", str(path)])
    cli.main()
    saved = read_config(path)
    assert saved["server"]["host"] == "0.0.0.0"
    assert saved["server"]["port"] == 9123
    assert saved["server"]["public_url"] == "https://chiliarch.example.test"
    assert saved["auth"]["secure_cookie"] is True
    assert saved["supervisor"]["mode"] == "ask"
    assert saved["logs"]["level"] == "WARNING"
    assert saved["model"]["api_mode"] == mode
    assert saved["model"]["model"] == "my-model-id"
    assert saved["model"]["base_url"] == "https://models.example.test/v1"
    assert saved["model"]["api_key"] == key
    assert saved["model"]["context_window"] == 64000
    assert saved["model"].get("anthropic_max_tokens") == (12345 if mode == "anthropic" else None)
    assert any("max_tokens" in prompt for prompt in prompts) == (mode == "anthropic")
    assert saved["model"]["request_params"] == {}
    assert verify_password(password, saved["auth"]["password_hash"])
    output = capsys.readouterr().out
    assert str(path) in output
    assert password not in output
    assert key not in output


def test_invalid_wizard_entries_are_reprompted_before_saving(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "config.toml"
    password_input(monkeypatch)
    answers(
        monkeypatch,
        [
            "https://invalid-host",
            "localhost:8080",
            "[::]",
            "abc",
            "0",
            "65536",
            "8081",
            "https://site.test/path",
            "https://user:secret@site.test",
            "http://site.test:8081",
            "invalid-mode",
            "yolo",
            "invalid-level",
            "debug",
            "maybe",
            "n",
        ],
    )
    monkeypatch.setattr(sys, "argv", ["chiliarch", "--config", str(path), "init"])
    cli.main()
    saved = read_config(path)
    assert saved["server"]["host"] == "::"
    assert saved["server"]["port"] == 8081
    assert saved["server"]["public_url"] == "http://site.test:8081"
    assert saved["auth"]["secure_cookie"] is False
    assert saved["supervisor"]["mode"] == "yolo"
    assert saved["logs"]["level"] == "DEBUG"


@pytest.mark.parametrize("placement", ["before", "after"])
def test_explicit_config_overrides_tool_default_and_defaults_skips_setting_prompts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, placement: str
) -> None:
    _, state = tool_environment(tmp_path, monkeypatch)
    path = tmp_path / "explicit.toml"
    password_input(monkeypatch)
    monkeypatch.setattr("builtins.input", lambda _: pytest.fail("--defaults prompted for settings"))
    config_arg = ["--config", str(path)]
    command = ["init", "--defaults"]
    monkeypatch.setattr(
        sys,
        "argv",
        ["chiliarch", *(config_arg + command if placement == "before" else command + config_arg)],
    )
    cli.main()
    assert read_config(path)["server"]["port"] == 8000
    assert not (state.parent / "server/chiliarch.toml").exists()


def test_cancelled_forced_setup_keeps_existing_configuration(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "config.toml"
    previous = b'[supervisor]\nmode = "ask"\n'
    path.write_bytes(previous)
    password_input(monkeypatch)

    def end_input(_: str) -> str:
        raise EOFError

    monkeypatch.setattr("builtins.input", end_input)
    monkeypatch.setattr(sys, "argv", ["chiliarch", "init", "--force", "--config", str(path)])
    with pytest.raises(SystemExit):
        cli.main()
    assert path.read_bytes() == previous
    assert list(tmp_path.iterdir()) == [path]


@pytest.mark.parametrize(
    "passwords", [["short", "short"], ["first-password", "different-password"]]
)
def test_invalid_password_never_creates_configuration(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, passwords: list[str]
) -> None:
    path = tmp_path / "new-directory/config.toml"
    values = iter(passwords)
    monkeypatch.setattr("chiliarch.cli.getpass.getpass", lambda _: next(values))
    monkeypatch.setattr(
        "builtins.input", lambda _: pytest.fail("Invalid password reached settings")
    )
    monkeypatch.setattr(sys, "argv", ["chiliarch", "init", "--config", str(path)])
    with pytest.raises(SystemExit):
        cli.main()
    assert not path.parent.exists()


def test_concurrent_initializers_do_not_overwrite_without_force(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "shared" / "config.toml"
    ready = threading.Barrier(2, timeout=10)
    local = threading.local()
    monkeypatch.setattr("chiliarch.cli.getpass.getpass", lambda _: "simultaneous-password")

    def configure(settings: Settings) -> None:
        settings.server.port = local.port
        # Both initial existence checks have completed before either can save.
        ready.wait()

    def initialize(port: int) -> tuple[int, bool]:
        local.port = port
        try:
            cli.initialize(path, configure=True)
        except ValueError:
            return port, False
        return port, True

    monkeypatch.setattr("chiliarch.initialization.prompt_settings", configure)
    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(initialize, [8001, 8002]))
    winners = [port for port, saved in results if saved]
    assert len(winners) == 1
    assert read_config(path)["server"]["port"] == winners[0]
