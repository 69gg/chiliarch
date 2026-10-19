"""Exercise task-query encoding in a real, console-less Windows PowerShell process.

Only the Task Scheduler COM object is replaced by read-only fixtures. The production
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

from chiliarch.daemon_services import CommandResult, ServiceError, ServiceManager, _execute

XML = (
    '<?xml version="1.0" encoding="UTF-16"?>'
    '<Task xmlns="http://schemas.microsoft.com/windows/2004/02/mit/task">'
    "<RegistrationInfo><Description>中文、café、🧪</Description></RegistrationInfo>"
    "<Actions><Exec><WorkingDirectory>C:\\用户\\中文 目录\\daemon</WorkingDirectory>"
    "</Exec></Actions></Task>"
)


def mock_scheduler(
    service: ServiceManager,
    *,
    exists: bool = True,
    error_stage: str = "",
    error_code: int = -2147024891,  # E_ACCESSDENIED
) -> list[CommandResult]:
    fixture = base64.b64encode(
        json.dumps(
            {
                "name": service.name,
                "exists": exists,
                "xml": XML,
                "error_stage": error_stage,
                "error_code": error_code,
            },
            ensure_ascii=False,
        ).encode("utf-8")
    ).decode("ascii")
    prelude = (
        "$ErrorActionPreference='Stop'; "
        f"$fixture=[Text.Encoding]::UTF8.GetString([Convert]::FromBase64String('{fixture}'))"
        " | ConvertFrom-Json; "
        "function Fail-Fixture([string]$Stage){if($fixture.error_stage -eq $Stage){"
        "throw [Runtime.InteropServices.COMException]::new('fixture failure',[int]$fixture.error_code)}}; "
        "$fixtureFolder=[pscustomobject]@{}; "
        "$fixtureFolder | Add-Member ScriptMethod GetTask {param([string]$Path) "
        "if($Path -cne $fixture.name -or $args.Count -ne 0){throw 'unexpected task lookup'}; "
        "Fail-Fixture 'task'; "
        "if(-not $fixture.exists){throw [Runtime.InteropServices.COMException]::new('missing',-2147024894)}; "
        "[pscustomobject]@{Xml=$fixture.xml; State=4; Enabled=$false}}; "
        "$fixtureService=[pscustomobject]@{Connected=$false}; "
        "$fixtureService | Add-Member ScriptMethod Connect {"
        "if($args.Count -ne 0){throw 'unexpected remote connection'}; "
        "Fail-Fixture 'connect'; $this.Connected=$true}; "
        "$fixtureService | Add-Member ScriptMethod GetFolder {param([string]$Path) "
        "if(-not $this.Connected -or $Path -cne '\\' -or $args.Count -ne 0){throw 'unexpected folder lookup'}; "
        "Fail-Fixture 'root'; $fixtureFolder}; "
        "function New-Object { [CmdletBinding()] param([string]$ComObject) "
        "if($ComObject -cne 'Schedule.Service'){throw 'unexpected COM object'}; $fixtureService}; "
    )
    results: list[CommandResult] = []

    def execute(arguments: Sequence[str]) -> CommandResult:
        # The fixture exposes only Connect, GetFolder and exact GetTask: an
        # enumeration, remote identity or unexpected name fails inside PowerShell.
        # _execute supplies CREATE_NO_WINDOW and redirected standard handles.
        result = _execute([*arguments[:-1], prelude + arguments[-1]])
        results.append(result)
        return result

    service.executor = execute
    return results


@pytest.mark.skipif(sys.platform != "win32", reason="Requires real Windows PowerShell and .NET")
@pytest.mark.parametrize("exists", [False, True])
def test_task_query_without_console_preserves_unicode_json(tmp_path: Path, exists: bool) -> None:
    service = ServiceManager(tmp_path / "守护进程 🧪 with spaces", platform="win32")
    results = mock_scheduler(service, exists=exists)
    task = service._windows_task()
    assert task == ({"xml": XML, "running": True, "enabled": False} if exists else {})
    assert len(results) == 1
    assert results[0].returncode == 0
    assert results[0].stderr == ""


@pytest.mark.skipif(sys.platform != "win32", reason="Requires real Windows PowerShell and .NET")
@pytest.mark.parametrize(
    ("stage", "code"),
    [("connect", -2147024894), ("root", -2147024894), ("task", -2147024891)],
)
def test_task_query_does_not_hide_connection_or_permission_failures(
    tmp_path: Path, stage: str, code: int
) -> None:
    service = ServiceManager(tmp_path, platform="win32")
    results = mock_scheduler(service, error_stage=stage, error_code=code)
    with pytest.raises(ServiceError) as raised:
        service._windows_task()
    assert raised.value.reason == "native_command_failed"
    assert len(results) == 1
    assert results[0].returncode != 0


@pytest.mark.skipif(sys.platform != "win32", reason="Requires real Windows PowerShell and .NET")
@pytest.mark.parametrize("error_stage", ["task", "connect"])
def test_scheduler_availability_only_connects_to_local_root(
    tmp_path: Path, error_stage: str
) -> None:
    service = ServiceManager(tmp_path, platform="win32")
    # Per-task errors are irrelevant to availability; Connect errors are not.
    results = mock_scheduler(service, error_stage=error_stage)
    assert service.available() is (error_stage == "task")
    assert len(results) == 1


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
