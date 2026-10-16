"""Structured lifecycle logs with bounded rotation and secret redaction."""

from __future__ import annotations

import json
import logging
import os
import re
import sys
import traceback
from datetime import UTC, datetime
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Any

from filelock import FileLock

from chiliarch.config import LogSettings

_STANDARD = set(logging.makeLogRecord({}).__dict__) | {"message", "asctime"}
_SENSITIVE = re.compile(
    r"password|credential|authorization|api[_-]?key|secret|cookie|^(token|access_token|refresh_token)$",
    re.I,
)


def redact(value: Any, key: str = "") -> Any:
    if _SENSITIVE.search(key):
        return "[redacted]"
    if isinstance(value, dict):
        return {str(name): redact(item, str(name)) for name, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [redact(item) for item in value]
    return value


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "timestamp": datetime.fromtimestamp(record.created, UTC).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        payload.update(
            {
                key: redact(value, key)
                for key, value in record.__dict__.items()
                if key not in _STANDARD
            }
        )
        if record.exc_info:
            # Exception text from SDKs can contain upstream request bodies and secrets.
            payload["exception"] = (
                record.exc_info[0].__name__ if record.exc_info[0] else "Exception"
            )
            payload["traceback"] = [
                {"file": frame.filename, "line": frame.lineno, "function": frame.name}
                for frame in traceback.extract_tb(record.exc_info[2])
            ]
        return json.dumps(payload, ensure_ascii=False, default=str)


class _SharedRotatingFileHandler(RotatingFileHandler):
    """Serialize rotation/writes and follow files rotated by another process."""

    def __init__(self, filename: Path, *, max_bytes: int, backup_count: int) -> None:
        super().__init__(
            filename,
            maxBytes=max_bytes,
            backupCount=backup_count,
            encoding="utf-8",
            delay=True,
        )
        self._process_lock = FileLock(self.baseFilename + ".lock")

    def _reopen_if_needed(self) -> None:
        if self.stream is None:
            return
        descriptor = os.fstat(self.stream.fileno())
        try:
            current = os.stat(self.baseFilename)
            replaced = (descriptor.st_dev, descriptor.st_ino) != (
                current.st_dev,
                current.st_ino,
            )
        except FileNotFoundError:
            replaced = True
        if replaced:
            self.stream.flush()
            self.stream.close()
            self.stream = None
            self.stream = self._open()
        # Another writer may have appended without rotating. Refresh our own
        # descriptor position before the standard handler checks the size.
        self.stream.seek(0, os.SEEK_END)

    def emit(self, record: logging.LogRecord) -> None:
        try:
            with self._process_lock:
                self._reopen_if_needed()
                try:
                    # Both the rollover decision and the complete JSONL write
                    # are inside the same inter-process critical section.
                    super().emit(record)
                finally:
                    if os.name == "nt" and self.stream is not None:
                        # Windows cannot rename files another process holds open.
                        # Keep shared streams open only while holding the lock.
                        self.stream.close()
                        self.stream = None
        except Exception:
            self.handleError(record)


def configure_logging(settings: LogSettings, *, shared: bool = False) -> logging.Logger:
    settings.directory.mkdir(parents=True, exist_ok=True)
    logger = logging.getLogger("chiliarch")
    logger.setLevel(settings.level)
    logger.propagate = False
    close_logging()
    if shared:
        handler: RotatingFileHandler = _SharedRotatingFileHandler(
            settings.directory / "chiliarch.jsonl",
            max_bytes=settings.max_bytes,
            backup_count=settings.backup_count,
        )
    else:
        handler = RotatingFileHandler(
            settings.directory / "chiliarch.jsonl",
            maxBytes=settings.max_bytes,
            backupCount=settings.backup_count,
            encoding="utf-8",
        )
    handler.setFormatter(JsonFormatter())
    logger.addHandler(handler)
    # pythonw has no stderr. Managed background services keep their rotating
    # file log without attempting to write to a missing or inherited console.
    if sys.stderr is not None and os.environ.get("CHILIARCH_BACKGROUND") != "1":
        console = logging.StreamHandler()
        console.setFormatter(JsonFormatter())
        logger.addHandler(console)
    return logger


def close_logging() -> None:
    logger = logging.getLogger("chiliarch")
    for handler in list(logger.handlers):
        logger.removeHandler(handler)
        handler.close()


def tail_logs(settings: LogSettings, limit: int = 100) -> list[dict[str, Any]]:
    from collections import deque

    if limit < 1 or limit > 1000:
        raise ValueError("limit must be between 1 and 1000")
    path = settings.directory / "chiliarch.jsonl"
    if not path.exists():
        return []
    with path.open(encoding="utf-8") as stream:
        lines = deque(stream, maxlen=limit)
    result: list[dict[str, Any]] = []
    for line in lines:
        try:
            item = json.loads(line)
            if isinstance(item, dict):
                result.append(item)
        except json.JSONDecodeError:
            continue
    return result
