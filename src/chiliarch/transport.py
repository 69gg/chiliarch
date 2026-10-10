"""Agent-initiated transports sharing one durable inbox and outbox."""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
from collections.abc import AsyncIterator
from typing import Any

from anyio import CancelScope
from fastapi import APIRouter, HTTPException, Query, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from chiliarch.delivery import DeliveryBackoff
from chiliarch.protocol import Envelope
from chiliarch.service import OperationError, OperationService

logger = logging.getLogger("chiliarch.transport")


class PairRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    code: str = Field(min_length=1, max_length=256)
    name: str = Field(min_length=1, max_length=256)
    machine: str = Field(min_length=1, max_length=256)
    capabilities: list[str] = Field(default_factory=list, max_length=256)


def bearer(authorization: str | None) -> str | None:
    if authorization and authorization.startswith("Bearer "):
        return authorization.removeprefix("Bearer ")
    return None


class TransportManager:
    def __init__(self, service: OperationService) -> None:
        self.service = service
        self.connections: set[WebSocket] = set()

    async def stop(self) -> None:
        for connection in tuple(self.connections):
            with contextlib.suppress(RuntimeError, WebSocketDisconnect):
                await connection.close(code=1012)

    async def _wait(self, duration: float) -> None:
        self.service.changed.clear()
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(self.service.changed.wait(), duration)

    def router(self) -> APIRouter:
        router = APIRouter(prefix="/api/agent", tags=["agent protocol"])

        @router.post("/pair")
        async def pair(request: PairRequest) -> dict[str, Any]:
            return await self.service.pair(
                request.code, request.name, request.machine, request.capabilities
            )

        @router.post("/messages")
        async def messages(request: Request) -> dict[str, Any]:
            agent = await self.service.authenticate_agent(
                bearer(request.headers.get("authorization"))
            )
            body = await request.body()
            if len(body) > self.service.settings_manager.current.transport.max_message_bytes:
                raise HTTPException(413, "Message exceeds configured size limit")
            try:
                envelope = Envelope.model_validate_json(body)
            except ValidationError as exc:
                raise HTTPException(422, "Invalid protocol envelope") from exc
            return await self.service.receive(agent.id, envelope)

        @router.get("/poll")
        async def poll(
            request: Request, wait_seconds: float = Query(default=25, ge=0, le=60, alias="timeout")
        ) -> dict[str, Any]:
            credential = bearer(request.headers.get("authorization"))
            agent = await self.service.authenticate_agent(credential)
            await self.service.touch_agent(agent.id, "poll")
            options = self.service.settings_manager.current.transport
            deadline = asyncio.get_running_loop().time() + min(
                wait_seconds, options.poll_timeout_seconds
            )
            while True:
                await self.service.authenticate_agent(credential)
                pending = await self.service.pending(agent.id)
                remaining = deadline - asyncio.get_running_loop().time()
                if pending or remaining <= 0 or await request.is_disconnected():
                    return {"messages": [message.model_dump(mode="json") for message in pending]}
                await self._wait(min(remaining, options.retry_seconds))

        @router.get("/events")
        async def events(request: Request) -> StreamingResponse:
            credential = bearer(request.headers.get("authorization"))
            agent = await self.service.authenticate_agent(credential)
            await self.service.touch_agent(agent.id, "sse")

            async def stream() -> AsyncIterator[str]:
                delivery = DeliveryBackoff()
                yield ": chiliarch connected\n\n"
                while not await request.is_disconnected():
                    try:
                        # Starlette may cancel a streaming producer using AnyIO's
                        # level cancellation. Finish transaction cleanup before exit.
                        with CancelScope(shield=True):
                            await self.service.authenticate_agent(credential)
                            pending = await self.service.pending(agent.id)
                    except OperationError:
                        yield "event: revoked\ndata: {}\n\n"
                        return
                    options = self.service.settings_manager.current.transport
                    outgoing = delivery.ready(
                        pending,
                        retry_seconds=options.retry_seconds,
                        max_retry_seconds=options.offline_after_seconds,
                    )
                    for message in outgoing:
                        yield (
                            "event: message\nid: "
                            + message.id
                            + "\ndata: "
                            + message.model_dump_json()
                            + "\n\n"
                        )
                    if not outgoing:
                        yield ": heartbeat\n\n"
                    await self._wait(self.service.settings_manager.current.transport.retry_seconds)

            return StreamingResponse(
                stream(),
                media_type="text/event-stream",
                headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
            )

        @router.websocket("/ws")
        async def websocket(socket: WebSocket) -> None:
            credential = bearer(socket.headers.get("authorization"))
            try:
                agent = await self.service.authenticate_agent(credential)
            except OperationError:
                await socket.close(code=1008)
                return
            await socket.accept()
            self.connections.add(socket)
            await self.service.touch_agent(agent.id, "ws")
            send_lock = asyncio.Lock()

            async def send(envelope: dict[str, Any]) -> None:
                async with send_lock:
                    await socket.send_json(envelope)

            async def sender() -> None:
                delivery = DeliveryBackoff()
                while True:
                    await self.service.authenticate_agent(credential)
                    options = self.service.settings_manager.current.transport
                    for message in delivery.ready(
                        await self.service.pending(agent.id),
                        retry_seconds=options.retry_seconds,
                        max_retry_seconds=options.offline_after_seconds,
                    ):
                        await send(message.model_dump(mode="json"))
                    await self._wait(self.service.settings_manager.current.transport.retry_seconds)

            async def receiver() -> None:
                while True:
                    raw = await socket.receive_text()
                    if (
                        len(raw.encode())
                        > self.service.settings_manager.current.transport.max_message_bytes
                    ):
                        raise OperationError("Message exceeds configured size limit", 413)
                    try:
                        envelope = Envelope.model_validate_json(raw)
                    except (ValidationError, json.JSONDecodeError) as exc:
                        raise OperationError("Invalid protocol envelope", 422) from exc
                    result = await self.service.receive(agent.id, envelope)
                    if result["ack"] is not None:
                        await send(result["ack"])

            tasks = [asyncio.create_task(sender()), asyncio.create_task(receiver())]
            try:
                done, _ = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
                for task in done:
                    task.result()
            except (WebSocketDisconnect, RuntimeError):
                pass
            except OperationError as exc:
                logger.info(
                    "Agent connection rejected", extra={"agent_id": agent.id, "error": str(exc)}
                )
                with contextlib.suppress(RuntimeError, WebSocketDisconnect):
                    await socket.close(code=1008, reason=str(exc)[:120])
            finally:
                for task in tasks:
                    task.cancel()
                await asyncio.gather(*tasks, return_exceptions=True)
                self.connections.discard(socket)
                with contextlib.suppress(RuntimeError, WebSocketDisconnect):
                    await socket.close()

        return router
