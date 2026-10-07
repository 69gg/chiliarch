"""Application composition and the authenticated platform HTTP API."""

from __future__ import annotations

import logging
import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, ConfigDict, Field
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from chiliarch import __version__
from chiliarch.auth import SESSION_COOKIE, Authentication
from chiliarch.config import Settings, SettingsManager
from chiliarch.database import Database
from chiliarch.logging import close_logging, configure_logging
from chiliarch.protocol import protocol_schema
from chiliarch.service import OperationError, OperationService
from chiliarch.transport import TransportManager

logger = logging.getLogger("chiliarch.api")


class RequestLoggingMiddleware:
    """Pure ASGI logging preserves the cancellation lifecycle of streaming routes."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        started = time.perf_counter()
        status_code = 500

        async def logged_send(message: Message) -> None:
            nonlocal status_code
            if message["type"] == "http.response.start":
                status_code = message["status"]
            await send(message)

        try:
            await self.app(scope, receive, logged_send)
        finally:
            logger.info(
                "HTTP request",
                extra={
                    "method": scope["method"],
                    "path": scope["path"],
                    "status_code": status_code,
                    "duration_ms": round((time.perf_counter() - started) * 1000, 2),
                },
            )


class LoginRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    password: str = Field(min_length=1, max_length=1024)


class OperationRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    arguments: dict[str, Any] = Field(default_factory=dict)
    idempotency_key: str | None = Field(default=None, min_length=1, max_length=256)


def create_app(config_path: Path | None = None, *, config: Settings | None = None) -> FastAPI:
    from chiliarch.chat import Supervisor, create_chat_router

    manager = SettingsManager(path=config_path, settings=config)
    db = Database(manager.current.database.url)
    service = OperationService(db, manager)
    authentication = Authentication(db, manager)
    transport = TransportManager(service)
    supervisor = Supervisor(db, service, manager)

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        if not manager.current.auth.password_hash:
            raise RuntimeError(
                "No login password configured. Run `chiliarch init` before starting the server."
            )
        configure_logging(manager.current.logs, shared=True)

        def reconfigure_logs(settings: Settings) -> None:
            configure_logging(settings.logs, shared=True)

        manager.subscribe(reconfigure_logs)
        try:
            await db.initialize()
            await manager.start()
            await service.start()
            await supervisor.start()
            logger.info(
                "Chiliarch started",
                extra={
                    "event": "server.started",
                    "host": manager.current.server.host,
                    "port": manager.current.server.port,
                },
            )
            yield
        finally:
            await supervisor.stop()
            await transport.stop()
            await service.stop()
            await manager.stop()
            await db.close()
            logger.info("Chiliarch stopped", extra={"event": "server.stopped"})
            close_logging()

    app = FastAPI(title="chiliarch", version=__version__, lifespan=lifespan)
    app.state.database = db
    app.state.service = service
    app.state.settings_manager = manager
    app.state.supervisor = supervisor
    app.state.authentication = authentication

    @app.exception_handler(OperationError)
    async def operation_error(_: Request, error: OperationError) -> JSONResponse:
        return JSONResponse(status_code=error.status_code, content={"detail": str(error)})

    app.add_middleware(RequestLoggingMiddleware)

    @app.get("/api/health")
    async def health() -> dict[str, Any]:
        return {"status": "ok", "version": __version__}

    @app.get("/api/auth/session")
    async def session(request: Request) -> dict[str, Any]:
        user_session = await authentication.session(request.cookies.get(SESSION_COOKIE))
        return {
            "authenticated": user_session is not None,
            "csrf_token": user_session.csrf_token if user_session else None,
        }

    @app.post("/api/auth/login")
    async def login(request: Request, body: LoginRequest) -> JSONResponse:
        authentication.check_origin(request)
        token, csrf = await authentication.login(
            body.password, request.client.host if request.client else "unknown"
        )
        response = JSONResponse({"authenticated": True, "csrf_token": csrf})
        response.set_cookie(
            SESSION_COOKIE,
            token,
            httponly=True,
            secure=manager.current.auth.secure_cookie,
            samesite="strict",
            max_age=manager.current.auth.session_ttl_seconds,
        )
        await db.audit("user", "auth.login", {})
        return response

    @app.post("/api/auth/logout", dependencies=[Depends(authentication.require_user)])
    async def logout(request: Request) -> JSONResponse:
        await authentication.logout(request.cookies.get(SESSION_COOKIE))
        response = JSONResponse({"authenticated": False})
        response.delete_cookie(SESSION_COOKIE)
        return response

    @app.get("/api/state", dependencies=[Depends(authentication.require_user)])
    async def state() -> dict[str, Any]:
        return await service.state()

    @app.get("/api/operations", dependencies=[Depends(authentication.require_user)])
    async def operations() -> dict[str, Any]:
        return {"operations": service.schemas()}

    @app.post("/api/operations/{name}", dependencies=[Depends(authentication.require_user)])
    async def execute(name: str, body: OperationRequest) -> dict[str, Any]:
        return await service.execute(
            name, body.arguments, actor="user", idempotency_key=body.idempotency_key
        )

    @app.get("/api/protocol/schema")
    async def schema() -> dict[str, Any]:
        return protocol_schema()

    app.include_router(transport.router())
    app.include_router(create_chat_router(supervisor, authentication.require_user))
    static = Path(__file__).parent / "static"
    if (static / "assets").is_dir():
        app.mount("/assets", StaticFiles(directory=static / "assets"), name="assets")

    @app.get("/{path:path}", include_in_schema=False)
    async def frontend(path: str) -> FileResponse:
        if path == "api" or path.startswith("api/"):
            raise HTTPException(404, "API endpoint not found")
        index = static / "index.html"
        if not index.exists():
            raise HTTPException(
                503, "WebUI assets are missing; build the web workspace before packaging"
            )
        candidate = (static / path).resolve()
        if candidate.is_relative_to(static.resolve()) and candidate.is_file():
            return FileResponse(candidate)
        return FileResponse(index)

    return app
