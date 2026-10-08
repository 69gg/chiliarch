"""Consistent local SQLite snapshots, without stopping the platform or copying WAL files."""

from __future__ import annotations

import math
import os
import sqlite3
import tempfile
import time
from contextlib import closing
from pathlib import Path

from sqlalchemy.engine import make_url

from chiliarch.database import SCHEMA_VERSION


def sqlite_source(url: str) -> Path:
    """Resolve an explicit local file, without opening or creating a database."""
    parsed = make_url(url)
    if (
        parsed.get_backend_name() != "sqlite"
        or not parsed.database
        or parsed.database == ":memory:"
        or parsed.database.startswith("file:")
        or parsed.query
    ):
        raise ValueError("Backup requires a local SQLite file URL without URI options")
    source = Path(parsed.database).expanduser().resolve(strict=True)
    if not source.is_file():
        raise ValueError("Backup source must be an existing SQLite database file")
    return source


def backup_database(url: str, output: Path, *, timeout: float = 30) -> Path:
    """Publish a checked, standalone snapshot; an existing destination is never replaced."""
    if not math.isfinite(timeout) or timeout <= 0:
        raise ValueError("Backup timeout must be finite and greater than zero")
    source = sqlite_source(url)
    # Resolve the parent only: resolving the final component would follow a
    # destination symlink and could publish the backup in an unintended place.
    output = output.expanduser().absolute()
    output.parent.mkdir(parents=True, exist_ok=True)
    output = output.parent.resolve() / output.name
    if output.exists() or output.is_symlink():
        raise FileExistsError(f"Backup destination already exists: {output}")
    descriptor, temporary = tempfile.mkstemp(prefix=f".{output.name}.", dir=output.parent)
    os.close(descriptor)  # mkstemp creates a private file; Windows needs the handle closed.
    staging = Path(temporary)
    deadline = time.monotonic() + timeout

    def progress(status: int, remaining: int, total: int) -> None:
        if time.monotonic() > deadline:
            raise TimeoutError("SQLite backup timed out; no snapshot was published")

    try:
        with (
            closing(
                sqlite3.connect(source.as_uri() + "?mode=ro", uri=True, timeout=timeout)
            ) as src,
            closing(sqlite3.connect(staging)) as dst,
        ):
            src.backup(dst, pages=256, progress=progress, sleep=min(0.1, timeout))
            # WAL mode is copied with the header. Convert the snapshot to a
            # standalone file before verifying and publishing it.
            dst.execute("PRAGMA journal_mode=DELETE")
            dst.set_progress_handler(lambda: int(time.monotonic() > deadline), 1000)
            if dst.execute("PRAGMA quick_check").fetchall() != [("ok",)]:
                raise ValueError("SQLite snapshot failed its integrity check")
            if dst.execute("SELECT version FROM schema_version WHERE id=1").fetchone() != (
                SCHEMA_VERSION,
            ):
                raise ValueError("Unsupported Chiliarch schema in backup source")
        # Windows FlushFileBuffers requires a handle opened for writing.
        with staging.open("r+b") as stream:
            os.fsync(stream.fileno())
        # Linking within the same directory publishes atomically and refuses
        # to clobber even a destination created while the backup was running.
        os.link(staging, output)
    except sqlite3.Error as error:
        raise ValueError(
            f"SQLite backup failed ({type(error).__name__}); no snapshot published"
        ) from error
    finally:
        staging.unlink(missing_ok=True)
        for suffix in ("-journal", "-wal", "-shm"):
            Path(str(staging) + suffix).unlink(missing_ok=True)
    return output
