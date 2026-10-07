"""Shared daemon lifecycle commands; native shutdown follows task cleanup."""

from __future__ import annotations

import argparse
import asyncio
import json
from dataclasses import asdict
from pathlib import Path

from chiliarch.daemon_client import DaemonClient, ensure_daemon, stop_daemon
from chiliarch.daemon_models import DaemonError
from chiliarch.daemon_services import ServiceManager, default_daemon_directory


def add_parser(commands: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    daemon = commands.add_parser("daemon", help="Manage the shared current-user agent daemon")
    actions = daemon.add_subparsers(dest="daemon_command", required=True)
    for name in ("install", "start", "run", "status", "stop", "restart", "agents", "uninstall"):
        command = actions.add_parser(name)
        command.add_argument("--directory", type=Path)
        if name in {"install", "start", "restart"}:
            command.add_argument("--no-autostart", action="store_true")
    autostart = actions.add_parser("autostart")
    autostart.add_argument("action", choices=("enable", "disable", "status"))
    autostart.add_argument("--directory", type=Path)
    register = actions.add_parser(
        "register", help="Register or update an independent agent identity"
    )
    register.add_argument("--directory", type=Path)
    register.add_argument("--state", type=Path, required=True)
    register.add_argument("--agent")
    register.add_argument("--settings", type=Path)
    register.add_argument("--max-concurrent-tasks", type=int, default=1)
    register.add_argument("--transport", choices=("ws", "sse", "poll"), default="ws")
    remove = actions.add_parser("remove", help="Stop one identity and retain its local credentials")
    remove.add_argument("--directory", type=Path)
    remove.add_argument("--agent", required=True)


async def main(arguments: argparse.Namespace) -> None:
    directory = (arguments.directory or default_daemon_directory()).expanduser().resolve()
    action = arguments.daemon_command
    manager = ServiceManager(directory)
    if action == "run":
        from chiliarch.daemon import run_daemon

        await run_daemon(directory)
        return
    if action in {"install", "start", "restart"}:
        if action == "restart":
            await stop_daemon(directory)
        result = await ensure_daemon(directory, autostart=not arguments.no_autostart)
    elif action in {"stop", "uninstall"}:
        result = await stop_daemon(directory)
        if action == "uninstall":
            service = await asyncio.to_thread(manager.status)
            if service.available:
                service = await asyncio.to_thread(manager.uninstall)
            result["autostart"] = asdict(service)
            result["credentials_preserved"] = True
    elif action == "autostart":
        if arguments.action == "enable":
            status = await asyncio.to_thread(manager.status)
            status = await asyncio.to_thread(
                manager.enable if status.installed else manager.install
            )
        elif arguments.action == "disable":
            status = await asyncio.to_thread(manager.disable)
        else:
            status = await asyncio.to_thread(manager.status)
        result = asdict(status)
    else:
        async with DaemonClient(directory) as client:
            if action == "status":
                result = {
                    "directory": str(directory),
                    "autostart": asdict(await asyncio.to_thread(manager.status)),
                }
                try:
                    result["daemon"] = await client.health()
                except DaemonError as error:
                    result["daemon"] = {"status": "unavailable", "detail": str(error)}
            elif action == "agents":
                result = {"agents": await client.agents()}
            elif action == "register":
                result = await client.register(
                    arguments.state,
                    handle=arguments.agent,
                    runner_settings=arguments.settings,
                    max_concurrent_tasks=arguments.max_concurrent_tasks,
                    preferred_transport=arguments.transport,
                )
            else:
                result = await client.unregister(arguments.agent)
    print(json.dumps(result, ensure_ascii=False, indent=2))
