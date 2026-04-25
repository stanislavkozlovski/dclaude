# Architecture

## Overview

This repo provides a Dockerized wrapper around the official Claude Code and Codex CLIs. The project is intentionally small:

- one shared Docker image
- two user-facing entrypoints: `dclaude` and `dcodex`
- one shared shell helper for runtime assembly
- one `docs/VERSION` file that drives image naming and releases
- three GitHub Actions workflows for CI, scheduled tool refreshes, and tagged releases

The design goal is path fidelity. The target repo and configured read-only directories are mounted into the container at the same absolute paths they have on the host. When no launcher config is present, no extra host directories are mounted. `/workspace` exists only as a compatibility alias.

The runtime model uses two separate path roles:

- **tool home**: where this `dclaude` repo lives
- **target repo**: the git repo from the user's current shell location, where he intends to run `dclaude` on

The launcher repo provides the image and shell assets. The target repo provides the live working tree.

## Components

### `Dockerfile`

Builds the shared runtime image on top of `python:3.12-slim`, installs Node 22, `uv`, and the official CLI packages:

- `@anthropic-ai/claude-code@2.1.119`
- `@openai/codex@0.125.0`
- `cx 0.6.5`

The image also pre-creates `/workspace` as an alias chain that can be repointed at runtime without root privileges.

### `dclaude` and `dcodex`

Thin wrappers that:

- resolve tool home from the wrapper script path
- read `docs/VERSION`
- require Docker
- resolve the target git repo root and current path from the current shell
- create minimal host state directories
- manage a persistent warm container per tool and target repo
- mount the target repo and support paths with same-path bind mounts
- run the container as the current host UID/GID
- launch the correct interactive CLI command
- expose `--version` and `--update-tool` without requiring a target git repo

### `scripts/agent-common.sh`

Holds the shared launch logic:

- version loading
- separation of tool home from target repo
- optional `scripts/dclaude.yaml` loading and mount parsing
- warm container naming, spec matching, reset, and stop flows
- image-content-aware warm-container invalidation
- bind-mounting the live launcher bootstrap script into warm containers
- interactive confirmed `--update-tool` flow for the wrapper's pinned CLI
- common Docker flags
- mount assembly
- optional SSH agent forwarding
- Codex multi-profile support (`--profile NAME`, `--list-profiles`)
- image build bootstrap
- `/workspace` compatibility alias setup

### `scripts/dclaude.yaml.example`

Example launcher mount config (users copy to `scripts/dclaude.yaml` and uncomment):

- uses a single `read_only_mounts:` list
- entries are host directory paths such as `~/Desktop` or `/tmp/dclaude-share`
- entries are bind-mounted read-only at the same absolute path inside the container
- entries that overlap `~/.ssh` or `/run/host-services` are rejected so SSH only enters through `--ssh`
- no mounts are configured by default; the example file ships with commented-out entries
- at runtime, the launcher reads `scripts/dclaude.yaml` from the launcher repo (if present)

### `scripts/bump_version.py`

Small release helper used by CI to:

- increment the patch version in `docs/VERSION`

### `scripts/sync_tool_versions.py`

Checks the pinned upstream tool versions and can rewrite the repo when newer versions are available:

- reads current `cx`, Claude Code, and Codex pins from `Dockerfile`
- fetches the latest upstream release metadata
- can target one tool or all tools
- can emit machine-readable status for wrapper automation
- refreshes the `Dockerfile` args plus the pinned-version mentions in `README.md` and `docs/ARCHITECTURE.md`

### `.github/workflows/ci.yml`

Runs shell linting and smoke checks on pull requests and `main`. On successful non-bot pushes to `main`, it also bumps the patch version and pushes the matching release tag.

### `.github/workflows/tool-updates.yml`

Runs on a schedule and on manual dispatch. It refreshes pinned upstream tool versions, verifies the updated image still builds, and opens a pull request when changes are detected.

### `.github/workflows/release.yml`

Runs on `v*` tags. It verifies the tag matches `docs/VERSION`, creates the release tarball, publishes the GitHub Release, and updates the Homebrew tap when the cross-repo token is configured.

## Runtime Mount Model

Always mounted:

- target repo root at its real host path, read/write
- launcher `scripts/container-launch.sh` into `/usr/local/bin/dclaude-container-launch`, read-only
- configured read-only directories at the same path, read-only
- host `~/.cache/dclaude/cx` into container `~/.cache/cx`
- `~/.cache/pip`
- `~/.cache/uv`

Default folder config when no `scripts/dclaude.yaml` is present:

- none — no extra host directories are mounted

Claude-only state:

- `~/.claude`
- `~/.claude.json`
- `~/.config/claude-code`

Codex-only state:

- `~/.codex` (or `~/.codex-NAME` when launched with `--profile NAME`)
- `cx skill` seeds `~/.codex/skills/dclaude-cx-navigation` at runtime if missing

Optional SSH mode:

- `/run/host-services/ssh-auth.sock`
- `~/.ssh/known_hosts` read-only

## Process Model

The wrappers ensure one warm container per tool, target repo, and profile (when `--profile` is used). They create it with `docker run -d` when missing or stale, then launch sessions with `docker exec`.

Warm container creation uses:

- `--init`
- `--cap-drop=ALL`
- `--security-opt=no-new-privileges:true`
- `--user <host uid>:<host gid>`
- `--workdir <target repo root>`

Interactive session launch uses:

- `docker exec`
- `--user <host uid>:<host gid>`
- `--workdir <current path inside the target repo>`

Lifecycle controls:

- `--reset` removes and recreates the warm container before launching
- `--stop` removes the warm container for the current repo and exits
- `--rebuild` rebuilds the image and recreates the warm container

Tool launch commands:

- Claude: `claude --dangerously-skip-permissions`
- Codex: `codex --dangerously-bypass-approvals-and-sandbox`

Image build context:

- always the launcher repo, not the target repo

Default image naming:

- `dclaude:<version>` where `<version>` is read from `docs/VERSION`
- `DCLAUDE_IMAGE_NAME` can override that default for local experiments or alternate tags

Container startup bootstrap:

- creates `/workspace` symlink to the target repo
- reports whether `cx` is available
- installs `bash`, `python`, and `typescript` grammars on first use
- seeds `~/.claude/CX.md` and wires `~/.claude/CLAUDE.md` when Claude guidance is missing
- seeds `~/.codex/AGENTS.md` and `~/.codex/skills/dclaude-cx-navigation` when Codex guidance is missing
- for named profiles (`~/.codex-NAME`): seeds `AGENTS.md` and `skills/dclaude-cx-navigation` from the default profile (`~/.codex`) on first use; only these specific files are copied, and only when absent in the profile dir

Release automation:

- scheduled automation checks for newer upstream `cx`, Claude Code, and Codex releases and proposes pin updates via pull requests
- CI increments the patch version after successful pushes to `main`
- release tags are `vX.Y.Z`
- tag builds publish a source tarball consumed by the Homebrew tap

## Persistent State

This repo has no application database. Persistence consists of host-mounted auth and cache directories plus warm Docker containers keyed by tool and target repo.

### Database Schema

None.

There are no tables, collections, migrations, or ORM models in this project. The only stateful paths are filesystem mounts:

- Claude auth/config: `~/.claude`, `~/.claude.json`, `~/.config/claude-code`
- Codex state: `~/.codex` (or `~/.codex-NAME` per profile)
- shared caches: `~/.cache/dclaude/cx` on host mapped to `~/.cache/cx` in-container, plus `~/.cache/pip`, `~/.cache/uv`

Additional non-database runtime state:

- warm Docker containers named per tool, target repo, and profile
- warm-container labels that track the expected runtime spec for invalidation and reuse

The launcher may create missing guidance files inside those mounted directories:

- `~/.claude/CX.md`
- `~/.claude/CLAUDE.md`
- `~/.codex/AGENTS.md` (or `~/.codex-NAME/AGENTS.md` per profile)
- `~/.codex/skills/dclaude-cx-navigation` (or `~/.codex-NAME/skills/dclaude-cx-navigation` per profile)

## Security Notes

The sandbox boundary is Docker plus the narrow mount set. The design explicitly excludes:

- `docker.sock`
- privileged mode
- full home-directory mounts
- implicit home-directory access beyond the explicitly configured read-only folder list
- configured mounts that overlap `~/.ssh` or `/run/host-services`
- raw private key file mounts
- API-key auth workflows for Claude or Codex
- copying any repo-local `.ssh` directory into the Docker build context
