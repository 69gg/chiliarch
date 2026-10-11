"""Immutable, bounded artifact transfer over the existing outbound request protocol."""

from __future__ import annotations

import base64
import binascii
import hashlib
import time
from typing import TYPE_CHECKING, Any

from pydantic import BaseModel, ConfigDict, Field, field_validator
from sqlalchemy import JSON, Float, ForeignKey, Integer, LargeBinary, String, delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Mapped, mapped_column

from chiliarch.database import Base, new_id
from chiliarch.protocol import Envelope

if TYPE_CHECKING:
    from chiliarch.service import OperationService

JsonObject = dict[str, Any]


class Artifact(Base):
    __tablename__ = "shared_artifacts"
    id: Mapped[str] = mapped_column(String(128), primary_key=True, default=new_id)
    name: Mapped[str] = mapped_column(String(255))
    size: Mapped[int] = mapped_column(Integer)
    sha256: Mapped[str] = mapped_column(String(64))
    chunk_bytes: Mapped[int] = mapped_column(Integer)
    owner: Mapped[str] = mapped_column(String(256))
    members: Mapped[list[str]] = mapped_column(JSON)
    goal_id: Mapped[str | None] = mapped_column(ForeignKey("coordination_goals.id"), nullable=True)
    task_id: Mapped[str | None] = mapped_column(ForeignKey("tasks.id"), nullable=True)
    state: Mapped[str] = mapped_column(String(32), default="uploading")
    created_at: Mapped[float] = mapped_column(Float, default=time.time)


class ArtifactChunk(Base):
    __tablename__ = "shared_artifact_chunks"
    artifact_id: Mapped[str] = mapped_column(ForeignKey(Artifact.id), primary_key=True)
    index: Mapped[int] = mapped_column(Integer, primary_key=True)
    data: Mapped[bytes] = mapped_column(LargeBinary)


class Arguments(BaseModel):
    model_config = ConfigDict(extra="forbid")


class BeginUpload(Arguments):
    name: str = Field(
        min_length=1,
        max_length=255,
        description="Plain filename without directory separators; this is a display name, not a local path.",
    )
    size: int = Field(
        ge=0,
        description="Exact total file size in bytes, measured before requesting upload authorization.",
    )
    sha256: str = Field(
        pattern=r"^[a-f0-9]{64}$",
        description="Lowercase SHA-256 hex digest of the complete file; do not invent a digest.",
    )
    members: list[str] = Field(
        default_factory=list,
        max_length=100,
        description="Registered agent IDs permitted to read the finished artifact, in addition to its owner.",
    )
    goal_id: str | None = Field(
        default=None, description="Optional existing goal ID associated with this file."
    )
    task_id: str | None = Field(
        default=None,
        description="Optional producing task ID; managed agents must use their bound task ID.",
    )

    @field_validator("name")
    @classmethod
    def filename_only(cls, name: str) -> str:
        if not name.strip() or name in {".", ".."} or any(c in name for c in "/\\\0"):
            raise ValueError("Artifact name must be a filename, not a path")
        return name


class UploadId(Arguments):
    upload_id: str = Field(
        min_length=1,
        max_length=128,
        description="Upload ID returned by begin_artifact_upload, representing its immutable authorized manifest.",
    )


class PutChunk(UploadId):
    index: int = Field(
        ge=0,
        description="Zero-based chunk index using the manifest's chunk_bytes size; the last chunk may be shorter.",
    )
    data_base64: str = Field(
        description="Base64-encoded exact chunk bytes; repeated indexes must contain identical bytes. Local transfer only, never model-context file content."
    )


class ArtifactId(Arguments):
    artifact_id: str = Field(
        min_length=1,
        max_length=128,
        description="Artifact ID from list_artifacts/get_artifact, or the ID suffix of a chiliarch:artifact: URI; not a filesystem path.",
    )


class ReadChunk(ArtifactId):
    index: int = Field(
        ge=0,
        description="Zero-based chunk index in a ready artifact; verify the returned chunk digest and final complete-file digest.",
    )


class ListArtifacts(Arguments):
    goal_id: str | None = Field(
        default=None,
        description="Filter by an existing goal ID; omit to include other accessible goals.",
    )
    task_id: str | None = Field(
        default=None,
        description="Filter by the producing task ID; omit to include other accessible tasks.",
    )
    after: int = Field(
        default=0,
        ge=0,
        description="Pagination offset; pass the prior returned cursor to continue, or 0 for the first page.",
    )
    limit: int = Field(
        default=100, ge=1, le=200, description="Maximum artifact metadata entries in this page."
    )


SPECS: list[tuple[str, str, type[BaseModel], str, bool]] = [
    (
        "begin_artifact_upload",
        "Reserve an immutable file upload using its exact size/hash and permitted agent members. Returns upload_id and artifact.chunk_bytes; allocation is not publication. Only the caller owning this upload can supply/finalize bytes. The supervisor should normally ask an agent to use its local upload_artifact tool instead, then inspect the returned ready metadata.",
        BeginUpload,
        "low",
        True,
    ),
    (
        "put_artifact_chunk",
        "Local transfer only: supply one chunk under the previously authorized immutable upload manifest. Index and bytes must match the declared size; repeated identical chunks are idempotent. Finish after all chunks are stored.",
        PutChunk,
        "low",
        True,
    ),
    (
        "finish_artifact_upload",
        "Finalize an upload owned by the caller after all chunks are stored. Verifies exact total size and SHA-256 before publishing state=ready to members; missing or mismatched bytes are rejected. Use the local uploader for byte transfer.",
        UploadId,
        "low",
        True,
    ),
    (
        "list_artifacts",
        "List accessible file metadata and configured transfer limits without loading contents into model context. Paginate with cursor; only state=ready files can be downloaded. Share artifact IDs/URIs with permitted agents.",
        ListArtifacts,
        "low",
        False,
    ),
    (
        "get_artifact",
        "Inspect a shared file's size, SHA-256, members and state; upload owners also see stored chunk indexes. Metadata is not file content; ask a permitted agent to use its local download_artifact tool when inspection is needed.",
        ArtifactId,
        "low",
        False,
    ),
    (
        "read_artifact_chunk",
        "Local transfer only: read a chunk from a ready accessible artifact; returns base64 bytes and a chunk SHA-256. Verify chunks and the complete manifest before using the local file; do not place bytes in model context.",
        ReadChunk,
        "low",
        False,
    ),
    (
        "discard_artifact_upload",
        "Remove an unfinished upload and its stored chunks to release reserved quota. Ready artifacts require delete_artifact; agent callers must own the upload.",
        UploadId,
        "low",
        True,
    ),
    (
        "delete_artifact",
        "Permanently delete a shared artifact's platform bytes and metadata, revoking future downloads for members. Previously downloaded local copies remain; this cannot undo their use.",
        ArtifactId,
        "high",
        True,
    ),
]
OPERATIONS = frozenset(spec[0] for spec in SPECS)
CONTINUATION_OPERATIONS = frozenset({"put_artifact_chunk", "finish_artifact_upload"})
BINARY_OPERATIONS = frozenset({"put_artifact_chunk", "read_artifact_chunk"})
INTERNAL_OPERATIONS = BINARY_OPERATIONS


def artifact_view(artifact: Artifact) -> JsonObject:
    result = {
        key: getattr(artifact, key)
        for key in (
            "id",
            "name",
            "size",
            "sha256",
            "chunk_bytes",
            "owner",
            "members",
            "goal_id",
            "task_id",
            "state",
            "created_at",
        )
    }
    result["uri"] = "chiliarch:artifact:" + artifact.id
    return result


class ArtifactService:
    def __init__(self, service: OperationService) -> None:
        self.service = service

    async def _artifact(self, session: AsyncSession, artifact_id: str, actor: str) -> Artifact:
        from chiliarch.service import OperationError

        artifact = await session.get(Artifact, artifact_id)
        if artifact is None:
            raise OperationError("Artifact not found", 404)
        if actor.startswith("agent:") and actor != artifact.owner:
            if actor.removeprefix("agent:") not in artifact.members:
                raise OperationError("Agent is not an artifact member", 403)
        return artifact

    async def _owned(self, session: AsyncSession, upload_id: str, actor: str) -> Artifact:
        from chiliarch.service import OperationError

        artifact = await self._artifact(session, upload_id, actor)
        if artifact.owner != actor:
            raise OperationError("Only the upload owner may supply or finalize bytes", 403)
        return artifact

    async def validate_scope(
        self,
        session: AsyncSession,
        operation: str,
        arguments: JsonObject,
        agent_id: str,
        task_id: str | None,
    ) -> None:
        from chiliarch.service import OperationError

        if task_id is None:
            return
        task = await self.service.collaboration._task(session, task_id, agent_id)
        if task.status not in {"running", "accepted"}:
            raise OperationError("Upload request belongs to work that is no longer running", 409)
        if operation == "begin_artifact_upload" and arguments.get("task_id") != task_id:
            raise OperationError("Upload cannot act as a different task", 403)
        if operation in CONTINUATION_OPERATIONS | {"discard_artifact_upload"}:
            artifact = await self._owned(session, arguments["upload_id"], "agent:" + agent_id)
            if artifact.task_id != task_id:
                raise OperationError("Upload does not belong to this managed task", 403)

    def _chunk_bytes(self) -> int:
        """Fit both directions including envelope overhead and base64 expansion."""
        from chiliarch.service import OperationError

        settings = self.service.settings_manager.current
        request = Envelope(
            id="x" * 128,
            correlation_id="x" * 128,
            type="agent.request",
            payload={
                "request_id": "x" * 128,
                "operation": "put_artifact_chunk",
                "arguments": {"upload_id": "x" * 128, "index": 2147483647, "data_base64": ""},
                "message": "",
                "metadata": {"task_id": "x" * 128},
            },
        )
        response = Envelope(
            id="x" * 128,
            correlation_id="x" * 128,
            type="request.resolved",
            payload={
                "request_id": "x" * 128,
                "status": "approved",
                "result": {
                    "artifact_id": "x" * 128,
                    "index": 2147483647,
                    "data_base64": "",
                    "sha256": "x" * 64,
                },
            },
        )
        overhead = max(len(e.canonical().encode()) for e in (request, response))
        budget = (settings.transport.max_message_bytes - overhead) // 4 * 3
        if budget <= 0:
            raise OperationError("Transport message limit leaves no room for artifact chunks", 409)
        return min(settings.artifacts.chunk_bytes, budget)

    async def execute(
        self, session: AsyncSession, name: str, args: JsonObject, actor: str
    ) -> JsonObject:
        from chiliarch.service import OperationError

        sender = actor.removeprefix("agent:") if actor.startswith("agent:") else None
        if sender:
            await self.service._agent(session, sender)
        if name == "begin_artifact_upload":
            limits = self.service.settings_manager.current.artifacts
            if args["size"] > limits.max_artifact_bytes:
                raise OperationError("Artifact exceeds configured file size limit", 413)
            reserved = await session.scalar(select(func.sum(Artifact.size))) or 0
            if reserved + args["size"] > limits.max_total_bytes:
                raise OperationError("Artifact storage quota exhausted", 409)
            members = list(dict.fromkeys([*args["members"], *([sender] if sender else [])]))
            for member in members:
                await self.service._agent(session, member)
            if args["goal_id"]:
                await self.service.coordination.goal(session, args["goal_id"])
            if args["task_id"]:
                await self.service.collaboration._task(session, args["task_id"], sender)
            artifact = Artifact(
                id=new_id(),
                name=args["name"],
                size=args["size"],
                sha256=args["sha256"],
                members=members,
                goal_id=args["goal_id"],
                task_id=args["task_id"],
                chunk_bytes=self._chunk_bytes(),
                owner=actor,
                state="uploading",
                created_at=time.time(),
            )
            session.add(artifact)
            return {"artifact": artifact_view(artifact), "upload_id": artifact.id}
        if name == "list_artifacts":
            query = select(Artifact).order_by(Artifact.created_at, Artifact.id)
            if args["goal_id"] is not None:
                query = query.where(Artifact.goal_id == args["goal_id"])
            if args["task_id"] is not None:
                query = query.where(Artifact.task_id == args["task_id"])
            visible = [
                row
                for row in (await session.scalars(query)).all()
                if (sender is None or actor == row.owner or sender in row.members)
                and (row.state == "ready" or row.owner == actor)
            ]
            page = visible[args["after"] : args["after"] + args["limit"]]
            return {
                "artifacts": [artifact_view(row) for row in page],
                "cursor": args["after"] + len(page),
                "limits": self.service.settings_manager.current.artifacts.model_dump(),
            }
        if name == "get_artifact":
            artifact = await self._artifact(session, args["artifact_id"], actor)
            result: JsonObject = {"artifact": artifact_view(artifact)}
            if artifact.owner == actor:
                result["uploaded_chunks"] = list(
                    (
                        await session.scalars(
                            select(ArtifactChunk.index)
                            .where(ArtifactChunk.artifact_id == artifact.id)
                            .order_by(ArtifactChunk.index)
                        )
                    ).all()
                )
            return result
        if name in {"delete_artifact", "discard_artifact_upload"}:
            artifact_id = args.get("artifact_id", args.get("upload_id"))
            assert isinstance(artifact_id, str)
            artifact = await self._artifact(session, artifact_id, actor)
            if sender and artifact.owner != actor:
                raise OperationError("Only the owner can request deletion of this artifact", 403)
            if name == "discard_artifact_upload" and artifact.state == "ready":
                raise OperationError("Use explicit artifact deletion for a completed artifact", 409)
            await session.execute(
                delete(ArtifactChunk).where(ArtifactChunk.artifact_id == artifact.id)
            )
            await session.delete(artifact)
            return {
                "artifact_id": artifact.id,
                "status": "deleted",
                "released_bytes": artifact.size,
            }
        if name == "put_artifact_chunk":
            artifact = await self._owned(session, args["upload_id"], actor)
            expected = min(
                artifact.chunk_bytes, artifact.size - args["index"] * artifact.chunk_bytes
            )
            if expected <= 0 or len(args["data_base64"]) > 4 * ((expected + 2) // 3):
                raise OperationError("Chunk is outside the approved upload size", 422)
            try:
                data = base64.b64decode(args["data_base64"], validate=True)
            except (binascii.Error, ValueError) as exc:
                raise OperationError("Chunk is not valid base64", 422) from exc
            if len(data) != expected:
                raise OperationError("Chunk does not match the approved size", 422)
            prior = await session.get(ArtifactChunk, (artifact.id, args["index"]))
            if prior:
                if prior.data != data:
                    raise OperationError("Chunk index already has different immutable content", 409)
                return {"upload_id": artifact.id, "index": args["index"], "duplicate": True}
            if artifact.state != "uploading":
                raise OperationError("Completed artifact cannot be modified", 409)
            session.add(ArtifactChunk(artifact_id=artifact.id, index=args["index"], data=data))
            return {"upload_id": artifact.id, "index": args["index"], "duplicate": False}
        if name == "finish_artifact_upload":
            artifact = await self._owned(session, args["upload_id"], actor)
            digest, length, index = hashlib.sha256(), 0, 0
            stream = await session.stream_scalars(
                select(ArtifactChunk)
                .where(ArtifactChunk.artifact_id == artifact.id)
                .order_by(ArtifactChunk.index)
            )
            async for chunk in stream:
                if chunk.index != index:
                    raise OperationError("Artifact has missing chunks", 409)
                digest.update(chunk.data)
                length += len(chunk.data)
                index += 1
            if length != artifact.size or digest.hexdigest() != artifact.sha256:
                raise OperationError(
                    "Artifact length or SHA256 does not match its approved manifest", 409
                )
            artifact.state = "ready"
            return {"artifact": artifact_view(artifact)}
        if name == "read_artifact_chunk":
            artifact = await self._artifact(session, args["artifact_id"], actor)
            if artifact.state != "ready":
                raise OperationError("Artifact is not complete", 409)
            downloaded_chunk = await session.get(ArtifactChunk, (artifact.id, args["index"]))
            if downloaded_chunk is None:
                raise OperationError("Artifact chunk not found", 404)
            return {
                "artifact_id": artifact.id,
                "index": downloaded_chunk.index,
                "data_base64": base64.b64encode(downloaded_chunk.data).decode("ascii"),
                "sha256": hashlib.sha256(downloaded_chunk.data).hexdigest(),
            }
        raise OperationError("Unknown artifact operation", 404)
