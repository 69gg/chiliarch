"""Prepare immutable, per-installation runtimes without replacing a running tool."""

from __future__ import annotations

import hashlib
import importlib.metadata
import json
import logging
import os
import platform
import shutil
import subprocess
import sys
import tomllib
import urllib.request
import uuid
from dataclasses import dataclass
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Any

from filelock import FileLock, Timeout
from packaging.requirements import Requirement
from packaging.specifiers import SpecifierSet
from packaging.tags import sys_tags
from packaging.utils import InvalidWheelFilename, parse_wheel_filename
from packaging.version import InvalidVersion, Version

from chiliarch.daemon_models import private_write
from chiliarch.daemon_services import default_daemon_directory

PACKAGE = "chiliarch"
PYPI_METADATA_URL = "https://pypi.org/pypi/chiliarch/json"
PYPI_INDEX_URL = "https://pypi.org/simple"
CHECK_TIMEOUT_SECONDS = 5
CREATE_TIMEOUT_SECONDS = 30
INSTALL_TIMEOUT_SECONDS = 120
VERIFY_TIMEOUT_SECONDS = 20
METADATA_LIMIT_BYTES = 4 * 1024 * 1024
ORIGIN_MARKER = "chiliarch-origin.json"
STARTUP_UPDATE_TIMEOUT_SECONDS = 180

_PROBE = """
import importlib.metadata, importlib.resources, json, sys
from chiliarch import api, bootstrap, cli, daemon, updates
from chiliarch.protocol import protocol_schema
root = importlib.resources.files('chiliarch')
assert root.joinpath('static/index.html').is_file(), 'Missing WebUI'
assert json.loads(root.joinpath('protocol/schema.json').read_text()) == protocol_schema()
assert importlib.metadata.version('chiliarch') == sys.argv[1]
"""


@dataclass(frozen=True)
class Installation:
    origin: Path
    version: Version
    primary: Requirement
    additional: tuple[str, ...]
    receipt_digest: str
    directory: Path
    uv: str | None
    supported: bool


@dataclass(frozen=True)
class _ToolReceipt:
    origin: Path
    content: bytes
    tool: dict[str, Any]
    directory: Path


def _notice(message: str, directory: Path | None = None) -> None:
    # Startup checks run silently, including failures that fall back to the
    # installed version. pythonw may have no standard streams at all; MCP owns
    # its streams and must never receive installer diagnostics either.
    try:
        if directory is None:
            try:
                receipt = _tool_receipt()
            except (OSError, ValueError, TypeError, KeyError):
                receipt = None
            directory = receipt.directory if receipt is not None else None
        if directory is None:
            return
        directory.mkdir(parents=True, exist_ok=True)
        os.chmod(directory, 0o700)
        handler = RotatingFileHandler(
            directory / "update.log", maxBytes=1024 * 1024, backupCount=2, encoding="utf-8"
        )
        try:
            os.chmod(directory / "update.log", 0o600)
            handler.setFormatter(logging.Formatter("%(asctime)s %(message)s"))
            handler.emit(
                logging.LogRecord("chiliarch.updates", logging.INFO, __file__, 0, message, (), None)
            )
        finally:
            handler.close()
    except OSError:
        pass


def _enabled() -> bool:
    return os.environ.get("CHILIARCH_AUTO_UPDATE", "1").lower() not in {"0", "false", "no", "off"}


def startup_budget() -> float:
    """Return the bounded preparation allowance without running commands or networking."""
    if not _enabled():
        return 0.0
    try:
        installation = _installation()
        if installation is not None and installation.supported and installation.uv:
            return float(STARTUP_UPDATE_TIMEOUT_SECONDS)
    except (OSError, ValueError, TypeError, KeyError):
        pass
    return 0.0


def _python(prefix: Path) -> Path:
    return prefix / ("Scripts/python.exe" if sys.platform == "win32" else "bin/python")


def _version(prefix: Path) -> Version | None:
    paths = [prefix / "Lib/site-packages", *prefix.glob("lib/python*/site-packages")]
    for distribution in importlib.metadata.distributions(path=[str(path) for path in paths]):
        name = distribution.metadata["Name"]
        if isinstance(name, str) and name.lower().replace("_", "-") == PACKAGE:
            try:
                return Version(distribution.version)
            except InvalidVersion:
                return None
    return None


def _json(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError("Invalid update record")
    return value


def _requirement(value: Any) -> Requirement:
    if isinstance(value, str):
        result = Requirement(value)
    elif isinstance(value, dict) and not set(value) - {"name", "specifier", "extras", "marker"}:
        name = value.get("name")
        extras = value.get("extras", [])
        specifier = value.get("specifier", "")
        marker = value.get("marker", "")
        if (
            not isinstance(name, str)
            or not isinstance(extras, list)
            or not all(isinstance(extra, str) for extra in extras)
            or not isinstance(specifier, str)
            or not isinstance(marker, str)
        ):
            raise ValueError("Unsupported tool requirement")
        result = Requirement(
            name
            + ("[" + ",".join(extras) + "]" if extras else "")
            + specifier
            + ("; " + marker if marker else "")
        )
    else:
        raise ValueError("Unsupported tool requirement")
    if result.url:
        raise ValueError("Only registry requirements can update automatically")
    return result


def _plain_pypi_options(options: Any) -> bool:
    if not isinstance(options, dict) or set(options) - {"index", "index-url"}:
        return False
    index_url = options.get("index-url", PYPI_INDEX_URL)
    if not isinstance(index_url, str) or index_url.rstrip("/") != PYPI_INDEX_URL:
        return False
    indexes = options.get("index", [])
    if not isinstance(indexes, list) or len(indexes) > 1:
        return False
    for index in indexes:
        if (
            not isinstance(index, dict)
            or set(index) - {"url", "default", "explicit", "format", "authenticate"}
            or not isinstance(index.get("url"), str)
            or index["url"].rstrip("/") != PYPI_INDEX_URL
            or index.get("explicit", False)
            or index.get("format", "simple") != "simple"
            or index.get("authenticate", "auto") != "auto"
        ):
            return False
    return True


def _command(arguments: list[str], *, timeout: float, cwd: Path) -> None:
    # Never echo installer output: user-level proxy/index configuration can contain secrets.
    environment = {
        key: value
        for key, value in os.environ.items()
        if not key.startswith("UV_") or key in {"UV_CACHE_DIR", "UV_NATIVE_TLS", "UV_SYSTEM_CERTS"}
    }
    environment.pop("VIRTUAL_ENV", None)
    environment.pop("PYTHONPATH", None)
    subprocess.run(
        arguments,
        cwd=cwd,
        env=environment,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        check=True,
        timeout=timeout,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0) if sys.platform == "win32" else 0,
    )


def _tool_receipt() -> _ToolReceipt | None:
    """Identify a persistent uv tool, independently of its update eligibility."""
    prefix = Path(sys.prefix).resolve()
    marker_path = prefix / ORIGIN_MARKER
    origin = prefix
    if marker_path.is_file():
        marker = _json(marker_path)
        raw_origin = marker.get("origin")
        if not isinstance(raw_origin, str) or not Path(raw_origin).is_absolute():
            return None
        origin = Path(raw_origin).resolve()
    receipt_path = origin / "uv-receipt.toml"
    if origin.name != PACKAGE or not receipt_path.is_file():
        return None
    receipt_bytes = receipt_path.read_bytes()
    receipt = tomllib.loads(receipt_bytes.decode("utf-8"))
    tool = receipt.get("tool", {})
    if not isinstance(tool, dict):
        return None
    values = tool.get("requirements", [])
    if not isinstance(values, list) or not values:
        return None
    # Local/editable sources and custom indexes are still tool installations,
    # even though the updater must not replace them with a registry release.
    primary = values[0]
    name = Requirement(primary).name if isinstance(primary, str) else None
    if isinstance(primary, dict):
        name = primary.get("name")
    if not isinstance(name, str) or name.lower().replace("_", "-") != PACKAGE:
        return None
    key = hashlib.sha256(os.fsencode(origin)).hexdigest()[:24]
    directory = default_daemon_directory().parent / "updates" / key
    if marker_path.is_file() and not prefix.is_relative_to(directory.resolve() / "runtimes"):
        return None
    return _ToolReceipt(origin, receipt_bytes, tool, directory)


def uv_tool_origin() -> Path | None:
    """Return the original uv tool environment without updating or running commands."""
    try:
        receipt = _tool_receipt()
        return receipt.origin if receipt is not None else None
    except (OSError, ValueError, TypeError, KeyError):
        return None


def _installation() -> Installation | None:
    receipt = _tool_receipt()
    if receipt is None:
        return None
    values = receipt.tool["requirements"]
    primary = _requirement(values[0])
    if primary.marker and not primary.marker.evaluate():
        return None
    version = _version(receipt.origin)
    if version is None:
        return None
    digest = hashlib.sha256(receipt.content).hexdigest()
    tool = receipt.tool
    uv = shutil.which("uv")
    additional: list[str] = []
    supported = _plain_pypi_options(tool.get("options", {}))
    supported = supported and not any(
        tool.get(field)
        for field in ("constraints", "overrides", "excludes", "build-constraint-dependencies")
    )
    for value in values[1:]:
        try:
            additional.append(str(_requirement(value)))
        except ValueError:
            supported = False
    return Installation(
        receipt.origin,
        version,
        primary,
        tuple(additional),
        digest,
        receipt.directory,
        uv,
        supported,
    )


def _active(installation: Installation) -> tuple[Version, Path] | None:
    try:
        manifest = _json(installation.directory / "active.json")
        version = Version(manifest["version"])
        prefix = Path(manifest["prefix"])
        if (
            manifest.get("receipt_digest") != installation.receipt_digest
            or not prefix.is_absolute()
            or not prefix.resolve().is_relative_to(installation.directory.resolve() / "runtimes")
            or version <= installation.version
            or version not in installation.primary.specifier
            or _version(prefix) != version
            or _json(prefix / ORIGIN_MARKER).get("origin") != str(installation.origin)
            or not _python(prefix).is_file()
        ):
            return None
        return version, _python(prefix)
    except (OSError, ValueError, TypeError, KeyError):
        return None


def _latest(installation: Installation, current: Version) -> Version | None:
    request = urllib.request.Request(
        PYPI_METADATA_URL,
        headers={"Accept": "application/json", "User-Agent": f"chiliarch/{current}"},
    )
    with urllib.request.urlopen(request, timeout=CHECK_TIMEOUT_SECONDS) as response:
        body = response.read(METADATA_LIMIT_BYTES + 1)
    if len(body) > METADATA_LIMIT_BYTES:
        raise ValueError("Package metadata exceeds limit")
    data = json.loads(body)
    if (
        not isinstance(data, dict)
        or not isinstance(data.get("info"), dict)
        or data["info"].get("name") != PACKAGE
        or not isinstance(data.get("releases"), dict)
    ):
        raise ValueError("Unexpected package metadata")
    candidates: list[Version] = []
    supported_tags = set(sys_tags())
    for name, artifacts in data.get("releases", {}).items():
        try:
            version = Version(name)
        except InvalidVersion:
            continue
        if (
            version <= current
            or version.is_prerelease
            or version.is_devrelease
            or version not in installation.primary.specifier
            or not isinstance(artifacts, list)
        ):
            continue
        for artifact in artifacts:
            if not isinstance(artifact, dict) or artifact.get("yanked", False):
                continue
            filename = artifact.get("filename")
            if not isinstance(filename, str):
                continue
            try:
                wheel_name, wheel_version, _, tags = parse_wheel_filename(filename)
            except InvalidWheelFilename:
                continue
            if (
                wheel_name != PACKAGE
                or wheel_version != version
                or not supported_tags.intersection(tags)
            ):
                continue
            required = artifact.get("requires_python") or ""
            try:
                compatible = platform.python_version() in SpecifierSet(required)
            except (ValueError, TypeError):
                continue
            if compatible:
                candidates.append(version)
                break
    return max(candidates, default=None)


def _discard_unpublished(directory: Path, prefix: Path) -> None:
    """Remove only this failed candidate, and preserve anything possibly published."""
    try:
        manifest = _json(directory / "active.json")
    except FileNotFoundError:
        manifest = None
    except (OSError, ValueError, TypeError):
        return
    if manifest is not None:
        selected = manifest.get("prefix")
        if not isinstance(selected, str):
            return
        try:
            if Path(selected).resolve() == prefix.resolve():
                return
        except (OSError, ValueError, RuntimeError):
            return
    # Windows may still hold a file after a timed-out installer. Cleanup is best effort;
    # it must never hide the installation error or touch another runtime directory.
    shutil.rmtree(prefix, ignore_errors=True)


def _prepare(installation: Installation, version: Version) -> Path:
    assert installation.uv is not None
    # Never move virtualenvs or overwrite an earlier runtime: script shebangs contain its path.
    prefix = installation.directory / "runtimes" / f"{version}-{uuid.uuid4().hex}"
    prefix.parent.mkdir(parents=True, exist_ok=True)
    try:
        _command(
            [
                installation.uv,
                "venv",
                "--no-config",
                "--no-project",
                "--no-python-downloads",
                "--python",
                str(_python(Path(sys.prefix))) if sys.platform == "win32" else sys.executable,
                str(prefix),
            ],
            timeout=CREATE_TIMEOUT_SECONDS,
            cwd=installation.directory,
        )
        python = _python(prefix)
        extras = (
            "[" + ",".join(sorted(installation.primary.extras)) + "]"
            if installation.primary.extras
            else ""
        )
        _command(
            [
                installation.uv,
                "pip",
                "install",
                "--no-config",
                "--python",
                str(python),
                "--default-index",
                PYPI_INDEX_URL,
                "--prerelease",
                "disallow",
                "--only-binary",
                ":all:",
                f"{PACKAGE}{extras}=={version}",
                *installation.additional,
            ],
            timeout=INSTALL_TIMEOUT_SECONDS,
            cwd=installation.directory,
        )
        _command(
            [str(python), "-I", "-c", _PROBE, str(version)],
            timeout=VERIFY_TIMEOUT_SECONDS,
            cwd=installation.directory,
        )
        private_write(prefix / ORIGIN_MARKER, {"origin": str(installation.origin)})
        private_write(
            installation.directory / "active.json",
            {
                "version": str(version),
                "prefix": str(prefix),
                "receipt_digest": installation.receipt_digest,
            },
        )
        return python
    except BaseException:
        _discard_unpublished(installation.directory, prefix)
        raise


def select_runtime(*, check: bool) -> Path | None:
    """Select an installed runtime offline, optionally preparing a newer stable release.

    Failure never changes the existing pointer, package environment, or application data.
    A contending startup uses the last validated runtime immediately.
    """
    selected: Path | None = None
    directory: Path | None = None
    try:
        installation = _installation()
        if installation is None:
            return None
        directory = installation.directory
        active = _active(installation)
        selected = active[1] if active else _python(installation.origin)
        if check and _enabled():
            if not installation.supported:
                _notice("自定义安装来源或约束需手动升级；继续使用已安装版本。", directory)
            elif installation.uv is None:
                _notice("未找到 uv；继续使用已安装版本。", directory)
            else:
                installation.directory.mkdir(parents=True, exist_ok=True)
                os.chmod(installation.directory, 0o700)
                lock = FileLock(installation.directory / "update.lock", timeout=0)
                try:
                    with lock:
                        active = _active(installation)
                        selected = active[1] if active else _python(installation.origin)
                        current = active[0] if active else installation.version
                        latest = _latest(installation, current)
                        if latest:
                            _notice(f"准备 {latest}，现有任务继续运行。", directory)
                            selected = _prepare(installation, latest)
                            _notice(f"已就绪，将使用 {latest} 启动。", directory)
                except Timeout:
                    pass
    except (OSError, ValueError, TypeError, KeyError, subprocess.SubprocessError):
        if check:
            _notice("检查或安装未完成；继续使用已安装版本。", directory)
    # The windowless Windows launcher and the console interpreter belong to the
    # same runtime. Switching pythonw.exe to python.exe would create a redundant
    # child (and could otherwise bring back a console at every startup).
    if selected is None or selected.parent.parent.resolve() == Path(sys.prefix).resolve():
        return None
    return selected
