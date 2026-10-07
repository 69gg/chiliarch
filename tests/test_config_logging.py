from __future__ import annotations

import asyncio
import json
import logging
from pathlib import Path
from typing import Any

import pytest
from fastapi import FastAPI
from pydantic import ValidationError

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
    assert restarted.current.database.url.endswith("/next.db")


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
