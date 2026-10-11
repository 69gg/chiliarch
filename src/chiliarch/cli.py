"""Project initialization, serving and outbound protocol demonstrations."""

from __future__ import annotations

import argparse
import asyncio
import getpass
import json
import socket
from pathlib import Path

from chiliarch import __version__
from chiliarch.config import Settings, SettingsManager, atomic_write


def parser() -> argparse.ArgumentParser:
    root = argparse.ArgumentParser(
        prog="chiliarch", description="Outbound-only agent orchestration"
    )
    root.add_argument("--version", action="version", version=f"chiliarch {__version__}")
    root.add_argument("--config", type=Path, default=Path("chiliarch.toml"))
    commands = root.add_subparsers(dest="command", required=True)
    initialize = commands.add_parser(
        "init", help="Initialize TOML configuration and a password hash"
    )
    initialize.add_argument(
        "--force", action="store_true", help="Replace an existing configuration"
    )
    serve = commands.add_parser("serve", help="Start the single-process HTTP/WS server")
    serve.add_argument("--host")
    serve.add_argument("--port", type=int)
    schema = commands.add_parser("schema", help="Export the independent protocol JSON Schema")
    schema.add_argument("--output", type=Path)
    backup = commands.add_parser("backup", help="Create a consistent online SQLite snapshot")
    backup.add_argument("--output", type=Path, required=True)
    backup.add_argument(
        "--database", type=Path, help="Explicit SQLite source instead of saved config"
    )
    backup.add_argument("--timeout", type=float, default=30, help="Backup deadline in seconds")
    pair = commands.add_parser("pair", help="Redeem an agent registration code from this machine")
    pair.add_argument("--server", required=True)
    pair.add_argument("--code", help="Omit to enter the one-time code interactively")
    pair.add_argument("--name", required=True)
    pair.add_argument("--machine", default=socket.gethostname())
    pair.add_argument("--state", type=Path, default=Path("data/agent.db"))
    demo = commands.add_parser(
        "demo-agent", help="Run the paired simulated agent; performs no external work"
    )
    demo.add_argument("--state", type=Path, default=Path("data/agent.db"))
    demo.add_argument("--transport", choices=("ws", "sse", "poll"), default="ws")
    from chiliarch.adapters.cli import add_parser
    from chiliarch.daemon_cli import add_parser as add_daemon_parser

    add_parser(commands)
    add_daemon_parser(commands)
    for command in (initialize, serve, schema, backup, pair, demo):
        command.add_argument("--config", type=Path, default=argparse.SUPPRESS)
    return root


def initialize(path: Path, *, force: bool = False) -> None:
    from chiliarch.auth import hash_password

    if path.exists() and not force:
        raise ValueError("Configuration exists; use --force only if you intend to replace it")
    password = getpass.getpass("WebUI 密码（至少 8 字符）: ")
    if password != getpass.getpass("再次输入密码: "):
        raise ValueError("两次密码不一致")
    settings = Settings()
    settings.auth.password_hash = hash_password(password)
    atomic_write(path.resolve(), settings)
    print(f"已创建配置：{path.resolve()}。运行 chiliarch serve 启动。")


async def _agent_command(arguments: argparse.Namespace) -> None:
    from chiliarch.client import AgentClient, simulated_task
    from chiliarch.logging import close_logging, configure_logging

    manager = SettingsManager(arguments.config)
    configure_logging(manager.current.logs, shared=True)
    try:
        if arguments.command == "pair":
            code = arguments.code or getpass.getpass("一次性注册码: ")
            await AgentClient.pair(
                arguments.server,
                code,
                arguments.name,
                arguments.machine,
                arguments.state,
                capabilities=["simulated.tasks"],
            )
            print(f"配对完成。本地凭证保存在 {arguments.state.resolve()}，不在终端显示。")
        else:
            client = await AgentClient.from_state(
                arguments.state, simulated_task, options=manager.current.transport
            )
            print("模拟 agent 已启动；只验证协议，不操作真实厂商 agent。Ctrl+C 停止。")
            await client.run(preferred=arguments.transport)
    finally:
        close_logging()


def main() -> None:
    arguments = parser().parse_args()
    try:
        if arguments.command == "daemon":
            from chiliarch.daemon_cli import main as daemon_main

            asyncio.run(daemon_main(arguments))
        elif arguments.command == "agent":
            from chiliarch.adapters.cli import main as agent_main

            agent_main(arguments)
        elif arguments.command == "init":
            initialize(arguments.config, force=arguments.force)
        elif arguments.command == "schema":
            from chiliarch.protocol import export_schema, protocol_schema

            if arguments.output:
                export_schema(arguments.output)
            else:
                print(json.dumps(protocol_schema(), ensure_ascii=False, indent=2))
        elif arguments.command == "backup":
            from sqlalchemy.engine import URL

            from chiliarch.backup import backup_database, sqlite_source

            if arguments.database is not None:
                url = URL.create(
                    "sqlite", database=str(arguments.database.expanduser().resolve())
                ).render_as_string()
            elif not arguments.config.is_file():
                raise ValueError("请指定现有 chiliarch.toml 配置文件")
            else:
                url = SettingsManager(arguments.config).current.database.url
            print(f"备份源：{sqlite_source(url)}")
            output = backup_database(url, arguments.output, timeout=arguments.timeout)
            print(f"数据库快照已校验并保存：{output}。请另行保管配置和日志。")
        elif arguments.command == "serve":
            import uvicorn

            from chiliarch.api import create_app

            if not arguments.config.exists():
                raise ValueError("请先运行 chiliarch init 初始化登录密码和配置")
            application = create_app(arguments.config)
            manager: SettingsManager = application.state.settings_manager
            if not manager.current.auth.password_hash:
                raise ValueError("配置中缺少登录密码，请运行 chiliarch init")
            # Runtime overrides must not become saved TOML or persist environment secrets.
            if arguments.host is not None:
                manager.current.server.host = arguments.host
            if arguments.port is not None:
                manager.current.server.port = arguments.port
            uvicorn.run(
                application,
                host=manager.current.server.host,
                port=manager.current.server.port,
                workers=1,
                log_config=None,
                timeout_graceful_shutdown=manager.current.server.graceful_shutdown_seconds,
            )
        else:
            asyncio.run(_agent_command(arguments))
    except KeyboardInterrupt:
        pass
    except (ValueError, OSError, RuntimeError) as error:
        raise SystemExit(str(error)) from error
