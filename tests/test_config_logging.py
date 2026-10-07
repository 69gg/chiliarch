from __future__ import annotations

import asyncio
import json
import logging
import os
import stat
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from fastapi import FastAPI
from pydantic import ValidationError

from chiliarch import config
from chiliarch.config import MASK, LogSettings, Settings, SettingsManager, atomic_write
from chiliarch.logging import close_logging, configure_logging, tail_logs


def test_update_is_validated_atomic_and_keeps_masked_secrets(tmp_path: Path) -> None:
    path = tmp_path / "config.toml"
    manager = SettingsManager(path)
    manager.update(
        {
            "model": {
                "model": "configured",
                "api_key": "test-secret",
                "headers": {"X-Key": "header-secret"},
            }
        }
    )
    previous = path.read_bytes()
    with pytest.raises(ValidationError):
        manager.update({"transport": {"heartbeat_seconds": -1}})
    assert path.read_bytes() == previous
    assert manager.revision == 2
    manager.update({"model": {"api_key": MASK, "headers": {"X-Key": MASK}}})
    assert manager.current.model.api_key.get_secret_value() == "test-secret"
    assert manager.current.model.headers["X-Key"] == "header-secret"
    assert manager.public()["model"]["api_key"] == MASK
    assert manager.snapshot().model is not manager.current.model


def test_restart_fields_are_saved_but_not_live(tmp_path: Path) -> None:
    manager = SettingsManager(tmp_path / "config.toml")
    old = manager.current.database.url
    manager.update({"server": {"port": 12345}, "database": {"url": "sqlite+aiosqlite:///next.db"}})
    assert manager.current.server.port == 8000
    assert manager.current.database.url == old
    assert set(manager.pending_restart) == {"server.port", "database.url"}
    restarted = SettingsManager(manager.path)
    assert restarted.current.server.port == 12345
    assert (
        Path(restarted.current.database.url.removeprefix("sqlite+aiosqlite:///")).name == "next.db"
    )


def test_windows_atomic_update_flushes_file_without_opening_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "config.toml"
    manager = SettingsManager(path)
    original_open = os.open
    original_fsync = os.fsync
    synchronized: list[int] = []

    def open_descriptor(
        filename: str | bytes | os.PathLike[str] | os.PathLike[bytes],
        flags: int,
        mode: int = 0o777,
        *,
        dir_fd: int | None = None,
    ) -> int:
        if Path(os.fsdecode(filename)).is_dir():
            raise PermissionError("Windows cannot open a directory as an ordinary file")
        return original_open(filename, flags, mode, dir_fd=dir_fd)

    def sync_descriptor(descriptor: int) -> None:
        synchronized.append(os.fstat(descriptor).st_mode)
        original_fsync(descriptor)

    monkeypatch.setattr(config, "sys", SimpleNamespace(platform="win32"))
    monkeypatch.setattr(os, "open", open_descriptor)
    monkeypatch.setattr(os, "fsync", sync_descriptor)
    manager.update({"supervisor": {"mode": "auto"}})

    assert manager.current.supervisor.mode == "auto"
    assert manager.revision == 2
    assert SettingsManager(path).current.supervisor.mode == "auto"
    assert len(synchronized) == 1 and stat.S_ISREG(synchronized[0])
    assert sorted(item.name for item in tmp_path.iterdir()) == ["config.toml"]


@pytest.mark.skipif(sys.platform == "win32", reason="Windows does not support directory fsync")
def test_atomic_update_flushes_parent_directory_on_posix(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    original_fsync = os.fsync
    synchronized: list[int] = []

    def sync_descriptor(descriptor: int) -> None:
        synchronized.append(os.fstat(descriptor).st_mode)
        original_fsync(descriptor)

    monkeypatch.setattr(os, "fsync", sync_descriptor)
    manager = SettingsManager(tmp_path / "config.toml")
    manager.update({"supervisor": {"mode": "auto"}})
    assert len(synchronized) == 2
    assert stat.S_ISREG(synchronized[0])
    assert stat.S_ISDIR(synchronized[1])


def test_atomic_update_fsync_failure_preserves_file_and_runtime(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "config.toml"
    manager = SettingsManager(path)
    manager.update({"supervisor": {"mode": "ask"}})
    previous = path.read_bytes()
    revision = manager.revision

    def fail_sync(descriptor: int) -> None:
        raise OSError("File flush failed")

    monkeypatch.setattr(os, "fsync", fail_sync)
    with pytest.raises(OSError, match="File flush failed"):
        manager.update({"supervisor": {"mode": "auto"}})
    assert path.read_bytes() == previous
    assert manager.current.supervisor.mode == "ask"
    assert manager.revision == revision
    assert sorted(item.name for item in tmp_path.iterdir()) == ["config.toml"]


def test_old_execution_limits_are_retired_and_context_window_hot_reloads(tmp_path: Path) -> None:
    path = tmp_path / "config.toml"
    path.write_text(
        "[model]\nmax_tokens = 4096\n"
        "[supervisor]\nmax_tool_rounds = 20\nmax_context_messages = 200\nmax_active_runs = 8\n",
        encoding="utf-8",
    )
    manager = SettingsManager(path)
    snapshot = manager.snapshot()
    assert manager.current.model.context_window == 200000
    assert "max_tokens" not in manager.public()["model"]
    assert not {"max_tool_rounds", "max_context_messages", "max_active_runs"}.intersection(
        manager.public()["supervisor"]
    )
    manager.update({"model": {"context_window": 128000}})
    assert manager.current.model.context_window == 128000
    assert snapshot.model.context_window == 200000
    assert manager.pending_restart == []
    assert SettingsManager(path).current.model.context_window == 128000
    saved = path.read_bytes()
    with pytest.raises(ValidationError):
        manager.update({"model": {"context_window": 0}})
    assert path.read_bytes() == saved
    with pytest.raises(ValidationError):
        manager.update({"supervisor": {"typo_limit": 10}})


def test_environment_secret_overrides_are_not_persisted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("CHILIARCH_MODEL__API_KEY", "environment-key")
    path = tmp_path / "config.toml"
    manager = SettingsManager(path)
    manager.update({"model": {"model": "example"}})
    assert manager.current.model.api_key.get_secret_value() == "environment-key"
    monkeypatch.delenv("CHILIARCH_MODEL__API_KEY")
    assert SettingsManager(path).current.model.api_key.get_secret_value() == ""


def test_cli_listen_override_and_environment_secret_stay_out_of_saved_config(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from chiliarch.auth import hash_password
    from chiliarch.cli import main

    path = tmp_path / "config.toml"
    settings = Settings()
    settings.auth.password_hash = hash_password("cli-test-password")
    atomic_write(path, settings)
    monkeypatch.setenv("CHILIARCH_MODEL__API_KEY", "environment-only")
    monkeypatch.setattr("sys.argv", ["chiliarch", "--config", str(path), "serve", "--port", "9123"])

    def serve(application: FastAPI, **options: Any) -> None:
        assert options["port"] == 9123
        manager: SettingsManager = application.state.settings_manager
        assert manager.current.server.port == 9123
        manager.update({"supervisor": {"mode": "auto"}})
        assert manager.current.server.port == 9123
        assert manager.saved.server.port == 8000

    monkeypatch.setattr("uvicorn.run", serve)
    main()
    monkeypatch.delenv("CHILIARCH_MODEL__API_KEY")
    restarted = SettingsManager(path)
    assert restarted.current.server.port == 8000
    assert restarted.current.model.api_key.get_secret_value() == ""
    assert restarted.current.supervisor.mode == "auto"


async def test_file_watcher_rejects_bad_config_and_applies_valid_file(tmp_path: Path) -> None:
    path = tmp_path / "config.toml"
    settings = Settings.model_validate({"server": {"config_poll_seconds": 0.02}})
    atomic_write(path, settings)
    manager = SettingsManager(path)
    await manager.start()
    try:
        path.write_text("not valid TOML", encoding="utf-8")
        async with asyncio.timeout(2):
            for _ in range(200):
                if manager.last_error is not None:
                    break
                await asyncio.sleep(0.01)
        assert manager.revision == 1
        settings.supervisor.mode = "auto"
        atomic_write(path, settings)
        async with asyncio.timeout(2):
            for _ in range(200):
                if manager.current.supervisor.mode == "auto":
                    break
                await asyncio.sleep(0.01)
        assert manager.last_error is None
        assert manager.revision == 2
    finally:
        await manager.stop()


def test_actual_log_rotation_json_and_redaction(tmp_path: Path) -> None:
    settings = LogSettings(directory=tmp_path, max_bytes=1024, backup_count=2)
    logger = configure_logging(settings)
    try:
        for index in range(35):
            logger.info(
                "task event",
                extra={
                    "event": "task.status",
                    "index": index,
                    "api_key": "never-log",
                    "usage": {"input_tokens": 123},
                    "arguments": {"credential": "never-log", "task_id": "task-1"},
                },
            )
        for handler in logger.handlers:
            handler.flush()
        files = list(tmp_path.glob("chiliarch.jsonl*"))
        assert len(files) == 3
        events = [json.loads(line) for path in files for line in path.read_text().splitlines()]
        assert events and all(event["api_key"] == "[redacted]" for event in events)
        assert all(event["arguments"]["credential"] == "[redacted]" for event in events)
        assert all(event["usage"]["input_tokens"] == 123 for event in events)
        assert tail_logs(settings)[-1]["index"] == 34
        assert logging.getLogger("chiliarch.child").getEffectiveLevel() == logging.INFO
    finally:
        close_logging()


def test_exception_log_retains_diagnostic_frames_without_exception_body(tmp_path: Path) -> None:
    settings = LogSettings(directory=tmp_path)
    logger = configure_logging(settings)
    try:
        try:
            raise ValueError("upstream response containing a private credential")
        except ValueError:
            logger.exception("Upstream failed", extra={"event": "model.failed"})
        entry = tail_logs(settings)[-1]
        assert entry["exception"] == "ValueError"
        assert entry["traceback"][-1]["function"] == (
            "test_exception_log_retains_diagnostic_frames_without_exception_body"
        )
        assert entry["traceback"][-1]["line"] > 0
        assert set(entry["traceback"][-1]) == {"file", "line", "function"}
        assert "credential" not in entry
        assert set(entry) == {
            "timestamp",
            "level",
            "logger",
            "message",
            "event",
            "exception",
            "traceback",
        }
    finally:
        close_logging()
