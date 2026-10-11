from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import event, inspect, select
from sqlalchemy.engine import Connection

from chiliarch.database import SCHEMA_VERSION, AuditEvent, Database, SchemaVersion


async def test_schema_creation_uses_one_real_sqlite_transaction(tmp_path: Path) -> None:
    database = Database(f"sqlite+aiosqlite:///{tmp_path / 'state.db'}")
    transactions: list[bool] = []

    def observe_ddl(
        connection: Connection,
        cursor: Any,
        statement: str,
        parameters: Any,
        context: Any,
        executemany: bool,
    ) -> None:
        if context.isddl:
            transactions.append(connection.connection.driver_connection.in_transaction)

    event.listen(database.engine.sync_engine, "before_cursor_execute", observe_ddl)
    try:
        await database.initialize()
        assert len(transactions) > 1
        assert all(transactions)
        assert await database.schema_version() == SCHEMA_VERSION
        async with database.sessions.begin() as session:
            session.add(AuditEvent(actor="user", action="retained", details={"value": 7}))
        await database.initialize()
        async with database.sessions() as session:
            assert (await session.scalars(select(SchemaVersion))).one().version == SCHEMA_VERSION
            assert (await session.scalars(select(AuditEvent))).one().details == {"value": 7}
    finally:
        await database.close()


@pytest.mark.parametrize("failure", ["ddl", "version"])
async def test_failed_initialization_rolls_back_schema_and_can_retry(
    tmp_path: Path, failure: str
) -> None:
    url = f"sqlite+aiosqlite:///{tmp_path / 'state.db'}"
    database = Database(url)
    ddl_count = 0

    def fail_initialization(
        connection: Connection,
        cursor: Any,
        statement: str,
        parameters: Any,
        context: Any,
        executemany: bool,
    ) -> None:
        nonlocal ddl_count
        if context.isddl:
            ddl_count += 1
        if (failure == "ddl" and context.isddl and ddl_count == 2) or (
            failure == "version"
            and context.isinsert
            and context.compiled.statement.table.name == SchemaVersion.__tablename__
        ):
            raise RuntimeError("Injected initialization failure")

    event.listen(database.engine.sync_engine, "before_cursor_execute", fail_initialization)
    try:
        with pytest.raises(RuntimeError, match="Injected initialization failure"):
            await database.initialize()
        assert ddl_count > 0
    finally:
        await database.close()

    reopened = Database(url)
    try:
        async with reopened.engine.connect() as connection:
            tables = await connection.run_sync(lambda sync: inspect(sync).get_table_names())
        assert tables == []
        await reopened.initialize()
        assert await reopened.schema_version() == SCHEMA_VERSION
    finally:
        await reopened.close()


async def test_concurrent_wal_initializers_preserve_one_complete_schema(tmp_path: Path) -> None:
    url = f"sqlite+aiosqlite:///{tmp_path / 'state.db'}"
    databases = [Database(url), Database(url)]
    try:
        # Establish WAL before racing schema initialization. Changing SQLite's
        # journal mode itself requires an exclusive lock and is not a schema
        # transaction; production runs one server process per database.
        async with databases[0].engine.connect():
            pass
        await asyncio.gather(*(database.initialize() for database in databases))
        for database in databases:
            assert await database.schema_version() == SCHEMA_VERSION
            async with database.sessions() as session:
                assert len((await session.scalars(select(SchemaVersion))).all()) == 1
    finally:
        await asyncio.gather(*(database.close() for database in databases))
