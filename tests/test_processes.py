"""Real process lifetime tests; Windows-only cases run in native Windows CI."""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import shutil
import signal
import subprocess
import sys
import textwrap
import venv
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from chiliarch.adapters import processes
from chiliarch.adapters.processes import ProcessLaunchError, start_process_tree
from tests.test_agent_runners import process_running


def test_job_member_snapshot_expands_and_handles_exit_or_pid_reuse(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    error = 0
    capacities: list[int] = []
    closed: list[int] = []

    def query(_: int, kind: int, pointer: Any, size: int, length: Any) -> bool:
        nonlocal error
        assert kind == 3
        information = pointer._obj
        capacities.append(len(information.pids))
        information.assigned = 17
        information.count = min(len(information.pids), 17)
        for index in range(information.count):
            information.pids[index] = 100 + index
        error = 234
        return len(information.pids) >= 17

    def open_process(access: int, inherit: bool, pid: int) -> int:
        nonlocal error
        assert access == 0x00100000 | 0x1000 and not inherit
        error = 87
        return 0 if pid == 100 else pid

    def member(handle: int, job: int, pointer: Any) -> bool:
        pointer._obj.value = handle != 101  # That released PID now belongs elsewhere.
        return True

    monkeypatch.setattr(processes, "_windows_ctypes", SimpleNamespace(get_last_error=lambda: error))
    job = processes.WindowsJob.__new__(processes.WindowsJob)
    job.handle = 1
    job.members = {}
    job.kernel = SimpleNamespace(
        QueryInformationJobObject=query,
        OpenProcess=open_process,
        IsProcessInJob=member,
        CloseHandle=closed.append,
    )
    try:
        job.capture_members()
        assert capacities == [16, 32]
        assert job.members == {pid: pid for pid in range(102, 117)}
        assert closed == [101]
    finally:
        job.close()
    assert set(closed) == {1, *range(101, 117)}
    assert not job.members


async def test_job_member_access_error_still_terminates_and_closes_owned_handles(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    leader = await asyncio.create_subprocess_exec(
        sys.executable, "-c", "import time;time.sleep(60)"
    )
    closed: list[int] = []
    terminated: list[int] = []

    def query(_: int, kind: int, pointer: Any, size: int, length: Any) -> bool:
        assert kind == 3
        pointer._obj.assigned = pointer._obj.count = 2
        pointer._obj.pids[0], pointer._obj.pids[1] = 10, 11
        return True

    def member(handle: int, job: int, pointer: Any) -> bool:
        pointer._obj.value = True
        return True

    def terminate(handle: int, code: int) -> bool:
        terminated.append(handle)
        leader.kill()
        return True

    monkeypatch.setattr(processes, "_windows_ctypes", SimpleNamespace(get_last_error=lambda: 5))
    job = processes.WindowsJob.__new__(processes.WindowsJob)
    job.handle = 1
    job.members = {}
    job.kernel = SimpleNamespace(
        QueryInformationJobObject=query,
        OpenProcess=lambda access, inherit, pid: pid if pid == 10 else 0,
        IsProcessInJob=member,
        TerminateJobObject=terminate,
        CloseHandle=closed.append,
    )
    try:
        with pytest.raises(ProcessLaunchError, match="error 5"):
            await processes.ProcessTree(leader, job).terminate(0.01)
        assert terminated == [1]
        assert closed == [1, 10]
        assert not job.members and job.handle is None
        await asyncio.wait_for(leader.wait(), 5)
    finally:
        with contextlib.suppress(ProcessLookupError):
            leader.kill()
        await leader.wait()


@pytest.mark.skipif(os.name != "nt", reason="Windows venv redirectors require native Windows")
async def test_windows_venv_interpreter_and_private_descendants_are_signaled(
    tmp_path: Path,
) -> None:
    environment = tmp_path / "venv"
    await asyncio.to_thread(venv.EnvBuilder(with_pip=False).create, environment)
    executable = environment / "Scripts" / "python.exe"
    script = tmp_path / "family.py"
    script.write_text(
        textwrap.dedent(
            """\
            import json, os, subprocess, sys, time
            from pathlib import Path
            role = int(sys.argv[1])
            if role < 2:
                subprocess.Popen([sys.executable, __file__, str(role + 1)],
                    stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            temporary = Path(f'process-{role}.tmp')
            temporary.write_text(json.dumps({'pid': os.getpid(), 'executable': sys.executable}))
            temporary.replace(f'process-{role}.json')
            time.sleep(60)
            """
        ),
        encoding="utf-8",
    )
    tree = await start_process_tree([str(executable), str(script), "0"], tmp_path, 65536)
    assert tree.job is not None
    job = tree.job
    probes: list[int] = []
    try:
        async with asyncio.timeout(10):
            while not all(  # noqa: ASYNC110 — native subprocess readiness.
                (tmp_path / f"process-{role}.json").exists() for role in range(3)
            ):
                await asyncio.sleep(0.01)
        observed = [
            json.loads((tmp_path / f"process-{role}.json").read_text()) for role in range(3)
        ]
        actual_pids = {entry["pid"] for entry in observed}
        assert all(Path(entry["executable"]) == executable for entry in observed)
        job.capture_members()
        assert actual_pids <= job.members.keys()
        assert len(job.members) > len(actual_pids)  # Redirectors are additional job members.
        for pid in actual_pids:
            handle = job.open_process(pid)
            assert handle is not None
            probes.append(handle)
        await tree.terminate(0.05)
        # These handles refer to each actual interpreter, not its venv launcher
        # or a PID that another parallel test may already have reused.
        assert all(job.process_exited(handle) for handle in probes)
        assert not job.members and job.handle is None
    finally:
        await tree.terminate(0.05)
        for handle in probes:
            job.close_process(handle)


async def test_job_accounting_does_not_replace_native_process_exit() -> None:
    native = await asyncio.create_subprocess_exec(
        sys.executable,
        "-u",
        "-c",
        "import sys;sys.stdout.buffer.write(b'ready\\n');"
        "sys.stdout.buffer.flush();sys.stdin.buffer.read(1)",
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    leader = await asyncio.create_subprocess_exec(sys.executable, "-c", "pass")
    requested = asyncio.Event()
    closed_while_running: list[bool] = []

    class ExitingJob(processes.WindowsJob):
        def __init__(self) -> None:
            self.members = {leader.pid: leader.pid, native.pid: native.pid}

        def capture_members(self) -> None:
            pass

        def terminate(self) -> None:
            requested.set()

        def active(self) -> bool:
            return False  # Accounting has already removed the exiting process.

        def process_exited(self, handle: int) -> bool:
            return (leader if handle == leader.pid else native).returncode is not None

        def close_process(self, handle: int) -> None:
            closed_while_running.append(native.returncode is None)

        def close(self) -> None:
            if self.members:
                closed_while_running.append(native.returncode is None)
                self.members.clear()

    cleanup: asyncio.Task[None] | None = None
    try:
        await leader.wait()
        assert native.stdout and native.stdin
        assert await native.stdout.readline() == b"ready\n"
        tree = processes.ProcessTree(leader, ExitingJob())
        cleanup = asyncio.create_task(tree.terminate(0.01))
        await asyncio.wait_for(requested.wait(), 5)
        with pytest.raises(TimeoutError):
            await asyncio.wait_for(asyncio.shield(cleanup), 0.05)
        assert native.returncode is None
        native.stdin.close()
        await asyncio.wait_for(native.communicate(), 5)
        await asyncio.wait_for(cleanup, 5)
        assert closed_while_running == [False]
        assert tree.job is not None and not tree.job.members
    finally:
        with contextlib.suppress(ProcessLookupError):
            native.kill()
        await native.communicate()
        with contextlib.suppress(ProcessLookupError):
            leader.kill()
        await leader.communicate()
        if cleanup is not None:
            await asyncio.gather(cleanup, return_exceptions=True)


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
async def test_posix_zombie_probe_distinguishes_live_and_unknown_members(
    snapshot: str, exited: bool, monkeypatch: pytest.MonkeyPatch
) -> None:
    def status_command(*args: object, **kwargs: object) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(["ps"], 0, stdout=snapshot)

    monkeypatch.setattr(processes.shutil, "which", lambda _: "ps")
    monkeypatch.setattr(processes.subprocess, "run", status_command)
    assert await processes._posix_group_exited(42) is exited


async def test_posix_zombie_probe_does_not_hide_status_command_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def failed_command(*args: object, **kwargs: object) -> subprocess.CompletedProcess[str]:
        raise subprocess.CalledProcessError(1, ["ps"])

    monkeypatch.setattr(processes.shutil, "which", lambda _: "ps")
    monkeypatch.setattr(processes.subprocess, "run", failed_command)
    assert not await processes._posix_group_exited(42)


@pytest.mark.skipif(os.name != "posix", reason="POSIX process groups require a POSIX host")
@pytest.mark.parametrize("release_kill", [True, False])
async def test_cleanup_requires_private_output_descendant_to_actually_exit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, release_kill: bool
) -> None:
    ready = tmp_path / "descendant-ready"
    child_code = (
        "import os,signal,time;from pathlib import Path;"
        "signal.signal(signal.SIGTERM,signal.SIG_IGN);"
        f"Path({str(ready)!r}).write_text(str(os.getpid()));"
        "time.sleep(60)"
    )
    script = tmp_path / "launcher.py"
    script.write_text(
        "import subprocess,sys;"
        f"child=subprocess.Popen([sys.executable,'-c',{child_code!r}],"
        "stdin=subprocess.DEVNULL,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL);"
        "print(child.pid,flush=True)",
        encoding="utf-8",
    )
    tree = await start_process_tree([str(script)], tmp_path, 65536)
    kill_requested = asyncio.Event()
    original_killpg = os.killpg
    cleanup: asyncio.Task[None] | None = None

    def defer_kill(pgid: int, signum: int) -> None:
        if pgid == tree.process.pid and signum == signal.SIGKILL:
            kill_requested.set()  # Signal delivery is not the child's actual exit.
            return
        original_killpg(pgid, signum)

    try:
        stdout, stderr = await asyncio.wait_for(tree.process.communicate(), 5)
        child_pid = int(stdout)
        assert not stderr and tree.process.returncode == 0
        async with asyncio.timeout(5):
            while not ready.exists():  # noqa: ASYNC110 — readiness comes from another process.
                await asyncio.sleep(0.01)
        assert process_running(child_pid)
        monkeypatch.setattr(processes.os, "killpg", defer_kill)
        cleanup = asyncio.create_task(tree.terminate(0.01))
        await asyncio.wait_for(kill_requested.wait(), 5)
        # The leader and its output have already ended, but this still must not
        # succeed and let another worker acquire the same workspace.
        if release_kill:
            with pytest.raises(TimeoutError):
                await asyncio.wait_for(asyncio.shield(cleanup), 0.1)
            assert process_running(child_pid)
            original_killpg(tree.process.pid, signal.SIGKILL)
            await asyncio.wait_for(cleanup, 5)
            assert await processes._posix_group_exited(tree.process.pid)
        else:
            with pytest.raises(TimeoutError):
                await asyncio.wait_for(asyncio.shield(cleanup), 7)
            assert cleanup.done()
            assert process_running(child_pid)
    finally:
        monkeypatch.setattr(processes.os, "killpg", original_killpg)
        if cleanup and not cleanup.done():
            cleanup.cancel()
            await asyncio.gather(cleanup, return_exceptions=True)
        await tree.terminate(0.01)


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
