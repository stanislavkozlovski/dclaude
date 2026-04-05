<div align="center">

# dclaude

Run Claude Code or Codex with full autonomous permissions inside a Docker container.

Includes `cx` for lower token usage.

<br>

| QUICKSTART |
| --- |
| [The Problem This Solves](#the-problem) |
| [What This Is](#what-this-is) |
| [Requirements](#requirements) |
| [Homebrew Install](#homebrew-install) |
| [Quick Start](#quick-start) |
| [Folder Mount Config](#folder-mount-config) |
| [Runtime Model](#runtime-model) |
| [cx Integration](#cx-integration) |
| [Auth Persistence](#auth-persistence) |
| [Tool Pin Refreshes](#tool-pin-refreshes) |
| [Releases](#releases) |
| [Security Boundary](#security-boundary) |

<br>

</div>


## The Problem This Solves

<p align="center">
  <i>“Ever have a 1-minute task take you 10+ minutes because it's stuck waiting on you approving multiple chained commands?”</i>
  <br />
  <sub>— I have, many times.</sub>
</p>

Modern coding harness usage requires many tool calls. They all usually require explicit approval from the user which takes time, breaks your flow and requires more multitasking brainpower than simply letting it YOLO.

But letting it YOLO is dangerous. You're allowing an autonomous system with full access to your computer. It's a security nightmare waiting to happen. Ultimately there's a spectrum where on one side sits max productivity and on the other max security. Here are a few of the ways you can go about this problem:

### 1. permission-mode auto, or whitelisted permissions

I tried this, but the harness still finds something stupid to block on.

![](./README/motivation_1.png)
![](./README/motivation_2.png)

It also isn't fully secure either.
![](./README/motivation_3.png)

### 2. run an isolated dev-only VM

Better and more secure, but too much work to set up, and is costly.

### 3. run inside a sandbox with a limited blast radius

That's what `dclaude` is here for! :)

---

## What this is

This repo ships two simple, thin Docker wrappers:
- `dclaude` - a wrapper on top of Claude Code
- `dcodex` - a wrapper on top of OpenAI's Codex

When ran from a folder, `dclaude` runs the coding CLI inside a docker container with the folder bind-mounted (meaning any changes in the container get reflected in the same folder on your computer). Some features:
- you can configure read-only folders, like `~/Desktop` or `~/Downloads`. The container can never write to them - it can only read.
- paths are seamless - the container sees the same absolute repo path you see on your computer. Sending the CLI agent an image path like `~/Desktop/image.png` just works. Vice-versa when it responds to you with a path.
- auth and caches persist across runs
- the Docker container comes pre-installed with Python and Node. (for more languages, either fork this repo or open a PR)

## Docs

Primary docs live under [`README/`](README/):

- [How To Run](README/HOWRUN.md)
- [Architecture](README/ARCHITECTURE.md)
- [Motivation](README/motivation.md)

## Requirements

- Docker Desktop or Docker Engine with `docker`
- a trusted repo
- Docker Desktop file sharing enabled for the repo path and each configured home mount on macOS

## Homebrew Install

```bash
brew install stanislavkozlovski/tap/dclaude
```

## Quick Start

From the repo you want to edit, run:

```bash
cd /path/to/repo
dclaude # or dcodex
```

If you prefer, calling the launcher by its full path also works as long as your current shell is already inside the target repo:

```bash
/path/to/dclaude-repo/dclaude
/path/to/dclaude-repo/dcodex
```

The first run builds the shared image automatically from the launcher repo and starts a warm container for the current repo. Later runs `docker exec` into that warm container unless you pass `--reset` or `--rebuild`.

Wrapper options:

- `--rebuild` forces a fresh `docker build` and recreates the warm container
- `--reset` recreates the warm container before launching
- `--stop` removes the warm container for the current repo and exits
- `--update-tool` checks the latest upstream version for this wrapper's CLI, rewrites the pin in the launcher repo, and rebuilds the image after confirmation
- `--yes` skips the confirmation prompt for `--update-tool`
- `--ssh` forwards `/run/host-services/ssh-auth.sock` and `~/.ssh/known_hosts` when available
- `--version` prints the installed launcher version without requiring Docker or a git repo
- `--` passes the remaining arguments to the underlying CLI

Examples:

```bash
./dclaude --version
./dcodex -- --help
./dclaude --rebuild
./dcodex --reset
./dcodex --stop
./dcodex --update-tool
./dcodex --update-tool --yes
./dcodex --ssh
```

## Folder Mount Config

The launcher reads a single config file at `dclaude.yaml` in the `dclaude` repo root. If no config is present, the default read-only mounts stay `~/Desktop` and `~/Downloads`.

Config shape:

```yaml
mounts:
  - /Desktop
  - /Downloads
```

Each entry is relative to `$HOME` and is bind-mounted read-only at the same absolute path inside the container. `/Desktop` becomes `$HOME/Desktop`.

Use `mounts: []` when the launcher should not expose any extra host directories beyond the repo itself and the shared caches.

Configured mounts that overlap `~/.ssh` or `/run/host-services` are rejected. SSH access is only exposed through `--ssh`.

## Runtime Model

Every launch does the following:

- resolves the launcher repo from the wrapper script path
- resolves the target repo from your current shell location
- ensures a warm per-tool container exists for that target repo
- bind-mounts the launcher bootstrap script into the container so launcher script changes take effect on warm-container recreation
- bind-mounts the repo read/write at its real host path
- bind-mounts configured read-only directories at the same path, defaulting to `~/Desktop` and `~/Downloads`
- bind-mounts a persistent container-specific `cx` cache
- runs as the current host UID/GID
- sets `HOME` to the host home path
- `docker exec`s into the current host working directory inside the warm container
- creates `/workspace` as a compatibility alias that resolves back to the repo root

This means:

- `pwd` inside the container matches the target repo path on the host
- the Docker build context comes from the launcher repo, not the target repo
- the default fast path is warm-container reuse, not a fresh `docker run --rm`

## `cx` Integration

The shared image includes [ind-igo/cx](https://github.com/ind-igo/cx) and install it into your claude/codex skills, so your agent automatically uses `cx`.

Why it exists: Agents burn a ton of tokens on reads. `cx` reduces that [by ~60%](https://github.com/ind-igo/cx?tab=readme-ov-file#why).

Persistent `cx` cache:
- host path: `~/.cache/dclaude/cx`
- container path: `~/.cache/cx`

Agent SKILL.md integration:

- if `~/.claude/CX.md` is missing, the launcher writes it from `cx skill`
- if `~/.claude/CLAUDE.md` is missing, the launcher creates it with `@CX.md`
- if `~/.claude/CLAUDE.md` exists but does not reference `@CX.md` or already contain `cx` guidance, the launcher appends `@CX.md`
- if `~/.codex/AGENTS.md` is missing `cx` guidance, the launcher writes or appends it from `cx skill`
- if `~/.codex/skills/dclaude-cx-navigation` is missing, the launcher seeds it from the repo template

## Auth Persistence

Auth state is **mounted** from the host (not copied).

Every bind mount is listed below. If the access column says `read/write`, edits made inside the container modify the real host path and persist after the container stops.

| Host path | Container path | Scope | Access in container | Host persistence | Purpose |
| --- | --- | --- | --- | --- | --- |
| target repo root | same absolute path | all launches | read/write | yes | live working tree |
| `scripts/container-launch.sh` in this repo | `/usr/local/bin/dclaude-container-launch` | all launches | read-only | no | bootstrap entrypoint |
| `~/.cache/dclaude/cx` | `~/.cache/cx` | all launches | read/write | yes | `cx` cache |
| `~/.cache/pip` | `~/.cache/pip` | all launches | read/write | yes | pip cache |
| `~/.cache/uv` | `~/.cache/uv` | all launches | read/write | yes | uv cache |
| configured home mounts such as `~/Desktop` and `~/Downloads` | same absolute path | all launches | read-only | no | extra host files exposed to the container |
| `~/.claude` | `~/.claude` | Claude only | read/write | yes | Claude auth and state |
| `~/.claude.json` | `~/.claude.json` | Claude only | read/write | yes | Claude auth file |
| `~/.config/claude-code` | `~/.config/claude-code` | Claude only | read/write | yes | Claude CLI config |
| `~/.codex` | `~/.codex` | Codex only | read/write | yes | Codex auth, state, and skills |
| `skills/cx-navigation` in this repo | `/opt/dclaude/skills/cx-navigation` | Codex only when present | read-only | no | bundled skill template copied into `~/.codex/skills/dclaude-cx-navigation` if missing |
| `/run/host-services/ssh-auth.sock` | same absolute path | only with `--ssh` | SSH agent socket passthrough | host agent is used directly | lets container processes authenticate through the host agent without copying keys |
| `~/.ssh/known_hosts` | `~/.ssh/known_hosts` | only with `--ssh` when present | read-only | no | host key verification |

Anything not listed in the table above is not bind-mounted from the host. Writes to those paths stay container-local and disappear when the warm container is removed or reset.

The launcher may also seed missing guidance into the mounted home directories:

- `~/.claude/CX.md`
- `~/.claude/CLAUDE.md`
- `~/.codex/AGENTS.md`
- `~/.codex/skills/dclaude-cx-navigation`

Interactive login happens through the official CLIs inside the container. If you are not logged in yet, run the wrapper and complete the normal login flow there. The mounted state keeps you logged in across container restarts.

API-key auth is intentionally NOT supported for both tools.

## Optional GitHub SSH Access

`--ssh` is the only supported SSH integration path. It enables forwarded SSH agent access without copying raw private keys into the image.

When enabled, the wrappers mount:

- `/run/host-services/ssh-auth.sock`
- `~/.ssh/known_hosts` read-only when present

They do not mount the full `~/.ssh` directory and they never copy private key files into the container. Configured read-only mounts that overlap `~/.ssh` or `/run/host-services` are rejected.

Recommended host checks before using `--ssh`:

```bash
ssh-add -L
ssh -T git@github.com
```

Recommended container checks after launching with `--ssh`:

```bash
echo "$SSH_AUTH_SOCK"
ssh-add -L
ssh -T git@github.com
```

Use a dedicated GitHub key loaded into a dedicated `ssh-agent` if you want a narrower blast radius.

## Rebuilds

By default the wrappers build and reuse `dclaude:<version>`, where `<version>` comes from the repo `VERSION` file. `DCLAUDE_IMAGE_NAME` still overrides that default when you need a custom tag. `--rebuild` also recreates the warm container so the new image is actually used.

Rebuild explicitly when you want updated pinned tool versions or image changes for the current release tag:

```bash
docker build -t "dclaude:$(cat VERSION)" .
./dclaude --rebuild
```

The image currently pins the installed CLI versions:

- `@anthropic-ai/claude-code@2.1.89`
- `@openai/codex@0.118.0`
- `cx 0.6.0`

The Codex full-access launcher was validated against `codex-cli 0.118.0`, which supports `--dangerously-bypass-approvals-and-sandbox`.

## Tool Pin Refreshes

Pinned upstream tool versions live in `Dockerfile`, which controls the globally installed CLIs inside the image. Check or refresh them manually with:

```bash
python3 scripts/sync_tool_versions.py
python3 scripts/sync_tool_versions.py --update
```

Wrapper-driven refresh:

```bash
dcodex --update-tool
dclaude --update-tool
```

`--update-tool` checks the latest upstream version for the wrapper's own CLI, shows a confirmation prompt, rewrites the launcher pin and docs, rebuilds the image, and lets warm containers recreate automatically on their next launch. Use `--yes` to skip the confirmation prompt for scripted or non-interactive runs.

A scheduled GitHub Actions workflow runs the same updater, validates that the image still builds, and opens a PR when upstream `@anthropic-ai/claude-code`, `@openai/codex`, or `cx` releases move.

## Releases

Release shape:

- `VERSION` is the source of truth for the launcher version
- CI on pull requests and `main` runs `shellcheck`, `bash -n`, `docker build`, `--help`, and `--version`
- a scheduled tool-update workflow refreshes pinned upstream tool versions and opens a PR when updates are available
- a successful non-bot push to `main` bumps the patch version, commits `chore: release vX.Y.Z`, and pushes the matching `vX.Y.Z` tag
- the tag workflow creates `dclaude-vX.Y.Z.tar.gz`, publishes the GitHub Release, and updates `stanislavkozlovski/homebrew-tap` when `HOMEBREW_TAP_TOKEN` is configured

Homebrew installs both wrappers from the same release tarball and exposes both `dclaude` and `dcodex`.

## Security Boundary

This setup is for trusted repos. Anything reachable inside the container is reachable by the agent.

Deliberately omitted:

- `docker.sock`
- `--privileged`
- full home-directory mounts
- configured mounts that overlap `~/.ssh` or `/run/host-services`
- copied SSH key files
- API-key auth shortcuts

The Docker build context also ignores any repo-local `.ssh` directory.

Verify the Desktop and Downloads mounts are read-only with:

```bash
touch ~/Desktop/test
touch ~/Downloads/test
```

Both commands should fail inside the container with the default config. If you override the folder config, test the paths listed in `dclaude.yaml` instead. Writing in the repo should still succeed.
