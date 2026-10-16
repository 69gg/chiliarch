"""Current-user login services; no platform credentials belong in startup definitions."""

from __future__ import annotations

import base64
import configparser
import csv
import hashlib
import io
import json
import locale
import os
import plistlib
import shlex
import subprocess
import sys
import tempfile
import xml.etree.ElementTree as ET
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

_TASK_NAMESPACE = "http://schemas.microsoft.com/windows/2004/02/mit/task"
_TASK_NS = {"t": _TASK_NAMESPACE}
_SAFE_ENVIRONMENT = (
    "PATH",
    "LANG",
    "LC_ALL",
    "LC_CTYPE",
    "CODEX_HOME",
    "CLAUDE_CONFIG_DIR",
    "OPENCODE_CONFIG",
    "OPENCODE_CONFIG_DIR",
    "OPENCODE_GIT_BASH_PATH",
    "XDG_CONFIG_HOME",
    "XDG_DATA_HOME",
    "XDG_STATE_HOME",
    "XDG_CACHE_HOME",
    "XDG_BIN_HOME",
    "CHILIARCH_AUTO_UPDATE",
    "UV_TOOL_DIR",
    "UV_TOOL_BIN_DIR",
    "UV_CACHE_DIR",
    "UV_CONFIG_FILE",
    "UV_PYTHON_INSTALL_DIR",
    "USERPROFILE",
    "APPDATA",
    "LOCALAPPDATA",
)
# Task Scheduler has no per-action environment. Apply only the captured whitelist
# in the same Python process before entering the CLI, without invoking a shell.
_WINDOWS_LEGACY_BOOTSTRAP = (
    "import base64,json,os,runpy,sys;"
    "environment,sys.argv=json.loads(base64.b64decode(sys.argv[1]));"
    "os.environ.update(environment);"
    "runpy.run_module('chiliarch',run_name='__main__',alter_sys=True)"
)
_WINDOWS_BOOTSTRAP = _WINDOWS_LEGACY_BOOTSTRAP.replace(
    "runpy.run_module(", "os.environ['CHILIARCH_BACKGROUND']='1';runpy.run_module("
)


class ServiceError(RuntimeError):
    """A startup manager is unavailable, failed, or would modify an unowned service."""


@dataclass(frozen=True)
class CommandResult:
    returncode: int
    stdout: str = ""
    stderr: str = ""


@dataclass(frozen=True)
class ServiceStatus:
    backend: str
    available: bool
    installed: bool
    enabled: bool
    running: bool
    detail: str = ""


Executor = Callable[[Sequence[str]], CommandResult]


def default_daemon_directory(
    *,
    platform: str | None = None,
    home: Path | None = None,
    environment: Mapping[str, str] | None = None,
) -> Path:
    """Resolve one persistent daemon state directory per logged-in user."""
    platform = platform or sys.platform
    home = home or Path.home()
    environment = os.environ if environment is None else environment
    if platform == "win32":
        root = Path(environment.get("LOCALAPPDATA", str(home / "AppData" / "Local")))
    elif platform == "darwin":
        root = home / "Library" / "Application Support"
    else:
        configured = environment.get("XDG_STATE_HOME")
        root = (
            Path(configured)
            if configured and Path(configured).is_absolute()
            else home / ".local" / "state"
        )
    return (root / "chiliarch" / "daemon").expanduser().absolute()


def _execute(arguments: Sequence[str]) -> CommandResult:
    # Use the native locale for Windows utilities; XML output carries its own declaration.
    encoding = (
        "utf-8"
        if arguments[0].lower().endswith("powershell.exe")
        else locale.getpreferredencoding(False)
    )
    result = subprocess.run(
        list(arguments),
        capture_output=True,
        text=True,
        encoding=encoding,
        errors="replace",
        timeout=30,
        check=False,
        stdin=subprocess.DEVNULL,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0) if sys.platform == "win32" else 0,
    )
    return CommandResult(result.returncode, result.stdout, result.stderr)


def _quote_unit(value: str) -> str:
    if any(ord(character) < 32 for character in value):
        raise ServiceError("Startup arguments cannot contain control characters")
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"').replace("%", "%%") + '"'


class ServiceManager:
    """Manage only this directory's user-level systemd/launchd/Task Scheduler entry.

    install() registers and enables login startup. start() is separate, and the caller
    must verify the daemon's authenticated health endpoint after requesting a start.
    A native manager reporting a running process does not prove Chiliarch is healthy.
    """

    def __init__(
        self,
        directory: Path,
        *,
        platform: str | None = None,
        python_executable: Path | None = None,
        home: Path | None = None,
        environment: Mapping[str, str] | None = None,
        executor: Executor | None = None,
        user_id: int | None = None,
    ) -> None:
        self.directory = directory.expanduser().resolve()
        self.platform = platform or sys.platform
        self.python = (python_executable or Path(sys.executable)).expanduser().absolute()
        self.home = (home or Path.home()).expanduser().absolute()
        self.environment = dict(os.environ if environment is None else environment)
        self.executor = executor or _execute
        self.user_id = user_id if user_id is not None else getattr(os, "getuid", lambda: 0)()
        self.identifier = hashlib.sha256(os.fsencode(self.directory)).hexdigest()[:16]
        self.name = f"chiliarch-daemon-{self.identifier}"
        self.marker = f"chiliarch user daemon {self.identifier}"
        self.command = [
            str(self.python),
            "-m",
            "chiliarch",
            "daemon",
            "run",
            "--directory",
            str(self.directory),
        ]
        if self.platform == "linux":
            configured = self.environment.get("XDG_CONFIG_HOME")
            root = (
                Path(configured)
                if configured and Path(configured).is_absolute()
                else self.home / ".config"
            )
            self.definition_path = root / "systemd" / "user" / f"{self.name}.service"
            self.backend = "systemd"
        elif self.platform == "darwin":
            self.name = f"io.chiliarch.daemon.{self.identifier}"
            self.definition_path = self.home / "Library" / "LaunchAgents" / f"{self.name}.plist"
            self.backend = "launchd"
        elif self.platform == "win32":
            self.definition_path = self.directory / "service-task.xml"
            self.backend = "task-scheduler"
        else:
            self.definition_path = self.directory / "service.unsupported"
            self.backend = "foreground"

    def _run(self, arguments: Sequence[str], *, check: bool = True) -> CommandResult:
        try:
            result = self.executor(arguments)
        except (OSError, subprocess.TimeoutExpired) as error:
            raise ServiceError(f"Cannot contact {self.backend}: {error}") from error
        if check and result.returncode:
            diagnostic = (result.stderr or result.stdout).strip()
            raise ServiceError(f"{self.backend} command failed ({result.returncode}): {diagnostic}")
        return result

    @property
    def target(self) -> str:
        return f"gui/{self.user_id}/{self.name}"

    def available(self) -> bool:
        if self.platform == "linux":
            command = ["systemctl", "--user", "show-environment"]
        elif self.platform == "darwin":
            command = ["launchctl", "print", f"gui/{self.user_id}"]
        elif self.platform == "win32":
            command = [
                "powershell.exe",
                "-NoProfile",
                "-NonInteractive",
                "-Command",
                "Get-ScheduledTask -ErrorAction Stop | Out-Null",
            ]
        else:
            return False
        try:
            return self._run(command, check=False).returncode == 0
        except ServiceError:
            return False

    def _require_available(self) -> None:
        if not self.available():
            raise ServiceError(
                f"Current-user {self.backend} manager is unavailable; run the daemon in the foreground instead"
            )

    def _environment(self) -> dict[str, str]:
        return {
            "HOME": str(self.home),
            "PYTHONUTF8": "1",
            **{key: self.environment[key] for key in _SAFE_ENVIRONMENT if key in self.environment},
            **({"CHILIARCH_BACKGROUND": "1"} if self.platform != "win32" else {}),
        }

    def _linux_definition(self) -> bytes:
        # ':' prevents $ expansion; %% prevents systemd specifier expansion in paths.
        command = ":" + " ".join(_quote_unit(argument) for argument in self.command)
        environment = " ".join(
            _quote_unit(f"{key}={value}") for key, value in self._environment().items()
        )
        text = (
            f"# {self.marker}\n[Unit]\nDescription=Chiliarch user daemon\n\n"
            f"[Service]\nType=simple\nExecStart={command}\n"
            f"WorkingDirectory={_quote_unit(str(self.directory))}\nEnvironment={environment}\n"
            "Restart=on-failure\nRestartSec=5\nTimeoutStopSec=45\nKillMode=control-group\n"
            "UMask=0077\n\n[Install]\nWantedBy=default.target\n"
        )
        return text.encode("utf-8")

    def _mac_definition(self) -> bytes:
        return plistlib.dumps(
            {
                "Label": self.name,
                "ProgramArguments": self.command,
                "WorkingDirectory": str(self.directory),
                "EnvironmentVariables": self._environment(),
                "RunAtLoad": True,
                "KeepAlive": {"SuccessfulExit": False},
                "ThrottleInterval": 5,
                "ExitTimeOut": 45,
                # The daemon writes bounded JSONL logs; do not create unbounded side logs.
                "StandardOutPath": "/dev/null",
                "StandardErrorPath": "/dev/null",
            },
            fmt=plistlib.FMT_XML,
        )

    def _windows_sid(self) -> str:
        result = self._run(["whoami.exe", "/user", "/fo", "csv", "/nh"])
        try:
            row = next(csv.reader(io.StringIO(result.stdout)))
            sid = row[1]
        except (IndexError, StopIteration) as error:
            raise ServiceError("Cannot determine the current Windows user SID") from error
        if not sid.startswith("S-1-"):
            raise ServiceError("Invalid current Windows user SID")
        return sid

    def _windows_definition(self) -> bytes:
        ET.register_namespace("", _TASK_NAMESPACE)
        task = ET.Element(f"{{{_TASK_NAMESPACE}}}Task", {"version": "1.3"})

        def section(parent: ET.Element, name: str, values: Mapping[str, str]) -> ET.Element:
            node = ET.SubElement(parent, name)
            for key, value in values.items():
                ET.SubElement(node, key).text = value
            return node

        sid = self._windows_sid()
        section(task, "RegistrationInfo", {"Description": self.marker})
        triggers = ET.SubElement(task, "Triggers")
        section(triggers, "LogonTrigger", {"Enabled": "true", "UserId": sid})
        principals = ET.SubElement(task, "Principals")
        principal = section(
            principals,
            "Principal",
            {"UserId": sid, "LogonType": "InteractiveToken", "RunLevel": "LeastPrivilege"},
        )
        principal.set("id", "CurrentUser")
        settings = section(
            task,
            "Settings",
            {
                "MultipleInstancesPolicy": "IgnoreNew",
                "DisallowStartIfOnBatteries": "false",
                "StopIfGoingOnBatteries": "false",
                "AllowHardTerminate": "true",
                "StartWhenAvailable": "true",
                "RunOnlyIfNetworkAvailable": "false",
                "Enabled": "true",
                "WakeToRun": "false",
                "ExecutionTimeLimit": "PT0S",
            },
        )
        section(settings, "RestartOnFailure", {"Interval": "PT1M", "Count": "10"})
        actions = ET.SubElement(task, "Actions", {"Context": "CurrentUser"})
        section(
            actions,
            "Exec",
            {
                "Command": str(self._windows_python()),
                "Arguments": subprocess.list2cmdline(self._windows_command()[1:]),
                "WorkingDirectory": str(self.directory),
            },
        )
        content: bytes = ET.tostring(task, encoding="utf-16", xml_declaration=True)
        return content

    def _windows_python(self, existing: str | None = None) -> Path:
        # Enter through this validated runtime on the next login. An older uv
        # origin may still contain a bootstrap that launches a visible console
        # while selecting the new runtime, even when its own entry is pythonw.
        # Updated runtimes keep their origin marker and remain able to select
        # later automatic releases or a manually upgraded uv tool installation.
        candidates = [self.python, Path(existing)] if existing else [self.python]
        for interpreter in candidates:
            pythonw = interpreter.with_name("pythonw.exe")
            if pythonw.is_file():
                return pythonw
        raise ServiceError("This Python installation has no pythonw.exe for background startup")

    def _windows_command(
        self,
        environment: Mapping[str, str] | None = None,
        *,
        python_executable: Path | None = None,
    ) -> list[str]:
        # Encode data separately from the fixed code and Windows argument quoting.
        # This also prevents Task Scheduler from expanding %NAME% in CLI arguments.
        payload = json.dumps(
            [self._environment() if environment is None else dict(environment), self.command[2:]]
        ).encode("utf-8")
        return [
            str(python_executable or self._windows_python()),
            "-c",
            _WINDOWS_BOOTSTRAP,
            base64.b64encode(payload).decode("ascii"),
        ]

    def _windows_environment(self, arguments: str | None) -> dict[str, str] | None:
        if arguments == subprocess.list2cmdline(self.command[1:]):
            return {}  # Old entries inherited the login environment without overrides.
        for bootstrap in (_WINDOWS_BOOTSTRAP, _WINDOWS_LEGACY_BOOTSTRAP):
            prefix = subprocess.list2cmdline(["-c", bootstrap]) + " "
            if arguments is None or not arguments.startswith(prefix):
                continue
            payload = json.loads(base64.b64decode(arguments[len(prefix) :], validate=True))
            if not isinstance(payload, list) or len(payload) != 2:
                return None
            environment, command = payload
            if (
                command == self.command[2:]
                and isinstance(environment, dict)
                and environment.keys() <= {*_SAFE_ENVIRONMENT, "HOME", "PYTHONUTF8"}
                and all(isinstance(value, str) for value in environment.values())
            ):
                return dict(environment)
        return None

    def definition(self) -> bytes:
        """Render the native startup definition, without installing or starting it."""
        if self.platform == "linux":
            return self._linux_definition()
        if self.platform == "darwin":
            return self._mac_definition()
        if self.platform == "win32":
            return self._windows_definition()
        raise ServiceError("This system only supports foreground execution")

    def _assert_owned(self, content: bytes | str) -> None:
        try:
            if self.platform == "linux":
                text = content.decode("utf-8") if isinstance(content, bytes) else content
                parser = configparser.ConfigParser(interpolation=None)
                parser.read_string(text)
                command = shlex.split(parser["Service"]["ExecStart"].removeprefix(":"))
                command = [part.replace("%%", "%") for part in command]
                owned = (
                    text.startswith(f"# {self.marker}\n")
                    and command[1:] == self.command[1:]
                    and Path(command[0]).is_absolute()
                )
            elif self.platform == "darwin":
                plist = plistlib.loads(
                    content if isinstance(content, bytes) else content.encode("utf-8")
                )
                command = plist.get("ProgramArguments", [])
                owned = (
                    plist.get("Label") == self.name
                    and command[1:] == self.command[1:]
                    and Path(command[0]).is_absolute()
                )
            else:
                task = ET.fromstring(content)
                command_text = task.findtext("t:Actions/t:Exec/t:Command", namespaces=_TASK_NS)
                owned = (
                    task.findtext("t:RegistrationInfo/t:Description", namespaces=_TASK_NS)
                    == self.marker
                    and self._windows_environment(
                        task.findtext("t:Actions/t:Exec/t:Arguments", namespaces=_TASK_NS)
                    )
                    is not None
                    and len(task.findall("t:Actions/*", _TASK_NS)) == 1
                    and command_text is not None
                    and Path(command_text).is_absolute()
                    and task.findtext("t:Principals/t:Principal/t:UserId", namespaces=_TASK_NS)
                    == self._windows_sid()
                    and task.findtext("t:Principals/t:Principal/t:LogonType", namespaces=_TASK_NS)
                    == "InteractiveToken"
                    and task.findtext("t:Principals/t:Principal/t:RunLevel", namespaces=_TASK_NS)
                    == "LeastPrivilege"
                )
        except (
            ValueError,
            KeyError,
            TypeError,
            IndexError,
            configparser.Error,
            ET.ParseError,
            plistlib.InvalidFileException,
        ) as error:
            raise ServiceError(
                "Existing startup definition is invalid or belongs to another application"
            ) from error
        if not owned:
            raise ServiceError(
                "Refusing to change an unowned startup definition with the same name"
            )

    def _windows_task(self) -> dict[str, Any]:
        # The task name is internally generated ASCII, never arbitrary user/script input.
        script = (
            "$ErrorActionPreference='Stop'; "
            "$OutputEncoding=[Console]::OutputEncoding=[Text.UTF8Encoding]::new(); "
            f"$t=Get-ScheduledTask -TaskPath '\\' -ErrorAction Stop | Where-Object {{$_.TaskName -eq '{self.name}'}}; "
            "if($null -eq $t){'{}'}else{"
            f"@{{xml=(Export-ScheduledTask -TaskPath '\\' -TaskName '{self.name}'); "
            "running=($t.State -eq 'Running'); enabled=[bool]$t.Settings.Enabled} | ConvertTo-Json -Compress}"
        )
        result = self._run(["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", script])
        try:
            value: dict[str, Any] = json.loads(result.stdout)
            if not isinstance(value, dict):
                raise ValueError("Unexpected task query response")
            if value and not isinstance(value.get("xml"), str):
                raise ValueError("Missing task XML definition")
            return value
        except ValueError as error:
            raise ServiceError("Task Scheduler returned an invalid status response") from error

    def _installed(self) -> bool:
        if self.platform == "win32":
            task = self._windows_task()
            if not task:
                return False
            self._assert_owned(task["xml"])
            return True
        if self.platform == "linux":
            existing = self._run(
                ["systemctl", "--user", "show", f"{self.name}.service", "--property=FragmentPath"],
                check=False,
            )
            fields = dict(
                line.split("=", 1) for line in existing.stdout.splitlines() if "=" in line
            )
            fragment = fields.get("FragmentPath")
            if fragment and Path(fragment) != self.definition_path:
                raise ServiceError("A different systemd definition shadows the owned startup entry")
        elif self.platform == "darwin":
            existing = self._run(["launchctl", "print", self.target], check=False)
            if existing.returncode == 0:
                paths = [
                    line.strip().removeprefix("path = ")
                    for line in existing.stdout.splitlines()
                    if line.strip().startswith("path = ")
                ]
                if not self.definition_path.exists() or paths != [str(self.definition_path)]:
                    raise ServiceError("An unowned launchd job already uses this service label")
        if not self.definition_path.exists():
            return False
        if self.definition_path.is_symlink():
            raise ServiceError("Refusing to manage a startup definition symlink")
        self._assert_owned(self.definition_path.read_bytes())
        return True

    def status(self) -> ServiceStatus:
        available = self.available()
        if not available:
            return ServiceStatus(
                self.backend,
                False,
                False,
                False,
                False,
                "User service manager unavailable; foreground execution remains available",
            )
        installed = self._installed()
        if not installed:
            return ServiceStatus(self.backend, True, False, False, False)
        if self.platform == "linux":
            state = self._run(
                [
                    "systemctl",
                    "--user",
                    "show",
                    f"{self.name}.service",
                    "--property=FragmentPath,ActiveState,UnitFileState",
                ],
                check=False,
            )
            if state.returncode:
                raise ServiceError(
                    f"Cannot query the installed systemd service: {state.stderr.strip()}"
                )
            fields = dict(line.split("=", 1) for line in state.stdout.splitlines() if "=" in line)
            fragment = fields.get("FragmentPath")
            if fragment and Path(fragment) != self.definition_path:
                raise ServiceError("A different systemd definition shadows the owned startup entry")
            enabled = fields.get("UnitFileState") in {"enabled", "enabled-runtime"}
            running = fields.get("ActiveState") in {"active", "activating", "reloading"}
        elif self.platform == "darwin":
            state = self._run(["launchctl", "print", self.target], check=False)
            disabled = self._run(["launchctl", "print-disabled", f"gui/{self.user_id}"])
            enabled = not any(
                f'"{self.name}" => true' in line for line in disabled.stdout.splitlines()
            )
            running = state.returncode == 0 and any(
                line.strip() == "state = running" for line in state.stdout.splitlines()
            )
        else:
            task = self._windows_task()
            enabled, running = bool(task.get("enabled")), bool(task.get("running"))
        return ServiceStatus(self.backend, True, True, enabled, running)

    def install(self) -> ServiceStatus:
        self._require_available()
        installed = self._installed()
        self.directory.mkdir(parents=True, exist_ok=True)
        content = self.definition()
        if self.definition_path.exists():
            if self.definition_path.is_symlink():
                raise ServiceError("Refusing to replace a startup definition symlink")
            self._assert_owned(self.definition_path.read_bytes())
        self.definition_path.parent.mkdir(parents=True, exist_ok=True)
        self.definition_path.write_bytes(content)
        if self.platform != "win32":
            self.definition_path.chmod(0o600)
        if self.platform == "linux":
            self._run(["systemctl", "--user", "daemon-reload"])
            self._run(["systemctl", "--user", "enable", f"{self.name}.service"])
        elif self.platform == "darwin":
            self._run(["launchctl", "enable", self.target])
        else:
            arguments = [
                "schtasks.exe",
                "/Create",
                "/TN",
                self.name,
                "/XML",
                str(self.definition_path),
            ]
            if installed:
                arguments.append("/F")
            self._run(arguments)
        return self.status()

    def refresh(self) -> bool:
        """Upgrade an owned Windows login task without starting or enabling it.

        Preserve the registered task's captured environment and user settings.
        Missing entries stay absent, and other native service managers are untouched.
        The current process keeps running; the new action applies to its next start.
        """
        if self.platform != "win32" or not self.available():
            return False
        existing = self._windows_task()
        if not existing:
            return False
        self._assert_owned(existing["xml"])
        task = ET.fromstring(existing["xml"])
        command = task.find("t:Actions/t:Exec/t:Command", _TASK_NS)
        arguments = task.find("t:Actions/t:Exec/t:Arguments", _TASK_NS)
        assert command is not None and arguments is not None  # Validated above.
        environment = self._windows_environment(arguments.text)
        assert environment is not None
        pythonw = str(self._windows_python(command.text))
        updated_arguments = subprocess.list2cmdline(
            self._windows_command(environment, python_executable=Path(pythonw))[1:]
        )
        if command.text == pythonw and arguments.text == updated_arguments:
            return False
        command.text = pythonw
        arguments.text = updated_arguments
        if self.definition_path.is_symlink():
            raise ServiceError("Refusing to replace a startup definition symlink")
        if self.definition_path.exists():
            self._assert_owned(self.definition_path.read_bytes())
        self.directory.mkdir(parents=True, exist_ok=True)
        ET.register_namespace("", _TASK_NAMESPACE)
        content = ET.tostring(task, encoding="utf-16", xml_declaration=True)
        descriptor, filename = tempfile.mkstemp(
            prefix="service-refresh-", suffix=".xml", dir=self.directory
        )
        temporary = Path(filename)
        try:
            with os.fdopen(descriptor, "wb") as stream:
                stream.write(content)
            self._run(["schtasks.exe", "/Create", "/TN", self.name, "/XML", str(temporary), "/F"])
            temporary.replace(self.definition_path)
        finally:
            temporary.unlink(missing_ok=True)
        return True

    def _require_installed(self) -> None:
        self._require_available()
        if not self._installed():
            raise ServiceError("No owned startup entry exists; install it first")

    def enable(self) -> ServiceStatus:
        self._require_installed()
        if self.platform == "linux":
            self._run(["systemctl", "--user", "enable", f"{self.name}.service"])
        elif self.platform == "darwin":
            self._run(["launchctl", "enable", self.target])
        else:
            self._run(["schtasks.exe", "/Change", "/TN", self.name, "/Enable"])
        return self.status()

    def disable(self) -> ServiceStatus:
        self._require_installed()
        if self.platform == "linux":
            self._run(["systemctl", "--user", "disable", f"{self.name}.service"])
        elif self.platform == "darwin":
            self._run(["launchctl", "disable", self.target])
        else:
            self._run(["schtasks.exe", "/Change", "/TN", self.name, "/Disable"])
        return self.status()

    def start(self) -> ServiceStatus:
        self._require_installed()
        if self.platform == "linux":
            self._run(["systemctl", "--user", "start", f"{self.name}.service"])
        elif self.platform == "darwin":
            existing = self._run(["launchctl", "print", self.target], check=False)
            if existing.returncode:
                self._run(
                    ["launchctl", "bootstrap", f"gui/{self.user_id}", str(self.definition_path)]
                )
            self._run(["launchctl", "kickstart", self.target])
        else:
            self._run(["schtasks.exe", "/Run", "/TN", self.name])
        return self.status()

    def stop(self) -> ServiceStatus:
        self._require_installed()
        if self.platform == "linux":
            self._run(["systemctl", "--user", "stop", f"{self.name}.service"])
        elif self.platform == "darwin":
            existing = self._run(["launchctl", "print", self.target], check=False)
            if existing.returncode == 0:
                self._run(["launchctl", "bootout", self.target])
        else:
            if self.status().running:
                self._run(["schtasks.exe", "/End", "/TN", self.name])
        return self.status()

    def uninstall(self) -> ServiceStatus:
        self._require_available()
        if not self._installed():
            return self.status()
        self.stop()
        self.disable()
        if self.platform == "win32":
            self._run(["schtasks.exe", "/Delete", "/TN", self.name, "/F"])
        self.definition_path.unlink(missing_ok=True)
        if self.platform == "linux":
            self._run(["systemctl", "--user", "daemon-reload"])
        # Agent ledgers, credentials and shared daemon state are intentionally retained.
        return self.status()
