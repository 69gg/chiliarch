"""Run OpenCode v1 and v2 through their shared, local ACP stdio protocol.

Only the agent's own tools touch the workspace. No client-side filesystem,
terminal, login or approval capabilities are advertised. Session-scoped MCP
servers are added through ACP without rewriting either generation's settings.
"""

from __future__ import annotations

import re
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

from chiliarch import __version__
from chiliarch.adapters.native import NativeInteraction

JsonObject = dict[str, Any]
Send = Callable[[JsonObject], Awaitable[None]]


class OpenCodeProtocolError(RuntimeError):
    """A bounded diagnostic that excludes native errors, settings and secrets."""


class OpenCodeProtocol:
    """Translate one ACP prompt into the runner's normalized turn events.

    Loading an owned session can replay its entire history before returning.
    Ignore those updates and collect only the subsequent prompt's output.
    A correlated prompt response with end_turn is the sole successful terminal
    event; the caller additionally verifies exit status and process-tree cleanup.
    """

    def __init__(
        self,
        prompt: str,
        cwd: Path,
        mcp_name: str,
        mcp: JsonObject,
        session_id: str | None,
    ) -> None:
        self._prompt = prompt
        self._cwd = cwd
        self._mcp = {"name": mcp_name, **mcp, "env": []}
        self._resume_id = session_id
        self._session_id: str | None = None
        self._pending: str | None = None
        self._started = False
        self._finished = False
        self._answer: list[str] = []

    def _request(self, stage: str, method: str, params: JsonObject) -> JsonObject:
        self._pending = stage
        return {
            "jsonrpc": "2.0",
            "id": f"chiliarch/{stage}",
            "method": method,
            "params": params,
        }

    def initial_request(self) -> JsonObject:
        if self._started:
            raise OpenCodeProtocolError("Native OpenCode initialization was already started")
        self._started = True
        return self._request(
            "initialize",
            "initialize",
            {
                "protocolVersion": 1,
                "clientCapabilities": {},
                "clientInfo": {"name": "chiliarch", "version": __version__},
            },
        )

    def interrupt_request(self) -> JsonObject | None:
        if self._session_id is None or self._finished:
            return None
        return {
            "jsonrpc": "2.0",
            "method": "session/cancel",
            "params": {"sessionId": self._session_id},
        }

    async def accept(self, event: JsonObject, send: Send) -> tuple[list[JsonObject], bool]:
        if event.get("jsonrpc") != "2.0":
            raise OpenCodeProtocolError("Native OpenCode emitted invalid JSON-RPC output")
        if "method" in event:
            method = event["method"]
            if not isinstance(method, str) or not method:
                raise OpenCodeProtocolError("Native OpenCode emitted an invalid protocol method")
            params = event.get("params")
            if not isinstance(params, dict):
                raise OpenCodeProtocolError(
                    "Native OpenCode emitted invalid notification parameters"
                )
            if "id" in event:
                await self._reject_request(event, send)
            return self._notification(method, params), self._finished
        if self._pending is None or event.get("id") != f"chiliarch/{self._pending}":
            raise OpenCodeProtocolError("Native OpenCode emitted an unexpected protocol response")
        stage = self._pending
        if "error" in event:
            raise OpenCodeProtocolError(f"Native OpenCode {stage} request failed")
        result = event.get("result")
        if not isinstance(result, dict):
            raise OpenCodeProtocolError("Native OpenCode emitted an invalid protocol result")
        self._pending = None
        if stage == "initialize":
            if type(result.get("protocolVersion")) is not int or result["protocolVersion"] != 1:
                raise OpenCodeProtocolError("Native OpenCode requires an unsupported ACP version")
            capabilities = result.get("agentCapabilities")
            if not isinstance(capabilities, dict):
                raise OpenCodeProtocolError("Native OpenCode did not provide ACP capabilities")
            params = {"cwd": str(self._cwd), "mcpServers": [self._mcp]}
            if self._resume_id is not None:
                if capabilities.get("loadSession") is not True:
                    raise OpenCodeProtocolError("Native OpenCode does not support loading sessions")
                params["sessionId"] = self._resume_id
            await send(
                self._request(
                    "session", "session/load" if self._resume_id else "session/new", params
                )
            )
            return [], False
        if stage == "session":
            # ACP load responses omit sessionId: the correlated request identifies it.
            identifier = result.get("sessionId", self._resume_id)
            if not isinstance(identifier, str) or not re.fullmatch(
                r"[A-Za-z0-9][A-Za-z0-9_-]{0,255}", identifier
            ):
                raise OpenCodeProtocolError("Native OpenCode emitted an invalid session identifier")
            if self._resume_id is not None and identifier != self._resume_id:
                raise OpenCodeProtocolError("Native OpenCode loaded a different session")
            self._session_id = identifier
            await send(
                self._request(
                    "prompt",
                    "session/prompt",
                    {"sessionId": identifier, "prompt": [{"type": "text", "text": self._prompt}]},
                )
            )
            return [
                {"type": "thread.started", "thread_id": identifier},
                {"type": "turn.started"},
            ], False
        self._finished = True
        if result.get("stopReason") != "end_turn":
            return [{"type": "turn.failed"}], True
        usage = result.get("usage", {})
        if not isinstance(usage, dict):
            raise OpenCodeProtocolError("Native OpenCode emitted invalid usage data")
        fields = {
            "inputTokens": "input_tokens",
            "outputTokens": "output_tokens",
            "cachedReadTokens": "cache_read_input_tokens",
            "cachedWriteTokens": "cache_write_input_tokens",
            "thoughtTokens": "reasoning_output_tokens",
        }
        if any(
            type(value) is not int or value < 0 for key, value in usage.items() if key in fields
        ):
            raise OpenCodeProtocolError("Native OpenCode emitted invalid token usage")
        return [
            {
                "type": "item.completed",
                "item": {"id": "answer", "type": "agent_message", "text": "".join(self._answer)},
            },
            {
                "type": "turn.completed",
                "usage": {fields[key]: value for key, value in usage.items() if key in fields},
            },
        ], True

    async def _reject_request(self, event: JsonObject, send: Send) -> None:
        if event["method"] == "session/request_permission":
            await send(
                {
                    "jsonrpc": "2.0",
                    "id": event["id"],
                    "result": {"outcome": {"outcome": "cancelled"}},
                }
            )
            raise OpenCodeProtocolError(
                "Native OpenCode requires interactive approval under its native permissions; "
                "this worker cannot collect native approvals"
            )
        await send(
            {
                "jsonrpc": "2.0",
                "id": event["id"],
                "error": {"code": -32601, "message": "Unsupported client method"},
            }
        )
        raise OpenCodeProtocolError("Native OpenCode requires unsupported interactive client input")

    async def handle_request(
        self, event: JsonObject, send: Send, interact: NativeInteraction
    ) -> None:
        """Bridge ACP v1 permission choices without granting invented permissions."""
        params = event.get("params")
        if (
            event.get("jsonrpc") != "2.0"
            or not isinstance(params, dict)
            or type(event.get("id")) not in {str, int}
        ):
            raise OpenCodeProtocolError("Native OpenCode emitted an invalid interactive request")
        if (
            self._finished
            or self._session_id is None
            or params.get("sessionId") != self._session_id
        ):
            raise OpenCodeProtocolError(
                "Native OpenCode request does not belong to the active session"
            )
        if event.get("method") != "session/request_permission":
            await self._reject_request(event, send)
            return
        options = params.get("options")
        if (
            not isinstance(options, list)
            or not options
            or any(
                not isinstance(option, dict)
                or not isinstance(option.get("optionId"), str)
                or not option["optionId"]
                or not isinstance(option.get("name"), str)
                or option.get("kind")
                not in {"allow_once", "allow_always", "reject_once", "reject_always"}
                for option in options
            )
        ):
            raise OpenCodeProtocolError("Native OpenCode emitted invalid permission choices")
        if len({option["optionId"] for option in options}) != len(options):
            raise OpenCodeProtocolError("Native OpenCode permission choice IDs must be unique")
        answer = await interact(
            {
                "kind": "permission",
                "title": "OpenCode 请求操作许可",
                "native_method": "session/request_permission",
                "details": params,
                "options": [
                    {"id": option["optionId"], "label": option["name"]} for option in options
                ],
            }
        )
        choice = answer.get("choice")
        if choice not in {option["optionId"] for option in options}:
            raise OpenCodeProtocolError("Native OpenCode approval did not select an offered option")
        if not self._finished:
            await send(
                {
                    "jsonrpc": "2.0",
                    "id": event["id"],
                    "result": {"outcome": {"outcome": "selected", "optionId": choice}},
                }
            )

    def _notification(self, method: str, params: JsonObject) -> list[JsonObject]:
        if method != "session/update" or self._pending != "prompt" or self._finished:
            return []
        if params.get("sessionId") != self._session_id:
            return []
        update = params.get("update")
        if not isinstance(update, dict):
            raise OpenCodeProtocolError("Native OpenCode emitted an invalid session update")
        # Child-session reports and provider thoughts are not the parent's answer.
        meta = update.get("_meta")
        child = isinstance(meta, dict) and "opencode/child-session" in meta
        if update.get("sessionUpdate") == "agent_message_chunk" and not child:
            content = update.get("content")
            if not isinstance(content, dict):
                raise OpenCodeProtocolError("Native OpenCode emitted invalid message content")
            if content.get("type") == "text":
                if not isinstance(content.get("text"), str):
                    raise OpenCodeProtocolError("Native OpenCode emitted invalid message text")
                self._answer.append(content["text"])
            return [{"type": "item.completed", "item": {"type": "message_progress"}}]
        if update.get("sessionUpdate") in {"tool_call", "tool_call_update"}:
            return [{"type": "item.completed", "item": {"type": "tool_progress"}}]
        return []
