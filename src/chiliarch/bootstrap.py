"""Choose an installed runtime before starting services or opening state stores."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

_BOOTSTRAPPED = "CHILIARCH_UPDATE_BOOTSTRAPPED"


def _checks_on_start(arguments: list[str]) -> bool:
    """Use the real parser, so option values cannot masquerade as commands."""
    if any(argument in {"--help", "-h", "--version"} for argument in arguments):
        return False
    from chiliarch.cli import parser

    parsed = parser().parse_args(arguments)
    if parsed.command in {"serve", "demo-agent"}:
        return True
    if parsed.command == "daemon":
        return parsed.daemon_command in {"install", "start", "restart", "run"}
    if parsed.command == "agent":
        return parsed.agent_command in {"pair", "run"} or (
            parsed.agent_command == "mcp"
            and (parsed.mode == "standalone" or (parsed.mode is None and parsed.state is not None))
        )
    return False


def _dispatch(python: Path, arguments: list[str]) -> None:
    environment = dict(os.environ, **{_BOOTSTRAPPED: "1"})
    # Isolate module discovery from the user's current directory/PYTHONPATH. The
    # working directory, arguments, native agent settings and stdio are retained.
    command = [str(python), "-I", "-X", "utf8", "-m", "chiliarch", *arguments]
    if os.name != "nt":
        os.execve(str(python), command, environment)
        return
    # Windows has no POSIX exec: keep the launcher alive so the Scheduled Task
    # and foreground shell track the complete service lifetime. Console Ctrl+C
    # also reaches the child, which owns its normal graceful shutdown.
    with subprocess.Popen(command, env=environment) as child:
        while True:
            try:
                raise SystemExit(child.wait())
            except KeyboardInterrupt:
                continue


def main() -> None:
    arguments = sys.argv[1:]
    # Consume this marker; native workers and later daemon starts must not
    # accidentally inherit a permanent opt-out from update checks.
    if os.environ.pop(_BOOTSTRAPPED, None) != "1":
        from chiliarch.updates import select_runtime

        python = select_runtime(check=_checks_on_start(arguments))
        if python is not None:
            try:
                _dispatch(python, arguments)
                return
            except OSError:
                print("Chiliarch 新版无法启动，继续使用当前版本。", file=sys.stderr)
    from chiliarch.cli import main as cli_main

    cli_main()
