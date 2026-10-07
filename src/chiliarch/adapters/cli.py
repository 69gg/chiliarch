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
from typing import TYPE_CHECKING, Any

import tomli_w

from chiliarch.adapters.runtime import connection_owner, outbound_client
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
    mcp = actions.add_parser("mcp", help="Serve the stdio MCP bridge; never opens a listening port")
    mcp.add_argument("--state", type=Path, required=True)
    mcp.add_argument("--mode", choices=("standalone", "mailbox"), default="standalone")
    mcp.add_argument("--managed-task")
    run = actions.add_parser("run", help="Run native CLI workers within local execution policy")
    run.add_argument("--settings", type=Path, required=True)
    run.add_argument("--state", type=Path, required=True)
    run.add_argument("--max-concurrent-tasks", type=int, default=1)
    for target in (mcp, run):
        target.add_argument("--transport", choices=("ws", "sse", "poll"), default="ws")
        target.add_argument("--log-directory", type=Path)
    config = actions.add_parser(
        "config", help="Print native MCP configuration; does not edit user files"
    )
    config.add_argument("--host", choices=HOSTS, required=True)
    config.add_argument("--state", type=Path, required=True)
    doctor = actions.add_parser(
        "doctor", help="Inspect local availability without invoking vendor agents"
    )
    doctor.add_argument("--state", type=Path)
    doctor.add_argument("--settings", type=Path)


def mcp_configuration(host: str, state_path: Path) -> dict[str, Any]:
    command = shutil.which("chiliarch")
    arguments: list[str] = []
    if command is None:
        command = sys.executable
        arguments.extend(["-m", "chiliarch"])
    arguments.extend(["agent", "mcp", "--state", str(state_path.expanduser().resolve())])
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
    state_path = arguments.state.expanduser().resolve()
    async with connection_owner(state_path):
        if await asyncio.to_thread(state_path.exists):
            # Never overwrite a credential/task ledger, even if the platform is unreachable.
            raise ValueError("State already exists; use another path for a new paired identity")
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
            ],
        )
    print(f"配对完成，凭证保存在 {state_path}，不在终端显示。")


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
        "managed_runner_platform": sys.platform != "win32",
    }
    if arguments.settings is not None:
        settings = await asyncio.to_thread(load_runner_settings, arguments.settings)
        report["runner"] = {
            "kind": settings.kind,
            "workdir": str(settings.workdir),
            "executable": shutil.which(
                settings.executable or ("codex" if settings.kind == "codex" else "claude")
            ),
            "permission": settings.codex_sandbox
            if settings.kind == "codex"
            else settings.claude_permission_mode,
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
    return report


def main(arguments: argparse.Namespace) -> None:
    if arguments.agent_command == "config":
        content = mcp_configuration(arguments.host, arguments.state)
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
    state_path = arguments.state.expanduser().resolve()
    configure_logging(
        LogSettings(directory=arguments.log_directory or state_path.parent / "logs"), shared=True
    )
    try:
        if arguments.agent_command == "mcp":
            from chiliarch.adapters.mcp import create_mcp_server

            create_mcp_server(
                state_path,
                mode=arguments.mode,
                preferred=arguments.transport,
                managed_task=arguments.managed_task,
            ).run(transport="stdio")
        else:
            asyncio.run(_run(arguments))
    finally:
        close_logging()
