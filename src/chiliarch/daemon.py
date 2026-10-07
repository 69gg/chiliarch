"""One loopback controller owning independent outbound agent connections."""

from __future__ import annotations

import asyncio
import inspect
import logging
import os
import secrets
import socket
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import uvicorn
from fastapi import FastAPI, HTTPException, Request
from filelock import FileLock, Timeout
from starlette.middleware.trustedhost import TrustedHostMiddleware
from starlette.responses import JSONResponse, Response

from chiliarch import __version__
from chiliarch.adapters.mailbox import AgentMailbox
from chiliarch.adapters.runtime import outbound_client
from chiliarch.client import AgentClient, ClientStore
from chiliarch.daemon_models import (
    AgentRegistration,
    DaemonError,
    MailboxRequest,
    RegistrationRequest,
    private_write,
    read_configuration,
)
from chiliarch.logging import close_logging, configure_logging

logger = logging.getLogger("chiliarch.daemon")
JsonObject = dict[str, Any]


class AgentSession:
    def __init__(self, registration: AgentRegistration, runtime: DaemonRuntime) -> None:
        self.registration = registration
        self.runtime = runtime
        self.client: AgentClient | None = None
        self.mailbox: AgentMailbox | None = None
        self.error: str | None = None
        self.task: asyncio.Task[None] | None = None
        self.ready: asyncio.Future[None] = asyncio.get_running_loop().create_future()

    async def run(self) -> None:
        entry = self.registration
        try:
            handler = None
            if entry.runner_settings is not None:
                from chiliarch.adapters.cli import load_runner_settings
                from chiliarch.adapters.runners import create_handler

                handler = create_handler(
                    load_runner_settings(entry.runner_settings), entry.state_path
                )
            async with outbound_client(
                entry.state_path,
                handler=handler,
                manual_tasks=handler is None,
                preferred=entry.preferred_transport,
                options=self.runtime.configuration.transport,
                max_concurrent_tasks=entry.max_concurrent_tasks,
            ) as (client, task):
                self.client = client
                self.mailbox = AgentMailbox(client.store, manual_tasks=handler is None)
                self.ready.set_result(None)
                logger.info("Agent connection started", extra={"handle": entry.handle})
                await task
        except asyncio.CancelledError:
            raise
        except Exception as error:
            # SDK/network exception bodies may contain credentials. Persist only the class.
            self.error = type(error).__name__
            logger.error(
                "Agent connection stopped", extra={"handle": entry.handle, "error": self.error}
            )
            if not self.ready.done():
                self.ready.set_exception(
                    DaemonError("Agent could not acquire its local connection")
                )
        finally:
            self.client = None
            self.mailbox = None
            if not self.ready.done():
                self.ready.cancel()

    async def stop(self) -> None:
        if self.task is not None:
            self.task.cancel()
            cleanup = asyncio.gather(self.task, return_exceptions=True)
            interrupted = False
            while not cleanup.done():
                try:
                    await asyncio.shield(cleanup)
                except asyncio.CancelledError:
                    interrupted = True
            if interrupted:
                raise asyncio.CancelledError

    async def status(self) -> JsonObject:
        result = self.registration.model_dump(mode="json", exclude={"token"})
        result.update(
            mode="manual" if self.registration.runner_settings is None else "managed",
            connection="running" if self.client is not None else "stopped",
            selected_transport=self.client.transport if self.client else None,
            error=self.error,
        )
        if self.mailbox is not None:
            result.update(await self.mailbox.status())
            result["mode"] = "manual" if self.registration.runner_settings is None else "managed"
            result["connection"] = "owned_by_daemon"
        return result


class DaemonRuntime:
    def __init__(self, directory: Path) -> None:
        self.directory = directory.expanduser().resolve()
        self.configuration = read_configuration(self.directory)
        self.admin_token = secrets.token_urlsafe(32)
        self.sessions: dict[str, AgentSession] = {}
        self.lock = asyncio.Lock()
        self.shutdown = asyncio.Event()

    def save(self) -> None:
        private_write(self.directory / "daemon.json", self.configuration.model_dump(mode="json"))

    def connection_file(self, handle: str) -> Path:
        return self.directory / "agents" / (handle + ".json")

    async def _start(self, entry: AgentRegistration) -> AgentSession:
        session = AgentSession(entry, self)
        session.task = asyncio.create_task(session.run(), name="chiliarch.daemon." + entry.handle)
        self.sessions[entry.handle] = session
        try:
            await asyncio.wait_for(
                asyncio.shield(session.ready), self.configuration.startup_timeout_seconds
            )
            private_write(
                self.connection_file(entry.handle), {"handle": entry.handle, "token": entry.token}
            )
        except BaseException as error:
            session.error = type(error).__name__
            await session.stop()
            raise
        return session

    async def start(self) -> None:
        self.save()
        for entry in self.configuration.agents:
            try:
                await self._start(entry)
            except (ValueError, OSError, TimeoutError):
                # One unavailable/revoked identity must not disable the rest of the machine.
                continue

    async def close(self) -> None:
        await asyncio.gather(*(session.stop() for session in self.sessions.values()))

    async def agents(self) -> list[JsonObject]:
        return [await session.status() for session in self.sessions.values()]

    async def register(self, request: RegistrationRequest) -> JsonObject:
        state_path = request.state_path.expanduser().resolve()
        if not state_path.is_file():
            raise DaemonError("Pair the agent before registering its state")
        store = ClientStore(state_path)
        await store.open()
        try:
            identity = await store.identity()
        finally:
            await store.close()
        if not all(identity.get(key) for key in ("agent_id", "credential", "server_url")):
            raise DaemonError("Agent state has no valid paired identity")
        handle = request.handle or identity["agent_id"]
        settings = (
            request.runner_settings.expanduser().resolve() if request.runner_settings else None
        )
        if settings is not None:
            from chiliarch.adapters.cli import load_runner_settings

            load_runner_settings(settings)
        async with self.lock:
            previous = self.sessions.get(handle)
            if previous is not None and previous.registration.state_path != state_path:
                raise DaemonError("This handle already belongs to another agent")
            if any(
                session.registration.state_path == state_path and name != handle
                for name, session in self.sessions.items()
            ):
                raise DaemonError("This paired identity already has a daemon handle")
            entry = AgentRegistration(
                handle=handle,
                state_path=state_path,
                runner_settings=settings,
                max_concurrent_tasks=request.max_concurrent_tasks,
                preferred_transport=request.preferred_transport,
                token=previous.registration.token if previous else secrets.token_urlsafe(32),
            )
            if (
                previous is not None
                and previous.registration == entry
                and previous.client is not None
            ):
                return await previous.status()
            if previous is not None:
                await previous.stop()
            previous_entries = self.configuration.agents
            try:
                session = await self._start(entry)
                self.configuration.agents = [
                    item for item in previous_entries if item.handle != handle
                ] + [entry]
                self.save()
            except BaseException:
                failed = self.sessions.pop(handle, None)
                if failed is not None:
                    await failed.stop()
                self.configuration.agents = previous_entries
                if previous is not None:
                    try:
                        await self._start(previous.registration)
                    except (ValueError, OSError, TimeoutError):
                        pass
                else:
                    self.connection_file(handle).unlink(missing_ok=True)
                raise
            logger.info(
                "Agent registered",
                extra={"handle": handle, "mode": "managed" if settings else "manual"},
            )
            return await session.status()

    async def unregister(self, handle: str) -> JsonObject:
        async with self.lock:
            session = self.sessions.get(handle)
            if session is None:
                raise DaemonError("Agent handle is not registered")
            await session.stop()
            previous_entries = self.configuration.agents
            self.configuration.agents = [
                entry for entry in self.configuration.agents if entry.handle != handle
            ]
            try:
                self.save()
            except BaseException:
                self.configuration.agents = previous_entries
                try:
                    await self._start(session.registration)
                except (ValueError, OSError, TimeoutError):
                    pass
                raise
            del self.sessions[handle]
            self.connection_file(handle).unlink(missing_ok=True)
            logger.info("Agent unregistered", extra={"handle": handle})
            return {"status": "removed", "handle": handle, "state_preserved": True}


def create_daemon_app(runtime: DaemonRuntime) -> FastAPI:
    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        await runtime.start()
        try:
            yield
        finally:
            await runtime.close()

    app = FastAPI(lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=["127.0.0.1"])

    @app.middleware("http")
    async def no_browser(request: Request, call_next: Any) -> Response:
        if request.headers.get("origin") is not None:
            return JSONResponse({"detail": "Browser origins are not allowed"}, status_code=403)
        response: Response = await call_next(request)
        return response

    def authorize(request: Request, token: str) -> None:
        if not secrets.compare_digest(request.headers.get("authorization", ""), "Bearer " + token):
            raise HTTPException(401, "Invalid local capability")

    @app.get("/health")
    async def health(request: Request) -> JsonObject:
        authorize(request, runtime.admin_token)
        return {
            "status": "ok",
            "version": __version__,
            "pid": os.getpid(),
            "directory": str(runtime.directory),
            "agent_count": len(runtime.sessions),
            "startup_timeout_seconds": runtime.configuration.startup_timeout_seconds,
        }

    @app.get("/agents")
    async def agents(request: Request) -> list[JsonObject]:
        authorize(request, runtime.admin_token)
        return await runtime.agents()

    @app.post("/agents")
    async def register(body: RegistrationRequest, request: Request) -> JsonObject:
        authorize(request, runtime.admin_token)
        try:
            return await runtime.register(body)
        except (ValueError, OSError) as error:
            raise HTTPException(
                400, str(error) if isinstance(error, DaemonError) else type(error).__name__
            ) from error

    @app.delete("/agents/{handle}")
    async def unregister(handle: str, request: Request) -> JsonObject:
        authorize(request, runtime.admin_token)
        try:
            return await runtime.unregister(handle)
        except DaemonError as error:
            raise HTTPException(404, str(error)) from error

    @app.post("/shutdown")
    async def shutdown(request: Request) -> JsonObject:
        authorize(request, runtime.admin_token)
        runtime.shutdown.set()
        return {"status": "stopping"}

    @app.post("/agents/{handle}/mailbox")
    async def mailbox(handle: str, body: MailboxRequest, request: Request) -> JsonObject:
        session = runtime.sessions.get(handle)
        if session is None:
            raise HTTPException(404, "Agent handle is not registered")
        authorize(request, session.registration.token)
        if session.mailbox is None:
            raise HTTPException(409, "Agent connection is stopped; check daemon agents")
        method = getattr(session.mailbox, body.method)  # Literal whitelist; never arbitrary RPC.
        try:
            inspect.signature(method).bind(**body.arguments)
            result: JsonObject = await method(**body.arguments)
            if body.method == "status":
                result.update(
                    mode="daemon",
                    connection="owned_by_daemon",
                    selected_transport=session.client.transport if session.client else None,
                )
            return result
        except (ValueError, TypeError) as error:
            raise HTTPException(400, str(error)) from error

    return app


async def run_daemon(directory: Path) -> None:
    directory = await asyncio.to_thread(lambda: directory.expanduser().resolve())
    directory.mkdir(parents=True, exist_ok=True)
    os.chmod(directory, 0o700)
    owner = FileLock(str(directory / "daemon.lock"), timeout=0, thread_local=False)
    try:
        await asyncio.to_thread(owner.acquire)
    except Timeout as error:
        raise DaemonError("A daemon already owns this directory") from error
    runtime_file = directory / "runtime.json"
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    runtime: DaemonRuntime | None = None
    try:
        runtime = DaemonRuntime(directory)
        configure_logging(runtime.configuration.logs, shared=True)
        listener.bind(("127.0.0.1", runtime.configuration.port))
        listener.setblocking(False)
        server = uvicorn.Server(
            uvicorn.Config(create_daemon_app(runtime), log_config=None, access_log=False)
        )
        private_write(
            runtime_file,
            {
                "url": f"http://127.0.0.1:{listener.getsockname()[1]}",
                "token": runtime.admin_token,
                "pid": os.getpid(),
                "version": __version__,
            },
        )

        async def request_shutdown() -> None:
            await runtime.shutdown.wait()
            server.should_exit = True

        watcher = asyncio.create_task(request_shutdown())
        logger.info(
            "Daemon started", extra={"event": "daemon.started", "directory": str(directory)}
        )
        try:
            await server.serve(sockets=[listener])
        finally:
            watcher.cancel()
            await asyncio.gather(watcher, return_exceptions=True)
    finally:
        if runtime is not None:
            await runtime.close()
        runtime_file.unlink(missing_ok=True)
        listener.close()
        close_logging()
        await asyncio.to_thread(owner.release)
