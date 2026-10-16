"""Startup dispatch preserves command semantics without updating inside tasks."""

from __future__ import annotations

import asyncio
import importlib.metadata
import json
import os
import runpy
import subprocess
import sys
import time
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, Mock

import pytest
from test_updates import Installer

from chiliarch import bootstrap, cli, updates
from chiliarch.config import SettingsManager


@pytest.mark.parametrize(
    ("arguments", "expected"),
    [
        (["serve"], True),
        (["--config", "elsewhere.toml", "serve", "--port", "8080"], True),
        (["daemon", "run", "--directory", "state"], True),
        (["daemon", "install", "--no-autostart"], True),
        (["daemon", "start"], True),
        (["daemon", "restart"], True),
        (["daemon", "status"], False),
        (["daemon", "stop"], False),
        (["daemon", "uninstall"], False),
        (["agent", "run", "--settings", "worker.toml", "--state", "agent.db"], True),
        (["agent", "mcp", "--agent", "main"], False),
        (["agent", "mcp", "--state", "agent.db"], True),
        (["agent", "mcp", "--mode", "mailbox", "--state", "agent.db"], False),
        (["agent", "mcp", "--agent", "serve"], False),
        (["agent", "doctor", "--agent", "main"], False),
        (["agent", "config", "--host", "codex", "--agent", "main"], False),
        (["schema"], False),
        (["--version"], False),
        (["--help"], False),
        (["serve", "--help"], False),
    ],
)
def test_only_actual_service_starts_check(arguments: list[str], expected: bool) -> None:
    assert bootstrap._checks_on_start(arguments) is expected


def test_invalid_arguments_fail_before_update(monkeypatch: pytest.MonkeyPatch) -> None:
    select = Mock()
    monkeypatch.setattr("chiliarch.updates.select_runtime", select)
    monkeypatch.setattr(sys, "argv", ["chiliarch", "serve", "--unknown"])
    with pytest.raises(SystemExit) as error:
        bootstrap.main()
    assert error.value.code == 2
    select.assert_not_called()


def test_dispatch_once_and_consume_marker(monkeypatch: pytest.MonkeyPatch) -> None:
    select = Mock(return_value=Path("new-python"))
    dispatch = Mock()
    execute = Mock()
    monkeypatch.setattr("chiliarch.updates.select_runtime", select)
    monkeypatch.setattr(bootstrap, "_dispatch", dispatch)
    monkeypatch.setattr(cli, "main", execute)
    monkeypatch.setattr(sys, "argv", ["chiliarch", "serve", "--port", "9000"])
    bootstrap.main()
    select.assert_called_once_with(check=True)
    dispatch.assert_called_once_with(Path("new-python"), ["serve", "--port", "9000"])
    execute.assert_not_called()
    monkeypatch.setenv("CHILIARCH_UPDATE_BOOTSTRAPPED", "1")
    bootstrap.main()
    assert select.call_count == 1
    execute.assert_called_once_with()
    assert "CHILIARCH_UPDATE_BOOTSTRAPPED" not in os.environ


def test_failed_process_creation_uses_current_runtime(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    installer = Installer(tmp_path, monkeypatch)
    monkeypatch.setattr(
        "chiliarch.updates.select_runtime", Mock(return_value=Path("missing-python"))
    )
    monkeypatch.setattr(bootstrap, "_dispatch", Mock(side_effect=FileNotFoundError))
    execute = Mock()
    monkeypatch.setattr(cli, "main", execute)
    monkeypatch.setattr(sys, "argv", ["chiliarch", "--version"])
    bootstrap.main()
    execute.assert_called_once_with()
    assert "新版无法启动" in (installer.directory / "update.log").read_text(encoding="utf-8")
    captured = capsys.readouterr()
    assert captured.out == captured.err == ""


@pytest.mark.parametrize(
    ("arguments", "marker", "background", "windowless"),
    [
        (["daemon", "run"], "1", True, True),
        (["serve", "--port", "9000"], "1", True, True),
        (["daemon", "run"], "0", False, False),
        (["agent", "mcp", "--agent", "worker"], "1", False, True),
        (["--config", "local.toml", "agent", "mcp", "--agent", "worker"], "0", False, True),
        (["agent", "doctor", "--agent", "mcp"], "1", False, False),
        (["--config", "mcp", "serve"], "0", False, False),
        (["daemon", "status"], "1", False, False),
        (["--version"], "1", False, False),
        (["daemon", "run", "--help"], "1", False, False),
    ],
)
def test_windows_background_dispatch_keeps_service_hidden_and_mcp_connected(
    monkeypatch: pytest.MonkeyPatch,
    arguments: list[str],
    marker: str,
    background: bool,
    windowless: bool,
) -> None:
    monkeypatch.setenv("CHILIARCH_BACKGROUND", marker)
    # Do not replace the host os.name globally: pathlib and pytest also use it.
    monkeypatch.setattr(bootstrap, "os", SimpleNamespace(name="nt", environ=os.environ))
    monkeypatch.setattr(subprocess, "CREATE_NO_WINDOW", 0x08000000, raising=False)
    process = MagicMock()
    process.__enter__.return_value.wait.return_value = 7
    start = Mock(return_value=process)
    monkeypatch.setattr(bootstrap.subprocess, "Popen", start)
    with pytest.raises(SystemExit) as error:
        bootstrap._dispatch(Path("new-python"), arguments)
    assert error.value.code == 7
    options = start.call_args.kwargs
    assert options["creationflags"] == (subprocess.CREATE_NO_WINDOW if windowless else 0)
    for descriptor, stream in enumerate(("stdin", "stdout", "stderr")):
        assert options[stream] == (subprocess.DEVNULL if background else descriptor)
    assert options["env"]["CHILIARCH_UPDATE_BOOTSTRAPPED"] == "1"
    process.__enter__.return_value.wait.assert_called_once_with()


def test_real_dispatch_preserves_stdio_arguments_cwd_and_exit(tmp_path: Path) -> None:
    # Exercise the real interpreter and CLI in another working directory. -I
    # must still find the installed package, with clean stdout and no recursion.
    from chiliarch import __version__

    run = subprocess.run(
        [
            sys.executable,
            "-c",
            "from pathlib import Path; from chiliarch.bootstrap import _dispatch; "
            "import sys; _dispatch(Path(sys.executable), ['--version'])",
        ],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert run.returncode == 0, run.stderr
    assert run.stdout.strip() == f"chiliarch {__version__}"


def test_dispatch_keeps_stdio_and_tracks_target_until_exit(tmp_path: Path) -> None:
    # Replace only the target module at the OS process boundary. The real
    # exec/Popen implementation still owns descriptors, environment and lifetime.
    target = """
import json, os, sys
from pathlib import Path
Path('ready.json').write_text(json.dumps({'pid': os.getpid(), 'launcher': int(os.environ['TEST_LAUNCHER_PID'])}))
print(json.dumps({'input': sys.stdin.readline(), 'arguments': json.loads(sys.argv[1]), 'cwd': os.getcwd(), 'marker': os.environ.get('CHILIARCH_UPDATE_BOOTSTRAPPED'), 'utf8': sys.flags.utf8_mode}), flush=True)
print('diagnostic 中文', file=sys.stderr, flush=True)
raise SystemExit(7)
"""
    launcher = tmp_path / "stdio-launcher.py"
    launcher.write_text(
        "import json, os, subprocess, sys\n"
        "from pathlib import Path\n"
        "from typing import Any\n"
        "from chiliarch import bootstrap\n"
        f"target = {target!r}\n"
        "os.environ['TEST_LAUNCHER_PID'] = str(os.getpid())\n"
        "def command(arguments: list[str]) -> list[str]:\n"
        "    assert arguments[1:6] == ['-I', '-X', 'utf8', '-m', 'chiliarch']\n"
        "    return [*arguments[:4], '-c', target, json.dumps(arguments[6:])]\n"
        "if os.name == 'nt':\n"
        "    original_popen = subprocess.Popen\n"
        "    def start(arguments: list[str], **options: Any) -> subprocess.Popen[bytes]:\n"
        "        return original_popen(command(arguments), **options)\n"
        "    bootstrap.subprocess.Popen = start\n"
        "else:\n"
        "    original_exec = os.execve\n"
        "    def execute(path: str, arguments: list[str], environment: dict[str, str]) -> None:\n"
        "        original_exec(path, command(arguments), environment)\n"
        "    bootstrap.os.execve = execute\n"
        "bootstrap._dispatch(Path(sys.executable), ['agent', 'mcp', '--agent', '中文 worker'])\n",
        encoding="utf-8",
    )
    process = subprocess.Popen(
        [sys.executable, str(launcher)],
        cwd=tmp_path,
        env=dict(os.environ, CHILIARCH_BACKGROUND="1"),
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
    )
    try:
        ready = tmp_path / "ready.json"
        deadline = time.monotonic() + 10
        while not ready.exists() and process.poll() is None and time.monotonic() < deadline:
            time.sleep(0.01)
        assert ready.exists()
        assert process.poll() is None  # Windows launcher must await its still-running child.
        stdout, stderr = process.communicate("标准输入 中文\n", timeout=10)
        assert process.returncode == 7
        result = json.loads(stdout)
        assert result == {
            "input": "标准输入 中文\n",
            "arguments": ["agent", "mcp", "--agent", "中文 worker"],
            "cwd": str(tmp_path),
            "marker": "1",
            "utf8": 1,
        }
        assert stderr == "diagnostic 中文\n"
        identities = json.loads(ready.read_text())
        assert (identities["pid"] == identities["launcher"]) is (os.name != "nt")
    finally:
        if process.poll() is None:
            try:
                process.communicate("\n", timeout=10)
            except subprocess.TimeoutExpired:
                process.kill()
                process.communicate(timeout=10)


@pytest.mark.skipif(os.name != "nt", reason="Windows windowless runtime handoff")
@pytest.mark.parametrize("launcher_name", ["python.exe", "pythonw.exe"])
async def test_windowless_mcp_handoff_retains_live_pipes(
    tmp_path: Path, launcher_name: str
) -> None:
    target = """
import ctypes, json, os, sys
kernel = ctypes.WinDLL('kernel32', use_last_error=True)
user = ctypes.WinDLL('user32', use_last_error=True)
kernel.GetConsoleWindow.restype = ctypes.c_void_p
user.IsWindowVisible.argtypes = [ctypes.c_void_p]
visible = bool(user.IsWindowVisible(kernel.GetConsoleWindow()))
for line in sys.stdin.buffer:
    request = json.loads(line)
    if request.get('exit'):
        raise SystemExit(7)
    response = {'request': request, 'visible_console': visible,
                'marker': os.getenv('CHILIARCH_UPDATE_BOOTSTRAPPED')}
    sys.stdout.buffer.write((json.dumps(response) + '\\n').encode())
    sys.stdout.buffer.flush()
    sys.stderr.buffer.write(b'diagnostic\\n')
    sys.stderr.buffer.flush()
"""
    launcher = tmp_path / "mcp-handoff.py"
    launcher.write_text(
        "import json, subprocess, sys\n"
        "from pathlib import Path\n"
        "from typing import Any\n"
        "from chiliarch import bootstrap\n"
        f"target = {target!r}\n"
        "original_popen = subprocess.Popen\n"
        "def start(arguments: list[str], **options: Any) -> subprocess.Popen[bytes]:\n"
        "    assert arguments[1:6] == ['-I', '-X', 'utf8', '-m', 'chiliarch']\n"
        "    return original_popen([*arguments[:4], '-c', target], **options)\n"
        "bootstrap.subprocess.Popen = start\n"
        "bootstrap._dispatch(Path(sys.executable).with_name('python.exe'), "
        "['agent', 'mcp', '--agent', 'worker'])\n",
        encoding="utf-8",
    )
    interpreter = Path(sys.executable).with_name(launcher_name)
    assert interpreter.is_file()
    process = await asyncio.create_subprocess_exec(
        str(interpreter),
        str(launcher),
        cwd=tmp_path,
        env=dict(os.environ, CHILIARCH_BACKGROUND="1"),
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        creationflags=subprocess.CREATE_NO_WINDOW,
    )
    assert process.stdin is not None
    assert process.stdout is not None
    assert process.stderr is not None
    try:
        async with asyncio.timeout(10):
            for sequence in range(2):
                frame = {"sequence": sequence, "message": "继续协作"}
                process.stdin.write((json.dumps(frame) + "\n").encode())
                await process.stdin.drain()
                assert json.loads(await process.stdout.readline()) == {
                    "request": frame,
                    "visible_console": False,
                    "marker": "1",
                }
                assert await process.stderr.readline() == b"diagnostic\n"
                assert process.returncode is None
                assert not process.stdin.is_closing()
            process.stdin.write(b'{"exit": true}\n')
            await process.stdin.drain()
            assert await process.wait() == 7
            assert await process.stdout.read() == b""
            assert await process.stderr.read() == b""
    finally:
        # Closing the protocol lets the target exit even if an assertion fails;
        # its Windows launcher remains responsible for waiting for that child.
        process.stdin.close()
        try:
            await asyncio.wait_for(process.wait(), 5)
        except TimeoutError:
            process.kill()
            await process.wait()


@pytest.mark.skipif(os.name == "nt", reason="POSIX exec preserves the supervisor PID")
def test_posix_dispatch_replaces_supervisor_process(tmp_path: Path) -> None:
    package = Path(bootstrap.__file__).parent
    script = tmp_path / "probe.py"
    script.write_text(
        "import json, os, sys\n"
        "from pathlib import Path\n"
        "from chiliarch import bootstrap\n"
        "original_exec = os.execve\n"
        "def execute(path, args, env):\n"
        "    original_exec(path, [path, '-c', "
        '"import json,os,sys; print(json.dumps([os.getpid(), os.getcwd(), '
        "sys.argv[1:], os.getenv('CHILIARCH_UPDATE_BOOTSTRAPPED')]))\", *args[6:]], env)\n"
        "bootstrap.os.execve = execute\n"
        "bootstrap._dispatch(Path(sys.executable), ['serve', '--config', '中文 path.toml'])\n",
        encoding="utf-8",
    )
    env = dict(os.environ, PYTHONPATH=str(package.parent))
    process = subprocess.Popen(
        [sys.executable, str(script)], cwd=tmp_path, env=env, stdout=subprocess.PIPE, text=True
    )
    output, _ = process.communicate(timeout=30)
    assert process.returncode == 0
    pid, cwd, arguments, marker = json.loads(output)
    assert pid == process.pid
    assert Path(cwd) == tmp_path
    assert arguments == ["serve", "--config", "中文 path.toml"]
    assert marker == "1"


@pytest.mark.parametrize("entrypoint", ["console", "module"])
def test_server_public_entrypoints_check_and_select_updated_runtime_before_serving(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, entrypoint: str
) -> None:
    # Exercise both published entrypoints through the real update selector. Only
    # external PyPI/uv I/O and process replacement are replaced by isolated fakes.
    console = next(
        item
        for item in importlib.metadata.distribution("chiliarch").entry_points
        if item.group == "console_scripts" and item.name == "chiliarch"
    ).load()
    installer = Installer(tmp_path, monkeypatch)
    arguments = ["--config", "server state/chiliarch.toml", "serve", "--port", "9000"]
    dispatch = Mock()
    serve = Mock()
    monkeypatch.setattr(bootstrap, "_dispatch", dispatch)
    monkeypatch.setattr(cli, "main", serve)
    monkeypatch.setattr(sys, "argv", ["chiliarch", *arguments])
    monkeypatch.delenv("CHILIARCH_UPDATE_BOOTSTRAPPED", raising=False)
    if entrypoint == "console":
        console()
    else:
        runpy.run_module("chiliarch", run_name="__main__")
    selected = updates.select_runtime(check=False)
    assert selected is not None
    assert len(installer.requests) == 1
    assert len(installer.commands) == 3
    assert updates._version(selected.parent.parent) == updates.Version("0.4.0")
    assert updates._version(installer.origin) == updates.Version("0.3.0")
    dispatch.assert_called_once_with(selected, arguments)
    serve.assert_not_called()


@pytest.mark.parametrize("failure", ["network", "install", "probe"])
def test_failed_server_startup_update_still_serves_existing_config(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, failure: str
) -> None:
    installer = Installer(tmp_path, monkeypatch)
    installer.failure = failure
    config = tmp_path / "server.toml"
    config.write_text('[auth]\npassword_hash = "existing-hash"\n', encoding="utf-8")
    manager = SettingsManager(config)
    assert manager.saved.auth.password_hash == "existing-hash"
    # Loading may fill newly introduced defaults; update failure must not alter
    # the resulting saved configuration or replace its existing credentials.
    before = config.read_bytes()
    application = SimpleNamespace(state=SimpleNamespace(settings_manager=manager))
    created: list[Path] = []

    def create_app(path: Path) -> SimpleNamespace:
        # No app, listener or database is opened until startup preparation ends.
        assert len(installer.requests) == 1
        assert not (installer.directory / "active.json").exists()
        created.append(path)
        return application

    serve = Mock()
    dispatch = Mock()
    monkeypatch.setattr("chiliarch.api.create_app", create_app)
    monkeypatch.setattr("uvicorn.run", serve)
    monkeypatch.setattr(bootstrap, "_dispatch", dispatch)
    monkeypatch.setattr(
        sys,
        "argv",
        ["chiliarch", "serve", "--config", str(config), "--host", "127.0.0.1", "--port", "9000"],
    )
    monkeypatch.delenv("CHILIARCH_UPDATE_BOOTSTRAPPED", raising=False)
    bootstrap.main()
    assert created == [config]
    assert config.read_bytes() == before
    assert updates._version(installer.origin) == updates.Version("0.3.0")
    dispatch.assert_not_called()
    serve.assert_called_once_with(
        application,
        host="127.0.0.1",
        port=9000,
        workers=1,
        log_config=None,
        timeout_graceful_shutdown=manager.current.server.graceful_shutdown_seconds,
    )
