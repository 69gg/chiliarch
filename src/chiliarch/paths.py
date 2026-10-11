"""Configuration locations for persistent installations and source checkouts."""

from pathlib import Path

from chiliarch.daemon_services import default_daemon_directory
from chiliarch.updates import uv_tool_origin


def default_config_path() -> Path:
    """Use stable user state for uv tools, or the current directory for development."""
    if uv_tool_origin() is not None:
        return default_daemon_directory().parent / "server" / "chiliarch.toml"
    return Path.cwd() / "chiliarch.toml"
