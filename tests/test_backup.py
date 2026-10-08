from __future__ import annotations

import os
import sqlite3
from contextlib import closing
from pathlib import Path

import pytest
from pydantic import SecretStr

from chiliarch import cli
from chiliarch.backup import backup_database
from chiliarch.config import Settings, atomic_write


def populate(connection: sqlite3.Connection) -> None:
    connection.executescript(
        "PRAGMA journal_mode=WAL; PRAGMA wal_autocheckpoint=0;"
        "CREATE TABLE schema_version(id INTEGER PRIMARY KEY, version INTEGER);"
        "INSERT INTO schema_version VALUES(1, 1);"
        "CREATE TABLE work(id INTEGER PRIMARY KEY, value TEXT);"
        "INSERT INTO work VALUES(1, 'committed result');"
    )


def test_online_snapshot_includes_committed_wal_and_excludes_uncommitted_work(
    tmp_path: Path,
) -> None:
    source, target = tmp_path / "platform.db", tmp_path / "snapshot.db"
    with closing(sqlite3.connect(source)) as writer:
        populate(writer)
        assert Path(str(source) + "-wal").stat().st_size > 0
        writer.execute("INSERT INTO work VALUES(2, 'still running')")
        result = backup_database(f"sqlite+aiosqlite:///{source}", target)
        assert result == target
        with closing(sqlite3.connect(target)) as restored:
            assert restored.execute("SELECT * FROM work").fetchall() == [(1, "committed result")]
            assert restored.execute("PRAGMA quick_check").fetchone() == ("ok",)
            assert restored.execute("PRAGMA journal_mode").fetchone() == ("delete",)
        writer.commit()
    # A separately opened file is sufficient; source sidecars are never needed.
    with closing(sqlite3.connect(target)) as restored:
        assert restored.execute("SELECT COUNT(*) FROM work").fetchone() == (1,)
    assert not Path(str(target) + "-wal").exists()
    if os.name != "nt":
        assert target.stat().st_mode & 0o777 == 0o600


def test_backup_cli_honors_config_location_and_does_not_emit_secrets(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    config = tmp_path / "platform.toml"
    settings = Settings()
    settings.database.url = "sqlite+aiosqlite:///platform.db"
    settings.model.api_key = SecretStr("private-backup-test")
    atomic_write(config, settings)
    with closing(sqlite3.connect(tmp_path / "platform.db")) as source:
        populate(source)
    output = tmp_path / "archives" / "中文 snapshot.db"
    monkeypatch.setattr(
        "sys.argv", ["chiliarch", "backup", "--config", str(config), "--output", str(output)]
    )
    cli.main()
    with closing(sqlite3.connect(output)) as snapshot:
        assert snapshot.execute("SELECT * FROM work").fetchall() == [(1, "committed result")]
    assert "private-backup-test" not in capsys.readouterr().out


def test_existing_destination_is_never_replaced(tmp_path: Path) -> None:
    source, target = tmp_path / "platform.db", tmp_path / "snapshot.db"
    with closing(sqlite3.connect(source)) as connection:
        populate(connection)
    before = source.read_bytes()
    target.write_bytes(b"existing archive")
    with pytest.raises(FileExistsError):
        backup_database(f"sqlite:///{source}", target)
    with pytest.raises(FileExistsError):
        backup_database(f"sqlite:///{source}", source)
    assert target.read_bytes() == b"existing archive"
    assert source.read_bytes() == before


def test_explicit_database_overrides_saved_pending_configuration(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    source, saved, target = (
        tmp_path / "running.db",
        tmp_path / "pending.db",
        tmp_path / "snapshot.db",
    )
    for path, value in [(source, "active instance"), (saved, "not yet active")]:
        with closing(sqlite3.connect(path)) as connection:
            populate(connection)
            connection.execute("UPDATE work SET value=?", (value,))
            connection.commit()
    config = tmp_path / "platform.toml"
    settings = Settings()
    settings.database.url = f"sqlite+aiosqlite:///{saved}"
    atomic_write(config, settings)
    monkeypatch.setattr(
        "sys.argv",
        [
            "chiliarch",
            "--config",
            str(config),
            "backup",
            "--database",
            str(source),
            "--output",
            str(target),
        ],
    )
    cli.main()
    with closing(sqlite3.connect(target)) as restored:
        assert restored.execute("SELECT value FROM work").fetchone() == ("active instance",)
    assert str(source) in capsys.readouterr().out


def test_destination_created_during_snapshot_is_preserved(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source, target = tmp_path / "platform.db", tmp_path / "snapshot.db"
    with closing(sqlite3.connect(source)) as connection:
        populate(connection)
    original_link = os.link

    def raced_link(src: Path, dst: Path) -> None:
        dst.write_bytes(b"another snapshot")
        original_link(src, dst)

    monkeypatch.setattr("chiliarch.backup.os.link", raced_link)
    with pytest.raises(FileExistsError):
        backup_database(f"sqlite:///{source}", target)
    assert target.read_bytes() == b"another snapshot"
    assert list(tmp_path.glob(".snapshot.db.*")) == []


def test_snapshot_flush_uses_a_writable_descriptor(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source, target = tmp_path / "platform.db", tmp_path / "snapshot.db"
    with closing(sqlite3.connect(source)) as connection:
        populate(connection)
    fsync = os.fsync
    flushed: list[int] = []

    def require_writable(descriptor: int) -> None:
        # Exercise descriptor permissions on every OS, including POSIX where
        # fsync alone would incorrectly permit a read-only file handle.
        os.write(descriptor, b"")
        fsync(descriptor)
        flushed.append(descriptor)

    monkeypatch.setattr("chiliarch.backup.os.fsync", require_writable)
    backup_database(f"sqlite:///{source}", target)
    assert len(flushed) == 1
    with closing(sqlite3.connect(target)) as restored:
        assert restored.execute("SELECT * FROM work").fetchall() == [(1, "committed result")]


@pytest.mark.parametrize("kind", ["corrupt", "foreign", "unsupported"])
def test_invalid_source_never_publishes_partial_output(tmp_path: Path, kind: str) -> None:
    source, target = tmp_path / "platform.db", tmp_path / "snapshot.db"
    if kind == "corrupt":
        source.write_bytes(b"this is not a database")
    else:
        with closing(sqlite3.connect(source)) as connection:
            connection.execute("CREATE TABLE work(id INTEGER)")
            if kind == "unsupported":
                connection.executescript(
                    "CREATE TABLE schema_version(id INTEGER, version INTEGER);"
                    "INSERT INTO schema_version VALUES(1, 99);"
                )
    with pytest.raises(ValueError):
        backup_database(f"sqlite:///{source}", target)
    assert not target.exists()
    assert list(tmp_path.glob(".snapshot.db.*")) == []


def test_timed_out_snapshot_cleans_partial_output(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source, target = tmp_path / "platform.db", tmp_path / "snapshot.db"
    with closing(sqlite3.connect(source)) as connection:
        populate(connection)
    ticks = iter([0.0, 2.0])
    monkeypatch.setattr("chiliarch.backup.time.monotonic", lambda: next(ticks))
    with pytest.raises(TimeoutError):
        backup_database(f"sqlite:///{source}", target, timeout=1)
    assert not target.exists()
    assert list(tmp_path.glob(".snapshot.db.*")) == []


@pytest.mark.parametrize(
    "url", ["sqlite:///:memory:", "sqlite:///file:db?uri=true", "postgresql://localhost/platform"]
)
def test_unsupported_database_does_not_create_output(tmp_path: Path, url: str) -> None:
    target = tmp_path / "snapshot.db"
    with pytest.raises(ValueError):
        backup_database(url, target)
    assert not target.exists()


def test_missing_source_is_not_created(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError):
        backup_database(f"sqlite:///{tmp_path / 'missing.db'}", tmp_path / "snapshot.db")
    assert list(tmp_path.iterdir()) == []
