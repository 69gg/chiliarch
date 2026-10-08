"""Bound native workers to one process tree on POSIX and Windows.

The Windows bootstrap waits for a byte before it can start a CLI. Its parent
assigns it to a non-inheritable, kill-on-close Job Object before releasing that
gate, so even an immediately spawned grandchild belongs to the same job.
"""

from __future__ import annotations

import asyncio
import contextlib
import ctypes
import json
import os
import shutil
import signal
import subprocess
import sys
import time
from ctypes import wintypes
from dataclasses import dataclass
from pathlib import Path
from typing import Any

_windows_ctypes: Any = ctypes  # Windows-only exports are absent from POSIX type stubs.


class ProcessLaunchError(OSError):
    """The configured local launcher cannot be used safely on this platform."""


def _npm_entrypoint(launcher: Path) -> list[str]:
    """Read npm's bin metadata instead of interpreting a batch shim in cmd.exe."""
    modules = launcher.parent / "node_modules"
    try:
        shim = launcher.read_text(encoding="utf-8").replace("\\", "/")
    except (OSError, UnicodeDecodeError):
        raise ProcessLaunchError("Configured npm launcher could not be read") from None
    candidates = [*modules.glob("*/package.json"), *modules.glob("@*/*/package.json")]
    for metadata in sorted(candidates):
        try:
            package = json.loads(metadata.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if not isinstance(package, dict):
            continue
        bins = package.get("bin")
        if isinstance(bins, str):
            name = package.get("name")
            bins = {name.rsplit("/", 1)[-1]: bins} if isinstance(name, str) else {}
        if not isinstance(bins, dict):
            continue
        entry = next(
            (value for name, value in bins.items() if name.casefold() == launcher.stem.casefold()),
            None,
        )
        if not isinstance(entry, str):
            continue
        entrypoint = (metadata.parent / entry).resolve()
        if (
            not entrypoint.is_file()
            or not entrypoint.is_relative_to(metadata.parent.resolve())
            or not entrypoint.is_relative_to(modules.resolve())
        ):
            continue
        # npm's generated shim names its actual entry. A different package with
        # the same bin name must not substitute its program for this launcher.
        referenced = "node_modules/" + entrypoint.relative_to(modules.resolve()).as_posix()
        if f'{referenced}"'.casefold() not in shim.casefold():
            continue
        node = launcher.parent / "node.exe"
        node_path = str(node) if node.is_file() else shutil.which("node")
        if not node_path:
            raise ProcessLaunchError("The configured npm launcher requires an installed Node.js")
        return [node_path, str(entrypoint)]
    raise ProcessLaunchError(
        "Batch launchers require npm bin metadata; configure the native executable instead"
    )


def resolve_command(command: list[str]) -> list[str]:
    """Resolve trusted launch policy; no task instructions enter shell syntax."""
    executable = shutil.which(command[0])
    if not executable:
        path = Path(command[0]).expanduser()
        if path.is_file():
            executable = str(path.resolve())
        else:
            raise ProcessLaunchError("Configured executable was not found")
    launcher = Path(executable)
    if launcher.suffix.lower() == ".py":
        return [sys.executable, "-u", str(launcher), *command[1:]]
    if os.name == "nt" and launcher.suffix.lower() in {".cmd", ".bat"}:
        return [*_npm_entrypoint(launcher), *command[1:]]
    return [str(launcher), *command[1:]]


class _BasicLimit(ctypes.Structure):
    _fields_ = [
        ("PerProcessUserTimeLimit", ctypes.c_int64),
        ("PerJobUserTimeLimit", ctypes.c_int64),
        ("LimitFlags", wintypes.DWORD),
        ("MinimumWorkingSetSize", ctypes.c_size_t),
        ("MaximumWorkingSetSize", ctypes.c_size_t),
        ("ActiveProcessLimit", wintypes.DWORD),
        ("Affinity", ctypes.c_size_t),
        ("PriorityClass", wintypes.DWORD),
        ("SchedulingClass", wintypes.DWORD),
    ]


class _IoCounters(ctypes.Structure):
    _fields_ = [
        (name, ctypes.c_uint64)
        for name in (
            "ReadOperationCount",
            "WriteOperationCount",
            "OtherOperationCount",
            "ReadTransferCount",
            "WriteTransferCount",
            "OtherTransferCount",
        )
    ]


class _ExtendedLimit(ctypes.Structure):
    _fields_ = [
        ("BasicLimitInformation", _BasicLimit),
        ("IoInfo", _IoCounters),
        ("ProcessMemoryLimit", ctypes.c_size_t),
        ("JobMemoryLimit", ctypes.c_size_t),
        ("PeakProcessMemoryUsed", ctypes.c_size_t),
        ("PeakJobMemoryUsed", ctypes.c_size_t),
    ]


class _Accounting(ctypes.Structure):
    _fields_ = [
        ("TotalUserTime", ctypes.c_int64),
        ("TotalKernelTime", ctypes.c_int64),
        ("ThisPeriodTotalUserTime", ctypes.c_int64),
        ("ThisPeriodTotalKernelTime", ctypes.c_int64),
        ("TotalPageFaultCount", wintypes.DWORD),
        ("TotalProcesses", wintypes.DWORD),
        ("ActiveProcesses", wintypes.DWORD),
        ("TotalTerminatedProcesses", wintypes.DWORD),
    ]


class WindowsJob:
    """Own the lifetime of a Windows CLI and all normal CreateProcess descendants."""

    def __init__(self) -> None:
        self.handle: int | None = None
        if os.name != "nt":
            raise ProcessLaunchError("Windows Job Objects are available only on Windows")
        self.kernel: Any = _windows_ctypes.WinDLL("kernel32", use_last_error=True)
        signatures = {
            "CreateJobObjectW": ([ctypes.c_void_p, wintypes.LPCWSTR], wintypes.HANDLE),
            "SetInformationJobObject": (
                [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD],
                wintypes.BOOL,
            ),
            "AssignProcessToJobObject": ([wintypes.HANDLE, wintypes.HANDLE], wintypes.BOOL),
            "OpenProcess": ([wintypes.DWORD, wintypes.BOOL, wintypes.DWORD], wintypes.HANDLE),
            "CloseHandle": ([wintypes.HANDLE], wintypes.BOOL),
            "TerminateJobObject": ([wintypes.HANDLE, wintypes.UINT], wintypes.BOOL),
            "QueryInformationJobObject": (
                [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD, ctypes.c_void_p],
                wintypes.BOOL,
            ),
        }
        for name, (arguments, result) in signatures.items():
            function = getattr(self.kernel, name)
            function.argtypes = arguments
            function.restype = result
        self.handle = self.kernel.CreateJobObjectW(None, None)
        if not self.handle:
            self._error()
        information = _ExtendedLimit()
        information.BasicLimitInformation.LimitFlags = 0x2000  # KILL_ON_JOB_CLOSE
        try:
            if not self.kernel.SetInformationJobObject(
                self.handle, 9, ctypes.byref(information), ctypes.sizeof(information)
            ):
                self._error()
        except BaseException:
            self.close()
            raise

    @staticmethod
    def _error() -> None:
        code = _windows_ctypes.get_last_error()
        raise ProcessLaunchError(f"Windows process-tree operation failed (error {code})")

    def assign(self, pid: int) -> None:
        process = self.kernel.OpenProcess(0x0100 | 0x0001, False, pid)
        if not process:
            self._error()
        try:
            if not self.kernel.AssignProcessToJobObject(self.handle, process):
                self._error()
        finally:
            self.kernel.CloseHandle(process)

    def terminate(self) -> None:
        if self.handle and not self.kernel.TerminateJobObject(self.handle, 1):
            self._error()

    def active(self) -> bool:
        if self.handle is None:
            return False
        information = _Accounting()
        if not self.kernel.QueryInformationJobObject(
            self.handle, 1, ctypes.byref(information), ctypes.sizeof(information), None
        ):
            self._error()
        return bool(information.ActiveProcesses)

    def close(self) -> None:
        if self.handle:
            self.kernel.CloseHandle(self.handle)
            self.handle = None

    def __del__(self) -> None:
        self.close()


_WINDOWS_BOOTSTRAP = (
    "import os,subprocess,sys;"
    "gate=os.read(0,1);"
    "sys.exit(subprocess.call(sys.argv[1:]) if gate == b'G' else 125)"
)


async def _posix_group_exited(pgid: int) -> bool:
    """Confirm no running group members remain; unreaped zombies cannot execute work."""
    executable = shutil.which("ps")
    if executable is None:
        return False
    try:
        snapshot = await asyncio.to_thread(
            subprocess.run,
            [executable, "-axo", "pgid=,stat="],
            check=True,
            capture_output=True,
            text=True,
            timeout=2,
        )
        if not snapshot.stdout.strip():
            return False
        for line in snapshot.stdout.splitlines():
            group, status = line.split()
            if int(group) == pgid and not status.startswith("Z"):
                return False
    except (OSError, subprocess.SubprocessError, UnicodeError, ValueError):
        return False  # An unavailable or incomplete snapshot cannot prove termination.
    return True


@dataclass
class ProcessTree:
    process: asyncio.subprocess.Process
    job: WindowsJob | None = None

    async def terminate(self, grace_seconds: float) -> None:
        """Confirm the tree is stopped, then drain pipes before reaping its leader."""
        try:
            if self.job is not None:
                self.job.terminate()
                async with asyncio.timeout(max(5.0, grace_seconds)):
                    while self.job.active():  # noqa: ASYNC110 — kernel state has no asyncio event.
                        await asyncio.sleep(0.01)
            else:
                await self._terminate_group(grace_seconds)

            async def drain(stream: asyncio.StreamReader | None) -> None:
                if stream:
                    while await stream.read(65536):
                        pass

            await asyncio.gather(
                self.process.wait(), drain(self.process.stdout), drain(self.process.stderr)
            )
        finally:
            if self.job is not None:
                self.job.close()

    async def _terminate_group(self, grace_seconds: float) -> None:
        if sys.platform == "win32":
            raise ProcessLaunchError("POSIX process-group cleanup cannot run on Windows")

        async def send_group(sig: signal.Signals | int) -> bool:
            try:
                os.killpg(self.process.pid, sig)
                return True
            except ProcessLookupError:
                return False
            except PermissionError:
                # Darwin excludes zombies from killpg and can report EPERM for
                # an exited group. A real permission failure still propagates.
                if sys.platform == "darwin" and await _posix_group_exited(self.process.pid):
                    return False
                raise

        if not await send_group(signal.SIGTERM):
            return
        until = time.monotonic() + grace_seconds
        while time.monotonic() < until:
            if not await send_group(0):
                return
            await asyncio.sleep(min(0.05, grace_seconds))
        if not await send_group(signal.SIGKILL):
            return
        # Sending SIGKILL is not confirmation: a descendant with private output
        # can outlive both the leader and its pipes. Do not release its workspace
        # until the group is absent or every remaining member is a zombie.
        async with asyncio.timeout(max(5.0, grace_seconds)):
            while await send_group(0):
                if await _posix_group_exited(self.process.pid):
                    return
                await asyncio.sleep(0.01)


async def _spawn(command: list[str], cwd: Path, limit: int) -> ProcessTree:
    job = WindowsJob() if os.name == "nt" else None
    arguments = [sys.executable, "-I", "-u", "-c", _WINDOWS_BOOTSTRAP, *command] if job else command
    process: asyncio.subprocess.Process | None = None
    try:
        process = await asyncio.create_subprocess_exec(
            *arguments,
            cwd=cwd,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            start_new_session=job is None,
            limit=limit,
        )
        if job:
            job.assign(process.pid)
            assert process.stdin is not None
            process.stdin.write(b"G")
            await process.stdin.drain()
        return ProcessTree(process, job)
    except BaseException:
        if job:
            job.close()
        if process:
            with contextlib.suppress(ProcessLookupError):
                process.kill()
            await process.communicate()
        raise


async def start_process_tree(command: list[str], cwd: Path, limit: int) -> ProcessTree:
    """Cancellation during spawn cannot orphan an already started process."""
    if os.name not in {"posix", "nt"}:
        raise ProcessLaunchError("Native workers require POSIX groups or Windows Job Objects")
    spawn = asyncio.create_task(_spawn(resolve_command(command), cwd, limit))
    cancelled = False
    while not spawn.done():
        try:
            await asyncio.shield(spawn)
        except asyncio.CancelledError:
            cancelled = True
    tree = spawn.result()
    if cancelled:
        cleanup = asyncio.create_task(tree.terminate(0.05))
        while not cleanup.done():
            try:
                await asyncio.shield(cleanup)
            except asyncio.CancelledError:
                pass
        cleanup.result()
        raise asyncio.CancelledError
    return tree
