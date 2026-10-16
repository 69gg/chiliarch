"""Local daemon control and agent-scoped MCP mailbox proxy; no reverse connections."""

from __future__ import annotations

import asyncio
import json
import logging
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
    DAEMON_API_VERSION,
    AgentRegistration,
    DaemonConfiguration,
    DaemonError,
    read_configuration,
)
from chiliarch.daemon_services import ServiceError, ServiceManager, default_daemon_directory

JsonObject = dict[str, Any]
logger = logging.getLogger(__name__)


class DaemonUnavailable(DaemonError):
    """There is no reachable local daemon; startup may safely be attempted."""


class DaemonClient:
    def __init__(
        self,
        directory: Path | None = None,
        *,
        token: str | None = None,
        allow_version_mismatch: bool = False,
        health_timeout: float = 1,
    ) -> None:
        self.directory = (directory or default_daemon_directory()).expanduser().resolve()
        self.token = token
        self.allow_version_mismatch = allow_version_mismatch
        self.health_timeout = health_timeout
        self.http = httpx.AsyncClient(trust_env=False, timeout=35)

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(self, *arguments: object) -> None:
        await self.http.aclose()

    def _check_compatibility(self, payload: JsonObject) -> None:
        if self.allow_version_mismatch:
            return
        if "api_version" in payload:
            api_version = payload["api_version"]
            if type(api_version) is not int or api_version != DAEMON_API_VERSION:
                raise DaemonError(
                    "Daemon API version differs from this installation; restart the daemon"
                )
        elif payload.get("version") != __version__:
            # Older daemons had no independent API contract. Do not infer
            # compatibility from software version ordering or a missing field.
            raise DaemonError("Daemon version differs from this installation; restart the daemon")

    async def request(
        self,
        method: str,
        path: str,
        body: JsonObject | None = None,
        *,
        request_timeout: float | None = None,
    ) -> Any:
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
            self._check_compatibility(runtime)
            response = await self.http.request(
                method,
                runtime["url"] + path,
                json=body,
                headers={"Authorization": "Bearer " + (self.token or runtime["token"])},
                timeout=request_timeout if request_timeout is not None else self.http.timeout,
            )
        except (OSError, KeyError, TypeError, json.JSONDecodeError, httpx.HTTPError) as error:
            raise DaemonUnavailable(
                "Local daemon is unavailable; run chiliarch daemon status/start"
            ) from error
        try:
            payload = response.json()
        except json.JSONDecodeError as error:
            raise DaemonError(
                f"Local daemon returned an invalid response (HTTP {response.status_code})"
            ) from error
        if response.is_error:
            detail = (
                payload.get("detail", "Local daemon rejected the operation")
                if isinstance(payload, dict)
                else "Local daemon rejected the operation"
            )
            raise DaemonError(str(detail))
        return payload

    async def health(self) -> JsonObject:
        result: JsonObject = await self.request(
            "GET", "/health", request_timeout=self.health_timeout
        )
        self._check_compatibility(result)
        if result.get("directory") != str(self.directory):
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

    async def read_messages(
        self, after: int | None = 0, limit: int = 100, task_id: str | None = None
    ) -> JsonObject:
        return await self._call("read_messages", after=after, limit=limit, task_id=task_id)

    async def acknowledge_messages(self, cursor: int, task_id: str | None = None) -> JsonObject:
        return await self._call("acknowledge_messages", cursor=cursor, task_id=task_id)

    async def send_message(
        self, message: str, task_id: str | None = None, correlation_id: str | None = None
    ) -> JsonObject:
        return await self._call(
            "send_message", message=message, task_id=task_id, correlation_id=correlation_id
        )

    async def request_operation(
        self,
        operation: str,
        arguments: JsonObject,
        message: str = "",
        wait_seconds: float = 0,
        client_request_id: str | None = None,
        task_id: str | None = None,
    ) -> JsonObject:
        return await self._call(
            "request_operation",
            operation=operation,
            arguments=arguments,
            message=message,
            wait_seconds=wait_seconds,
            client_request_id=client_request_id,
            task_id=task_id,
        )

    async def get_request_result(self, request_id: str, wait_seconds: float = 0) -> JsonObject:
        return await self._call(
            "get_request_result", request_id=request_id, wait_seconds=wait_seconds
        )

    async def list_agents(self, wait_seconds: float = 10) -> JsonObject:
        return await self._call("list_agents", wait_seconds=wait_seconds)

    async def create_channel(
        self,
        title: str,
        members: list[str],
        goal_id: str | None = None,
        wait_seconds: float = 10,
        client_request_id: str | None = None,
    ) -> JsonObject:
        return await self._call(
            "create_channel",
            title=title,
            members=members,
            goal_id=goal_id,
            wait_seconds=wait_seconds,
            client_request_id=client_request_id,
        )

    async def list_channels(self, wait_seconds: float = 10) -> JsonObject:
        return await self._call("list_channels", wait_seconds=wait_seconds)

    async def get_channel(
        self, channel_id: str, after: int = 0, limit: int = 100, wait_seconds: float = 10
    ) -> JsonObject:
        return await self._call(
            "get_channel",
            channel_id=channel_id,
            after=after,
            limit=limit,
            wait_seconds=wait_seconds,
        )

    async def send_peer_message(
        self,
        message: str,
        recipient_agent_id: str | None = None,
        channel_id: str | None = None,
        task_id: str | None = None,
        recipient_task_id: str | None = None,
        reply_to: str | None = None,
        wake: bool = False,
        wait_seconds: float = 10,
        client_request_id: str | None = None,
    ) -> JsonObject:
        return await self._call(
            "send_peer_message",
            message=message,
            recipient_agent_id=recipient_agent_id,
            channel_id=channel_id,
            task_id=task_id,
            recipient_task_id=recipient_task_id,
            reply_to=reply_to,
            wake=wake,
            wait_seconds=wait_seconds,
            client_request_id=client_request_id,
        )

    async def acknowledge_peer_message(
        self,
        message_id: str,
        state: Literal["read", "processed"],
        wait_seconds: float = 10,
        client_request_id: str | None = None,
    ) -> JsonObject:
        return await self._call(
            "acknowledge_peer_message",
            message_id=message_id,
            state=state,
            wait_seconds=wait_seconds,
            client_request_id=client_request_id,
        )

    async def get_peer_message(self, message_id: str, wait_seconds: float = 10) -> JsonObject:
        return await self._call(
            "get_peer_message", message_id=message_id, wait_seconds=wait_seconds
        )

    async def wait_for_tasks(
        self,
        waiting_for: list[str],
        task_id: str | None = None,
        reason: str = "",
        wait_seconds: float = 10,
        client_request_id: str | None = None,
    ) -> JsonObject:
        return await self._call(
            "wait_for_tasks",
            waiting_for=waiting_for,
            task_id=task_id,
            reason=reason,
            wait_seconds=wait_seconds,
            client_request_id=client_request_id,
        )

    async def list_task_waits(
        self, task_id: str | None = None, wait_seconds: float = 10
    ) -> JsonObject:
        return await self._call("list_task_waits", task_id=task_id, wait_seconds=wait_seconds)

    async def upload_artifact(
        self,
        path: str,
        members: list[str],
        goal_id: str | None = None,
        task_id: str | None = None,
        client_request_id: str | None = None,
        timeout_seconds: float = 20,
    ) -> JsonObject:
        path = str(await asyncio.to_thread(lambda: Path(path).expanduser().absolute()))
        return await self._call(
            "upload_artifact",
            path=path,
            members=members,
            goal_id=goal_id,
            task_id=task_id,
            client_request_id=client_request_id,
            timeout_seconds=timeout_seconds,
        )

    async def download_artifact(
        self,
        artifact_id: str,
        destination: str,
        task_id: str | None = None,
        timeout_seconds: float = 20,
    ) -> JsonObject:
        destination = str(
            await asyncio.to_thread(lambda: Path(destination).expanduser().absolute())
        )
        return await self._call(
            "download_artifact",
            artifact_id=artifact_id,
            destination=destination,
            task_id=task_id,
            timeout_seconds=timeout_seconds,
        )

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
    from chiliarch.updates import startup_budget

    directory = (directory or default_daemon_directory()).expanduser().resolve()
    directory.mkdir(parents=True, exist_ok=True)
    os.chmod(directory, 0o700)
    update_budget = startup_budget()
    lock = FileLock(
        str(directory / "install.lock"), timeout=120 + update_budget, thread_local=False
    )
    await asyncio.to_thread(lock.acquire)
    try:
        manager = ServiceManager(directory)
        service = await asyncio.to_thread(manager.status)
        if autostart and service.available and not service.installed:
            service = await asyncio.to_thread(manager.install)
        elif service.available and service.installed:
            try:
                await asyncio.to_thread(manager.refresh)
            except (ServiceError, OSError):
                logger.warning(
                    "Login startup definition could not be refreshed; preserving existing service",
                    extra={"event": "daemon.startup_refresh_failed"},
                )
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
                env=dict(os.environ, CHILIARCH_BACKGROUND="1"),
                cwd=directory,
                **options,
            )
        deadline = time.monotonic() + configuration.startup_timeout_seconds + update_budget
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
