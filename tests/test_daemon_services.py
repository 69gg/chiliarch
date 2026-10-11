from __future__ import annotations

import base64
import configparser
import json
import os
import plistlib
import shlex
import subprocess
import sys
import xml.etree.ElementTree as ET
from collections.abc import Sequence
from pathlib import Path
from typing import Any

import pytest

from chiliarch.daemon_services import (
    CommandResult,
    ServiceError,
    ServiceManager,
    default_daemon_directory,
)

NS = {"t": "http://schemas.microsoft.com/windows/2004/02/mit/task"}
SID = "S-1-5-21-111-222-333-1001"


class NativeManager:
    """Simulate native manager transitions while parsing the installed definitions."""

    def __init__(self) -> None:
        self.manager: ServiceManager
        self.calls: list[list[str]] = []
        self.enabled = False
        self.running = False
        self.loaded = False
        self.task: dict[str, Any] = {}
        self.fail_action: str | None = None
        self.foreign_fragment: Path | None = None

    def __call__(self, arguments: Sequence[str]) -> CommandResult:
        args = list(arguments)
        self.calls.append(args)
        if self.fail_action and self.fail_action in args:
            return CommandResult(1, stderr="registration denied")
        if args[0] == "whoami.exe":
            return CommandResult(0, f'"DOMAIN\\user","{SID}"\n')
        if args[0] == "systemctl":
            action = args[2]
            if action == "show":
                path = self.foreign_fragment or (
                    self.manager.definition_path if self.manager.definition_path.exists() else ""
                )
                return CommandResult(
                    0,
                    f"FragmentPath={path}\nActiveState={'active' if self.running else 'inactive'}\nUnitFileState={'enabled' if self.enabled else 'disabled'}\n",
                )
            if action in {"enable", "disable"}:
                self.enabled = action == "enable"
            if action in {"start", "stop"}:
                self.running = action == "start"
            return CommandResult(0)
        if args[0] == "launchctl":
            action = args[1]
            if action == "print" and len(args[2].split("/")) == 3:
                return CommandResult(
                    0 if self.loaded else 113,
                    f"path = {self.manager.definition_path}\nstate = {'running' if self.running else 'waiting'}\n",
                )
            if action == "print-disabled":
                return CommandResult(
                    0, f'"{self.manager.name}" => {str(not self.enabled).lower()}\n'
                )
            if action in {"enable", "disable"}:
                self.enabled = action == "enable"
            if action == "bootstrap":
                self.loaded = self.running = True
            if action == "kickstart":
                self.running = True
            if action == "bootout":
                self.loaded = self.running = False
            return CommandResult(0)
        if args[0] == "powershell.exe":
            return CommandResult(0, json.dumps(self.task, ensure_ascii=False))
        if args[0] == "schtasks.exe":
            action = args[1]
            if action == "/Create":
                path = Path(args[args.index("/XML") + 1])
                self.task = {
                    "xml": path.read_bytes().decode("utf-16"),
                    "enabled": True,
                    "running": False,
                }
            elif action == "/Change":
                self.task["enabled"] = "/Enable" in args
            elif action in {"/Run", "/End"}:
                self.task["running"] = action == "/Run"
            elif action == "/Delete":
                self.task = {}
            return CommandResult(0)
        raise AssertionError(f"Unexpected command: {args}")


def manager(tmp_path: Path, platform: str) -> tuple[ServiceManager, NativeManager]:
    native = NativeManager()
    service = ServiceManager(
        tmp_path / "共享 工作目录 %foo $bar",
        platform=platform,
        python_executable=tmp_path / "持久 uv tools" / "python",
        home=tmp_path / "用户 home",
        environment={
            "PATH": "/custom bin:/usr/bin",
            "LANG": "zh_CN.UTF-8",
            "API_KEY": "never-store",
        },
        executor=native,
        user_id=501,
    )
    native.manager = service
    return service, native


@pytest.mark.parametrize("platform", ["linux", "darwin", "win32"])
def test_current_user_service_lifecycle_retains_agent_state(tmp_path: Path, platform: str) -> None:
    service, native = manager(tmp_path, platform)
    assert service.status().available
    assert not service.status().installed
    status = service.install()
    assert status.installed and status.enabled and not status.running
    ledger = service.directory / "agent-ledger.db"
    ledger.write_bytes(b"existing agent state")
    assert service.start().running
    assert not service.disable().enabled
    assert service.enable().enabled
    assert not service.stop().running
    assert service.start().running
    assert not service.uninstall().installed
    assert ledger.read_bytes() == b"existing agent state"
    assert not service.definition_path.exists()
    assert not service.uninstall().installed
    if platform == "win32":
        create = next(call for call in native.calls if "/Create" in call)
        assert "/F" not in create
        assert not any("/RU" in call or "/RP" in call for call in native.calls)


def test_linux_definition_preserves_unicode_paths_and_disables_expansion(tmp_path: Path) -> None:
    service, _ = manager(tmp_path, "linux")
    parser = configparser.ConfigParser(interpolation=None)
    parser.read_string(service.definition().decode("utf-8"))
    command = parser["Service"]["ExecStart"]
    assert command.startswith(":")
    decoded = [part.replace("%%", "%") for part in shlex.split(command[1:])]
    assert decoded == service.command
    assert parser["Service"]["Restart"] == "on-failure"
    assert parser["Service"]["KillMode"] == "control-group"
    assert parser["Install"]["WantedBy"] == "default.target"
    variables = dict(
        part.replace("%%", "%").split("=", 1)
        for part in shlex.split(parser["Service"]["Environment"])
    )
    assert variables == {
        "HOME": str(service.home),
        "PYTHONUTF8": "1",
        "PATH": "/custom bin:/usr/bin",
        "LANG": "zh_CN.UTF-8",
    }


def test_mac_definition_uses_argument_array_and_current_user_domain(tmp_path: Path) -> None:
    service, native = manager(tmp_path, "darwin")
    definition = plistlib.loads(service.definition())
    assert definition["ProgramArguments"] == service.command
    assert definition["WorkingDirectory"] == str(service.directory)
    assert definition["EnvironmentVariables"]["PATH"] == "/custom bin:/usr/bin"
    assert "API_KEY" not in definition["EnvironmentVariables"]
    assert definition["KeepAlive"] == {"SuccessfulExit": False}
    service.install()
    service.start()
    assert ["launchctl", "bootstrap", "gui/501", str(service.definition_path)] in native.calls
    assert service.definition_path.parent == service.home / "Library" / "LaunchAgents"


def test_windows_definition_is_user_bound_unprivileged_and_suitable_for_daemon(
    tmp_path: Path,
) -> None:
    service, _ = manager(tmp_path, "win32")
    task = ET.fromstring(service.definition())
    assert task.findtext("t:Triggers/t:LogonTrigger/t:UserId", namespaces=NS) == SID
    assert task.findtext("t:Principals/t:Principal/t:UserId", namespaces=NS) == SID
    assert (
        task.findtext("t:Principals/t:Principal/t:LogonType", namespaces=NS) == "InteractiveToken"
    )
    assert task.findtext("t:Principals/t:Principal/t:RunLevel", namespaces=NS) == "LeastPrivilege"
    settings = task.find("t:Settings", NS)
    assert settings is not None
    assert settings.findtext("t:ExecutionTimeLimit", namespaces=NS) == "PT0S"
    assert settings.findtext("t:DisallowStartIfOnBatteries", namespaces=NS) == "false"
    assert settings.findtext("t:StopIfGoingOnBatteries", namespaces=NS) == "false"
    assert settings.findtext("t:MultipleInstancesPolicy", namespaces=NS) == "IgnoreNew"
    assert settings.findtext("t:RestartOnFailure/t:Interval", namespaces=NS) == "PT1M"
    assert task.findtext("t:Actions/t:Exec/t:Command", namespaces=NS) == str(service.python)
    assert task.findtext("t:Actions/t:Exec/t:Arguments", namespaces=NS) == subprocess.list2cmdline(
        service._windows_command()[1:]
    )
    assert task.findtext("t:Actions/t:Exec/t:WorkingDirectory", namespaces=NS) == str(
        service.directory
    )


def definition_environment(service: ServiceManager) -> dict[str, str]:
    content = service.definition()
    if service.platform == "linux":
        parser = configparser.ConfigParser(interpolation=None)
        parser.read_string(content.decode("utf-8"))
        return dict(
            part.replace("%%", "%").split("=", 1)
            for part in shlex.split(parser["Service"]["Environment"])
        )
    if service.platform == "darwin":
        variables: dict[str, str] = plistlib.loads(content)["EnvironmentVariables"]
        return variables
    arguments = ET.fromstring(content).findtext("t:Actions/t:Exec/t:Arguments", namespaces=NS)
    assert arguments is not None
    environment: dict[str, str]
    environment, command = json.loads(base64.b64decode(arguments.rsplit(" ", 1)[1]))
    assert command == service.command[2:]
    return environment


@pytest.mark.parametrize("platform", ["linux", "darwin", "win32"])
def test_startup_inherits_only_nonsecret_configuration_directories(
    tmp_path: Path, platform: str
) -> None:
    service, _ = manager(tmp_path, platform)
    directories = {
        key: str(tmp_path / f"用户 {key} %PATH% $literal `literal` 'quoted' \"double\" ")
        for key in (
            "CODEX_HOME",
            "CLAUDE_CONFIG_DIR",
            "XDG_CONFIG_HOME",
            "XDG_DATA_HOME",
            "XDG_STATE_HOME",
            "XDG_CACHE_HOME",
            "USERPROFILE",
            "APPDATA",
            "LOCALAPPDATA",
        )
    }
    service.environment.update(
        {
            **directories,
            "OPENAI_API_KEY": "secret-openai",
            "ANTHROPIC_API_KEY": "secret-anthropic",
            "CLAUDE_CODE_OAUTH_TOKEN": "secret-oauth",
            "HTTPS_PROXY": "https://user:password@proxy.invalid",
            "UNRELATED_CONFIG_DIR": "/unrelated",
        }
    )
    assert definition_environment(service) == {
        "HOME": str(service.home),
        "PYTHONUTF8": "1",
        "PATH": "/custom bin:/usr/bin",
        "LANG": "zh_CN.UTF-8",
        **directories,
    }


def test_windows_startup_applies_environment_in_same_process_without_shell(
    tmp_path: Path,
) -> None:
    service, _ = manager(tmp_path, "win32")
    literal = "C:\\用户 data\\%PATH%\\$value\\`value`\\'single'\\\"double\"\\"
    service.environment.update(
        {"CODEX_HOME": literal, "CLAUDE_CONFIG_DIR": literal, "XDG_CONFIG_HOME": literal}
    )
    # A harmless package stands in for the CLI; no daemon or agent is started.
    package = tmp_path / "chiliarch"
    package.mkdir()
    (package / "__init__.py").write_text("", encoding="utf-8")
    (package / "__main__.py").write_text(
        "import json, os, sys\n"
        "print(json.dumps({'environment': dict(os.environ), 'arguments': sys.argv[1:], 'pid': os.getpid()}))\n",
        encoding="utf-8",
    )
    command = service._windows_command()
    task = ET.fromstring(service.definition())
    assert task.findtext("t:Actions/t:Exec/t:Arguments", namespaces=NS) == subprocess.list2cmdline(
        command[1:]
    )
    inherited = {key: os.environ[key] for key in ("SYSTEMROOT", "WINDIR") if key in os.environ}
    inherited.update({"CODEX_HOME": "different-login-default", "UNRELATED_RUNTIME": "retained"})
    # A Windows venv launcher can have a different PID from its Python interpreter.
    # Measure inside that interpreter, immediately before running the real bootstrap.
    command[2] = "import os;print(os.getpid(),flush=True);" + command[2]
    process = subprocess.Popen(
        [sys.executable, *command[1:]],
        cwd=tmp_path,
        env=inherited,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
    )
    stdout, stderr = process.communicate(timeout=10)
    assert process.returncode == 0, stderr
    bootstrap_pid, payload = stdout.split("\n", 1)
    received = json.loads(payload)
    assert received["pid"] == int(bootstrap_pid)
    assert received["arguments"] == service.command[3:]
    assert received["environment"].items() >= definition_environment(service).items()
    assert received["environment"]["UNRELATED_RUNTIME"] == "retained"
    assert "API_KEY" not in received["environment"]


@pytest.mark.parametrize("platform", ["linux", "darwin", "win32"])
def test_installed_service_remains_owned_after_configuration_directory_changes(
    tmp_path: Path, platform: str
) -> None:
    service, _ = manager(tmp_path, platform)
    service.environment["CODEX_HOME"] = str(tmp_path / "original")
    assert service.install().installed
    service.environment["CODEX_HOME"] = str(tmp_path / "updated")
    assert service.status().installed
    assert service.install().installed
    assert definition_environment(service)["CODEX_HOME"] == str(tmp_path / "updated")


def test_windows_legacy_task_can_be_identified_and_upgraded(tmp_path: Path) -> None:
    service, native = manager(tmp_path, "win32")
    task = ET.fromstring(service.definition())
    arguments = task.find("t:Actions/t:Exec/t:Arguments", NS)
    assert arguments is not None
    arguments.text = subprocess.list2cmdline(service.command[1:])
    native.task = {"xml": ET.tostring(task, encoding="unicode")}
    assert service.status().installed
    service.environment["CODEX_HOME"] = str(tmp_path / "native config")
    assert service.install().installed
    assert definition_environment(service)["CODEX_HOME"] == str(tmp_path / "native config")
    assert "/F" in next(call for call in native.calls if "/Create" in call)


@pytest.mark.parametrize("tampering", ["bootstrap", "environment", "arguments", "encoding"])
def test_windows_task_with_altered_startup_is_not_owned(tmp_path: Path, tampering: str) -> None:
    service, native = manager(tmp_path, "win32")
    task = ET.fromstring(service.definition())
    arguments = task.find("t:Actions/t:Exec/t:Arguments", NS)
    assert arguments is not None
    command = service._windows_command()
    payload = json.loads(base64.b64decode(command[-1]))
    if tampering == "bootstrap":
        command[2] += ";print('injected')"
    elif tampering == "environment":
        payload[0]["PYTHONPATH"] = "/untrusted"
        command[-1] = base64.b64encode(json.dumps(payload).encode()).decode()
    elif tampering == "arguments":
        payload[1][-1] = "/another-daemon"
        command[-1] = base64.b64encode(json.dumps(payload).encode()).decode()
    else:
        command[-1] = "invalid-base64"
    arguments.text = subprocess.list2cmdline(command[1:])
    native.task = {"xml": ET.tostring(task, encoding="unicode")}
    with pytest.raises(ServiceError, match="unowned|another application"):
        service.install()
    assert not any("/Create" in call for call in native.calls)


@pytest.mark.parametrize("platform", ["linux", "darwin", "win32"])
def test_foreign_same_named_definition_cannot_be_overwritten_or_removed(
    tmp_path: Path, platform: str
) -> None:
    service, native = manager(tmp_path, platform)
    if platform == "win32":
        native.task = {
            "xml": '<Task xmlns="http://schemas.microsoft.com/windows/2004/02/mit/task"><Actions /></Task>'
        }
    else:
        service.definition_path.parent.mkdir(parents=True)
        service.definition_path.write_bytes(b"another application's startup configuration")
    for action in (service.install, service.uninstall, service.enable, service.start, service.stop):
        with pytest.raises(ServiceError, match="unowned|another application"):
            action()
    assert not any(
        "enable" in call or "/Create" in call or "/Delete" in call for call in native.calls
    )


def test_shadowing_systemd_definition_is_preserved(tmp_path: Path) -> None:
    service, native = manager(tmp_path, "linux")
    native.foreign_fragment = tmp_path / "different.service"
    with pytest.raises(ServiceError, match="shadows"):
        service.install()
    assert not service.definition_path.exists()


def test_loaded_foreign_launchd_label_is_preserved(tmp_path: Path) -> None:
    service, native = manager(tmp_path, "darwin")
    native.loaded = True
    with pytest.raises(ServiceError, match="unowned launchd"):
        service.install()
    assert not service.definition_path.exists()


@pytest.mark.parametrize("platform", ["linux", "darwin", "win32"])
def test_missing_manager_does_not_claim_autostart_and_does_not_write_files(
    tmp_path: Path, platform: str
) -> None:
    def unavailable(arguments: Sequence[str]) -> CommandResult:
        raise FileNotFoundError(arguments[0])

    service = ServiceManager(
        tmp_path / "daemon", platform=platform, home=tmp_path, executor=unavailable
    )
    assert not service.available()
    status = service.status()
    assert (
        not status.available and not status.installed and not status.enabled and not status.running
    )
    with pytest.raises(ServiceError, match="foreground"):
        service.install()
    assert not service.definition_path.exists()


@pytest.mark.parametrize(
    "platform,action", [("linux", "enable"), ("darwin", "enable"), ("win32", "/Create")]
)
def test_registration_failure_is_reported_without_claiming_success(
    tmp_path: Path, platform: str, action: str
) -> None:
    service, native = manager(tmp_path, platform)
    native.fail_action = action
    with pytest.raises(ServiceError, match="registration denied"):
        service.install()
    assert not native.enabled
    assert not native.task


def test_directory_defaults_use_user_platform_conventions_and_xdg(tmp_path: Path) -> None:
    assert (
        default_daemon_directory(platform="linux", home=tmp_path, environment={})
        == tmp_path / ".local" / "state" / "chiliarch" / "daemon"
    )
    assert (
        default_daemon_directory(
            platform="linux", home=tmp_path, environment={"XDG_STATE_HOME": str(tmp_path / "state")}
        )
        == tmp_path / "state" / "chiliarch" / "daemon"
    )
    assert (
        default_daemon_directory(
            platform="linux", home=tmp_path, environment={"XDG_STATE_HOME": "relative"}
        )
        == tmp_path / ".local" / "state" / "chiliarch" / "daemon"
    )
    assert (
        default_daemon_directory(platform="darwin", home=tmp_path, environment={})
        == tmp_path / "Library" / "Application Support" / "chiliarch" / "daemon"
    )
    assert (
        default_daemon_directory(
            platform="win32", home=tmp_path, environment={"LOCALAPPDATA": str(tmp_path / "local")}
        )
        == tmp_path / "local" / "chiliarch" / "daemon"
    )


def test_unsupported_platform_remains_explicit_foreground(tmp_path: Path) -> None:
    service = ServiceManager(tmp_path, platform="unknown")
    assert service.status().backend == "foreground"
    assert not service.available()
    with pytest.raises(ServiceError, match="foreground"):
        service.install()


@pytest.mark.skipif(
    os.name == "nt", reason="Unprivileged Windows users cannot always create symlinks"
)
def test_service_definition_symlink_is_not_replaced(tmp_path: Path) -> None:
    service, _ = manager(tmp_path, "linux")
    source = tmp_path / "source"
    source.write_bytes(service.definition())
    service.definition_path.parent.mkdir(parents=True)
    service.definition_path.symlink_to(source)
    with pytest.raises(ServiceError, match="symlink"):
        service.install()
    assert service.definition_path.is_symlink()
    assert source.read_bytes() == service.definition()


def test_windows_current_user_lookup_is_required(tmp_path: Path) -> None:
    service, native = manager(tmp_path, "win32")
    native.fail_action = "/user"
    with pytest.raises(ServiceError, match="registration denied"):
        service.install()
    assert not native.task


def test_unit_control_character_rejected_before_any_registration(tmp_path: Path) -> None:
    service, native = manager(tmp_path, "linux")
    service.command[-1] += "\nExecStart=/unsafe"
    with pytest.raises(ServiceError, match="control characters"):
        service.install()
    assert not any("enable" in call for call in native.calls)
