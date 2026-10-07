"""Local daemon control and agent-scoped MCP mailbox proxy; no reverse connections."""

from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
import time
from dataclasses import asdict
from pathlib import Path
from typing import Any, Literal, Self
from urllib.parse import urlsplit

import httpx
from filelock import FileLock, Timeout

from chiliarch import __version__
from chiliarch.daemon_models import (
    AgentRegistration,
    DaemonConfiguration,
    DaemonError,
    read_configuration,
)
from chiliarch.daemon_services import ServiceManager, default_daemon_directory

JsonObject = dict[str, Any]


class DaemonUnavailable(DaemonError):
    """There is no reachable local daemon; startup may safely be attempted."""


class DaemonClient:
    def __init__(
        self,
        directory: Path | None = None,
        *,
        token: str | None = None,
        allow_version_mismatch: bool = False,
    ) -> None:
        self.directory = (directory or default_daemon_directory()).expanduser().resolve()
        self.token = token
        self.allow_version_mismatch = allow_version_mismatch
        self.http = httpx.AsyncClient(trust_env=False, timeout=35)

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(self, *arguments: object) -> None:
        await self.http.aclose()

    async def request(self, method: str, path: str, body: JsonObject | None = None) -> Any:
        try:
            runtime = json.loads((self.directory / "runtime.json").read_text(encoding="utf-8"))
            parsed = urlsplit(runtime["url"])
            if (
                parsed.scheme != "http"
                or parsed.hostname != "127.0.0.1"
                or not parsed.port
                or parsed.username
                or parsed.password
                or parsed.path
                or parsed.query
                or parsed.fragment
            ):
                raise DaemonError("Daemon runtime endpoint is not a loopback address")
            if runtime["version"] != __version__ and not self.allow_version_mismatch:
                raise DaemonError(
                    "Daemon version differs from this installation; restart the daemon"
                )
            response = await self.http.request(
                method,
                runtime["url"] + path,
                json=body,
                headers={"Authorization": "Bearer " + (self.token or runtime["token"])},
            )
        except (OSError, KeyError, TypeError, json.JSONDecodeError, httpx.HTTPError) as error:
            raise DaemonUnavailable(
                "Local daemon is unavailable; run chiliarch daemon status/start"
            ) from error
        if response.is_error:
            detail = response.json().get("detail", "Local daemon rejected the operation")
            raise DaemonError(str(detail))
        return response.json()

    async def health(self) -> JsonObject:
        result: JsonObject = await self.request("GET", "/health")
        if result.get("directory") != str(self.directory) or (
            result.get("version") != __version__ and not self.allow_version_mismatch
        ):
            raise DaemonError("Daemon identity does not match this installation/directory")
        return result

    async def agents(self) -> list[JsonObject]:
        result: list[JsonObject] = await self.request("GET", "/agents")
        return result

    async def register(
        self,
        state_path: Path,
        *,
        handle: str | None = None,
        runner_settings: Path | None = None,
        max_concurrent_tasks: int = 1,
        preferred_transport: Literal["ws", "sse", "poll"] = "ws",
    ) -> JsonObject:
        result: JsonObject = await self.request(
            "POST",
            "/agents",
            {
                "state_path": str(
                    await asyncio.to_thread(lambda: state_path.expanduser().resolve())
                ),
                "handle": handle,
                "runner_settings": str(
                    await asyncio.to_thread(lambda: runner_settings.expanduser().resolve())
                )
                if runner_settings
                else None,
                "max_concurrent_tasks": max_concurrent_tasks,
                "preferred_transport": preferred_transport,
            },
        )
        return result

    async def unregister(self, handle: str) -> JsonObject:
        result: JsonObject = await self.request("DELETE", "/agents/" + handle)
        return result

    async def shutdown(self) -> JsonObject:
        result: JsonObject = await self.request("POST", "/shutdown")
        return result


class RemoteMailbox:
    """An MCP bridge holds only its registered agent's local capability."""

    def __init__(self, directory: Path | None, handle: str) -> None:
        # Use the same constrained handle grammar before constructing a file path.
        AgentRegistration(handle=handle, state_path=Path("unused"), token="unused")
        self.directory = (directory or default_daemon_directory()).expanduser().resolve()
        self.handle = handle
        try:
            identity = json.loads(
                (self.directory / "agents" / (handle + ".json")).read_text(encoding="utf-8")
            )
            if identity["handle"] != handle:
                raise DaemonError("Local agent capability does not match its handle")
            self.client = DaemonClient(self.directory, token=str(identity["token"]))
        except (OSError, KeyError, TypeError, json.JSONDecodeError) as error:
            raise DaemonError("Register this agent with the daemon before opening MCP") from error

    async def close(self) -> None:
        await self.client.http.aclose()

    async def _call(self, method: str, **arguments: Any) -> JsonObject:
        result: JsonObject = await self.client.request(
            "POST", f"/agents/{self.handle}/mailbox", {"method": method, "arguments": arguments}
        )
        return result

    async def status(self) -> JsonObject:
        return await self._call("status")

    async def list_tasks(self, status: str | None = None) -> JsonObject:
        return await self._call("list_tasks", status=status)

    async def read_messages(self, after: int = 0, limit: int = 100) -> JsonObject:
        return await self._call("read_messages", after=after, limit=limit)

    async def send_message(
        self, message: str, task_id: str | None = None, correlation_id: str | None = None
    ) -> JsonObject:
        return await self._call(
            "send_message", message=message, task_id=task_id, correlation_id=correlation_id
        )

    async def request_operation(
        self, operation: str, arguments: JsonObject, message: str = "", wait_seconds: float = 0
    ) -> JsonObject:
        return await self._call(
            "request_operation",
            operation=operation,
            arguments=arguments,
            message=message,
            wait_seconds=wait_seconds,
        )

    async def get_request_result(self, request_id: str, wait_seconds: float = 0) -> JsonObject:
        return await self._call(
            "get_request_result", request_id=request_id, wait_seconds=wait_seconds
        )

    async def list_agents(self, wait_seconds: float = 10) -> JsonObject:
        return await self._call("list_agents", wait_seconds=wait_seconds)

    async def claim_task(self, task_id: str | None = None, wait_seconds: float = 0) -> JsonObject:
        return await self._call("claim_task", task_id=task_id, wait_seconds=wait_seconds)

    async def report_progress(self, task_id: str, progress: float, message: str = "") -> JsonObject:
        return await self._call(
            "report_progress", task_id=task_id, progress=progress, message=message
        )

    async def complete_task(self, task_id: str, result: JsonObject) -> JsonObject:
        return await self._call("complete_task", task_id=task_id, result=result)

    async def fail_task(self, task_id: str, error: str) -> JsonObject:
        return await self._call("fail_task", task_id=task_id, error=error)

    async def confirm_cancellation(self, task_id: str) -> JsonObject:
        return await self._call("confirm_cancellation", task_id=task_id)


async def _health(directory: Path) -> JsonObject | None:
    async with DaemonClient(directory) as client:
        try:
            return await client.health()
        except DaemonUnavailable:
            return None


def _spawn_background(command: list[str], **options: Any) -> subprocess.Popen[bytes]:
    return subprocess.Popen(command, **options)


async def ensure_daemon(directory: Path | None = None, *, autostart: bool = True) -> JsonObject:
    """Reuse a healthy daemon, or install/start once under an interprocess lock."""
    directory = (directory or default_daemon_directory()).expanduser().resolve()
    directory.mkdir(parents=True, exist_ok=True)
    os.chmod(directory, 0o700)
    lock = FileLock(str(directory / "install.lock"), timeout=120, thread_local=False)
    await asyncio.to_thread(lock.acquire)
    try:
        manager = ServiceManager(directory)
        service = await asyncio.to_thread(manager.status)
        if autostart and service.available and not service.installed:
            service = await asyncio.to_thread(manager.install)
        healthy = await _health(directory)
        if healthy is not None:
            return {**healthy, "reused": True, "autostart": asdict(service)}
        configuration = read_configuration(directory)
        child: subprocess.Popen[bytes] | None = None
        if service.available and service.installed and service.enabled:
            await asyncio.to_thread(manager.start)
        else:
            command = [
                sys.executable,
                "-m",
                "chiliarch",
                "daemon",
                "run",
                "--directory",
                str(directory),
            ]
            options: dict[str, Any] = {"start_new_session": True}
            if sys.platform == "win32":
                options = {
                    "creationflags": subprocess.CREATE_NEW_PROCESS_GROUP
                    | subprocess.DETACHED_PROCESS
                }
            child = await asyncio.to_thread(
                _spawn_background,
                command,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                cwd=directory,
                **options,
            )
        deadline = time.monotonic() + configuration.startup_timeout_seconds
        while time.monotonic() < deadline:
            healthy = await _health(directory)
            if healthy is not None:
                service = await asyncio.to_thread(manager.status)
                return {**healthy, "reused": False, "autostart": asdict(service)}
            if child is not None and child.poll() is not None:
                raise DaemonError(
                    "Daemon failed to start; inspect its logs and run daemon run in foreground"
                )
            await asyncio.sleep(0.1)
        raise DaemonError("Daemon did not become healthy before the configured startup timeout")
    finally:
        await asyncio.to_thread(lock.release)


async def stop_daemon(directory: Path) -> JsonObject:
    directory = await asyncio.to_thread(lambda: directory.expanduser().resolve())
    await asyncio.to_thread(directory.mkdir, parents=True, exist_ok=True)
    lock = FileLock(str(directory / "install.lock"), timeout=120, thread_local=False)
    await asyncio.to_thread(lock.acquire)
    try:
        return await _stop_daemon_locked(directory)
    finally:
        await asyncio.to_thread(lock.release)


async def _stop_daemon_locked(directory: Path) -> JsonObject:
    manager = ServiceManager(directory)
    async with DaemonClient(directory, allow_version_mismatch=True) as client:
        try:
            healthy = await client.health()
        except DaemonUnavailable:
            healthy = None
    if healthy is not None:
        async with DaemonClient(directory, allow_version_mismatch=True) as client:
            await client.shutdown()
        deadline = time.monotonic() + float(
            healthy.get("startup_timeout_seconds", DaemonConfiguration().startup_timeout_seconds)
        )
        while (directory / "runtime.json").exists() and time.monotonic() < deadline:  # noqa: ASYNC110 - observe another process's private runtime file
            await asyncio.sleep(0.1)
        if (directory / "runtime.json").exists():
            raise DaemonError("Daemon is still cleaning up; refusing to forcibly stop active tasks")
    else:
        owner = FileLock(str(directory / "daemon.lock"), timeout=0, thread_local=False)
        try:
            await asyncio.to_thread(owner.acquire)
        except Timeout as error:
            raise DaemonError(
                "Daemon still owns its directory but is unreachable; shutdown state is unknown"
            ) from error
        else:
            await asyncio.to_thread(owner.release)
    status = await asyncio.to_thread(manager.status)
    if status.available and status.installed:
        await asyncio.to_thread(manager.stop)
    return {"status": "stopped", "directory": str(directory)}
