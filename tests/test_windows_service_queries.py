"""Exercise task-query encoding in a real, console-less Windows PowerShell process.

Only the ScheduledTasks cmdlets are replaced by read-only fixtures. The production
query, PowerShell parser, .NET streams, Windows process flags and Python decoder
are real; no user task is registered, modified, started or deleted by these tests.
"""

from __future__ import annotations

import base64
import json
import sys
from collections.abc import Sequence
from pathlib import Path

import pytest

from chiliarch.daemon_services import CommandResult, ServiceManager, _execute


@pytest.mark.skipif(sys.platform != "win32", reason="Requires real Windows PowerShell and .NET")
@pytest.mark.parametrize("exists", [False, True])
def test_task_query_without_console_preserves_unicode_json(tmp_path: Path, exists: bool) -> None:
    service = ServiceManager(tmp_path / "守护进程 🧪 with spaces", platform="win32")
    xml = (
        '<?xml version="1.0" encoding="UTF-16"?>'
        '<Task xmlns="http://schemas.microsoft.com/windows/2004/02/mit/task">'
        "<RegistrationInfo><Description>中文、café、🧪</Description></RegistrationInfo>"
        "<Actions><Exec><WorkingDirectory>C:\\用户\\中文 目录\\daemon</WorkingDirectory>"
        "</Exec></Actions></Task>"
    )
    fixture = base64.b64encode(
        json.dumps({"name": service.name, "exists": exists, "xml": xml}, ensure_ascii=False).encode(
            "utf-8"
        )
    ).decode("ascii")
    prelude = (
        "$ErrorActionPreference='Stop'; "
        f"$fixture=[Text.Encoding]::UTF8.GetString([Convert]::FromBase64String('{fixture}'))"
        " | ConvertFrom-Json; "
        "function Get-ScheduledTask { [CmdletBinding()] param([string]$TaskPath) "
        "if($fixture.exists){[pscustomobject]@{TaskName=$fixture.name; State='Running'; "
        "Settings=[pscustomobject]@{Enabled=$false}}} }; "
        "function Export-ScheduledTask { [CmdletBinding()] "
        "param([string]$TaskPath,[string]$TaskName) $fixture.xml }; "
    )
    results: list[CommandResult] = []

    def execute(arguments: Sequence[str]) -> CommandResult:
        # Capture the entire production query before adding fixture cmdlets.
        # _execute supplies CREATE_NO_WINDOW and redirected standard handles.
        result = _execute([*arguments[:-1], prelude + arguments[-1]])
        results.append(result)
        return result

    service.executor = execute
    task = service._windows_task()
    assert task == ({"xml": xml, "running": True, "enabled": False} if exists else {})
    assert len(results) == 1
    assert results[0].returncode == 0
    assert results[0].stderr == ""


@pytest.mark.skipif(sys.platform != "win32", reason="Requires the Windows console API")
def test_native_executor_really_has_no_console() -> None:
    # Observe the same production launcher through ctypes instead of compiling a
    # C# probe in every PowerShell query. Neither test needs a C# toolchain.
    result = _execute(
        [
            sys.executable,
            "-c",
            "import ctypes,json; "
            "get_console=ctypes.windll.kernel32.GetConsoleWindow; "
            "get_console.argtypes=[]; get_console.restype=ctypes.c_void_p; "
            "print(json.dumps({'console': bool(get_console())}))",
        ]
    )
    assert result.returncode == 0
    assert result.stderr == ""
    assert json.loads(result.stdout) == {"console": False}
