from __future__ import annotations

import hashlib
import json
import os
import sys
from pathlib import Path
from typing import Any

import pytest
import tomli_w

from chiliarch import paths, updates
from chiliarch.daemon_services import default_daemon_directory


def tool_environment(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, *, requirement: Any = None
) -> tuple[Path, Path]:
    origin = tmp_path / "arbitrary tools location" / "chiliarch"
    origin.mkdir(parents=True)
    (origin / "uv-receipt.toml").write_text(
        tomli_w.dumps({"tool": {"requirements": [requirement or {"name": "chiliarch"}]}}),
        encoding="utf-8",
    )
    state = tmp_path / "user state" / "chiliarch" / "daemon"
    monkeypatch.setattr(sys, "prefix", str(origin))
    monkeypatch.setattr(paths, "default_daemon_directory", lambda: state)
    monkeypatch.setattr(updates, "default_daemon_directory", lambda: state)
    monkeypatch.setattr(
        updates.shutil, "which", lambda name: pytest.fail("Path resolution queried uv")
    )
    monkeypatch.setattr(
        updates.urllib.request,
        "urlopen",
        lambda *args, **kwargs: pytest.fail("Path resolution queried the network"),
    )
    return origin, state


@pytest.mark.parametrize(
    "requirement",
    [
        {"name": "chiliarch"},
        "chiliarch==0.4.0",
        {"name": "chiliarch", "specifier": "==0.4.0"},
        {"name": "chiliarch", "directory": "/local/source", "editable": True},
        {"name": "chiliarch", "git": "https://example.invalid/chiliarch.git"},
        "chiliarch @ https://example.invalid/chiliarch.whl",
    ],
)
def test_tool_config_location_does_not_depend_on_update_eligibility(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, requirement: Any
) -> None:
    origin, state = tool_environment(tmp_path, monkeypatch, requirement=requirement)
    monkeypatch.setenv("CHILIARCH_AUTO_UPDATE", "0")
    receipt = tomli_w.dumps(
        {
            "tool": {
                "requirements": [requirement],
                "options": {"index-url": "https://private.invalid/simple"},
            }
        }
    )
    (origin / "uv-receipt.toml").write_text(receipt, encoding="utf-8")
    expected = state.parent / "server" / "chiliarch.toml"
    assert paths.default_config_path() == expected
    other_directory = tmp_path / "another working directory"
    other_directory.mkdir()
    monkeypatch.chdir(other_directory)
    assert paths.default_config_path() == expected
    assert not state.exists()
    assert (origin / "uv-receipt.toml").read_text(encoding="utf-8") == receipt


@pytest.mark.parametrize(
    ("platform", "environment", "relative_root"),
    [
        ("linux", {}, ".local/state"),
        ("linux", {"XDG_STATE_HOME": "relative"}, ".local/state"),
        ("darwin", {}, "Library/Application Support"),
        ("win32", {}, "AppData/Local"),
    ],
)
def test_tool_config_uses_platform_user_state_directory(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    platform: str,
    environment: dict[str, str],
    relative_root: str,
) -> None:
    tool_environment(tmp_path, monkeypatch)
    monkeypatch.setattr(
        paths,
        "default_daemon_directory",
        lambda: default_daemon_directory(platform=platform, home=tmp_path, environment=environment),
    )
    assert (
        paths.default_config_path() == tmp_path / relative_root / "chiliarch/server/chiliarch.toml"
    )


@pytest.mark.parametrize(
    ("platform", "variable"), [("linux", "XDG_STATE_HOME"), ("win32", "LOCALAPPDATA")]
)
def test_tool_config_respects_user_state_override(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, platform: str, variable: str
) -> None:
    tool_environment(tmp_path, monkeypatch)
    configured = tmp_path / "custom state"
    monkeypatch.setattr(
        paths,
        "default_daemon_directory",
        lambda: default_daemon_directory(
            platform=platform, home=tmp_path, environment={variable: str(configured)}
        ),
    )
    assert paths.default_config_path() == configured / "chiliarch/server/chiliarch.toml"


def test_source_or_plain_venv_uses_current_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A directory named uv is not evidence that this is a uv tool installation.
    prefix = tmp_path / "uv" / "chiliarch"
    prefix.mkdir(parents=True)
    monkeypatch.setattr(sys, "prefix", str(prefix))
    monkeypatch.chdir(tmp_path)
    assert paths.default_config_path() == tmp_path / "chiliarch.toml"
    elsewhere = tmp_path / "project"
    elsewhere.mkdir()
    monkeypatch.chdir(elsewhere)
    assert paths.default_config_path() == elsewhere / "chiliarch.toml"


@pytest.mark.parametrize(
    "receipt", ["[broken", "[tool]\nrequirements=[]", "[tool]\nrequirements=['other-package']"]
)
def test_invalid_or_unrelated_receipt_does_not_select_user_config(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, receipt: str
) -> None:
    origin, _ = tool_environment(tmp_path, monkeypatch)
    (origin / "uv-receipt.toml").write_text(receipt, encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    assert paths.default_config_path() == tmp_path / "chiliarch.toml"


def test_updated_runtime_keeps_original_tool_config(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    origin, state = tool_environment(tmp_path, monkeypatch)
    original_path = paths.default_config_path()
    key = hashlib.sha256(os.fsencode(origin.resolve())).hexdigest()[:24]
    runtime = state.parent / "updates" / key / "runtimes" / "0.4.0-runtime"
    runtime.mkdir(parents=True)
    (runtime / updates.ORIGIN_MARKER).write_text(
        json.dumps({"origin": str(origin.resolve())}), encoding="utf-8"
    )
    monkeypatch.setattr(sys, "prefix", str(runtime))
    monkeypatch.setenv("CHILIARCH_AUTO_UPDATE", "0")
    assert updates.uv_tool_origin() == origin.resolve()
    assert paths.default_config_path() == original_path


@pytest.mark.parametrize("raw_origin", ["relative", "valid"])
def test_origin_marker_outside_managed_runtimes_does_not_select_user_config(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, raw_origin: str
) -> None:
    origin, _ = tool_environment(tmp_path, monkeypatch)
    foreign = tmp_path / "unrelated environment"
    foreign.mkdir()
    (foreign / updates.ORIGIN_MARKER).write_text(
        json.dumps({"origin": str(origin.resolve()) if raw_origin == "valid" else raw_origin}),
        encoding="utf-8",
    )
    monkeypatch.setattr(sys, "prefix", str(foreign))
    monkeypatch.chdir(tmp_path)
    assert paths.default_config_path() == tmp_path / "chiliarch.toml"
