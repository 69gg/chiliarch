"""Agent-machine commands, independent of the platform's password/configuration."""

from __future__ import annotations

import argparse
import asyncio
import getpass
import importlib.metadata
import json
import shutil
import socket
import sys
import tomllib
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal

import tomli_w

from chiliarch.adapters.runtime import connection_owner, outbound_client, recover_workspace
from chiliarch.client import AgentClient, ClientStore
from chiliarch.config import LogSettings
from chiliarch.logging import close_logging, configure_logging

HOSTS = ("codex-app", "codex", "claude-code")

if TYPE_CHECKING:
    from chiliarch.adapters.runners import RunnerSettings


def add_parser(commands: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    agent = commands.add_parser(
        "agent", help="Connect real agents through MCP or managed CLI workers"
    )
    actions = agent.add_subparsers(dest="agent_command", required=True)
    pair = actions.add_parser("pair", help="Pair this machine using a one-time registration code")
    pair.add_argument("--server", required=True)
    pair.add_argument("--code", help="Omit to enter the one-time code privately")
    pair.add_argument("--name", required=True)
    pair.add_argument("--machine", default=socket.gethostname())
    pair.add_argument("--adapter", choices=HOSTS, required=True)
    pair.add_argument("--state", type=Path, required=True)
    pair.add_argument(
        "--agent", help="Stable local handle; defaults to the paired platform identity"
    )
    pair.add_argument("--daemon-directory", type=Path)
    pair.add_argument("--mode", choices=("managed", "manual"), default="managed")
    local = pair.add_mutually_exclusive_group()
    local.add_argument("--workdir", type=Path, help="Default local workspace for unattended tasks")
    local.add_argument("--settings", type=Path, help="Existing local runner TOML")
    mcp = actions.add_parser("mcp", help="Serve the stdio MCP bridge; never opens a listening port")
    target = mcp.add_mutually_exclusive_group(required=True)
    target.add_argument("--state", type=Path, help="Legacy standalone/mailbox state")
    target.add_argument("--agent", help="Registered local daemon handle")
    mcp.add_argument("--daemon-directory", type=Path)
    mcp.add_argument("--mode", choices=("daemon", "standalone", "mailbox"))
    mcp.add_argument("--managed-task")
    run = actions.add_parser("run", help="Run native CLI workers within local execution policy")
    run.add_argument("--settings", type=Path, required=True)
    run.add_argument("--state", type=Path, required=True)
    run.add_argument("--max-concurrent-tasks", type=int, default=1)
    for bridge in (mcp, run):
        bridge.add_argument("--transport", choices=("ws", "sse", "poll"), default="ws")
        bridge.add_argument("--log-directory", type=Path)
    config = actions.add_parser(
        "config", help="Print native MCP configuration; does not edit user files"
    )
    config.add_argument("--host", choices=HOSTS, required=True)
    target = config.add_mutually_exclusive_group(required=True)
    target.add_argument("--state", type=Path, help="Legacy standalone/mailbox state")
    target.add_argument("--agent", help="Registered local daemon handle")
    config.add_argument("--mode", choices=("daemon", "standalone", "mailbox"))
    config.add_argument("--daemon-directory", type=Path)
    doctor = actions.add_parser(
        "doctor", help="Inspect local availability without invoking vendor agents"
    )
    target = doctor.add_mutually_exclusive_group()
    target.add_argument(
        "--state", type=Path, help="Read a legacy private state without network calls"
    )
    target.add_argument("--agent", help="Check one registered daemon identity")
    doctor.add_argument("--daemon-directory", type=Path)
    doctor.add_argument("--settings", type=Path)
    recovery = actions.add_parser(
        "workspace-recover",
        help="Inspect workspace isolation; clear only after confirming native workers stopped",
    )
    recovery.add_argument("--workdir", type=Path, required=True)
    recovery.add_argument("--confirm-stopped", action="store_true")


def _bridge_mode(arguments: argparse.Namespace) -> Literal["daemon", "standalone", "mailbox"]:
    mode: Literal["daemon", "standalone", "mailbox"] = arguments.mode or (
        "standalone" if arguments.state is not None else "daemon"
    )
    if mode == "daemon" and arguments.agent is None:
        raise ValueError(
            "Daemon MCP requires --agent; legacy state uses --mode standalone or mailbox"
        )
    if mode != "daemon" and arguments.state is None:
        raise ValueError("Standalone/mailbox MCP requires --state")
    return mode


def mcp_configuration(
    host: str,
    state_path: Path | None = None,
    *,
    agent_handle: str | None = None,
    daemon_directory: Path | None = None,
    mode: str | None = None,
) -> dict[str, Any]:
    from chiliarch.daemon_services import default_daemon_directory

    selected_mode = mode or ("standalone" if state_path is not None else "daemon")
    if selected_mode not in {"daemon", "standalone", "mailbox"}:
        raise ValueError("Unknown MCP bridge mode")
    if (state_path is None) == (agent_handle is None):
        raise ValueError("Provide either one daemon agent handle or one legacy state path")
    if (selected_mode == "daemon") != (agent_handle is not None):
        raise ValueError("Daemon mode requires --agent; standalone/mailbox requires --state")
    command = shutil.which("chiliarch")
    arguments: list[str] = []
    if command is None:
        command = sys.executable
        arguments.extend(["-m", "chiliarch"])
    else:
        command = str(Path(command).expanduser().resolve())
    arguments.extend(["agent", "mcp", "--mode", selected_mode])
    if agent_handle is not None:
        directory = (daemon_directory or default_daemon_directory()).expanduser().resolve()
        arguments.extend(["--daemon-directory", str(directory), "--agent", agent_handle])
    else:
        assert state_path is not None
        arguments.extend(["--state", str(state_path.expanduser().resolve())])
    entry: dict[str, Any] = {"command": command, "args": arguments}
    if host in {"codex-app", "codex"}:
        entry["startup_timeout_sec"] = 30
        entry["tool_timeout_sec"] = 60
        return {"mcp_servers": {"chiliarch": entry}}
    if host == "claude-code":
        return {"mcpServers": {"chiliarch": {"type": "stdio", **entry}}}
    raise ValueError("Unknown MCP host")


def load_runner_settings(path: Path) -> RunnerSettings:
    from chiliarch.adapters.runners import RunnerSettings

    with path.expanduser().open("rb") as stream:
        data = tomllib.load(stream)
    # Relative workdirs are relative to this trusted local settings file.
    if isinstance(data.get("workdir"), str):
        workdir = Path(data["workdir"]).expanduser()
        if not workdir.is_absolute():
            data["workdir"] = str(path.expanduser().resolve().parent / workdir)
    return RunnerSettings.model_validate(data)


async def _pair(arguments: argparse.Namespace) -> None:
    from chiliarch.adapters.runners import RunnerSettings
    from chiliarch.config import atomic_write_toml
    from chiliarch.daemon_client import DaemonClient, ensure_daemon

    state_path = arguments.state.expanduser().resolve()
    settings_path: Path | None = None
    generated: RunnerSettings | None = None
    async with connection_owner(state_path):
        if await asyncio.to_thread(state_path.exists):
            # Never overwrite a credential/task ledger, even if the platform is unreachable.
            raise ValueError("State already exists; use another path for a new paired identity")
        await ensure_daemon(arguments.daemon_directory)
        if arguments.mode == "manual":
            if arguments.workdir is not None or arguments.settings is not None:
                raise ValueError("Manual mode does not use runner settings or a default workdir")
        else:
            kind = "claude-code" if arguments.adapter == "claude-code" else "codex"
            if arguments.settings is not None:
                settings_path = arguments.settings.expanduser().resolve()
                settings = load_runner_settings(settings_path)
                if settings.kind != kind:
                    raise ValueError("Runner kind must match the paired adapter")
            else:
                workdir = arguments.workdir
                if workdir is None:
                    entered = (await asyncio.to_thread(input, "默认工作目录（已有目录）: ")).strip()
                    if not entered:
                        raise ValueError("A default workdir is required for managed mode")
                    workdir = Path(entered)
                generated = RunnerSettings(kind=kind, workdir=workdir)
                settings_path = state_path.with_suffix(".runner.toml")
                if settings_path.exists():
                    raise ValueError("Runner settings already exist; pass --settings to reuse them")
        await AgentClient.pair(
            arguments.server,
            arguments.code or getpass.getpass("一次性注册码: "),
            arguments.name,
            arguments.machine,
            state_path,
            capabilities=[
                "chiliarch.tasks",
                "chiliarch.contacts",
                "chiliarch.requests",
                f"adapter.{arguments.adapter}",
                *(["chiliarch.task_workdir"] if settings_path is not None else []),
            ],
        )
        if generated is not None:
            assert settings_path is not None
            atomic_write_toml(settings_path, generated.model_dump(mode="json", exclude_none=True))
    async with DaemonClient(arguments.daemon_directory) as daemon:
        registration = await daemon.register(
            state_path, handle=arguments.agent, runner_settings=settings_path
        )
    print(f"配对完成，本地标识 {registration['handle']}；凭证保存在 {state_path}，不在终端显示。")
    if settings_path is not None:
        print(f"无人值守已登记；默认工作目录配置：{settings_path}")


async def _run(arguments: argparse.Namespace) -> None:
    from chiliarch.adapters.runners import create_handler

    settings = await asyncio.to_thread(load_runner_settings, arguments.settings)
    state_path = arguments.state.expanduser().resolve()
    handler = create_handler(settings, state_path)
    async with outbound_client(
        state_path,
        handler=handler,
        preferred=arguments.transport,
        max_concurrent_tasks=arguments.max_concurrent_tasks,
    ) as (_, task):
        await task


async def _doctor(arguments: argparse.Namespace) -> dict[str, Any]:
    report: dict[str, Any] = {
        "chiliarch": importlib.metadata.version("chiliarch"),
        "python": sys.version.split()[0],
        "codex_executable": shutil.which("codex"),
        "claude_executable": shutil.which("claude"),
        "managed_runner_platform": sys.platform in {"linux", "darwin", "win32"},
    }
    if arguments.settings is not None:
        settings = await asyncio.to_thread(load_runner_settings, arguments.settings)
        report["runner"] = {
            "kind": settings.kind,
            "workdir": str(settings.workdir),
            "executable": shutil.which(
                settings.executable or ("codex" if settings.kind == "codex" else "claude")
            ),
            "permission": "native",
            "configuration_source": "native_user_and_project",
        }
    if arguments.state is not None:
        state_path = arguments.state.expanduser().resolve()
        if not await asyncio.to_thread(state_path.is_file):
            report["paired"] = False
        else:
            store = ClientStore(state_path)
            await store.open()
            try:
                identity = await store.identity()
                report["paired"] = bool(identity.get("credential") and identity.get("server_url"))
                report["identity"] = {
                    key: identity[key]
                    for key in ("agent_id", "name", "machine", "server_url")
                    if key in identity
                }
            finally:
                await store.close()
    else:
        from chiliarch.daemon_client import DaemonClient
        from chiliarch.daemon_models import DaemonError

        try:
            async with DaemonClient(arguments.daemon_directory) as daemon:
                report["daemon"] = await daemon.health()
                registrations = await daemon.agents()
                if arguments.agent is not None:
                    registrations = [
                        agent for agent in registrations if agent.get("handle") == arguments.agent
                    ]
                    report["registered"] = bool(registrations)
                report["agents"] = registrations
        except (DaemonError, OSError) as error:
            report["daemon"] = {"status": "unavailable", "error": str(error)}
            if arguments.agent is not None:
                report["registered"] = False
    return report


def main(arguments: argparse.Namespace) -> None:
    if arguments.agent_command == "workspace-recover":
        print(
            json.dumps(
                asyncio.run(
                    recover_workspace(arguments.workdir, confirm_stopped=arguments.confirm_stopped)
                ),
                ensure_ascii=False,
                indent=2,
            )
        )
        return
    if arguments.agent_command == "config":
        content = mcp_configuration(
            arguments.host,
            arguments.state,
            agent_handle=arguments.agent,
            daemon_directory=arguments.daemon_directory,
            mode=_bridge_mode(arguments),
        )
        print(
            tomli_w.dumps(content)
            if arguments.host != "claude-code"
            else json.dumps(content, ensure_ascii=False, indent=2)
        )
        return
    if arguments.agent_command == "doctor":
        print(json.dumps(asyncio.run(_doctor(arguments)), ensure_ascii=False, indent=2))
        return
    if arguments.agent_command == "pair":
        asyncio.run(_pair(arguments))
        return
    if arguments.agent_command == "mcp":
        mode = _bridge_mode(arguments)
        if mode == "daemon":
            from chiliarch.daemon_services import default_daemon_directory

            directory = (
                (arguments.daemon_directory or default_daemon_directory()).expanduser().resolve()
            )
            configure_logging(
                LogSettings(directory=arguments.log_directory or directory / "logs"), shared=True
            )
            try:
                from chiliarch.adapters.mcp import create_mcp_server

                create_mcp_server(
                    mode="daemon",
                    daemon_directory=directory,
                    agent_handle=arguments.agent,
                    preferred=arguments.transport,
                    managed_task=arguments.managed_task,
                ).run(transport="stdio")
            finally:
                close_logging()
            return
    state_path = arguments.state.expanduser().resolve()
    configure_logging(
        LogSettings(directory=arguments.log_directory or state_path.parent / "logs"), shared=True
    )
    try:
        if arguments.agent_command == "mcp":
            from chiliarch.adapters.mcp import create_mcp_server

            create_mcp_server(
                state_path,
                mode=mode,
                preferred=arguments.transport,
                managed_task=arguments.managed_task,
            ).run(transport="stdio")
        else:
            asyncio.run(_run(arguments))
    finally:
        close_logging()
