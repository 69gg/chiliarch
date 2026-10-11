"""Isolated native subprocess fixture, copied into each test's fake executable."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

JsonObject = dict[str, Any]


def emit(event: Any) -> None:
    print(json.dumps(event), flush=True)


def request(requests: list[JsonObject], method: str) -> JsonObject:
    event: JsonObject = json.loads(sys.stdin.readline())
    assert event["method"] == method, event
    requests.append(event)
    return event


def native_events(events: list[Any], thread_id: str, turn_id: str) -> list[Any]:
    """Translate older fixture events without supplying missing semantic success."""
    converted: list[Any] = []
    for event in events:
        if not isinstance(event, dict) or "type" not in event:
            converted.append(event)
            continue
        kind = event["type"]
        params: JsonObject = {"threadId": thread_id, "turnId": turn_id}
        if kind in {"thread.started", "turn.started"}:
            continue  # These are represented by the handshake responses.
        if kind in {"item.started", "item.completed"}:
            item = dict(event["item"])
            item["type"] = {
                "agent_message": "agentMessage",
                "command_execution": "commandExecution",
            }.get(item.get("type"), item.get("type"))
            params["item"] = item
            converted.append({"method": kind.replace(".", "/"), "params": params})
        elif kind in {"turn.completed", "turn.failed"}:
            if "usage" in event:
                fields = {
                    "input_tokens": "inputTokens",
                    "output_tokens": "outputTokens",
                    "cached_input_tokens": "cachedInputTokens",
                }
                usage = event["usage"]
                total = (
                    {fields.get(key, key): value for key, value in usage.items()}
                    if isinstance(usage, dict)
                    else usage
                )
                converted.append(
                    {
                        "method": "thread/tokenUsage/updated",
                        "params": {**params, "tokenUsage": {"total": total}},
                    }
                )
            turn: JsonObject = {"id": turn_id, "status": kind.removeprefix("turn.")}
            if "error" in event:
                turn["error"] = event["error"]
            converted.append(
                {"method": "turn/completed", "params": {"threadId": thread_id, "turn": turn}}
            )
        elif kind == "error":
            converted.append({"method": "error", "params": {**params, "error": event}})
        else:
            converted.append(event)
    return converted


def main(fixture: JsonObject) -> None:
    sys.stdin.reconfigure(encoding="utf-8")
    arguments = sys.argv[1:]
    observed: JsonObject = {
        "argv": arguments,
        "cwd": os.getcwd(),
        "pid": os.getpid(),
        "environment": {
            key: os.environ[key]
            for key in ("CODEX_HOME", "CLAUDE_CONFIG_DIR", "XDG_CONFIG_HOME")
            if key in os.environ
        },
    }
    events = fixture["events"]
    if arguments == ["app-server"]:
        requests: list[JsonObject] = []
        initialize = request(requests, "initialize")
        emit({"id": initialize["id"], "result": {"userAgent": "isolated-test-cli"}})
        request(requests, "initialized")
        thread: JsonObject = json.loads(sys.stdin.readline())
        assert thread["method"] in {"thread/start", "thread/resume"}, thread
        requests.append(thread)
        thread_id = next(
            (
                item.get("thread_id")
                for item in events
                if isinstance(item, dict) and item.get("type") == "thread.started"
            ),
            thread["params"].get("threadId", "native-codex-session"),
        )
        emit({"id": thread["id"], "result": {"thread": {"id": thread_id}}})
        turn = request(requests, "turn/start")
        observed["requests"] = requests
        observed["stdin"] = "".join(item["text"] for item in turn["params"]["input"])
        turn_id = "native-turn"
        emit({"id": turn["id"], "result": {"turn": {"id": turn_id}}})
        events = native_events(events, thread_id, turn_id)
    else:
        observed["stdin"] = sys.stdin.read()
        if "--mcp-config" in arguments:
            config_path = Path(arguments[arguments.index("--mcp-config") + 1])
            observed["mcp_path"] = str(config_path)
            observed["mcp_config"] = json.loads(config_path.read_text(encoding="utf-8"))
            observed["mcp_mode"] = config_path.stat().st_mode & 0o777
    # Readers use appearance of the file as readiness; publish only complete JSON.
    observation = Path("observed.json.tmp")
    observation.write_text(json.dumps(observed), encoding="utf-8")
    observation.replace("observed.json")
    if fixture["child"]:
        process = subprocess.Popen(
            [
                sys.executable,
                "-c",
                "import signal,time;signal.signal(signal.SIGTERM,signal.SIG_IGN);time.sleep(60)",
            ],
            stdout=subprocess.DEVNULL if fixture["child_has_private_output"] else None,
            stderr=subprocess.DEVNULL if fixture["child_has_private_output"] else None,
        )
        Path("child.pid").write_text(str(process.pid), encoding="utf-8")
    if fixture["stderr_bytes"]:
        sys.stderr.write("x" * fixture["stderr_bytes"])
        sys.stderr.flush()
    for event in events:
        emit(event)
    if fixture["raw"] is not None:
        sys.stdout.buffer.write(fixture["raw"])
        sys.stdout.buffer.flush()
    if fixture["hang"]:
        time.sleep(60)
    elif arguments == ["app-server"] and any(
        isinstance(event, dict) and event.get("method") == "turn/completed" for event in events
    ):
        # A normal app-server survives its turn and exits when its client closes stdin.
        assert sys.stdin.read() == ""
    sys.exit(fixture["exitcode"])
