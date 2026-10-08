"""Application resources close even when startup or an earlier cleanup fails."""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi import FastAPI
from sqlalchemy import event, text

from chiliarch.api import create_app
from chiliarch.auth import hash_password
from chiliarch.config import Settings


def application(tmp_path: Path) -> FastAPI:
    return create_app(
        config=Settings.model_validate(
            {
                "database": {"url": f"sqlite+aiosqlite:///{tmp_path}/lifespan.db"},
                "logs": {"directory": str(tmp_path / "logs")},
                "auth": {"password_hash": hash_password("test-password")},
            }
        )
    )


async def test_failed_schema_initialization_closes_engine_without_stopping_unstarted_supervisor(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    app = application(tmp_path)
    database = app.state.database
    closed: list[object] = []

    def connection_closed(connection: object, record: object) -> None:
        closed.append(connection)

    event.listen(database.engine.sync_engine, "close", connection_closed)

    async def initialize() -> None:
        async with database.engine.begin() as connection:
            await connection.execute(text("SELECT 1"))
        raise RuntimeError("schema initialization failed")

    monkeypatch.setattr(database, "initialize", initialize)
    with pytest.raises(RuntimeError, match="schema initialization failed"):
        async with app.router.lifespan_context(app):
            pytest.fail("An incomplete schema must not serve requests")
    assert len(closed) == 1
    assert app.state.service._maintenance_task is None
    assert app.state.supervisor.sweeper is None


async def test_cleanup_error_still_closes_remaining_resources(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    app = application(tmp_path)
    closed: list[object] = []
    original_stop = app.state.service.stop

    def connection_closed(connection: object, record: object) -> None:
        closed.append(connection)

    event.listen(app.state.database.engine.sync_engine, "close", connection_closed)

    async def stop() -> None:
        await original_stop()
        raise RuntimeError("service cleanup failed")

    monkeypatch.setattr(app.state.service, "stop", stop)
    with pytest.raises(RuntimeError, match="service cleanup failed"):
        async with app.router.lifespan_context(app):
            maintenance = app.state.service._maintenance_task
            assert maintenance is not None and not maintenance.done()
    assert closed
    assert maintenance.done()
    assert app.state.supervisor.sweeper.done()
