"""Native background workers retain pipes without creating Windows consoles."""

from __future__ import annotations

import asyncio
import json
import os
import sys
from pathlib import Path

import pytest

from chiliarch.adapters.processes import start_process_tree

_WORKER = """
import ctypes
import json
import os
import sys

visible = False
if os.name == 'nt':
    kernel = ctypes.WinDLL('kernel32', use_last_error=True)
    user = ctypes.WinDLL('user32', use_last_error=True)
    kernel.GetConsoleWindow.restype = ctypes.c_void_p
    user.IsWindowVisible.argtypes = [ctypes.c_void_p]
    visible = bool(user.IsWindowVisible(kernel.GetConsoleWindow()))
request = sys.stdin.buffer.read()
sys.stdout.buffer.write(json.dumps({
    'request': request.decode('utf-8'),
    'cwd': os.getcwd(),
    'visible_console': visible,
}).encode('utf-8'))
sys.stdout.buffer.flush()
sys.stderr.buffer.write(b'native diagnostic')
sys.stderr.buffer.flush()
"""


@pytest.mark.parametrize("windowless_launcher", [False, True])
async def test_background_worker_keeps_pipes_and_no_visible_console(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, windowless_launcher: bool
) -> None:
    # Exercise the exact process-tree owner used by native agents and GitHub
    # commands. The fake console worker requires no agent installation/login.
    python = Path(sys.executable)
    if windowless_launcher:
        if os.name != "nt":
            pytest.skip("pythonw is a Windows entrypoint")
        pythonw = python.with_name("pythonw.exe")
        assert pythonw.is_file(), "Windows service startup requires pythonw.exe"
        monkeypatch.setattr(sys, "executable", str(pythonw))
    workspace = tmp_path / "工作目录 with spaces"
    workspace.mkdir()
    tree = await start_process_tree([str(python), "-u", "-c", _WORKER], workspace, 65536)
    try:
        async with asyncio.timeout(10):
            stdout, stderr = await tree.process.communicate("调度消息\n".encode())
        assert tree.process.returncode == 0
        assert json.loads(stdout) == {
            "request": "调度消息\n",
            "cwd": str(workspace),
            "visible_console": False,
        }
        assert stderr == b"native diagnostic"
    finally:
        await tree.terminate(0.05)
    if os.name == "nt":
        assert tree.job is not None
        assert tree.job.handle is None
    else:
        assert tree.job is None
