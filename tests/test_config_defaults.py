from __future__ import annotations

import logging
import os
import tomllib
from pathlib import Path
from typing import Any

import pytest
import tomli_w
from pydantic import BaseModel, Field, ValidationError

from chiliarch import config
from chiliarch.config import (
    Settings,
    SettingsManager,
    atomic_write,
    atomic_write_toml,
    raw_settings,
)


def read_toml(path: Path) -> dict[str, Any]:
    return tomllib.loads(path.read_text(encoding="utf-8"))


@pytest.mark.parametrize("enabled", [None, False, True])
def test_loading_old_configuration_persists_missing_defaults_and_retains_user_values(
    tmp_path: Path, enabled: bool | None
) -> None:
    path = tmp_path / "chiliarch.toml"
    original: dict[str, Any] = {
        "server": {"host": "0.0.0.0", "port": 9123},
        "model": {
            "api_key": "private-model-key",
            "model": "custom-model",
            "max_tokens": 8192,
            "enable_thinking_param_enabled": False,
            "headers": {"Authorization": "Bearer private-header"},
            "request_params": {"max_tokens": 512, "metadata": {"custom": False}},
        },
        "supervisor": {"mode": "ask", "system_prompt": "My custom prompt", "max_tool_rounds": 9},
        "auth": {"password_hash": "existing-password-hash"},
    }
    if enabled is not None:
        original["github"] = {"enabled": enabled}
    path.write_text(tomli_w.dumps(original), encoding="utf-8")

    manager = SettingsManager(path)
    saved = read_toml(path)
    for section, values in original.items():
        for key, value in values.items():
            assert saved[section][key] == value
    assert saved["github"]["enabled"] is (True if enabled is None else enabled)
    assert (
        saved["server"]["graceful_shutdown_seconds"] == Settings().server.graceful_shutdown_seconds
    )
    assert saved["model"]["context_window"] == Settings().model.context_window
    assert "temperature" not in saved["model"]
    assert "anthropic_max_tokens" not in saved["model"]
    assert manager.current.model.api_key.get_secret_value() == "private-model-key"
    assert manager.current.model.request_params == original["model"]["request_params"]
    assert manager.current.supervisor.system_prompt == "My custom prompt"
    assert manager.revision == 1


def test_environment_overrides_are_validated_but_never_persisted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "chiliarch.toml"
    path.write_text('[server]\nport = 9000\n[model]\napi_key = "saved-key"\n', encoding="utf-8")
    monkeypatch.setenv("CHILIARCH_GITHUB__ENABLED", "false")
    monkeypatch.setenv("CHILIARCH_SERVER__PORT", "9123")
    monkeypatch.setenv("CHILIARCH_MODEL__API_KEY", "environment-only-key")
    monkeypatch.setenv("CHILIARCH_MODEL__REQUEST_PARAMS", '{"custom":"environment-only"}')
    manager = SettingsManager(path)
    assert not manager.current.github.enabled
    assert manager.current.server.port == 9123
    assert manager.current.model.api_key.get_secret_value() == "environment-only-key"
    assert manager.current.model.request_params == {"custom": "environment-only"}
    saved = read_toml(path)
    assert saved["github"]["enabled"] is True
    assert saved["server"]["port"] == 9000
    assert saved["model"]["api_key"] == "saved-key"
    assert saved["model"]["request_params"] == {}


def test_absent_configuration_is_not_initialized_while_loading_defaults(tmp_path: Path) -> None:
    path = tmp_path / "missing" / "chiliarch.toml"
    assert SettingsManager(path).current.github.enabled
    assert not path.exists()
    assert not path.parent.exists()


def test_complete_configuration_is_not_rewritten_and_keeps_comments(tmp_path: Path) -> None:
    path = tmp_path / "chiliarch.toml"
    atomic_write(path, Settings())
    original = b"# My configuration\n" + path.read_bytes()
    path.write_bytes(original)
    initial_mtime = path.stat().st_mtime_ns
    manager = SettingsManager(path)
    assert manager.reload()
    assert path.read_bytes() == original
    assert path.stat().st_mtime_ns == initial_mtime


def test_default_completion_is_idempotent(tmp_path: Path) -> None:
    path = tmp_path / "chiliarch.toml"
    path.write_text("[github]\nenabled = false\n", encoding="utf-8")
    first = SettingsManager(path)
    original = path.read_bytes()
    initial_mtime = path.stat().st_mtime_ns
    second = SettingsManager(path)
    assert first.snapshot() == second.snapshot()
    assert path.read_bytes() == original
    assert path.stat().st_mtime_ns == initial_mtime


def test_reload_fills_new_defaults_and_applies_saved_choices(tmp_path: Path) -> None:
    path = tmp_path / "chiliarch.toml"
    atomic_write(path, Settings())
    manager = SettingsManager(path)
    path.write_text('[github]\nenabled = false\n[model]\nmodel = "changed"\n', encoding="utf-8")
    assert manager.reload()
    assert not manager.current.github.enabled
    assert manager.current.model.model == "changed"
    saved = read_toml(path)
    assert saved["github"]["enabled"] is False
    assert saved["model"]["model"] == "changed"
    assert saved["transport"]["heartbeat_seconds"] == Settings().transport.heartbeat_seconds
    assert manager.revision == 2


@pytest.mark.parametrize(
    "source", [b"not valid TOML", b"[server]\nport = -1\n", b"[model]\nunknown_typo = true\n"]
)
def test_invalid_configuration_is_rejected_without_rewriting(tmp_path: Path, source: bytes) -> None:
    path = tmp_path / "chiliarch.toml"
    path.write_bytes(source)
    with pytest.raises(ValueError):
        SettingsManager(path)
    assert path.read_bytes() == source


def test_invalid_environment_prevents_default_persistence(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "chiliarch.toml"
    source = b"# Keep this configuration\n[github]\nenabled = false\n"
    path.write_bytes(source)
    monkeypatch.setenv("CHILIARCH_SERVER__PORT", "-1")
    with pytest.raises(ValidationError):
        SettingsManager(path)
    assert path.read_bytes() == source


@pytest.mark.parametrize("failure", ["readonly", "replace", "fsync"])
def test_persistence_failure_keeps_original_file_and_uses_runtime_defaults(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture, failure: str
) -> None:
    path = tmp_path / "chiliarch.toml"
    original = b'[model]\napi_key = "private-key"\n'
    path.write_bytes(original)

    def fail(*args: Any, **kwargs: Any) -> None:
        raise PermissionError("private diagnostic must not leak")

    if failure == "readonly":
        path.chmod(0o400)
    else:
        monkeypatch.setattr(config.os, failure, fail)
    try:
        with caplog.at_level(logging.WARNING, logger="chiliarch.config"):
            manager = SettingsManager(path)
        assert manager.current.github.enabled
        assert manager.current.model.api_key.get_secret_value() == "private-key"
        assert path.read_bytes() == original
        assert [item.name for item in tmp_path.iterdir()] == [path.name]
        warnings = [
            record
            for record in caplog.records
            if getattr(record, "event", "") == "config.defaults_deferred"
        ]
        assert warnings and warnings[-1].levelno == logging.WARNING
        assert "private diagnostic" not in warnings[-1].getMessage()
        assert "private-key" not in warnings[-1].getMessage()
    finally:
        path.chmod(0o600)


def test_concurrent_editor_change_is_reloaded_instead_of_overwritten(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    path = tmp_path / "chiliarch.toml"
    path.write_text("[github]\nenabled = true\n", encoding="utf-8")
    replacement = b'# New user choice\n[github]\nenabled = false\n[model]\nmodel = "new-choice"\n'
    original_fsync = os.fsync
    changed = False

    def concurrent_edit(descriptor: int) -> None:
        nonlocal changed
        original_fsync(descriptor)
        if not changed:
            changed = True
            path.write_bytes(replacement)

    monkeypatch.setattr(config.os, "fsync", concurrent_edit)
    with caplog.at_level(logging.WARNING, logger="chiliarch.config"):
        manager = SettingsManager(path)
    assert not manager.current.github.enabled
    assert manager.current.model.model == "new-choice"
    assert path.read_bytes() == replacement
    assert [item.name for item in tmp_path.iterdir()] == [path.name]
    assert any(
        getattr(record, "event", "") == "config.defaults_deferred" for record in caplog.records
    )


def test_concurrently_deleted_configuration_is_not_recreated(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "chiliarch.toml"
    path.write_text("[github]\nenabled = false\n", encoding="utf-8")
    original_fsync = os.fsync

    def concurrent_delete(descriptor: int) -> None:
        original_fsync(descriptor)
        path.unlink(missing_ok=True)

    monkeypatch.setattr(config.os, "fsync", concurrent_delete)
    assert SettingsManager(path).current.github.enabled
    assert not path.exists()
    assert list(tmp_path.iterdir()) == []


def test_default_completion_never_recurses_into_user_owned_maps() -> None:
    class Example(BaseModel):
        options: dict[str, Any] = Field(default_factory=lambda: {"provider_default": True})

    raw = {"options": {"explicit": False}}
    model = Example.model_validate(raw)
    assert config._missing_defaults(raw, model, model.model_dump()) == raw


def test_expected_content_check_preserves_a_newer_file(tmp_path: Path) -> None:
    path = tmp_path / "chiliarch.toml"
    original = b"[github]\nenabled = false\n"
    path.write_bytes(original)
    with pytest.raises(OSError):
        atomic_write_toml(path, raw_settings(Settings()), expected_content=b"older source")
    assert path.read_bytes() == original
    assert [item.name for item in tmp_path.iterdir()] == [path.name]
