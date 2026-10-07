from __future__ import annotations

import json
import shutil
import tomllib
from pathlib import Path

import pytest

from scripts.bump_version import bump_version, normalize_version
from scripts.release_notes import changelog_entry, validate

ROOT = Path(__file__).resolve().parents[1]


def project_copy(path: Path) -> None:
    (path / "web").mkdir()
    for name in ("pyproject.toml", "uv.lock", "CHANGELOG.md"):
        shutil.copy2(ROOT / name, path / name)
    shutil.copy2(ROOT / "web/package.json", path / "web/package.json")


def test_real_uv_bump_updates_lock_and_recovers_same_version(tmp_path: Path) -> None:
    project_copy(tmp_path)
    previous = (tmp_path / "pyproject.toml").read_bytes()
    preview = bump_version("0.2.0", tmp_path, dry_run=True)
    assert preview["managed"] == ["pyproject.toml", "uv.lock", "web/package.json"]
    assert (tmp_path / "pyproject.toml").read_bytes() == previous
    result = bump_version("0.2.0", tmp_path)
    with (tmp_path / "pyproject.toml").open("rb") as stream:
        assert tomllib.load(stream)["project"]["version"] == "0.2.0"
    with (tmp_path / "uv.lock").open("rb") as stream:
        packages = tomllib.load(stream)["package"]
    assert (
        next(package["version"] for package in packages if package["name"] == "chiliarch")
        == "0.2.0"
    )
    changed = result["changed"]
    assert isinstance(changed, list)
    assert set(changed) == {"pyproject.toml", "uv.lock", "web/package.json"}
    manifest = json.loads((tmp_path / "web/package.json").read_text())
    manifest["version"] = "0.1.9"
    (tmp_path / "web/package.json").write_text(json.dumps(manifest))
    bump_version("0.2.0", tmp_path)
    assert json.loads((tmp_path / "web/package.json").read_text())["version"] == "0.2.0"


def test_bump_failure_restores_all_managed_files(tmp_path: Path) -> None:
    project_copy(tmp_path)
    # A malformed frontend manifest fails after uv has updated Python metadata.
    (tmp_path / "web/package.json").write_text("invalid JSON")
    names = ("pyproject.toml", "uv.lock", "web/package.json")
    before = {name: (tmp_path / name).read_bytes() for name in names}
    with pytest.raises(json.JSONDecodeError):
        bump_version("0.2.0", tmp_path)
    assert all((tmp_path / name).read_bytes() == content for name, content in before.items())


def test_release_validator_rejects_mismatches_and_extracts_exact_entry(tmp_path: Path) -> None:
    project_copy(tmp_path)
    assert validate(tmp_path, "v0.1.0")["uv.lock"] == "0.1.0"
    with pytest.raises(ValueError):
        validate(tmp_path, "v0.2.0")
    (tmp_path / "CHANGELOG.md").write_text(
        "# CHANGELOG\n\n## v0.2.0 New\n\nNew behavior.\n\n---\n\n## v0.1.0 First\n\nInitial behavior.\n\n---\n"
    )
    assert changelog_entry(tmp_path, "v0.1.0") == "## v0.1.0 First\n\nInitial behavior.\n"
    (tmp_path / "CHANGELOG.md").write_text("## v0.1.0 First\n\n---\n")
    with pytest.raises(ValueError):
        validate(tmp_path, "v0.1.0")


@pytest.mark.parametrize("version", ["1.2", "1.2.3+local", "2!1.2.3", "invalid"])
def test_invalid_release_versions_are_rejected(version: str) -> None:
    with pytest.raises(ValueError):
        normalize_version(version)


def test_prerelease_uses_same_public_version_rules() -> None:
    assert normalize_version("v0.2.0rc1") == "0.2.0rc1"


@pytest.mark.parametrize("version,expected", [("0.1.0", "false"), ("0.2.0rc1", "true")])
def test_release_kind_is_derived_from_validated_project_version(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    version: str,
    expected: str,
) -> None:
    from scripts.release_notes import main

    project_copy(tmp_path)
    bump_version(version, tmp_path)
    (tmp_path / "CHANGELOG.md").write_text(f"## v{version} Release\n\nRelease behavior.\n")
    monkeypatch.setattr("scripts.release_notes.ROOT", tmp_path)
    monkeypatch.setattr("sys.argv", ["release_notes.py", "prerelease", "--tag", f"v{version}"])
    main()
    assert capsys.readouterr().out.strip() == expected
