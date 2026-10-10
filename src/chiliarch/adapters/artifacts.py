"""Local-only file IO; the model receives metadata, never encoded file chunks."""

from __future__ import annotations

import asyncio
import base64
import binascii
import hashlib
import os
import time
from pathlib import Path
from typing import Any, Protocol, overload
from uuid import uuid4

JsonObject = dict[str, Any]


class TransferMailbox(Protocol):
    async def request_operation(
        self,
        operation: str,
        arguments: JsonObject,
        message: str = "",
        wait_seconds: float = 0,
        client_request_id: str | None = None,
        task_id: str | None = None,
    ) -> JsonObject: ...


@overload
def public_result(value: JsonObject) -> JsonObject: ...


@overload
def public_result(value: Any) -> Any: ...


def public_result(value: Any) -> Any:
    """Raw transport content is for the local file writer, not MCP/model context."""
    if isinstance(value, dict):
        return {key: public_result(item) for key, item in value.items() if key != "data_base64"}
    if isinstance(value, list):
        return [public_result(item) for item in value]
    return value


class TransferPending(Exception):
    def __init__(self, result: JsonObject) -> None:
        self.result = public_result(result)


class Transfer:
    def __init__(
        self, mailbox: TransferMailbox, timeout_seconds: float, task_id: str | None
    ) -> None:
        if not 0 < timeout_seconds <= 30:
            raise ValueError("timeout_seconds must be greater than 0 and at most 30")
        self.mailbox, self.task_id = mailbox, task_id
        self.deadline = time.monotonic() + timeout_seconds

    async def request(
        self, operation: str, arguments: JsonObject, key: str | None = None
    ) -> JsonObject:
        remaining = self.deadline - time.monotonic()
        if remaining <= 0:
            raise TransferPending({"status": "transfer_pending"})
        result = await self.mailbox.request_operation(
            operation,
            arguments,
            wait_seconds=min(remaining, 30),
            client_request_id=key,
            task_id=self.task_id,
        )
        if result.get("status") == "pending":
            raise TransferPending(result)
        if result.get("status") != "approved":
            details = result.get("result")
            reason = details.get("error") if isinstance(details, dict) else result.get("error")
            raise ValueError(str(reason or "Artifact operation was not approved"))
        payload: JsonObject = result["result"]
        return payload


def _manifest(path: Path) -> tuple[int, str]:
    if not path.is_file():
        raise ValueError("Upload path is not a regular file")
    with path.open("rb") as source:
        digest = hashlib.file_digest(source, "sha256").hexdigest()
        size = source.tell()
    return size, digest


def _file_size(path: Path) -> int:
    if not path.is_file():
        raise ValueError("Upload path is not a regular file")
    return path.stat().st_size


def _read_chunk(path: Path, index: int, chunk_bytes: int) -> bytes:
    with path.open("rb") as source:
        source.seek(index * chunk_bytes)
        return source.read(chunk_bytes)


def _key(base: str, part: str) -> str:
    return "artifact:" + hashlib.sha256(base.encode()).hexdigest() + ":" + part


async def upload_artifact(
    mailbox: TransferMailbox,
    path: str,
    members: list[str],
    *,
    goal_id: str | None = None,
    task_id: str | None = None,
    client_request_id: str | None = None,
    timeout_seconds: float = 20,
) -> JsonObject:
    transfer = Transfer(mailbox, timeout_seconds, task_id)
    source = await asyncio.to_thread(lambda: Path(path).expanduser().absolute())
    size = await asyncio.to_thread(_file_size, source)
    key = client_request_id or str(uuid4())
    progress: JsonObject = {
        "client_request_id": key,
        "name": source.name,
        "size": size,
    }
    try:
        visible = await transfer.request("list_artifacts", {"limit": 1})
        if size > visible["limits"]["max_artifact_bytes"]:
            raise ValueError("Local file exceeds configured artifact size limit")
        size, digest = await asyncio.to_thread(_manifest, source)
        progress.update(size=size, sha256=digest)
        begun = await transfer.request(
            "begin_artifact_upload",
            {
                "name": source.name,
                "size": size,
                "sha256": digest,
                "members": members,
                "goal_id": goal_id,
                "task_id": task_id,
            },
            _key(key, "begin"),
        )
        upload_id = str(begun["upload_id"])
        progress["upload_id"] = upload_id
        current = await transfer.request("get_artifact", {"artifact_id": upload_id})
        artifact = current["artifact"]
        if artifact["state"] == "ready":
            return {"status": "completed", **progress, "artifact": artifact}
        present = set(current["uploaded_chunks"])
        chunk_bytes = int(artifact["chunk_bytes"])
        count = (size + chunk_bytes - 1) // chunk_bytes
        progress["uploaded_chunks"] = len(present)
        progress["total_chunks"] = count
        for index in range(count):
            if index in present:
                continue
            data = await asyncio.to_thread(_read_chunk, source, index, chunk_bytes)
            expected = min(chunk_bytes, size - index * chunk_bytes)
            if len(data) != expected:
                raise ValueError(
                    "Local file changed during upload; retain or discard the unfinished upload"
                )
            await transfer.request(
                "put_artifact_chunk",
                {
                    "upload_id": upload_id,
                    "index": index,
                    "data_base64": base64.b64encode(data).decode("ascii"),
                },
                _key(key, str(index)),
            )
            progress["uploaded_chunks"] += 1
        finished = await transfer.request(
            "finish_artifact_upload", {"upload_id": upload_id}, _key(key, "finish")
        )
        return {"status": "completed", **progress, "artifact": finished["artifact"]}
    except TransferPending as pending:
        return {**progress, **pending.result}


def _staging(destination: Path, artifact: JsonObject) -> tuple[Path, int]:
    if destination.exists() or destination.is_symlink():
        raise ValueError("Download destination already exists; choose a new path")
    token = hashlib.sha256(
        (str(destination) + artifact["id"] + artifact["sha256"]).encode()
    ).hexdigest()
    temporary = destination.with_name(".chiliarch-" + token + ".part")
    flags = os.O_CREAT | os.O_EXCL | os.O_WRONLY
    try:
        descriptor = os.open(temporary, flags, 0o600)
    except FileExistsError:
        if temporary.is_symlink() or not temporary.is_file():
            raise ValueError("Download staging path is not a regular file") from None
    else:
        os.close(descriptor)
    offset = temporary.stat().st_size
    if offset > artifact["size"] or (
        offset != artifact["size"] and offset % artifact["chunk_bytes"]
    ):
        raise ValueError("Partial download is invalid; remove the staging file before retrying")
    return temporary, offset


def _append(path: Path, offset: int, data: bytes) -> None:
    if path.is_symlink():
        raise ValueError("Download staging path changed")
    descriptor = os.open(
        path, os.O_RDWR | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_BINARY", 0)
    )
    with os.fdopen(descriptor, "r+b") as target:
        if os.fstat(target.fileno()).st_size != offset:
            raise ValueError("Partial download changed; do not run two downloads to the same path")
        target.seek(offset)
        target.write(data)
        target.flush()
        os.fsync(target.fileno())


def _publish(temporary: Path, destination: Path, artifact: JsonObject) -> None:
    size, digest = _manifest(temporary)
    if size != artifact["size"] or digest != artifact["sha256"]:
        raise ValueError("Downloaded file failed SHA256 verification; destination was not created")
    # Hard-link publication is atomic and never overwrites a concurrently-created target.
    # Unsupported filesystems fail explicitly, preserving the verified staging file.
    os.link(temporary, destination)
    temporary.unlink()


async def download_artifact(
    mailbox: TransferMailbox,
    artifact_id: str,
    destination: str,
    *,
    task_id: str | None = None,
    timeout_seconds: float = 20,
) -> JsonObject:
    transfer = Transfer(mailbox, timeout_seconds, task_id)
    target = await asyncio.to_thread(lambda: Path(destination).expanduser().absolute())
    progress: JsonObject = {"artifact_id": artifact_id, "destination": str(target)}
    try:
        current = await transfer.request("get_artifact", {"artifact_id": artifact_id})
        artifact = current["artifact"]
        if artifact["state"] != "ready":
            raise ValueError("Artifact is not ready for download")
        temporary, offset = await asyncio.to_thread(_staging, target, artifact)
        progress["downloaded_bytes"] = offset
        progress["size"] = artifact["size"]
        chunk_bytes = int(artifact["chunk_bytes"])
        for index in range(
            offset // chunk_bytes, (artifact["size"] + chunk_bytes - 1) // chunk_bytes
        ):
            if offset == artifact["size"]:
                break
            chunk = await transfer.request(
                "read_artifact_chunk", {"artifact_id": artifact_id, "index": index}
            )
            try:
                data = base64.b64decode(chunk["data_base64"], validate=True)
            except (binascii.Error, ValueError) as exc:
                raise ValueError("Downloaded chunk is not valid base64") from exc
            expected = min(chunk_bytes, artifact["size"] - offset)
            if len(data) != expected or hashlib.sha256(data).hexdigest() != chunk["sha256"]:
                raise ValueError("Downloaded chunk failed size or SHA256 verification")
            await asyncio.to_thread(_append, temporary, offset, data)
            offset += len(data)
            progress["downloaded_bytes"] = offset
        await asyncio.to_thread(_publish, temporary, target, artifact)
        return {"status": "completed", **progress, "sha256": artifact["sha256"]}
    except TransferPending as pending:
        return {**progress, **pending.result}
