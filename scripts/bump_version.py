#!/usr/bin/env python3
from __future__ import annotations

import pathlib
import re


REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent
VERSION_PATH = REPO_ROOT / "docs" / "VERSION"
SEMVER_RE = re.compile(r"^(?P<major>0|[1-9]\d*)\.(?P<minor>0|[1-9]\d*)\.(?P<patch>0|[1-9]\d*)$")


def read_version() -> tuple[int, int, int]:
    version = VERSION_PATH.read_text(encoding="utf-8").strip()
    match = SEMVER_RE.fullmatch(version)
    if not match:
        raise SystemExit(f"Invalid VERSION value: {version!r}")

    return tuple(int(match.group(part)) for part in ("major", "minor", "patch"))


def write_version(version: str) -> None:
    VERSION_PATH.write_text(f"{version}\n", encoding="utf-8")


def main() -> None:
    major, minor, patch = read_version()
    next_version = f"{major}.{minor}.{patch + 1}"
    write_version(next_version)
    print(next_version)


if __name__ == "__main__":
    main()
