"""Async persistence and the initial, versioned database schema."""

from __future__ import annotations

import asyncio
import time
from pathlib import Path
from typing import Any
from uuid import uuid4

from sqlalchemy import (
    JSON,
    Boolean,
    Float,
    Integer,
    String,
    Text,
    UniqueConstraint,
    event,
    inspect,
    select,
)
from sqlalchemy.engine import URL, make_url
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

JsonObject = dict[str, Any]


def new_id() -> str:
    return str(uuid4())


class Base(DeclarativeBase):
    pass


class SchemaVersion(Base):
    __tablename__ = "schema_version"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, default=1)
    version: Mapped[int] = mapped_column(Integer)


class Agent(Base):
    __tablename__ = "agents"
    id: Mapped[str] = mapped_column(String(128), primary_key=True, default=new_id)
    name: Mapped[str] = mapped_column(String(256))
    machine: Mapped[str] = mapped_column(String(256))
    capabilities: Mapped[list[str]] = mapped_column(JSON, default=list)
    credential_hash: Mapped[str] = mapped_column(String(64), unique=True)
    revoked: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[float] = mapped_column(Float, default=time.time)
    last_seen: Mapped[float | None] = mapped_column(Float, nullable=True)
    metadata_json: Mapped[JsonObject] = mapped_column(JSON, default=dict)


class PairingCode(Base):
    __tablename__ = "pairing_codes"
    id: Mapped[str] = mapped_column(String(128), primary_key=True, default=new_id)
    code_hash: Mapped[str] = mapped_column(String(64), unique=True)
    created_at: Mapped[float] = mapped_column(Float, default=time.time)
    expires_at: Mapped[float] = mapped_column(Float)
    redeemed_at: Mapped[float | None] = mapped_column(Float, nullable=True)


class UserSession(Base):
    __tablename__ = "user_sessions"
    token_hash: Mapped[str] = mapped_column(String(64), primary_key=True)
    csrf_token: Mapped[str] = mapped_column(String(128))
    expires_at: Mapped[float] = mapped_column(Float)
    created_at: Mapped[float] = mapped_column(Float, default=time.time)


class Task(Base):
    __tablename__ = "tasks"
    id: Mapped[str] = mapped_column(String(128), primary_key=True, default=new_id)
    agent_id: Mapped[str] = mapped_column(String(128), index=True)
    instruction: Mapped[str] = mapped_column(Text)
    parameters: Mapped[JsonObject] = mapped_column(JSON, default=dict)
    status: Mapped[str] = mapped_column(String(32), default="queued")
    progress: Mapped[float | None] = mapped_column(Float, nullable=True)
    result: Mapped[Any] = mapped_column(JSON, nullable=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    message: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[float] = mapped_column(Float, default=time.time)
    updated_at: Mapped[float] = mapped_column(Float, default=time.time)
    requested_by: Mapped[str] = mapped_column(String(256))


class Message(Base):
    __tablename__ = "messages"
    __table_args__ = (UniqueConstraint("agent_id", "direction", "message_id"),)
    id: Mapped[str] = mapped_column(String(128), primary_key=True, default=new_id)
    agent_id: Mapped[str] = mapped_column(String(128), index=True)
    direction: Mapped[str] = mapped_column(String(16))
    message_id: Mapped[str] = mapped_column(String(128))
    body: Mapped[JsonObject] = mapped_column(JSON)
    body_hash: Mapped[str] = mapped_column(String(64))
    acked: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[float] = mapped_column(Float, default=time.time)
    expires_at: Mapped[float | None] = mapped_column(Float, nullable=True)
    expired: Mapped[bool] = mapped_column(Boolean, default=False)


class AgentRequest(Base):
    __tablename__ = "agent_requests"
    __table_args__ = (UniqueConstraint("agent_id", "external_id"),)
    id: Mapped[str] = mapped_column(String(128), primary_key=True, default=new_id)
    agent_id: Mapped[str] = mapped_column(String(128), index=True)
    external_id: Mapped[str] = mapped_column(String(128))
    operation: Mapped[str] = mapped_column(String(128))
    arguments: Mapped[JsonObject] = mapped_column(JSON, default=dict)
    message: Mapped[str | None] = mapped_column(Text, nullable=True)
    status: Mapped[str] = mapped_column(String(32), default="pending")
    result: Mapped[Any] = mapped_column(JSON, nullable=True)
    created_at: Mapped[float] = mapped_column(Float, default=time.time)
    resolved_at: Mapped[float | None] = mapped_column(Float, nullable=True)


class Approval(Base):
    __tablename__ = "approvals"
    id: Mapped[str] = mapped_column(String(128), primary_key=True, default=new_id)
    operation: Mapped[str] = mapped_column(String(128))
    arguments: Mapped[JsonObject] = mapped_column(JSON)
    requested_by: Mapped[str] = mapped_column(String(256))
    status: Mapped[str] = mapped_column(String(32), default="pending")
    reason: Mapped[str] = mapped_column(Text)
    created_at: Mapped[float] = mapped_column(Float, default=time.time)
    resolved_at: Mapped[float | None] = mapped_column(Float, nullable=True)
    result: Mapped[Any] = mapped_column(JSON, nullable=True)
    operation_record_id: Mapped[str] = mapped_column(String(128), unique=True)


class OperationRecord(Base):
    __tablename__ = "operation_records"
    __table_args__ = (UniqueConstraint("actor", "idempotency_key"),)
    id: Mapped[str] = mapped_column(String(128), primary_key=True, default=new_id)
    actor: Mapped[str] = mapped_column(String(256))
    idempotency_key: Mapped[str | None] = mapped_column(String(256), nullable=True)
    name: Mapped[str] = mapped_column(String(128))
    arguments_hash: Mapped[str] = mapped_column(String(64))
    status: Mapped[str] = mapped_column(String(32), default="running")
    result: Mapped[JsonObject] = mapped_column(JSON, default=dict)
    created_at: Mapped[float] = mapped_column(Float, default=time.time)


class AuditEvent(Base):
    __tablename__ = "audit_events"
    id: Mapped[str] = mapped_column(String(128), primary_key=True, default=new_id)
    timestamp: Mapped[float] = mapped_column(Float, default=time.time, index=True)
    actor: Mapped[str] = mapped_column(String(256))
    action: Mapped[str] = mapped_column(String(128))
    details: Mapped[JsonObject] = mapped_column(JSON, default=dict)


class Database:
    """A single service process owns mutations; SQLite uses durable WAL transactions."""

    def __init__(self, url: str | URL) -> None:
        parsed = make_url(url) if isinstance(url, str) else url
        if parsed.get_backend_name() == "sqlite" and parsed.drivername == "sqlite":
            parsed = parsed.set(drivername="sqlite+aiosqlite")
        if (
            parsed.get_backend_name() == "sqlite"
            and parsed.database
            and parsed.database != ":memory:"
            and not parsed.database.startswith("file:")
        ):
            Path(parsed.database).parent.mkdir(parents=True, exist_ok=True)
        self.sqlite_path = (
            Path(parsed.database)
            if parsed.get_backend_name() == "sqlite"
            and parsed.database
            and parsed.database != ":memory:"
            and not parsed.database.startswith("file:")
            else None
        )
        self.engine = create_async_engine(parsed)
        self.sessions = async_sessionmaker(self.engine, expire_on_commit=False)
        self.write_lock = asyncio.Lock()
        if parsed.get_backend_name() == "sqlite":

            @event.listens_for(self.engine.sync_engine, "connect")
            def configure_sqlite(connection: Any, _: Any) -> None:
                cursor = connection.cursor()
                cursor.execute("PRAGMA journal_mode=WAL")
                cursor.execute("PRAGMA foreign_keys=ON")
                cursor.execute("PRAGMA busy_timeout=10000")
                cursor.close()

    async def initialize(self) -> None:
        async with self.engine.begin() as connection:
            exists = await connection.run_sync(
                lambda sync: inspect(sync).has_table("schema_version")
            )
            if exists:
                version_number = (
                    await connection.execute(
                        select(SchemaVersion.version).where(SchemaVersion.id == 1)
                    )
                ).scalar_one_or_none()
                if version_number is not None and version_number != 1:
                    raise RuntimeError(
                        f"Unsupported database schema version {version_number}; expected 1"
                    )
            await connection.run_sync(Base.metadata.create_all)
        async with self.sessions.begin() as session:
            version = await session.get(SchemaVersion, 1)
            if version is None:
                session.add(SchemaVersion(id=1, version=1))
            elif version.version != 1:
                raise RuntimeError(
                    f"Unsupported database schema version {version.version}; expected 1"
                )

        if self.sqlite_path is not None:
            for path in (
                self.sqlite_path,
                Path(str(self.sqlite_path) + "-wal"),
                Path(str(self.sqlite_path) + "-shm"),
            ):
                if path.exists():
                    path.chmod(0o600)

    async def close(self) -> None:
        await self.engine.dispose()

    async def audit(self, actor: str, action: str, details: JsonObject) -> None:
        async with self.sessions.begin() as session:
            session.add(AuditEvent(actor=actor, action=action, details=details))

    async def schema_version(self) -> int:
        async with self.sessions() as session:
            return int((await session.execute(select(SchemaVersion.version))).scalar_one())
