"""Real process lifetime tests; Windows-only cases run in native Windows CI."""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import subprocess
import sys
import textwrap
from pathlib import Path
from types import SimpleNamespace

import pytest

from chiliarch.adapters import processes
from chiliarch.adapters.processes import ProcessLaunchError, start_process_tree
from tests.test_agent_runners import process_running


async def test_python_wrapper_preserves_argv_stdin_and_unicode_workspace(tmp_path: Path) -> None:
    directory = tmp_path / "local 工作区 & spaces"
    directory.mkdir()
    script = directory / "trusted wrapper.py"
    script.write_text(
        "import json,os,sys;"
        "sys.stdin.reconfigure(encoding='utf-8');"
        "print(json.dumps({'argv':sys.argv[1:],'cwd':os.getcwd(),'stdin':sys.stdin.read()}))",
        encoding="utf-8",
    )
    arguments = ['embedded "quotes"', "%PATH% & | ^ ! ( )", "--model=本机模型"]
    tree = await start_process_tree([str(script), *arguments], directory, 65536)
    try:
        stdout, stderr = await tree.process.communicate("任务内容；$(不执行)\n".encode())
        observed = json.loads(stdout)
        assert tree.process.returncode == 0
        assert not stderr
        assert observed == {
            "argv": arguments,
            "cwd": str(directory),
            "stdin": "任务内容；$(不执行)\n",
        }
    finally:
        await tree.terminate(0.05)


async def test_cancellation_while_spawning_cleans_the_started_tree(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    started: asyncio.Future[processes.ProcessTree] = asyncio.get_running_loop().create_future()
    release = asyncio.Event()
    original_spawn = processes._spawn

    async def gated_spawn(command: list[str], cwd: Path, limit: int) -> processes.ProcessTree:
        tree = await original_spawn(command, cwd, limit)
        started.set_result(tree)
        await release.wait()
        return tree

    monkeypatch.setattr(processes, "_spawn", gated_spawn)
    task = asyncio.create_task(
        start_process_tree([sys.executable, "-c", "import time;time.sleep(60)"], tmp_path, 65536)
    )
    tree = await asyncio.wait_for(started, 5)
    try:
        task.cancel()
        await asyncio.sleep(0)
        task.cancel()
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, 5)
        assert tree.process.returncode is not None
        assert not process_running(tree.process.pid)
    finally:
        release.set()
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        await tree.terminate(0.05)


@pytest.mark.skipif(os.name != "posix", reason="POSIX process groups require a POSIX host")
async def test_darwin_permission_error_requires_an_exited_process_group(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    tree = await start_process_tree(
        [sys.executable, "-c", "import time;time.sleep(60)"], tmp_path, 65536
    )
    original_killpg = os.killpg

    def denied_group(pgid: int, signal: int) -> None:
        assert pgid == tree.process.pid
        raise PermissionError("simulated Darwin group signal denial")

    monkeypatch.setattr(processes, "sys", SimpleNamespace(platform="darwin"))
    monkeypatch.setattr(processes.os, "killpg", denied_group)
    try:
        with pytest.raises(PermissionError):
            await tree.terminate(0.05)
        assert process_running(tree.process.pid)
        tree.process.kill()
        await tree.process.communicate()
        assert not process_running(tree.process.pid)
        # The same EPERM now refers to an absent group and must not turn a
        # successfully stopped worker into a cancellation failure.
        await tree.terminate(0.05)
    finally:
        monkeypatch.setattr(processes.os, "killpg", original_killpg)
        await tree.terminate(0.05)


@pytest.mark.parametrize(
    ("snapshot", "exited"),
    [
        ("42 Z\n42 Z+\n7 S\n", True),
        ("42 Z\n42 S\n", False),
        ("7 S\n", True),
        ("", False),
        ("unreadable snapshot", False),
    ],
)
async def test_darwin_zombie_probe_distinguishes_live_and_unknown_members(
    snapshot: str, exited: bool, monkeypatch: pytest.MonkeyPatch
) -> None:
    def status_command(*args: object, **kwargs: object) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(["ps"], 0, stdout=snapshot)

    monkeypatch.setattr(processes.shutil, "which", lambda _: "ps")
    monkeypatch.setattr(processes.subprocess, "run", status_command)
    assert await processes._macos_group_exited(42) is exited


async def test_darwin_zombie_probe_does_not_hide_status_command_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def failed_command(*args: object, **kwargs: object) -> subprocess.CompletedProcess[str]:
        raise subprocess.CalledProcessError(1, ["ps"])

    monkeypatch.setattr(processes.shutil, "which", lambda _: "ps")
    monkeypatch.setattr(processes.subprocess, "run", failed_command)
    assert not await processes._macos_group_exited(42)


def npm_launcher(tmp_path: Path, name: str = "fake-agent") -> tuple[Path, Path]:
    package = tmp_path / "node_modules" / "@test" / "fake-package"
    package.mkdir(parents=True)
    entrypoint = package / "entry.js"
    entrypoint.write_text("", encoding="utf-8")
    (package / "package.json").write_text(
        json.dumps({"name": "@test/fake-package", "bin": {name: "entry.js"}}), encoding="utf-8"
    )
    launcher = tmp_path / f"{name}.cmd"
    launcher.write_text(
        '@ECHO off\r\nnode "%dp0%node_modules\\@test\\fake-package\\entry.js" %*\r\n',
        encoding="utf-8",
    )
    return launcher, entrypoint


def test_npm_resolution_uses_exact_local_bin_and_shim_target(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    launcher, entrypoint = npm_launcher(tmp_path)
    node = tmp_path / "node.exe"
    node.write_bytes(b"fixture")
    monkeypatch.setattr(processes.shutil, "which", lambda _: None)
    assert processes._npm_entrypoint(launcher) == [str(node), str(entrypoint.resolve())]

    unrelated = tmp_path / "node_modules" / "a-different-package"
    unrelated.mkdir()
    (unrelated / "entry.js").write_text("", encoding="utf-8")
    (unrelated / "package.json").write_text(
        json.dumps({"bin": {"fake-agent": "entry.js"}}), encoding="utf-8"
    )
    assert processes._npm_entrypoint(launcher) == [str(node), str(entrypoint.resolve())]
    launcher.write_text('@echo off\r\nnode "another-script.js" %*\r\n', encoding="utf-8")
    with pytest.raises(ProcessLaunchError, match="npm bin metadata"):
        processes._npm_entrypoint(launcher)


def test_npm_resolution_rejects_entrypoint_escape_and_missing_node(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    launcher, entrypoint = npm_launcher(tmp_path)
    monkeypatch.setattr(processes.shutil, "which", lambda _: None)
    with pytest.raises(ProcessLaunchError, match="Node.js"):
        processes._npm_entrypoint(launcher)
    outside = tmp_path / "escape.js"
    outside.write_text("", encoding="utf-8")
    (entrypoint.parent / "package.json").write_text(
        json.dumps({"bin": {"fake-agent": "../../../escape.js"}}), encoding="utf-8"
    )
    with pytest.raises(ProcessLaunchError, match="npm bin metadata"):
        processes._npm_entrypoint(launcher)


@pytest.mark.skipif(os.name != "nt", reason="Windows npm launchers require native Windows")
async def test_windows_npm_cmd_launch_preserves_shell_metacharacters_as_data(
    tmp_path: Path,
) -> None:
    assert shutil.which("node"), "Windows CI installs Node.js for npm launcher behavior tests"
    workspace = tmp_path / "中文 & space"
    workspace.mkdir()
    launcher, entrypoint = npm_launcher(workspace)
    entrypoint.write_text(
        "let input='';process.stdin.setEncoding('utf8');"
        "process.stdin.on('data',data=>input+=data);"
        "process.stdin.on('end',()=>console.log(JSON.stringify({argv:process.argv.slice(2),input})));",
        encoding="utf-8",
    )
    arguments = ['mcp={"args":["task & echo wrong", "quote\\""]}', "%PATH%", "!name!"]
    tree = await start_process_tree([str(launcher), *arguments], workspace, 65536)
    try:
        stdout, stderr = await tree.process.communicate("任务只进入 stdin".encode())
        assert not stderr
        assert tree.process.returncode == 0
        assert json.loads(stdout) == {"argv": arguments, "input": "任务只进入 stdin"}
    finally:
        await tree.terminate(0.05)


@pytest.mark.skipif(os.name != "nt", reason="Windows Job Objects require native Windows")
async def test_windows_job_close_stops_children_even_after_owner_crashes(tmp_path: Path) -> None:
    marker = tmp_path / "processes.json"
    worker = tmp_path / "worker.py"
    worker.write_text(
        "import json,os,subprocess,sys,time;"
        "from pathlib import Path;"
        "child=subprocess.Popen([sys.executable,'-c','import time;time.sleep(60)']);"
        f"Path({str(marker)!r}).write_text(json.dumps([os.getpid(),child.pid]));"
        "time.sleep(60)",
        encoding="utf-8",
    )
    owner = tmp_path / "owner.py"
    owner.write_text(
        textwrap.dedent(
            f"""\
            import asyncio,os
            from pathlib import Path
            from chiliarch.adapters.processes import start_process_tree
            async def main() -> None:
                tree = await start_process_tree([{str(worker)!r}], Path({str(tmp_path)!r}), 65536)
                for _ in range(500):
                    if Path({str(marker)!r}).exists():
                        os._exit(0)
                    await asyncio.sleep(0.01)
                os._exit(2)
            asyncio.run(main())
            """
        ),
        encoding="utf-8",
    )
    process = await asyncio.create_subprocess_exec(
        sys.executable, str(owner), stdout=subprocess.PIPE, stderr=subprocess.PIPE
    )
    async with asyncio.timeout(10):
        _, stderr = await process.communicate()
    assert process.returncode == 0, stderr.decode(errors="replace")
    pids: list[int] = json.loads(marker.read_text(encoding="utf-8"))
    async with asyncio.timeout(5):
        for _ in range(500):
            if all(not process_running(pid) for pid in pids):
                break
            await asyncio.sleep(0.01)
        else:
            pytest.fail("A child survived the abrupt Job Object owner exit")
