"""Startup dispatch preserves command semantics without updating inside tasks."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path
from unittest.mock import Mock

import pytest

from chiliarch import bootstrap, cli


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


def test_failed_process_creation_uses_current_runtime(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "chiliarch.updates.select_runtime", Mock(return_value=Path("missing-python"))
    )
    monkeypatch.setattr(bootstrap, "_dispatch", Mock(side_effect=FileNotFoundError))
    execute = Mock()
    monkeypatch.setattr(cli, "main", execute)
    monkeypatch.setattr(sys, "argv", ["chiliarch", "--version"])
    bootstrap.main()
    execute.assert_called_once_with()


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
