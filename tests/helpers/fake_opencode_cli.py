"""Isolated ACP subprocess fixture; never invokes an installed coding agent."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

JsonObject = dict[str, Any]


def emit(event: JsonObject) -> None:
    print(json.dumps({"jsonrpc": "2.0", **event}, ensure_ascii=False), flush=True)


def request(requests: list[JsonObject], methods: tuple[str, ...]) -> JsonObject:
    event: JsonObject = json.loads(sys.stdin.readline())
    assert event["jsonrpc"] == "2.0", event
    assert event["method"] in methods, event
    requests.append(event)
    return event


def publish(observed: JsonObject) -> None:
    path = Path("observed.json.tmp")
    path.write_text(json.dumps(observed, ensure_ascii=False), encoding="utf-8")
    path.replace("observed.json")


def main(fixture: JsonObject) -> None:
    sys.stdin.reconfigure(encoding="utf-8")
    sys.stdout.reconfigure(encoding="utf-8")
    requests: list[JsonObject] = []
    initialize = request(requests, ("initialize",))
    emit(
        {
            "id": initialize["id"],
            "result": {"protocolVersion": 1, "agentCapabilities": {"loadSession": True}},
        }
    )
    session = request(requests, ("session/new", "session/load"))
    session_id = session["params"].get("sessionId", "native-opencode-session")
    if session["method"] == "session/load":
        emit(
            {
                "method": "session/update",
                "params": {
                    "sessionId": session_id,
                    "update": {
                        "sessionUpdate": "agent_message_chunk",
                        "content": {"type": "text", "text": "old answer must not be replayed"},
                    },
                },
            }
        )
    emit(
        {
            "id": session["id"],
            "result": {"sessionId": session_id} if session["method"] == "session/new" else {},
        }
    )
    prompt = request(requests, ("session/prompt",))
    observed: JsonObject = {
        "argv": sys.argv[1:],
        "cwd": os.getcwd(),
        "pid": os.getpid(),
        "requests": requests,
        "stdin": "".join(block["text"] for block in prompt["params"]["prompt"]),
        "environment": {
            name: os.environ[name]
            for name in ("OPENCODE_CONFIG", "OPENCODE_CONFIG_DIR", "XDG_CONFIG_HOME")
            if name in os.environ
        },
    }
    if fixture.get("child"):
        child = subprocess.Popen(
            [
                sys.executable,
                "-c",
                "import signal,time;signal.signal(signal.SIGTERM,signal.SIG_IGN);time.sleep(60)",
            ],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        observed["child_pid"] = child.pid
    publish(observed)
    if fixture.get("stderr_bytes"):
        sys.stderr.write("x" * fixture["stderr_bytes"])
        sys.stderr.flush()
    for update in fixture.get("updates", []):
        emit({"method": "session/update", "params": {"sessionId": session_id, "update": update}})
    if fixture.get("permission"):
        emit(
            {
                "id": "permission-1",
                "method": "session/request_permission",
                "params": {
                    "sessionId": session_id,
                    "toolCall": {"toolCallId": "tool-1", "title": "private diagnostic"},
                    "options": [{"optionId": "allow", "name": "Allow", "kind": "allow_once"}],
                },
            }
        )
        sys.stdin.read()
    elif fixture.get("hang"):
        time.sleep(60)
    elif "error" in fixture:
        emit({"id": prompt["id"], "error": fixture["error"]})
    elif fixture.get("result") is not None:
        emit({"id": prompt["id"], "result": fixture["result"]})
        # An ACP server survives its turn until Chiliarch closes the connection.
        assert sys.stdin.read() == ""
    sys.exit(fixture.get("exitcode", 0))
