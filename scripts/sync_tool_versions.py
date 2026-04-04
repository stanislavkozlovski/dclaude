#!/usr/bin/env python3
from __future__ import annotations

import argparse
import dataclasses
import hashlib
import json
import pathlib
import re
import sys
import urllib.parse
import urllib.request


REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent
DOCKERFILE_PATH = REPO_ROOT / "Dockerfile"
README_PATH = REPO_ROOT / "README.md"
ARCHITECTURE_PATH = REPO_ROOT / "ARCHITECTURE.md"
USER_AGENT = "dclaude-tool-version-sync/1.0"

DOCKER_PATTERNS = {
    "cx": re.compile(r"^ARG CX_VERSION=(?P<value>\S+)$", re.MULTILINE),
    "cx_sha256": re.compile(r"^ARG CX_SHA256=(?P<value>\S+)$", re.MULTILINE),
    "claude_code": re.compile(r"^ARG CLAUDE_CODE_VERSION=(?P<value>\S+)$", re.MULTILINE),
    "codex": re.compile(r"^ARG CODEX_VERSION=(?P<value>\S+)$", re.MULTILINE),
}

TOOL_ALIASES = {
    "cx": "cx",
    "claude-code": "claude_code",
    "codex": "codex",
}


@dataclasses.dataclass(frozen=True)
class ToolVersions:
    cx: str
    cx_sha256: str
    claude_code: str
    codex: str


def selected_tools(tool_name: str) -> tuple[str, ...]:
    if tool_name == "all":
        return ("cx", "claude_code", "codex")

    return (TOOL_ALIASES[tool_name],)


def fetch_json(url: str) -> dict[str, object]:
    request = urllib.request.Request(
        url,
        headers={
            "Accept": "application/json",
            "User-Agent": USER_AGENT,
        },
    )
    with urllib.request.urlopen(request, timeout=60) as response:
        return json.load(response)


def fetch_npm_latest(package_name: str) -> str:
    encoded_package = urllib.parse.quote(package_name, safe="")
    payload = fetch_json(f"https://registry.npmjs.org/{encoded_package}/latest")
    version = payload.get("version")
    if not isinstance(version, str) or not version:
        raise SystemExit(f"npm registry did not return a version for {package_name}")
    return version


def fetch_latest_cx_version() -> str:
    payload = fetch_json("https://api.github.com/repos/ind-igo/cx/releases/latest")
    tag_name = payload.get("tag_name")
    if not isinstance(tag_name, str) or not tag_name:
        raise SystemExit("GitHub did not return a cx release tag")
    return tag_name.removeprefix("v")


def fetch_cx_sha256(version: str) -> str:
    archive_url = f"https://github.com/ind-igo/cx/archive/refs/tags/v{version}.tar.gz"
    request = urllib.request.Request(
        archive_url,
        headers={"User-Agent": USER_AGENT},
    )
    digest = hashlib.sha256()
    with urllib.request.urlopen(request, timeout=60) as response:
        while True:
            chunk = response.read(1024 * 1024)
            if not chunk:
                break
            digest.update(chunk)
    return digest.hexdigest()


def read_current_versions() -> ToolVersions:
    dockerfile = DOCKERFILE_PATH.read_text(encoding="utf-8")

    values: dict[str, str] = {}
    for name, pattern in DOCKER_PATTERNS.items():
        match = pattern.search(dockerfile)
        if not match:
            raise SystemExit(f"Failed to find {name} in {DOCKERFILE_PATH}")
        values[name] = match.group("value")

    return ToolVersions(**values)


def resolve_latest_versions(current: ToolVersions, tools: tuple[str, ...]) -> ToolVersions:
    latest = current

    if "cx" in tools:
        cx_version = fetch_latest_cx_version()
        latest = dataclasses.replace(
            latest,
            cx=cx_version,
            cx_sha256=fetch_cx_sha256(cx_version),
        )

    if "claude_code" in tools:
        latest = dataclasses.replace(
            latest,
            claude_code=fetch_npm_latest("@anthropic-ai/claude-code"),
        )

    if "codex" in tools:
        latest = dataclasses.replace(
            latest,
            codex=fetch_npm_latest("@openai/codex"),
        )

    return latest


def apply_replacements(path: pathlib.Path, replacements: list[tuple[str, str, str]]) -> None:
    original = path.read_text(encoding="utf-8")
    updated = original
    for label, pattern, replacement in replacements:
        updated, count = re.subn(pattern, replacement, updated, count=1, flags=re.MULTILINE)
        if count != 1:
            raise SystemExit(f"Failed to update {label} in {path}")

    if updated != original:
        path.write_text(updated, encoding="utf-8")


def update_files(latest: ToolVersions, tools: tuple[str, ...]) -> None:
    docker_replacements: list[tuple[str, str, str]] = []
    readme_replacements: list[tuple[str, str, str]] = []
    architecture_replacements: list[tuple[str, str, str]] = []

    if "cx" in tools:
        docker_replacements.extend(
            [
                ("CX_VERSION", r"^ARG CX_VERSION=\S+$", f"ARG CX_VERSION={latest.cx}"),
                ("CX_SHA256", r"^ARG CX_SHA256=\S+$", f"ARG CX_SHA256={latest.cx_sha256}"),
            ]
        )
        readme_replacements.append(("README cx version", r"`cx [^`]+`", f"`cx {latest.cx}`"))
        architecture_replacements.append(("ARCHITECTURE cx version", r"`cx [^`]+`", f"`cx {latest.cx}`"))

    if "claude_code" in tools:
        docker_replacements.append(
            (
                "CLAUDE_CODE_VERSION",
                r"^ARG CLAUDE_CODE_VERSION=\S+$",
                f"ARG CLAUDE_CODE_VERSION={latest.claude_code}",
            )
        )
        readme_replacements.append(
            (
                "README Claude Code version",
                r"`@anthropic-ai/claude-code@[^`]+`",
                f"`@anthropic-ai/claude-code@{latest.claude_code}`",
            )
        )
        architecture_replacements.append(
            (
                "ARCHITECTURE Claude Code version",
                r"`@anthropic-ai/claude-code@[^`]+`",
                f"`@anthropic-ai/claude-code@{latest.claude_code}`",
            )
        )

    if "codex" in tools:
        docker_replacements.append(
            ("CODEX_VERSION", r"^ARG CODEX_VERSION=\S+$", f"ARG CODEX_VERSION={latest.codex}")
        )
        readme_replacements.extend(
            [
                (
                    "README Codex version",
                    r"`@openai/codex@[^`]+`",
                    f"`@openai/codex@{latest.codex}`",
                ),
                (
                    "README codex-cli validation version",
                    r"`codex-cli [^`]+`",
                    f"`codex-cli {latest.codex}`",
                ),
            ]
        )
        architecture_replacements.append(
            (
                "ARCHITECTURE Codex version",
                r"`@openai/codex@[^`]+`",
                f"`@openai/codex@{latest.codex}`",
            )
        )

    if docker_replacements:
        apply_replacements(DOCKERFILE_PATH, docker_replacements)

    if readme_replacements:
        apply_replacements(README_PATH, readme_replacements)

    if architecture_replacements:
        apply_replacements(ARCHITECTURE_PATH, architecture_replacements)


def print_status(current: ToolVersions, latest: ToolVersions, tools: tuple[str, ...]) -> None:
    rows: list[tuple[str, str, str]] = []

    if "cx" in tools:
        rows.extend(
            [
                ("cx", current.cx, latest.cx),
                ("cx sha256", current.cx_sha256, latest.cx_sha256),
            ]
        )

    if "claude_code" in tools:
        rows.append(("claude-code", current.claude_code, latest.claude_code))

    if "codex" in tools:
        rows.append(("codex", current.codex, latest.codex))

    for label, current_value, latest_value in rows:
        status = "up to date" if current_value == latest_value else f"update available -> {latest_value}"
        print(f"{label:12} current={current_value}  {status}")


def build_status_payload(current: ToolVersions, latest: ToolVersions, tools: tuple[str, ...]) -> dict[str, object]:
    payload_tools: dict[str, dict[str, object]] = {}

    if "cx" in tools:
        payload_tools["cx"] = {
            "current": current.cx,
            "latest": latest.cx,
            "up_to_date": current.cx == latest.cx,
            "sha256_current": current.cx_sha256,
            "sha256_latest": latest.cx_sha256,
            "sha256_up_to_date": current.cx_sha256 == latest.cx_sha256,
        }

    if "claude_code" in tools:
        payload_tools["claude-code"] = {
            "current": current.claude_code,
            "latest": latest.claude_code,
            "up_to_date": current.claude_code == latest.claude_code,
        }

    if "codex" in tools:
        payload_tools["codex"] = {
            "current": current.codex,
            "latest": latest.codex,
            "up_to_date": current.codex == latest.codex,
        }

    return {
        "tools": payload_tools,
        "any_update_available": any(
            not info["up_to_date"] for info in payload_tools.values()
        ),
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Check or refresh pinned upstream tool versions.")
    parser.add_argument(
        "--tool",
        choices=("all", "cx", "claude-code", "codex"),
        default="all",
        help="limit checks or updates to one tool",
    )
    parser.add_argument(
        "--update",
        action="store_true",
        help="rewrite Dockerfile and docs to the latest upstream versions",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="emit machine-readable status instead of human-readable rows",
    )
    args = parser.parse_args()

    current = read_current_versions()
    tools = selected_tools(args.tool)
    latest = resolve_latest_versions(current, tools)

    if args.json:
        print(json.dumps(build_status_payload(current, latest, tools), sort_keys=True))
        return

    print_status(current, latest, tools)

    if args.update:
        update_files(latest, tools)
        print("Updated Dockerfile, README.md, and ARCHITECTURE.md")
        return

    if any(getattr(current, tool) != getattr(latest, tool) for tool in tools):
        sys.exit(1)


if __name__ == "__main__":
    main()
