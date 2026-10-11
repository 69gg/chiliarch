from __future__ import annotations

import io
import json
import subprocess
import sys
import venv
from pathlib import Path
from typing import Any

import pytest
import tomli_w
from filelock import FileLock

from chiliarch import updates


def environment(prefix: Path, version: str) -> None:
    python = updates._python(prefix)
    python.parent.mkdir(parents=True, exist_ok=True)
    python.write_text("isolated fake Python", encoding="utf-8")
    packages = prefix / "Lib/site-packages"
    packages.mkdir(parents=True, exist_ok=True)
    for previous in packages.glob("chiliarch-*.dist-info/METADATA"):
        previous.unlink()
        previous.parent.rmdir()
    metadata = packages / f"chiliarch-{version}.dist-info"
    metadata.mkdir()
    (metadata / "METADATA").write_text(f"Name: chiliarch\nVersion: {version}\n", encoding="utf-8")


class Installer:
    def __init__(self, root: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        self.origin = root / "uv tools/chiliarch"
        self.state = root / "user state/daemon"
        self.commands: list[list[str]] = []
        self.requests: list[Any] = []
        self.releases: dict[str, list[dict[str, Any]]] = {
            "0.3.0": [{"requires_python": ">=3.11", "yanked": False}],
            "0.4.0": [{"requires_python": ">=3.11", "yanked": False}],
        }
        self.failure: str | None = None
        environment(self.origin, "0.3.0")
        self.receipt({"requirements": [{"name": "chiliarch"}], "entrypoints": []})
        monkeypatch.setattr(sys, "prefix", str(self.origin))
        monkeypatch.setattr(sys, "executable", str(updates._python(self.origin)))
        monkeypatch.setattr(updates, "default_daemon_directory", lambda: self.state)
        monkeypatch.setattr(updates.shutil, "which", lambda name: "/fake/uv")
        monkeypatch.setattr(updates.urllib.request, "urlopen", self.request)
        monkeypatch.setattr(updates, "_command", self.command)
        monkeypatch.delenv("CHILIARCH_AUTO_UPDATE", raising=False)

    def receipt(self, tool: dict[str, Any]) -> None:
        (self.origin / "uv-receipt.toml").write_text(
            tomli_w.dumps({"tool": tool}), encoding="utf-8"
        )

    def request(self, request: Any, **kwargs: Any) -> io.BytesIO:
        self.requests.append(request)
        assert kwargs["timeout"] == updates.CHECK_TIMEOUT_SECONDS
        if self.failure == "network":
            raise TimeoutError("offline; never leak password")
        releases = {
            version: [
                {"filename": f"chiliarch-{version}-py3-none-any.whl", **artifact}
                for artifact in artifacts
            ]
            for version, artifacts in self.releases.items()
        }
        return io.BytesIO(
            json.dumps({"info": {"name": "chiliarch"}, "releases": releases}).encode()
        )

    def command(self, args: list[str], **kwargs: Any) -> None:
        self.commands.append(args)
        if "venv" in args:
            assert "--no-config" in args and "--no-project" in args
            prefix = Path(args[-1])
            environment(prefix, "0.0.0")
        elif "install" in args:
            assert "--no-config" in args
            prefix = Path(args[args.index("--python") + 1]).parent.parent
            requirement = next(arg for arg in args if arg.startswith("chiliarch") and "==" in arg)
            version = requirement.rsplit("==", 1)[1]
            if self.failure == "install":
                raise subprocess.CalledProcessError(1, args, stderr="token=secret")
            environment(prefix, version)
        else:
            assert args[1:3] == ["-I", "-c"]
            if self.failure == "probe":
                raise subprocess.CalledProcessError(1, args)

    @property
    def directory(self) -> Path:
        installation = updates._installation()
        assert installation is not None
        return installation.directory


@pytest.fixture
def installer(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Installer:
    return Installer(tmp_path, monkeypatch)


def test_prepares_separate_runtime_preserves_tool_and_selects_offline(
    installer: Installer,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    receipt = (installer.origin / "uv-receipt.toml").read_bytes()
    selected = updates.select_runtime(check=True)
    assert selected is not None and selected.is_file()
    assert not selected.is_relative_to(installer.origin)
    assert updates._version(installer.origin) == updates.Version("0.3.0")
    assert (installer.origin / "uv-receipt.toml").read_bytes() == receipt
    assert updates.select_runtime(check=False) == selected
    assert len(installer.requests) == 1 and len(installer.commands) == 3
    monkeypatch.setenv("CHILIARCH_AUTO_UPDATE", "0")
    assert updates.select_runtime(check=True) == selected
    assert len(installer.requests) == 1
    monkeypatch.setattr(sys, "prefix", str(selected.parent.parent))
    monkeypatch.setattr(sys, "executable", str(selected))
    assert updates.select_runtime(check=False) is None
    assert capsys.readouterr().out == ""
    assert (installer.directory / "update.log").is_file()


def test_receipt_preserves_primary_range_extras_and_additional_requirements(
    installer: Installer,
) -> None:
    installer.receipt(
        {
            "requirements": [
                {"name": "chiliarch", "specifier": ">=0.3,<0.5", "extras": ["extra"]},
                "other-package>=2; python_version >= '3.11'",
            ]
        }
    )
    installer.releases["0.5.0"] = [{"yanked": False}]
    assert updates.select_runtime(check=True) is not None
    installed = installer.commands[1]
    assert "chiliarch[extra]==0.4.0" in installed
    assert 'other-package>=2; python_version >= "3.11"' in installed


@pytest.mark.parametrize(
    "requirement", ["chiliarch==0.3.0", {"name": "chiliarch", "specifier": "==0.3.0"}]
)
def test_pinned_installation_does_not_upgrade(installer: Installer, requirement: Any) -> None:
    installer.receipt({"requirements": [requirement]})
    assert updates.select_runtime(check=True) is None
    assert len(installer.requests) == 1
    assert installer.commands == []


@pytest.mark.parametrize(
    "extra",
    [
        {"options": {"index-url": "https://private.invalid/simple"}},
        {"options": {"prerelease": "allow"}},
        {"constraints": [{"name": "httpx", "specifier": "<0.29"}]},
    ],
)
def test_custom_install_configuration_is_not_silently_replaced(
    installer: Installer, extra: dict[str, Any]
) -> None:
    installer.receipt({"requirements": [{"name": "chiliarch"}], **extra})
    assert updates.select_runtime(check=True) is None
    assert installer.requests == installer.commands == []


@pytest.mark.parametrize(
    "requirement",
    [
        {"name": "chiliarch", "git": "https://example.invalid/code.git"},
        "chiliarch @ https://example.invalid/chiliarch.whl",
        {"name": "chiliarch", "directory": "/source", "editable": True},
        {"name": "other-tool"},
    ],
)
def test_non_registry_and_unrelated_tool_is_skipped(installer: Installer, requirement: Any) -> None:
    installer.receipt({"requirements": [requirement]})
    assert updates.select_runtime(check=True) is None
    assert installer.requests == installer.commands == []


def test_source_checkout_never_looks_up_uv_or_network(
    installer: Installer, monkeypatch: pytest.MonkeyPatch
) -> None:
    (installer.origin / "uv-receipt.toml").unlink()
    monkeypatch.setattr(
        updates.shutil, "which", lambda name: pytest.fail("Source checkout queried uv")
    )
    assert updates.select_runtime(check=True) is None
    assert updates.startup_budget() == 0
    assert installer.requests == installer.commands == []


def test_only_stable_not_yanked_python_compatible_release_is_selected(installer: Installer) -> None:
    installer.releases.update(
        {
            "1.0.0rc1": [{"yanked": False}],
            "1.0.0.dev1": [{"yanked": False}],
            "0.9.0": [{"yanked": True}],
            "0.8.0": [{"requires_python": ">=99"}],
            "0.7.0": [],
            "broken": [{"yanked": False}],
            "0.6.0": [{"requires_python": "bad"}],
        }
    )
    selected = updates.select_runtime(check=True)
    assert selected is not None
    assert updates._version(selected.parent.parent) == updates.Version("0.4.0")


@pytest.mark.parametrize("failure", ["network", "install", "probe"])
def test_failed_update_preserves_verified_runtime_and_no_secrets_in_logs(
    installer: Installer,
    failure: str,
    capsys: pytest.CaptureFixture[str],
) -> None:
    selected = updates.select_runtime(check=True)
    previous = (installer.directory / "active.json").read_bytes()
    installer.releases["0.5.0"] = [{"yanked": False}]
    installer.failure = failure
    assert updates.select_runtime(check=True) == selected
    assert (installer.directory / "active.json").read_bytes() == previous
    assert selected is not None and selected.exists()
    captured = capsys.readouterr()
    assert captured.out == "" and "secret" not in captured.err
    assert "secret" not in (installer.directory / "update.log").read_text()


def test_lock_contention_uses_active_without_installing(installer: Installer) -> None:
    selected = updates.select_runtime(check=True)
    before = len(installer.requests)
    with FileLock(installer.directory / "update.lock", timeout=0):
        assert updates.select_runtime(check=True) == selected
    assert len(installer.requests) == before


def test_manual_tool_upgrade_is_never_downgraded(
    installer: Installer, monkeypatch: pytest.MonkeyPatch
) -> None:
    selected = updates.select_runtime(check=True)
    assert selected is not None
    environment(installer.origin, "0.6.0")
    assert updates.select_runtime(check=False) is None
    monkeypatch.setattr(sys, "prefix", str(selected.parent.parent))
    monkeypatch.setattr(sys, "executable", str(selected))
    assert updates.select_runtime(check=False) == updates._python(installer.origin)
    assert selected.exists()


def test_unknown_manifest_cannot_run_outside_private_runtime(
    installer: Installer, tmp_path: Path
) -> None:
    updates.select_runtime(check=True)
    path = installer.directory / "active.json"
    manifest = json.loads(path.read_text())
    foreign = tmp_path / "foreign"
    environment(foreign, "0.4.0")
    (foreign / updates.ORIGIN_MARKER).write_text(json.dumps({"origin": str(installer.origin)}))
    manifest["prefix"] = str(foreign)
    path.write_text(json.dumps(manifest))
    assert updates.select_runtime(check=False) is None


def test_missing_active_interpreter_falls_back_to_original(installer: Installer) -> None:
    selected = updates.select_runtime(check=True)
    assert selected is not None
    selected.unlink()
    assert updates.select_runtime(check=False) is None


def test_startup_budget_is_local_and_respects_disable(
    installer: Installer, monkeypatch: pytest.MonkeyPatch
) -> None:
    assert updates.startup_budget() == updates.STARTUP_UPDATE_TIMEOUT_SECONDS
    monkeypatch.setenv("CHILIARCH_AUTO_UPDATE", "0")
    assert updates.startup_budget() == 0
    assert installer.requests == installer.commands == []


def test_default_pypi_receipt_options_are_supported(installer: Installer) -> None:
    installer.receipt(
        {
            "requirements": [{"name": "chiliarch"}],
            "options": {
                "index": [
                    {
                        "url": "https://pypi.org/simple/",
                        "default": True,
                        "explicit": False,
                        "format": "simple",
                        "authenticate": "auto",
                    }
                ]
            },
        }
    )
    assert updates.select_runtime(check=True) is not None


def test_installer_command_cannot_follow_uv_target_or_current_project(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("UV_TARGET", str(tmp_path / "do-not-write"))
    monkeypatch.setenv("UV_SYSTEM_PYTHON", "1")
    monkeypatch.setenv("VIRTUAL_ENV", "other-env")
    monkeypatch.setenv("HTTPS_PROXY", "http://proxy.invalid")
    calls: list[dict[str, Any]] = []

    def command(arguments: list[str], **kwargs: Any) -> None:
        calls.append(kwargs)

    monkeypatch.setattr(updates.subprocess, "run", command)
    updates._command(["fake-uv", "pip", "install"], timeout=1, cwd=tmp_path)
    environment = calls[0]["env"]
    assert "UV_TARGET" not in environment and "UV_SYSTEM_PYTHON" not in environment
    assert "VIRTUAL_ENV" not in environment
    assert environment["HTTPS_PROXY"] == "http://proxy.invalid"


def test_verification_really_checks_imports_version_and_shipped_resources(tmp_path: Path) -> None:
    prefix = tmp_path / "isolated probe"
    venv.create(prefix, with_pip=False)
    python = updates._python(prefix)
    query = subprocess.run(
        [str(python), "-I", "-c", "import sysconfig; print(sysconfig.get_path('purelib'))"],
        check=True,
        capture_output=True,
        text=True,
        timeout=20,
    )
    packages = Path(query.stdout.strip())
    package = packages / "chiliarch"
    package.mkdir()
    for name in ("__init__", "api", "bootstrap", "cli", "daemon", "updates"):
        (package / f"{name}.py").write_text("", encoding="utf-8")
    (package / "static").mkdir()
    (package / "static/index.html").write_text("<main>Installed UI</main>", encoding="utf-8")
    (package / "protocol").mkdir()
    (package / "protocol/__init__.py").write_text(
        "def protocol_schema(): return {}\n", encoding="utf-8"
    )
    (package / "protocol/schema.json").write_text("{}", encoding="utf-8")
    metadata = packages / "chiliarch-0.4.0.dist-info"
    metadata.mkdir()
    (metadata / "METADATA").write_text("Name: chiliarch\nVersion: 0.4.0\n", encoding="utf-8")
    command = [str(python), "-I", "-c", updates._PROBE, "0.4.0"]
    updates._command(command, timeout=20, cwd=tmp_path)
    with pytest.raises(subprocess.CalledProcessError):
        updates._command([*command[:-1], "0.5.0"], timeout=20, cwd=tmp_path)
    (package / "protocol/schema.json").write_text('{"wrong": true}', encoding="utf-8")
    with pytest.raises(subprocess.CalledProcessError):
        updates._command(command, timeout=20, cwd=tmp_path)
    (package / "protocol/schema.json").write_text("{}", encoding="utf-8")
    (package / "static/index.html").unlink()
    with pytest.raises(subprocess.CalledProcessError):
        updates._command(command, timeout=20, cwd=tmp_path)


@pytest.mark.parametrize(
    "payload", [None, [], {"info": "invalid"}, {"info": {"name": "other"}, "releases": {}}]
)
def test_invalid_metadata_cannot_change_active(
    installer: Installer,
    monkeypatch: pytest.MonkeyPatch,
    payload: Any,
) -> None:
    selected = updates.select_runtime(check=True)
    monkeypatch.setattr(
        updates.urllib.request,
        "urlopen",
        lambda *args, **kwargs: io.BytesIO(json.dumps(payload).encode()),
    )
    assert updates.select_runtime(check=True) == selected


def test_no_uv_still_selects_previous_runtime(
    installer: Installer, monkeypatch: pytest.MonkeyPatch
) -> None:
    selected = updates.select_runtime(check=True)
    monkeypatch.setattr(updates.shutil, "which", lambda name: None)
    assert updates.select_runtime(check=True) == selected
    assert updates.select_runtime(check=False) == selected
    assert updates.startup_budget() == 0


@pytest.mark.parametrize(
    "filename",
    [
        "chiliarch-0.5.0-cp299-cp299-win32.whl",
        "chiliarch-0.5.0.tar.gz",
        "unrelated-0.5.0-py3-none-any.whl",
        "chiliarch-0.4.0-py3-none-any.whl",
    ],
)
def test_update_requires_wheel_for_this_package_version_and_interpreter(
    installer: Installer, filename: str
) -> None:
    installer.releases["0.5.0"] = [{"filename": filename}]
    selected = updates.select_runtime(check=True)
    assert selected is not None
    assert updates._version(selected.parent.parent) == updates.Version("0.4.0")


@pytest.mark.parametrize("failure", ["install", "probe"])
def test_repeated_failures_remove_only_unpublished_candidate(
    installer: Installer, failure: str
) -> None:
    selected = updates.select_runtime(check=True)
    assert selected is not None
    installer.releases["0.5.0"] = [{"yanked": False}]
    selected = updates.select_runtime(check=True)
    assert selected is not None
    runtimes = installer.directory / "runtimes"
    original = {path.name for path in runtimes.iterdir()}
    assert len(original) == 2
    installer.releases["0.6.0"] = [{"yanked": False}]
    installer.failure = failure
    for _ in range(3):
        assert updates.select_runtime(check=True) == selected
        assert {path.name for path in runtimes.iterdir()} == original
        assert selected.is_file()


def test_error_after_pointer_published_cannot_delete_active_runtime(
    installer: Installer,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    write = updates.private_write

    def interrupted_write(path: Path, payload: dict[str, Any]) -> None:
        write(path, payload)
        if path.name == "active.json":
            raise OSError("Interrupted after atomic publish")

    monkeypatch.setattr(updates, "private_write", interrupted_write)
    assert updates.select_runtime(check=True) is None
    selected = updates.select_runtime(check=False)
    assert selected is not None and selected.is_file()


def test_failed_candidate_cleanup_does_not_hide_install_failure(
    installer: Installer,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    installation = updates._installation()
    assert installation is not None
    installer.failure = "install"
    cleaned: list[Path] = []

    def unavailable_cleanup(path: Path, *, ignore_errors: bool) -> None:
        assert ignore_errors
        cleaned.append(path)

    monkeypatch.setattr(updates.shutil, "rmtree", unavailable_cleanup)
    with pytest.raises(subprocess.CalledProcessError):
        updates._prepare(installation, updates.Version("0.4.0"))
    assert len(cleaned) == 1
    assert cleaned[0].parent == installer.directory / "runtimes"
