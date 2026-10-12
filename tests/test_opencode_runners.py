"""Exercise both OpenCode adapters through isolated, real ACP subprocesses."""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import signal
import sys
from pathlib import Path
from typing import Any, Literal

import pytest

from chiliarch.adapters.runners import RunnerError, RunnerHandler, RunnerSettings, create_handler
from tests.test_agent_runners import process_running
from tests.test_runner_continuations import client_for, observed, run, task

JsonObject = dict[str, Any]
Kind = Literal["opencode", "opencode-v1"]


def fake_cli(directory: Path, **overrides: Any) -> Path:
    path = directory / ("fake opencode 中文.py" if os.name == "nt" else "fake-opencode")
    fixture = {
        "updates": [
            {"sessionUpdate": "agent_message_chunk", "content": {"type": "text", "text": "检查"}},
            {
                "sessionUpdate": "agent_thought_chunk",
                "content": {"type": "text", "text": "private thought"},
            },
            {
                "sessionUpdate": "tool_call",
                "toolCallId": "tool-1",
                "title": "Inspect",
                "status": "in_progress",
                "kind": "read",
            },
            {
                "sessionUpdate": "agent_message_chunk",
                "content": {"type": "text", "text": "完成。"},
            },
        ],
        "result": {
            "stopReason": "end_turn",
            "usage": {"inputTokens": 20, "outputTokens": 10, "cachedReadTokens": 3},
        },
        **overrides,
    }
    source = (Path(__file__).parent / "helpers" / "fake_opencode_cli.py").read_text(
        encoding="utf-8"
    )
    path.write_text(f"#!{sys.executable}\n{source}\nmain({fixture!r})\n", encoding="utf-8")
    path.chmod(0o755)
    return path


def settings(directory: Path, kind: Kind, **fixture: Any) -> RunnerSettings:
    return RunnerSettings(
        kind=kind,
        workdir=directory,
        executable=str(fake_cli(directory, **fixture)),
        terminate_grace_seconds=0.05,
    )


async def no_progress(_: float, __: str) -> None:
    pass


@pytest.mark.parametrize("kind", ["opencode", "opencode-v1"])
async def test_native_task_preserves_configuration_and_injects_only_its_mailbox(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, kind: Kind
) -> None:
    config = tmp_path / "native-config.json"
    contents = json.dumps(
        {
            "model": "configured-provider/model",
            "permission": {"edit": "ask"},
            "mcp": {"native-tool": {"type": "local", "command": ["configured-tool"]}},
        }
    )
    config.write_text(contents, encoding="utf-8")
    monkeypatch.setenv("OPENCODE_CONFIG", str(config))
    monkeypatch.setenv("OPENCODE_CONFIG_DIR", str(tmp_path))
    configuration = settings(tmp_path, kind, stderr_bytes=100000)
    state_path = tmp_path / "state.sqlite"
    progress: list[tuple[float, str]] = []

    async def report(value: float, message: str) -> None:
        progress.append((value, message))

    instruction = f"--model remote-model; $(touch {tmp_path / 'must-not-exist'})"
    result = await create_handler(configuration, state_path)(
        {
            "task_id": "task-1",
            "instruction": instruction,
            "parameters": {
                "model": "remote-model",
                "executable": "untrusted-binary",
                "api_key": "private-credential",
            },
        },
        report,
    )
    execution = observed(tmp_path)
    assert execution["argv"] == (
        ["acp", "--hostname", "127.0.0.1", "--port", "0", "--mdns=false"]
        if kind == "opencode-v1"
        else ["acp"]
    )
    assert execution["environment"]["OPENCODE_CONFIG"] == str(config)
    assert execution["environment"]["OPENCODE_CONFIG_DIR"] == str(tmp_path)
    assert config.read_text(encoding="utf-8") == contents
    assert instruction in execution["stdin"]
    assert instruction not in execution["argv"]
    task_parameters = json.loads(execution["stdin"].split("\n\nTask parameters:\n", 1)[1])
    assert task_parameters == {
        "model": "remote-model",
        "executable": "untrusted-binary",
        "api_key": "private-credential",
    }
    assert not (tmp_path / "must-not-exist").exists()
    session = execution["requests"][1]
    assert session["method"] == "session/new"
    assert set(session["params"]) == {"cwd", "mcpServers"}
    assert session["params"]["cwd"] == str(tmp_path)
    servers = session["params"]["mcpServers"]
    assert len(servers) == 1
    mcp = servers[0]
    assert mcp["name"].startswith("chiliarch_")
    assert f"Use the {mcp['name']} MCP server's tools" in execution["stdin"]
    assert mcp["command"] == sys.executable
    assert mcp["args"] == [
        "-m",
        "chiliarch",
        "agent",
        "mcp",
        "--state",
        str(state_path),
        "--mode",
        "mailbox",
        "--managed-task",
        "task-1",
    ]
    assert mcp["env"] == []
    assert result["adapter"] == kind
    assert result["answer"] == "检查完成。"
    assert result["session_id"] == "native-opencode-session"
    assert result["usage"] == {
        "input_tokens": 20,
        "output_tokens": 10,
        "cache_read_input_tokens": 3,
    }
    assert progress and all(value == 0.0 for value, _ in progress)
    assert "private thought" not in str(progress)


@pytest.mark.parametrize("kind", ["opencode", "opencode-v1"])
async def test_task_workdir_changes_process_and_acp_cwd_but_not_default(
    tmp_path: Path, kind: Kind
) -> None:
    other = tmp_path / "other workspace 中文"
    other.mkdir()
    configuration = settings(tmp_path, kind)
    handler = create_handler(configuration, tmp_path / "state.db")
    result = await handler(
        {"task_id": "override", "instruction": "inspect", "workdir": str(other)}, no_progress
    )
    assert result["workdir"] == str(other)
    assert observed(other)["cwd"] == str(other)
    assert observed(other)["requests"][1]["params"]["cwd"] == str(other)
    default = await handler({"task_id": "default", "instruction": "inspect"}, no_progress)
    assert default["workdir"] == str(tmp_path)
    assert observed(tmp_path)["cwd"] == str(tmp_path)
    assert configuration.workdir == tmp_path


@pytest.mark.parametrize("kind", ["opencode", "opencode-v1"])
@pytest.mark.parametrize(
    "fixture",
    [
        {"result": None},
        {"result": {"stopReason": "cancelled"}},
        {"result": {"stopReason": "max_tokens"}},
        {"error": {"code": -32000, "message": "private-credential"}},
        {"permission": True},
        {"exitcode": 2},
    ],
)
async def test_output_or_zero_exit_cannot_replace_native_success(
    tmp_path: Path, kind: Kind, fixture: JsonObject
) -> None:
    configuration = settings(tmp_path, kind, **fixture)
    async with asyncio.timeout(5):
        with pytest.raises(RunnerError) as failure:
            await create_handler(configuration, tmp_path / "state.db")(
                {"task_id": "task", "instruction": "inspect"}, no_progress
            )
    assert "private-credential" not in str(failure.value)
    if fixture.get("permission"):
        assert "approval" in str(failure.value)
    if fixture.get("exitcode"):
        assert "exit code 2" in str(failure.value)
    execution = observed(tmp_path)
    assert not process_running(execution["pid"], expected_creation_time=execution["creation_time"])


@pytest.mark.skipif(os.name != "nt", reason="Windows process identity requires native Windows")
async def test_windows_exit_probe_checks_process_identity_instead_of_reused_pid() -> None:
    source = (Path(__file__).parent / "helpers" / "fake_opencode_cli.py").read_text(
        encoding="utf-8"
    )
    program = (
        source
        + '\nprint(json.dumps({"pid": os.getpid(), "creation_time": process_creation_time()}), '
        "flush=True)\nsys.stdin.read()\n"
    )
    workers: list[asyncio.subprocess.Process] = []
    identities: list[JsonObject] = []
    try:
        for _ in range(2):
            process = await asyncio.create_subprocess_exec(
                sys.executable,
                "-u",
                "-c",
                program,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
            )
            workers.append(process)
            assert process.stdout is not None
            identities.append(json.loads(await asyncio.wait_for(process.stdout.readline(), 5)))
        first, second = identities
        assert process_running(first["pid"], expected_creation_time=first["creation_time"])
        assert process_running(second["pid"], expected_creation_time=second["creation_time"])
        # A stale identity must not report another live process as the original
        # worker. This checks the PID-reuse boundary without waiting for OS reuse.
        assert not process_running(second["pid"], expected_creation_time=first["creation_time"])
        assert workers[0].stdin is not None
        workers[0].stdin.close()
        await asyncio.wait_for(workers[0].wait(), 5)
        assert not process_running(first["pid"], expected_creation_time=first["creation_time"])
        assert process_running(second["pid"], expected_creation_time=second["creation_time"])
    finally:
        for process in workers:
            if process.returncode is None:
                process.kill()
            await process.communicate()


@pytest.mark.parametrize("kind", ["opencode", "opencode-v1"])
async def test_continuation_rebinds_mailbox_and_excludes_history_after_restart(
    tmp_path: Path, kind: Kind
) -> None:
    configuration = settings(tmp_path, kind)
    initial = await client_for(tmp_path, configuration)
    try:
        first = await run(initial, task("first"))
        assert first["status"] == "completed"
        original = observed(tmp_path)["requests"][1]["params"]["mcpServers"][0]
    finally:
        await initial.store.close()
    restarted = await client_for(tmp_path, configuration)
    try:
        followup = await run(restarted, task("second", "first"))
        assert followup["status"] == "completed"
        assert followup["result"]["answer"] == "检查完成。"
        assert followup["result"]["session_id"] == first["result"]["session_id"]
        assert followup["result"]["continued_task_id"] == "first"
        load = observed(tmp_path)["requests"][1]
        assert load["method"] == "session/load"
        assert load["params"]["sessionId"] == first["result"]["session_id"]
        mcp = load["params"]["mcpServers"][0]
        assert mcp["name"] != original["name"]
        assert mcp["args"][-2:] == ["--managed-task", "second"]
        before = observed(tmp_path)
        stale = await run(restarted, task("stale", "first"))
        assert stale["status"] == "failed"
        assert observed(tmp_path) == before
        assert (await run(restarted, task("third", "second")))["status"] == "completed"
    finally:
        await restarted.store.close()


@pytest.mark.parametrize("kind", ["opencode", "opencode-v1"])
async def test_continuation_cannot_cross_identity_or_failed_turn(
    tmp_path: Path, kind: Kind
) -> None:
    configuration = settings(tmp_path, kind)
    client = await client_for(tmp_path, configuration)
    try:
        assert (await run(client, task("first")))["status"] == "completed"
        before = observed(tmp_path)
        await client.store.save_identity({"agent_id": "another-agent"})
        assert (await run(client, task("forbidden", "first")))["status"] == "failed"
        assert observed(tmp_path) == before
        await client.store.save_identity({"agent_id": "managed-agent"})
        fake_cli(tmp_path, result={"stopReason": "refusal"})
        assert (await run(client, task("interrupted", "first")))["status"] == "failed"
        before = observed(tmp_path)
        fake_cli(tmp_path)
        for parent in ("first", "interrupted"):
            assert (await run(client, task(f"retry-{parent}", parent)))["status"] == "failed"
            assert observed(tmp_path) == before
    finally:
        await client.store.close()


@pytest.mark.parametrize("kind", ["opencode", "opencode-v1"])
async def test_cancellation_stops_worker_and_descendant_before_releasing_workspace(
    tmp_path: Path, kind: Kind
) -> None:
    configuration = settings(tmp_path, kind, hang=True, child=True)
    handler = RunnerHandler(configuration, tmp_path / "state.db")
    running = asyncio.create_task(
        handler({"task_id": "cancelled", "instruction": "inspect"}, no_progress)
    )
    child_pid = 0
    try:
        async with asyncio.timeout(10):
            while not (tmp_path / "observed.json").exists():
                if running.done():
                    running.result()
                await asyncio.sleep(0.01)
        execution = observed(tmp_path)
        child_pid = execution["child_pid"]
        assert process_running(child_pid)
        running.cancel()
        await asyncio.sleep(0.01)
        running.cancel()
        with pytest.raises(asyncio.CancelledError):
            await running
        async with asyncio.timeout(5):
            for _ in range(200):
                if not process_running(execution["pid"]) and not process_running(child_pid):
                    break
                await asyncio.sleep(0.02)
            else:
                pytest.fail("OpenCode worker or descendant survived cancellation")
        # A second process in the same directory proves the lease was released.
        fake_cli(tmp_path)
        result = await handler({"task_id": "next", "instruction": "inspect"}, no_progress)
        assert result["answer"] == "检查完成。"
    finally:
        running.cancel()
        await asyncio.gather(running, return_exceptions=True)
        if child_pid and process_running(child_pid):
            with contextlib.suppress(ProcessLookupError):
                os.kill(child_pid, signal.SIGTERM if os.name == "nt" else signal.SIGKILL)


@pytest.mark.parametrize("kind", ["opencode", "opencode-v1"])
async def test_untrusted_raw_session_id_cannot_resume_native_history(
    tmp_path: Path, kind: Kind
) -> None:
    configuration = settings(tmp_path, kind)
    result = await create_handler(configuration, tmp_path / "state.db")(
        {
            "task_id": "fresh",
            "instruction": "inspect",
            "parameters": {"session_id": "someone-elses-session", "resume": True},
        },
        no_progress,
    )
    assert result["answer"] == "检查完成。"
    request = observed(tmp_path)["requests"][1]
    assert request["method"] == "session/new"
    assert "sessionId" not in request["params"]
