"""Keep runner lock and isolation state out of the developer's user profile."""

from __future__ import annotations

from pathlib import Path

import pytest


@pytest.fixture(autouse=True)
def isolated_runner_workspace_state(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # Patch only the runtime import; daemon/service path-resolution tests continue
    # to exercise their real platform-specific resolver. Subprocess tests pass an
    # explicit lock directory because Python monkeypatches do not cross processes.
    monkeypatch.setattr(
        "chiliarch.adapters.runtime.default_daemon_directory",
        lambda: tmp_path / "runner-state" / "daemon",
    )
