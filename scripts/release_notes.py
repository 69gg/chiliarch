"""Validate release identities and extract one CHANGELOG entry."""

from __future__ import annotations

import argparse
import json
import re
import tomllib
from pathlib import Path

from packaging.version import Version

ROOT = Path(__file__).resolve().parents[1]
HEADER = re.compile(r"^##\s+v(\S+)(?:\s+.*)?$", re.MULTILINE)


def changelog_entry(root: Path, tag: str) -> str:
    target = Version(tag.removeprefix("v"))
    text = (root / "CHANGELOG.md").read_text(encoding="utf-8")
    headings = list(HEADER.finditer(text))
    for index, heading in enumerate(headings):
        if Version(heading.group(1)) == target:
            end = headings[index + 1].start() if index + 1 < len(headings) else len(text)
            content = text[heading.end() : end].strip()
            if content.endswith("---"):
                content = content[:-3].rstrip()
            if not content:
                raise ValueError(f"CHANGELOG entry for {tag} is empty")
            return text[heading.start() : heading.end()] + "\n\n" + content + "\n"
    raise ValueError(f"Missing CHANGELOG entry for {tag}")


def validate(root: Path, tag: str) -> dict[str, str]:
    if not tag.startswith("v"):
        raise ValueError("Release tags must start with v")
    target = Version(tag[1:])
    if len(target.release) != 3 or target.local or target.epoch:
        raise ValueError("Release tags require a three-part public PEP 440 version")
    with (root / "pyproject.toml").open("rb") as stream:
        project = tomllib.load(stream)["project"]
    frontend = json.loads((root / "web/package.json").read_text())
    with (root / "uv.lock").open("rb") as stream:
        lock = tomllib.load(stream)
    locked = [
        item
        for item in lock["package"]
        if item["name"] == project["name"] and item.get("source", {}).get("editable") == "."
    ]
    if not locked:
        raise ValueError("Project is missing from uv.lock")
    sources = {
        "pyproject.toml": str(project["version"]),
        "web/package.json": str(frontend["version"]),
        "uv.lock": str(locked[0]["version"]),
    }
    for source, version in sources.items():
        if Version(version) != target:
            raise ValueError(f"{source} version {version} does not match {tag}")
    changelog_entry(root, tag)
    return sources


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("validate", "extract", "prerelease"))
    parser.add_argument("--tag", required=True)
    parser.add_argument("--output", type=Path)
    arguments = parser.parse_args()
    validate(ROOT, arguments.tag)
    if arguments.command == "prerelease":
        print(str(Version(arguments.tag.removeprefix("v")).is_prerelease).lower())
    elif arguments.command == "extract":
        notes = changelog_entry(ROOT, arguments.tag)
        if arguments.output:
            arguments.output.write_text(notes, encoding="utf-8")
        else:
            print(notes, end="")
    else:
        print(f"Validated {arguments.tag}: project, frontend, lock and CHANGELOG agree")


if __name__ == "__main__":
    main()
