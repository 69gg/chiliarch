"""Synchronize the authoritative Python version and frontend manifest via uv."""

from __future__ import annotations

import argparse
import json
import subprocess
import tomllib
from pathlib import Path

from packaging.version import Version

ROOT = Path(__file__).resolve().parents[1]


def normalize_version(value: str) -> str:
    version = Version(value.removeprefix("v"))
    if len(version.release) != 3 or version.local or version.epoch:
        raise ValueError("Use a three-part public PEP 440 version (e.g. 0.2.0 or 0.2.0rc1)")
    return str(version)


def current_version(root: Path) -> str:
    with (root / "pyproject.toml").open("rb") as stream:
        return str(tomllib.load(stream)["project"]["version"])


def bump_version(value: str, root: Path = ROOT, *, dry_run: bool = False) -> dict[str, object]:
    target = normalize_version(value)
    old = current_version(root)
    paths = (root / "pyproject.toml", root / "uv.lock", root / "web/package.json")
    before = {path: path.read_bytes() if path.exists() else None for path in paths}
    if dry_run:
        return {
            "old_version": old,
            "new_version": target,
            "dry_run": True,
            "managed": [path.relative_to(root).as_posix() for path in paths],
        }
    try:
        # uv updates its own lock format, including recovery of same-version reruns.
        subprocess.run(["uv", "version", target, "--no-sync"], cwd=root, check=True)
        frontend = root / "web/package.json"
        manifest = json.loads(frontend.read_text(encoding="utf-8"))
        manifest["version"] = target
        frontend.write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
        changed = [
            path.relative_to(root).as_posix()
            for path, content in before.items()
            if path.read_bytes() != content
        ]
    except BaseException:
        for path, content in before.items():
            if content is None:
                path.unlink(missing_ok=True)
            else:
                path.write_bytes(content)
        raise
    return {"old_version": old, "new_version": target, "changed": changed}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("version")
    parser.add_argument("--dry-run", action="store_true")
    arguments = parser.parse_args()
    print(
        json.dumps(
            bump_version(arguments.version, dry_run=arguments.dry_run), ensure_ascii=False, indent=2
        )
    )


if __name__ == "__main__":
    main()
