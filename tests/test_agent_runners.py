"""CLI adapter contracts exercised with real, isolated fake subprocesses."""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import signal
import sys
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from chiliarch.adapters.guidance import INBOX_POLLING_GUIDANCE
from chiliarch.adapters.runners import RunnerError, RunnerHandler, RunnerSettings, create_handler
from chiliarch.client import AgentClient
from chiliarch.protocol import Envelope

JsonObject = dict[str, Any]


def fake_cli(
    directory: Path,
    events: list[Any],
    *,
    exitcode: int = 0,
    stderr_bytes: int = 0,
    raw: bytes | None = None,
    hang: bool = False,
    child: bool = False,
    child_has_private_output: bool = False,
) -> Path:
    """Create a fake CLI, not a wrapper invoking a user's installed agent."""
    path = directory / ("fake cli 中文.py" if os.name == "nt" else "fake-cli")
    fixture: JsonObject = {
        "events": events,
        "exitcode": exitcode,
        "stderr_bytes": stderr_bytes,
        "raw": raw,
        "hang": hang,
        "child": child,
        "child_has_private_output": child_has_private_output,
    }
    script = (Path(__file__).parent / "helpers" / "fake_native_cli.py").read_text(encoding="utf-8")
    path.write_text(f"#!{sys.executable}\n" + script + f"\nmain({fixture!r})\n", encoding="utf-8")
    path.chmod(0o755)
    return path


def codex_success() -> list[JsonObject]:
    return [
        {"type": "thread.started", "thread_id": "native-codex-session"},
        {"type": "turn.started"},
        {"type": "item.started", "item": {"id": "tool", "type": "command_execution"}},
        {
            "type": "item.completed",
            "item": {
                "id": "answer",
                "type": "agent_message",
                "text": "检查完成，两个任务正在运行。",
            },
        },
        {
            "type": "turn.completed",
            "usage": {"input_tokens": 20, "output_tokens": 10, "cached_input_tokens": 3},
        },
    ]


def claude_success() -> list[JsonObject]:
    return [
        {"type": "system", "subtype": "init", "session_id": "native-claude-session"},
        {
            "type": "stream_event",
            "event": {
                "type": "content_block_delta",
                "delta": {"type": "text_delta", "text": "partial"},
            },
        },
        {"type": "assistant", "message": {"content": [{"type": "text", "text": "partial"}]}},
        {
            "type": "result",
            "subtype": "success",
            "is_error": False,
            "session_id": "native-claude-session",
            "result": "最终结果，而不是重复 partial。",
            "usage": {"input_tokens": 25, "output_tokens": 12, "cache_read_input_tokens": 4},
            "total_cost_usd": 0.05,
            "permission_denials": [],
        },
    ]


async def progress_recorder(records: list[tuple[float, str]], value: float, message: str) -> None:
    records.append((value, message))


@pytest.mark.parametrize("kind", ["codex", "claude-code"])
async def test_native_success_inherits_policy_and_receives_stdin_only_task(
    tmp_path: Path, kind: str
) -> None:
    workspace = tmp_path / "fixed workspace"
    workspace.mkdir()
    events = codex_success() if kind == "codex" else claude_success()
    binary = fake_cli(tmp_path, events, stderr_bytes=100000)
    state_path = tmp_path / "identity.sqlite"
    settings = RunnerSettings.model_validate(
        {
            "kind": kind,
            "workdir": workspace,
            "executable": str(binary),
            "model": "locally-selected-model",
            "codex_sandbox": "read-only",
            "codex_skip_git_repo_check": True,
            "claude_permission_mode": "plan",
            "claude_allowed_tools": ["Read"],
            "terminate_grace_seconds": 0.05,
        }
    )
    handler = create_handler(settings, state_path)
    records: list[tuple[float, str]] = []

    async def progress(value: float, message: str) -> None:
        await progress_recorder(records, value, message)

    instruction = f"--sandbox danger-full-access; $(touch {tmp_path / 'should-not-exist'})"
    result = await handler(
        {
            "task_id": "platform-task",
            "instruction": instruction,
            "parameters": {
                "cwd": str(tmp_path),
                "executable": "untrusted-launcher",
                "model": "remote-model",
                "arguments": ["--dangerously-skip-permissions"],
                "api_key": "fixture-secret-must-not-enter-prompt",
            },
        },
        progress,
    )
    observed = json.loads((workspace / "observed.json").read_text())
    args = observed["argv"]
    assert observed["cwd"] == str(workspace)
    assert instruction in observed["stdin"]
    assert INBOX_POLLING_GUIDANCE in observed["stdin"]
    assert instruction not in args
    assert "untrusted-launcher" not in args
    assert "remote-model" not in args
    assert "fixture-secret-must-not-enter-prompt" not in observed["stdin"]
    assert not (tmp_path / "should-not-exist").exists()
    assert "locally-selected-model" not in str(observed)
    assert result["adapter"] == kind
    assert result["session_id"] == f"native-{'codex' if kind == 'codex' else 'claude'}-session"
    assert result["usage"]["input_tokens"] in {20, 25}
    assert records and all(value == 0.0 for value, _ in records)
    assert "fixture-secret" not in str(records)
    expected_mcp_args = [
        "-m",
        "chiliarch",
        "agent",
        "mcp",
        "--state",
        str(state_path),
        "--mode",
        "mailbox",
        "--managed-task",
        "platform-task",
    ]
    if kind == "codex":
        assert args == ["app-server"]
        requests = observed["requests"]
        assert [request["method"] for request in requests] == [
            "initialize",
            "initialized",
            "thread/start",
            "turn/start",
        ]
        thread = requests[2]["params"]
        assert set(thread) == {"cwd", "config"}
        assert thread["cwd"] == str(workspace)
        assert len(thread["config"]) == 1
        config_key, mcp = next(iter(thread["config"].items()))
        assert config_key.startswith("mcp_servers.chiliarch_")
        mcp_name = config_key.removeprefix("mcp_servers.")
        assert f"Use the {mcp_name} MCP server's tools" in observed["stdin"]
        assert set(requests[3]["params"]) == {"threadId", "input"}
        assert requests[3]["params"]["input"] == [
            {"type": "text", "text": observed["stdin"], "text_elements": []}
        ]
        assert mcp["command"] == sys.executable
        assert mcp["args"] == expected_mcp_args
        assert set(mcp) == {"command", "args", "enabled", "required"}
        assert mcp["enabled"] is True
        assert mcp["required"] is True
        assert result["answer"] == "检查完成，两个任务正在运行。"
    else:
        assert "--print" in args
        assert "--permission-mode" not in args
        assert "--strict-mcp-config" not in args
        assert "--verbose" in args
        assert "--include-partial-messages" in args
        assert not any(arg.startswith(("--allowedTools", "--model")) for arg in args)
        assert "--bare" not in args
        if os.name == "posix":
            assert observed["mcp_mode"] == 0o600
        servers = observed["mcp_config"]["mcpServers"]
        assert len(servers) == 1
        mcp_name, mcp = next(iter(servers.items()))
        assert mcp_name.startswith("chiliarch_")
        assert f"Use the {mcp_name} MCP server's tools" in observed["stdin"]
        assert mcp["command"] == sys.executable
        assert mcp["args"] == expected_mcp_args
        assert not await asyncio.to_thread(Path(observed["mcp_path"]).exists)
        assert result["answer"] == "最终结果，而不是重复 partial。"
        assert result["total_cost_usd"] == 0.05
        assert result["native_permission_denials"] == 0


async def test_codex_success_does_not_require_optional_token_usage(tmp_path: Path) -> None:
    binary = fake_cli(tmp_path, [{"type": "turn.completed"}])
    settings = RunnerSettings(kind="codex", workdir=tmp_path, executable=str(binary))

    async def progress(_: float, __: str) -> None:
        pass

    result = await create_handler(settings, tmp_path / "state.db")(
        {"task_id": "task", "instruction": "inspect"}, progress
    )
    assert result["session_id"] == "native-codex-session"
    assert result["usage"] == {}


@pytest.mark.parametrize("kind", ["codex", "claude-code"])
async def test_each_task_adds_its_own_mcp_without_replacing_native_configuration(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, kind: str
) -> None:
    config_directory = tmp_path / "native config"
    config_directory.mkdir()
    configuration = config_directory / "fixture-settings.json"
    contents = json.dumps(
        {
            "model": "native-selected-model",
            "permissions": {"existing-rule": "preserve"},
            "mcpServers": {
                "chiliarch": {"command": "existing-chiliarch"},
                "existing-server": {"command": "existing-tool"},
            },
        }
    )
    configuration.write_text(contents, encoding="utf-8")
    for variable in ("CODEX_HOME", "CLAUDE_CONFIG_DIR", "XDG_CONFIG_HOME"):
        monkeypatch.setenv(variable, str(config_directory))
    events = codex_success() if kind == "codex" else claude_success()
    binary = fake_cli(tmp_path, events)
    settings = RunnerSettings.model_validate(
        {"kind": kind, "workdir": tmp_path, "executable": str(binary)}
    )
    handler = create_handler(settings, tmp_path / "state.db")
    names: list[str] = []

    async def progress(_: float, __: str) -> None:
        pass

    for task_id in ("first", "second"):
        await handler({"task_id": task_id, "instruction": "inspect"}, progress)
        observed = json.loads((tmp_path / "observed.json").read_text(encoding="utf-8"))
        assert set(observed["environment"].values()) == {str(config_directory)}
        if kind == "codex":
            params = observed["requests"][2]["params"]
            assert set(params) == {"cwd", "config"}
            # A dotted per-server overlay leaves the native mcp_servers table intact.
            assert len(params["config"]) == 1
            key, server = next(iter(params["config"].items()))
            assert key.startswith("mcp_servers.chiliarch_")
            name = key.removeprefix("mcp_servers.")
        else:
            assert "--strict-mcp-config" not in observed["argv"]
            assert "--settings" not in observed["argv"]
            assert len(observed["mcp_config"]["mcpServers"]) == 1
            name, server = next(iter(observed["mcp_config"]["mcpServers"].items()))
        assert name.startswith("chiliarch_")
        assert name not in {"chiliarch", "existing-server"}
        assert server["args"][-2:] == ["--managed-task", task_id]
        assert f"Use the {name} MCP server's tools" in observed["stdin"]
        names.append(name)
        assert configuration.read_text(encoding="utf-8") == contents
    assert names[0] != names[1]


@pytest.mark.parametrize(
    "kind,events",
    [
        ("codex", [{"type": "turn.failed", "error": {"message": "fixture-secret"}}]),
        ("codex", [{"type": "error", "message": "fixture-secret"}, *codex_success()]),
        (
            "codex",
            [{"type": "item.completed", "item": {"type": "agent_message", "text": "partial"}}],
        ),
        (
            "claude-code",
            [
                {
                    "type": "result",
                    "subtype": "error_during_execution",
                    "is_error": True,
                    "errors": ["fixture-secret"],
                }
            ],
        ),
        ("claude-code", [{"type": "result", "subtype": "success", "is_error": True}]),
        ("claude-code", [{"type": "error", "message": "fixture-secret"}, *claude_success()]),
        ("claude-code", [{"type": "result", "subtype": "success"}]),
        (
            "claude-code",
            [{"type": "assistant", "message": {"content": [{"type": "text", "text": "partial"}]}}],
        ),
    ],
)
async def test_zero_exit_does_not_replace_a_successful_semantic_terminal_event(
    tmp_path: Path, kind: str, events: list[JsonObject]
) -> None:
    binary = fake_cli(tmp_path, events, raw=b"\n")
    settings = RunnerSettings.model_validate(
        {"kind": kind, "workdir": tmp_path, "executable": str(binary)}
    )

    async def progress(_: float, __: str) -> None:
        pass

    with pytest.raises(RunnerError) as error:
        await create_handler(settings, tmp_path / "state.db")(
            {"task_id": "task", "instruction": "inspect"}, progress
        )
    assert "fixture-secret" not in str(error.value)


@pytest.mark.parametrize("exitcode", [1, 2, 143])
async def test_nonzero_exit_overrides_successful_output(tmp_path: Path, exitcode: int) -> None:
    binary = fake_cli(tmp_path, codex_success(), exitcode=exitcode)
    settings = RunnerSettings(kind="codex", workdir=tmp_path, executable=str(binary))

    async def progress(_: float, __: str) -> None:
        pass

    with pytest.raises(RunnerError, match=f"exit code {exitcode}"):
        await create_handler(settings, tmp_path / "state.db")(
            {"task_id": "task", "instruction": "inspect"}, progress
        )


@pytest.mark.parametrize(
    "raw,limit,error",
    [
        (b"not-json secret-content\n", 4096, "invalid JSONL"),
        (b"[]\n", 4096, "invalid JSONL event"),
        (b'{"x":1}\n', 4096, "invalid protocol response"),
        (b"x" * 5000, 4096, "line size limit"),
        (b'{"type":"unrecognized","data":"' + b"x" * 3000 + b'"}\n', 2048, "line size limit"),
    ],
)
async def test_invalid_and_oversized_jsonl_fails_without_exposing_output(
    tmp_path: Path, raw: bytes, limit: int, error: str
) -> None:
    binary = fake_cli(tmp_path, [], raw=raw)
    settings = RunnerSettings(
        kind="codex",
        workdir=tmp_path,
        executable=str(binary),
        max_line_bytes=limit,
        terminate_grace_seconds=0.05,
    )

    async def progress(_: float, __: str) -> None:
        pass

    async with asyncio.timeout(3):
        with pytest.raises(RunnerError, match=error) as captured:
            await create_handler(settings, tmp_path / "state.db")(
                {"task_id": "task", "instruction": "inspect"}, progress
            )
    assert "secret-content" not in str(captured.value)


async def test_stderr_is_drained_and_counted_against_output_limit(tmp_path: Path) -> None:
    binary = fake_cli(tmp_path, codex_success(), stderr_bytes=200000, hang=True)
    settings = RunnerSettings(
        kind="codex",
        workdir=tmp_path,
        executable=str(binary),
        max_output_bytes=65536,
        max_line_bytes=4096,
        terminate_grace_seconds=0.05,
    )

    async def progress(_: float, __: str) -> None:
        pass

    async with asyncio.timeout(3):
        with pytest.raises(RunnerError, match="output size limit"):
            await create_handler(settings, tmp_path / "state.db")(
                {"task_id": "task", "instruction": "inspect"}, progress
            )


def process_running(pid: int, *, expected_creation_time: int | None = None) -> bool:
    """Linux orphan zombies are stopped even before PID 1 reaps them."""
    if os.name == "nt":
        import ctypes
        from ctypes import wintypes

        windows_ctypes: Any = ctypes
        kernel: Any = windows_ctypes.WinDLL("kernel32", use_last_error=True)
        kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        kernel.OpenProcess.restype = wintypes.HANDLE
        kernel.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
        kernel.WaitForSingleObject.restype = wintypes.DWORD
        kernel.CloseHandle.argtypes = [wintypes.HANDLE]
        access = 0x00100000  # SYNCHRONIZE
        if expected_creation_time is not None:
            access |= 0x1000  # PROCESS_QUERY_LIMITED_INFORMATION
        handle = kernel.OpenProcess(access, False, pid)
        if not handle:
            code = windows_ctypes.get_last_error()
            if code == 87:  # ERROR_INVALID_PARAMETER: no process currently has this PID.
                return False
            raise windows_ctypes.WinError(code)
        try:
            if expected_creation_time is not None:
                kernel.GetProcessTimes.argtypes = [wintypes.HANDLE] + [
                    ctypes.POINTER(wintypes.FILETIME)
                ] * 4
                kernel.GetProcessTimes.restype = wintypes.BOOL
                creation, exit_time, kernel_time, user_time = (
                    wintypes.FILETIME() for _ in range(4)
                )
                if not kernel.GetProcessTimes(
                    handle,
                    ctypes.byref(creation),
                    ctypes.byref(exit_time),
                    ctypes.byref(kernel_time),
                    ctypes.byref(user_time),
                ):
                    raise windows_ctypes.WinError(windows_ctypes.get_last_error())
                actual_creation_time = (creation.dwHighDateTime << 32) | creation.dwLowDateTime
                if actual_creation_time != expected_creation_time:
                    return False  # Another worker may already have reused the released PID.
            result = kernel.WaitForSingleObject(handle, 0)
            if result == 0:  # WAIT_OBJECT_0
                return False
            if result == 258:  # WAIT_TIMEOUT
                return True
            raise windows_ctypes.WinError(windows_ctypes.get_last_error())
        finally:
            kernel.CloseHandle(handle)
    status = Path(f"/proc/{pid}/stat")
    if status.exists() and status.read_text().rsplit(")", 1)[1].strip().split()[0] == "Z":
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    return True


@pytest.mark.parametrize("cancel", [False, True])
async def test_timeout_and_cancellation_stop_worker_and_term_ignoring_descendant(
    tmp_path: Path, cancel: bool
) -> None:
    binary = fake_cli(
        tmp_path, [{"type": "thread.started", "thread_id": "running"}], hang=True, child=True
    )
    settings = RunnerSettings(
        kind="codex",
        workdir=tmp_path,
        executable=str(binary),
        timeout_seconds=5 if cancel else (1 if os.name == "nt" else 0.3),
        terminate_grace_seconds=0.05,
    )

    async def progress(_: float, __: str) -> None:
        pass

    task = asyncio.create_task(
        create_handler(settings, tmp_path / "state.db")(
            {"task_id": "task", "instruction": "inspect"}, progress
        )
    )
    child_pid = 0
    try:
        async with asyncio.timeout(4):
            for _ in range(400):
                if (tmp_path / "child.pid").exists():
                    break
                await asyncio.sleep(0.01)
            else:
                pytest.fail("Fake worker did not start its descendant")
        child_pid = int((tmp_path / "child.pid").read_text())
        parent_pid = int(json.loads((tmp_path / "observed.json").read_text())["pid"])
        if cancel:
            task.cancel()
            await asyncio.sleep(0.01)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
        else:
            with pytest.raises(RunnerError, match="timeout"):
                await task
        async with asyncio.timeout(2):
            for _ in range(100):
                if not process_running(parent_pid) and not process_running(child_pid):
                    break
                await asyncio.sleep(0.02)
            else:
                pytest.fail("Worker or descendant remained alive after cancellation")
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        if child_pid and process_running(child_pid):
            with contextlib.suppress(ProcessLookupError):
                os.kill(child_pid, signal.SIGTERM if os.name == "nt" else signal.SIGKILL)


async def test_parent_exit_does_not_leave_background_descendant(tmp_path: Path) -> None:
    binary = fake_cli(tmp_path, codex_success(), child=True)
    settings = RunnerSettings(
        kind="codex",
        workdir=tmp_path,
        executable=str(binary),
        timeout_seconds=1 if os.name == "nt" else 0.4,
        terminate_grace_seconds=0.05,
    )

    async def progress(_: float, __: str) -> None:
        pass

    # The background child inherits the stdout/stderr pipes, so successful JSON
    # alone cannot complete the task until its entire native process lifetime ends.
    with pytest.raises(RunnerError, match="timeout"):
        await create_handler(settings, tmp_path / "state.db")(
            {"task_id": "task", "instruction": "inspect"}, progress
        )
    child_pid = int((tmp_path / "child.pid").read_text())
    assert not process_running(child_pid)


async def test_success_cleanup_stops_descendant_without_inherited_output(tmp_path: Path) -> None:
    binary = fake_cli(tmp_path, codex_success(), child=True, child_has_private_output=True)
    settings = RunnerSettings(
        kind="codex",
        workdir=tmp_path,
        executable=str(binary),
        terminate_grace_seconds=0.05,
    )

    async def progress(_: float, __: str) -> None:
        pass

    result = await create_handler(settings, tmp_path / "state.db")(
        {"task_id": "task", "instruction": "inspect"}, progress
    )
    assert result["answer"] == "检查完成，两个任务正在运行。"
    child_pid = int((tmp_path / "child.pid").read_text())
    assert not process_running(child_pid)


async def test_prompt_validation_prevents_any_process_start(tmp_path: Path) -> None:
    binary = fake_cli(tmp_path, codex_success())
    settings = RunnerSettings(
        kind="codex", workdir=tmp_path, executable=str(binary), max_input_bytes=1024
    )
    handler = RunnerHandler(settings, tmp_path / "state.db")

    async def progress(_: float, __: str) -> None:
        pass

    for payload in [
        {"task_id": "task", "instruction": ""},
        {"instruction": "inspect"},
        {"task_id": "task", "instruction": "inspect", "parameters": []},
        {"task_id": "task", "instruction": "x" * 2000},
    ]:
        with pytest.raises(RunnerError):
            await handler(payload, progress)
    assert not (tmp_path / "observed.json").exists()


@pytest.mark.parametrize(
    "patch",
    [
        {"args": ["--dangerously-skip-permissions"]},
        {"executable": "binary\x00"},
        {"max_line_bytes": 8192, "max_output_bytes": 4096},
    ],
)
def test_local_settings_reject_unknown_launch_options_and_invalid_limits(
    tmp_path: Path, patch: JsonObject
) -> None:
    with pytest.raises(ValidationError):
        RunnerSettings.model_validate({"kind": "codex", "workdir": tmp_path, **patch})


async def test_missing_executable_has_a_sanitized_error(tmp_path: Path) -> None:
    settings = RunnerSettings(
        kind="codex", workdir=tmp_path, executable=str(tmp_path / "not-installed")
    )

    async def progress(_: float, __: str) -> None:
        pass

    with pytest.raises(RunnerError, match="Could not start"):
        await create_handler(settings, tmp_path / "state.db")(
            {"task_id": "task", "instruction": "inspect"}, progress
        )


async def test_native_failure_is_reported_with_safe_classification_in_durable_status(
    tmp_path: Path,
) -> None:
    binary = fake_cli(
        tmp_path,
        [{"type": "turn.failed", "error": {"message": "native-auth-secret-diagnostic"}}],
    )
    settings = RunnerSettings(kind="codex", workdir=tmp_path, executable=str(binary))
    state_path = tmp_path / "state.db"
    client = AgentClient(
        "http://unused.invalid",
        "test-credential",
        state_path,
        handler=create_handler(settings, state_path),
    )
    await client.store.open()
    command = Envelope(type="task.dispatch", payload={"task_id": "task", "instruction": "inspect"})
    try:
        await client.store.accept(command)
        await client._execute(command)
        failures = [
            report
            for report in await client.store.pending()
            if report.type == "task.status" and report.payload["status"] == "failed"
        ]
        assert len(failures) == 1
        assert failures[0].payload["error"] == "Native CLI reported a failed run"
        assert "native-auth-secret-diagnostic" not in failures[0].canonical()
        assert await client.store.work("completed") == []
        assert (await client.store.work("failed"))[0].payload["task_id"] == "task"
    finally:
        await client.store.close()


async def test_arbitrary_handler_exception_is_not_exposed_in_task_status(tmp_path: Path) -> None:
    async def handler(_: JsonObject, __: Any) -> None:
        raise ValueError("untrusted handler private diagnostic")

    client = AgentClient("http://unused.invalid", "test-credential", tmp_path / "state.db", handler)
    await client.store.open()
    command = Envelope(type="task.dispatch", payload={"task_id": "task", "instruction": "inspect"})
    try:
        await client.store.accept(command)
        await client._execute(command)
        failure = next(
            report
            for report in await client.store.pending()
            if report.type == "task.status" and report.payload["status"] == "failed"
        )
        assert failure.payload["error"] == "Task handler failed: ValueError"
        assert "private diagnostic" not in failure.canonical()
    finally:
        await client.store.close()
