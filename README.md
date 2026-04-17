<div align="center">

# dclaude

Run Claude Code or Codex with full autonomous permissions inside a Docker container.

Includes `cx` for lower token usage.

<img src="./docs/logo.jpg" alt="dclaude logo" width="300" />

<br>

| Action | Link |
| --- | --- |
| What is this? | [The What](#the-what) |
| Install Guide | [Install](#install) |
| Run & Config Guide | [How to Run](#how-to-run) |
| Security Overview | [Security](#security) |
| Miscellaneous | [Miscellaneous](#miscellaneous) |

<br>

<p align="center">
  <i>“Ever have a 1-minute task take you 10+ minutes because it's stuck waiting on you approving multiple chained commands?”</i>
  <br />
  <sub>— I have, many times.</sub>
</p>

</div>


```bash
$ cd $WORK_FOLDER
$ dclaude
$ dcodex

# Claude Code or Codex with full permissions;
# No yes/no time wasting.
# Limited. Can read only the current folder and optional whitelisted ones.
```

# The What

## The Problem

<p align="center">
  <i>“Ever have a 1-minute task take you 10+ minutes because it's stuck waiting on you approving multiple chained commands?”</i>
  <br />
  <sub>— I have, many times.</sub>
</p>

Modern coding harness usage requires many tool calls. They all usually require explicit approval from the user which takes time, breaks your flow and requires more multitasking brainpower than simply letting it YOLO.

But letting it YOLO is dangerous. You're allowing an autonomous system with full access to your computer. It's a security nightmare waiting to happen. Ultimately there's a spectrum where on one side sits max productivity and on the other max security. Here are a few of the ways you can go about this problem:

### 1. permission-mode auto, or whitelisted permissions

I tried this, but the harness still finds something stupid to block on.

<p align="center">
  <img src="./docs/motivation_1.png" alt="Permission mode auto still blocks commands" width="700" />
  <img src="./docs/motivation_2.png" alt="Repetitive approval prompts break flow" width="700" />
</p>

It also isn't fully secure either.

<p align="center">
  <img src="./docs/motivation_3.png" alt="Permission bypass discussion" width="700" />
</p>

### 2. run an isolated dev-only VM

Better and more secure, but too much work to set up, and is costly.

### 3. run inside a sandbox with a limited blast radius

That's what `dclaude` is here for! :)

## What This Is

This repo ships two simple, thin Docker wrappers:

- `dclaude` - a wrapper on top of Claude Code
- `dcodex` - a wrapper on top of OpenAI's Codex

When ran from a folder, `dclaude` runs the coding CLI inside a docker container with the folder bind-mounted. Any changes in the container get reflected in the same folder on your computer.

Some features:

- you can configure read-only folders, like `~/Desktop` or `~/Downloads`. The container can never write to them and can only read.
- paths are seamless. The container sees the same absolute repo path you see on your computer, so sending the CLI agent an image path like `~/Desktop/image.png` just works.
- auth and caches persist across runs.
- the Docker container comes pre-installed with Python and Node. For more languages, either fork this repo or open a PR.

# Install

## Requirements

- Docker Desktop or Docker Engine with `docker`
- a trusted repo
- Docker Desktop file sharing enabled for the repo path and each configured home mount on macOS

## Option A: Homebrew

```bash
brew install stanislavkozlovski/tap/dclaude
```

Optionally, configure read-only host directories the container can see:

```bash
DCLAUDE_HOME="$(dirname "$(readlink -f "$(which dclaude)")")"
cp "$DCLAUDE_HOME/scripts/dclaude.yaml.example" "$DCLAUDE_HOME/scripts/dclaude.yaml"
# edit dclaude.yaml to add your read-only mounts
```

## Option B: Git Clone

```bash
git clone https://github.com/stanislavkozlovski/dclaude.git ~/dclaude
```

Add to your shell profile (`~/.bashrc`, `~/.zshrc`, etc.):

```bash
alias dclaude='~/dclaude/dclaude'
alias dcodex='~/dclaude/dcodex'
```

Optionally, configure read-only host directories the container can see:

```bash
cp ~/dclaude/scripts/dclaude.yaml.example ~/dclaude/scripts/dclaude.yaml
# edit dclaude.yaml to add your read-only mounts
```

# How to Run

## Quick Start

The container gets **read/write** access to your current repo and **read-only** access to any directories you configure in `scripts/dclaude.yaml`. Nothing else from your host is exposed.

From the repo you want to edit, run:

```bash
cd /path/to/repo
dclaude # or dcodex
```

If you want GitHub SSH access from inside the container, for example to `git push` from there, launch with `--ssh`:

```bash
cd /path/to/repo
dclaude --ssh # or dcodex --ssh
```

If you commit in-container but push from the host yourself, you do not need `--ssh`.

If you prefer, calling the launcher by its full path also works as long as your current shell is already inside the target repo:

```bash
/path/to/dclaude-repo/dclaude
/path/to/dclaude-repo/dcodex
```

> [!NOTE]
> The very first `dcodex`/`dclaude` run can take around 60 seconds while Docker builds the image. Subsequent runs are instant.

Wrapper options:

- `--rebuild` forces a fresh `docker build` and recreates the warm container
- `--reset` recreates the warm container before launching
- `--stop` removes the warm container for the current repo and exits
- `--update-tool` checks the latest upstream version for this wrapper's CLI, rewrites the pin in the launcher repo, and rebuilds the image after confirmation
- `--yes` skips the confirmation prompt for `--update-tool`
- `--ssh` enables SSH agent forwarding by mounting `/run/host-services/ssh-auth.sock` and `~/.ssh/known_hosts` when available
- `--profile NAME` (Codex only) uses a named profile with a separate `~/.codex-NAME` config directory, giving you isolated auth, state, and skills per profile
- `--list-profiles` (Codex only) lists available Codex profiles
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
./dclaude --ssh
./dcodex --ssh
./dcodex --profile magi
./dcodex --profile magi --ssh
./dcodex --list-profiles
```

## Folder Mount Config

The launcher reads a single config file at `scripts/dclaude.yaml` in the `dclaude` repo. If no config is present, no extra host directories are mounted beyond the repo itself and shared caches.

To get started, copy the example and edit it:

```bash
cp scripts/dclaude.yaml.example scripts/dclaude.yaml
```

Config shape:

```yaml
read_only_mounts:
  - ~/Desktop
  - ~/Downloads
```

Each entry is an absolute path or starts with `~/` (resolved to `$HOME`). Entries are bind-mounted read-only at the same absolute path inside the container.

Use `read_only_mounts: []` to explicitly mount no extra directories.

Configured mounts that overlap `~/.ssh` or `/run/host-services` are rejected. SSH access is only exposed through `--ssh`.

## GitHub SSH Access

SSH is disabled by default.

Pass `--ssh` to enable GitHub SSH access inside the container.

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

If those host checks fail, `--ssh` inside the container will fail too.

Recommended container checks after launching with `--ssh`:

```bash
echo "$SSH_AUTH_SOCK"
ssh-add -L
ssh -T git@github.com
```

Use a dedicated GitHub key loaded into a dedicated `ssh-agent` if you want a narrower blast radius.

# Security

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

Verify configured read-only mounts by trying to write to them inside the container:

```bash
touch ~/Desktop/test   # should fail if ~/Desktop is in your dclaude.yaml
```

Writing in the repo should still succeed.

## Auth Persistence

Auth state is **mounted** from the host, not copied.

Every bind mount is listed below. If the access column says `read/write`, edits made inside the container modify the real host path and persist after the container stops.

| Host path | Container path | Scope | Access in container | Host persistence | Purpose |
| --- | --- | --- | --- | --- | --- |
| target repo root | same absolute path | all launches | read/write | yes | live working tree |
| `scripts/container-launch.sh` in this repo | `/usr/local/bin/dclaude-container-launch` | all launches | read-only | no | bootstrap entrypoint |
| `~/.cache/dclaude/cx` | `~/.cache/cx` | all launches | read/write | yes | `cx` cache |
| `~/.cache/pip` | `~/.cache/pip` | all launches | read/write | yes | pip cache |
| `~/.cache/uv` | `~/.cache/uv` | all launches | read/write | yes | uv cache |
| configured read-only mounts from `scripts/dclaude.yaml` | same absolute path | all launches | read-only | no | extra host files exposed to the container |
| `~/.claude` | `~/.claude` | Claude only | read/write | yes | Claude auth and state |
| `~/.claude.json` | `~/.claude.json` | Claude only | read/write | yes | Claude auth file |
| `~/.config/claude-code` | `~/.config/claude-code` | Claude only | read/write | yes | Claude CLI config |
| `~/.codex` (or `~/.codex-NAME` with `--profile`) | same path | Codex only | read/write | yes | Codex auth, state, and skills |
| `/run/host-services/ssh-auth.sock` | same absolute path | only with `--ssh` | SSH agent socket passthrough | host agent is used directly | lets container processes authenticate through the host agent without copying keys |
| `~/.ssh/known_hosts` | `~/.ssh/known_hosts` | only with `--ssh` when present | read-only | no | host key verification |

Anything not listed in the table above is not bind-mounted from the host. Writes to those paths stay container-local and disappear when the warm container is removed or reset.

The launcher may also seed missing guidance into the mounted home directories:

- `~/.claude/CX.md`
- `~/.claude/CLAUDE.md`
- `~/.codex/AGENTS.md`
- `~/.codex/skills/dclaude-cx-navigation`

When using `--profile NAME`, the profile directory `~/.codex-NAME` is used instead of `~/.codex`. On first use, `AGENTS.md` and skills are copied from the default `~/.codex` if they exist there.

Interactive login happens through the official CLIs inside the container. If you are not logged in yet, run the wrapper and complete the normal login flow there. The mounted state keeps you logged in across container restarts.

API-key auth is intentionally NOT supported for both tools.

# Miscellaneous

## Docs

Primary docs live under [`docs/`](docs/):

- [How To Run](docs/HOWRUN.md)
- [Architecture](docs/ARCHITECTURE.md)
- [Motivation](docs/motivation.md)
- [License](docs/LICENSE)
- [Version](docs/VERSION)
- [Sample config](scripts/dclaude.yaml.example)

## Runtime Model

Every launch does the following:

- resolves the launcher repo from the wrapper script path
- resolves the target repo from your current shell location
- ensures a warm per-tool container exists for that target repo
- bind-mounts the launcher bootstrap script into the container so launcher script changes take effect on warm-container recreation
- bind-mounts the repo read/write at its real host path
- bind-mounts configured read-only directories at the same path (none by default; see `scripts/dclaude.yaml.example`)
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

The shared image includes [ind-igo/cx](https://github.com/ind-igo/cx) and installs it into your claude/codex skills, so your agent automatically uses `cx`.

Why it exists: agents burn a ton of tokens on reads. `cx` reduces that [by ~60%](https://github.com/ind-igo/cx?tab=readme-ov-file#why).

Persistent `cx` cache:

- host path: `~/.cache/dclaude/cx`
- container path: `~/.cache/cx`

Agent `SKILL.md` integration:

- if `~/.claude/CX.md` is missing, the launcher writes it from `cx skill`
- if `~/.claude/CLAUDE.md` is missing, the launcher creates it with `@CX.md`
- if `~/.claude/CLAUDE.md` exists but does not reference `@CX.md` or already contain `cx` guidance, the launcher appends `@CX.md`
- if `~/.codex/AGENTS.md` is missing `cx` guidance, the launcher writes or appends it from `cx skill` (same for `~/.codex-NAME` with profiles)
- if `~/.codex/skills/dclaude-cx-navigation` is missing, the launcher seeds it from `cx skill` (same for profiles)

## Rebuilds

By default the wrappers build and reuse `dclaude:<version>`, where `<version>` comes from `docs/VERSION`. `DCLAUDE_IMAGE_NAME` still overrides that default when you need a custom tag. `--rebuild` also recreates the warm container so the new image is actually used.

Rebuild explicitly when you want updated pinned tool versions or image changes for the current release tag:

```bash
docker build -t "dclaude:$(cat docs/VERSION)" .
./dclaude --rebuild
```

The image currently pins the installed CLI versions:

- `@anthropic-ai/claude-code@2.1.92`
- `@openai/codex@0.120.0`
- `cx 0.6.4`

The Codex full-access launcher was validated against `codex-cli 0.120.0`, which supports `--dangerously-bypass-approvals-and-sandbox`.

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

- `docs/VERSION` is the source of truth for the launcher version
- CI on pull requests and `main` runs `shellcheck`, `bash -n`, `docker build`, `--help`, and `--version`
- a scheduled tool-update workflow refreshes pinned upstream tool versions and opens a PR when updates are available
- a successful non-bot push to `main` bumps the patch version, commits `chore: release vX.Y.Z`, and pushes the matching `vX.Y.Z` tag
- the tag workflow creates `dclaude-vX.Y.Z.tar.gz`, publishes the GitHub Release, and updates `stanislavkozlovski/homebrew-tap` when `HOMEBREW_TAP_TOKEN` is configured

Homebrew installs both wrappers from the same release tarball and exposes both `dclaude` and `dcodex`.
