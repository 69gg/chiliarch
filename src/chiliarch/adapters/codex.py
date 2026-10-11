"""Drive Codex's native app-server without replacing its configuration policy."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

from chiliarch import __version__

JsonObject = dict[str, Any]
Send = Callable[[JsonObject], Awaitable[None]]


class CodexProtocolError(RuntimeError):
    """A bounded diagnostic that never contains native request bodies or errors."""


class CodexProtocol:
    """Translate one app-server turn into the runner's existing output contract.

    The caller owns byte limits, the process lifetime and stdin serialization.
    Notifications may precede the turn/start response; retain those until its
    authoritative turn ID is known. User approval requests are never accepted
    automatically, including when a dispatched task was approved by Chiliarch.
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
        self._mcp_name = mcp_name
        self._mcp = mcp
        self._resume_id = session_id
        self._pending: str | None = None
        self._started = False
        self._thread_id: str | None = None
        self._turn_id: str | None = None
        self._early_notifications: list[JsonObject] = []
        self._usage: JsonObject = {}
        self._failed = False
        self._finished = False

    def _request(self, stage: str, method: str, params: JsonObject) -> JsonObject:
        self._pending = stage
        return {"id": f"chiliarch/{stage}", "method": method, "params": params}

    def initial_request(self) -> JsonObject:
        """Start the one-time native protocol handshake."""
        if self._started:
            raise CodexProtocolError("Native Codex initialization was already started")
        self._started = True
        return self._request(
            "initialize",
            "initialize",
            {"clientInfo": {"name": "chiliarch", "title": "Chiliarch", "version": __version__}},
        )

    def interrupt_request(self) -> JsonObject | None:
        """Request cancellation; its response does not confirm the turn stopped."""
        if self._thread_id is None or self._turn_id is None or self._finished:
            return None
        return {
            "id": "chiliarch/interrupt",
            "method": "turn/interrupt",
            "params": {"threadId": self._thread_id, "turnId": self._turn_id},
        }

    async def accept(self, event: JsonObject, send: Send) -> tuple[list[JsonObject], bool]:
        """Consume a native response/notification and send dependent requests."""
        method = event.get("method")
        if "method" in event:
            if not isinstance(method, str) or not method:
                raise CodexProtocolError("Native Codex emitted an invalid protocol method")
            if "id" in event:
                self._reject_server_request(method)
            if not isinstance(event.get("params"), dict):
                raise CodexProtocolError("Native Codex emitted invalid notification parameters")
            if self._pending == "turn" and method in {
                "item/completed",
                "thread/tokenUsage/updated",
                "error",
                "turn/completed",
            }:
                self._early_notifications.append(event)
                return [], False
            return self._notification(event), self._finished
        if "id" not in event:
            raise CodexProtocolError("Native Codex emitted an invalid protocol response")
        if event["id"] == "chiliarch/interrupt":
            if "error" in event or not isinstance(event.get("result"), dict):
                raise CodexProtocolError("Native Codex could not confirm the interrupt request")
            return [], self._finished
        if self._pending is None or event["id"] != f"chiliarch/{self._pending}":
            raise CodexProtocolError("Native Codex emitted an unexpected protocol response")
        stage = self._pending
        if "error" in event:
            raise CodexProtocolError(f"Native Codex {stage} request failed")
        result = event.get("result")
        if not isinstance(result, dict):
            raise CodexProtocolError("Native Codex emitted an invalid protocol result")
        self._pending = None
        if stage == "initialize":
            await send({"method": "initialized", "params": {}})
            params: JsonObject = {
                "cwd": str(self._cwd),
                "config": {f"mcp_servers.{self._mcp_name}": self._mcp},
            }
            if self._resume_id is not None:
                params["threadId"] = self._resume_id
                params["excludeTurns"] = True
            await send(
                self._request(
                    "thread", "thread/resume" if self._resume_id else "thread/start", params
                )
            )
            return [], False
        if stage == "thread":
            thread = result.get("thread")
            self._thread_id = self._object_id(thread, "thread")
            if self._resume_id is not None and self._thread_id != self._resume_id:
                raise CodexProtocolError("Native Codex resumed a different thread")
            await send(
                self._request(
                    "turn",
                    "turn/start",
                    {
                        "threadId": self._thread_id,
                        "input": [{"type": "text", "text": self._prompt, "text_elements": []}],
                    },
                )
            )
            return [{"type": "thread.started", "thread_id": self._thread_id}], False
        self._turn_id = self._object_id(result.get("turn"), "turn")
        converted: list[JsonObject] = [{"type": "turn.started"}]
        for notification in self._early_notifications:
            converted.extend(self._notification(notification))
        self._early_notifications.clear()
        return converted, self._finished

    @staticmethod
    def _object_id(value: Any, kind: str) -> str:
        identifier = value.get("id") if isinstance(value, dict) else None
        if not isinstance(identifier, str) or not identifier:
            raise CodexProtocolError(f"Native Codex emitted an invalid {kind} identifier")
        return identifier

    @staticmethod
    def _reject_server_request(method: str) -> None:
        if method in {
            "item/commandExecution/requestApproval",
            "item/fileChange/requestApproval",
            "item/permissions/requestApproval",
        }:
            raise CodexProtocolError(
                "Native Codex requires approval from its configured reviewer; "
                "this worker cannot collect interactive native approvals"
            )
        if method in {"item/tool/requestUserInput", "mcpServer/elicitation/request"}:
            raise CodexProtocolError(
                "Native Codex requires interactive user input; "
                "this worker cannot answer native input requests"
            )
        raise CodexProtocolError("Native Codex requires an unsupported client response")

    def _notification(self, event: JsonObject) -> list[JsonObject]:
        if self._finished or self._turn_id is None:
            return []
        method = event["method"]
        if method not in {
            "item/completed",
            "thread/tokenUsage/updated",
            "error",
            "turn/completed",
        }:
            return []
        params = event["params"]
        if params.get("threadId") != self._thread_id:
            return []
        if method == "turn/completed":
            turn = params.get("turn")
            if not isinstance(turn, dict):
                raise CodexProtocolError("Native Codex emitted an invalid completed turn")
            if turn.get("id") != self._turn_id:
                return []
            status = turn.get("status")
            if status not in {"completed", "failed", "interrupted"}:
                raise CodexProtocolError("Native Codex emitted an invalid terminal turn status")
            items = turn.get("items", [])
            if not isinstance(items, list):
                raise CodexProtocolError("Native Codex emitted invalid terminal turn items")
            converted = [self._item(item) for item in items]
            self._finished = True
            if status != "completed" or turn.get("error") is not None or self._failed:
                converted.append({"type": "turn.failed"})
            else:
                converted.append({"type": "turn.completed", "usage": dict(self._usage)})
            return converted
        if params.get("turnId") != self._turn_id:
            return []
        if method == "item/completed":
            return [self._item(params.get("item"))]
        if method == "error":
            if params.get("willRetry") is True:
                return []
            self._failed = True
            return [{"type": "error"}]
        token_usage = params.get("tokenUsage")
        total = token_usage.get("total") if isinstance(token_usage, dict) else None
        if not isinstance(total, dict):
            raise CodexProtocolError("Native Codex emitted invalid token usage")
        # Match codex exec: usage is the thread's cumulative native usage, not
        # tokenUsage.last (which only covers the most recent model completion).
        fields = {
            "inputTokens": "input_tokens",
            "cachedInputTokens": "cached_input_tokens",
            "cacheWriteInputTokens": "cache_write_input_tokens",
            "outputTokens": "output_tokens",
            "reasoningOutputTokens": "reasoning_output_tokens",
        }
        usage: JsonObject = {}
        for source, destination in fields.items():
            if source not in total:
                continue
            count = total[source]
            if not isinstance(count, int) or isinstance(count, bool) or count < 0:
                raise CodexProtocolError("Native Codex emitted invalid token usage")
            usage[destination] = count
        self._usage = usage
        return []

    @staticmethod
    def _item(item: Any) -> JsonObject:
        identifier = CodexProtocol._object_id(item, "item")
        kind = item.get("type")
        if not isinstance(kind, str) or not kind:
            raise CodexProtocolError("Native Codex emitted an invalid item type")
        converted: JsonObject = {"id": identifier, "type": kind}
        if kind == "agentMessage":
            text = item.get("text")
            if not isinstance(text, str):
                raise CodexProtocolError("Native Codex emitted invalid agent message text")
            converted.update(type="agent_message", text=text)
        return {"type": "item.completed", "item": converted}
