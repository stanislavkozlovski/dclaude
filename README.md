<div align="center">

# dclaude

Run Claude Code and Codex inside Docker against your real repo, not a copied workspace.

No fake paths. No re-login tax every session. `cx` included.

Live bind mounts. Real absolute paths. Persistent auth. Optional SSH.

<br>

| QUICKSTART |
| --- |
| [Why This Exists](#why-this-exists) |
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

## Why This Exists

`dclaude` and `dcodex` are thin Docker launchers around the official Claude Code and Codex CLIs.

Most Docker wrappers for coding agents break the thing that matters most: paths.

This one is optimized for path fidelity and operational convenience:

- the agent works on the live bind-mounted working tree
- the container sees the same absolute repo path you see on the host
- configured home folders such as `~/Desktop` and `~/Downloads` stay available at their real host paths
- auth and caches persist across runs instead of resetting every session

The launcher repo and the repo you want to edit are allowed to be different:

- the launcher repo provides `dclaude`, `dcodex`, the `Dockerfile`, and the shared helper scripts
- the target repo is the git repo from your current shell location
- the container always operates on that target repo, not on the launcher repo

That path fidelity is the whole point:

- when the agent writes `/Users/.../plan.md`, that is the real file on the host
- when you paste a configured path such as `~/Desktop/image.png`, that path is real inside the container
- `/workspace` exists only as a compatibility alias

## Requirements

- Docker Desktop or Docker Engine with `docker`
- a trusted repo
- existing configured home-mount directories, or the default `~/Desktop` and `~/Downloads` if no config file is present
- Docker Desktop file sharing enabled for the repo path and each configured home mount on macOS

## Homebrew Install

```bash
brew install stanislavkozlovski/tap/dclaude
```

## Quick Start

From the repo you want to edit, run:

```bash
cd /path/to/repo
dclaude
dcodex
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

The launcher reads a single config file at `dclaude.yaml` in the `dclaude` repo root. If no config is present, the default read-only home mounts stay `~/Desktop` and `~/Downloads`.

Config shape:

```yaml
mounts:
  - /Desktop
  - /Downloads
```

Each entry is relative to `$HOME` and is bind-mounted read-only at the same absolute path inside the container. `/Desktop` becomes `$HOME/Desktop`.

Use `mounts: []` when the launcher should not expose any extra home directories beyond the repo itself and the shared caches.

## Runtime Model

Every launch does the following:

- resolves the launcher repo from the wrapper script path
- resolves the target repo from your current shell location
- ensures a warm per-tool container exists for that target repo
- bind-mounts the launcher bootstrap script into the container so launcher script changes take effect on warm-container recreation
- bind-mounts the repo read/write at its real host path
- bind-mounts configured home folders read-only at the same path, defaulting to `~/Desktop` and `~/Downloads`
- bind-mounts a persistent container-specific `cx` cache
- runs as the current host UID/GID
- sets `HOME` to the host home path
- `docker exec`s into the current host working directory inside the warm container
- creates `/workspace` as a compatibility alias that resolves back to the repo root

This means:

- `pwd` inside the container matches the target repo path on the host, not `/workspace`
- the Docker build context comes from the launcher repo, not the target repo
- the default fast path is warm-container reuse, not a fresh `docker run --rm`

## `cx` Integration

The shared image now includes `cx 0.6.0`.

Why it exists:

- `cx overview` is cheaper than reading a full file
- `cx definition` gives the exact symbol body you need
- `cx references` is better than manually grepping through many files before a refactor

Container behavior:

- the image includes the `cx` binary
- on first run, the container bootstraps `bash`, `python`, and `typescript` grammars
- grammars and indexes persist across sessions

Persistent `cx` cache:

- host path: `~/.cache/dclaude/cx`
- container path: `~/.cache/cx`

Agent guidance:

- if `~/.claude/CX.md` is missing, the launcher writes it from `cx skill`
- if `~/.claude/CLAUDE.md` is missing, the launcher creates it with `@CX.md`
- if `~/.claude/CLAUDE.md` exists but does not reference `@CX.md` or already contain `cx` guidance, the launcher appends `@CX.md`
- if `~/.codex/AGENTS.md` is missing `cx` guidance, the launcher writes or appends it from `cx skill`
- if `~/.codex/skills/dclaude-cx-navigation` is missing, the launcher seeds it from the repo template

## Auth Persistence

Auth state is mounted from the host. It is not copied into the image or into container-local state.

Claude mounts:

- `~/.claude`
- `~/.claude.json`
- `~/.config/claude-code`

Codex mounts:

- `~/.codex`

Shared caches:

- `~/.cache/dclaude/cx` for container `cx`
- `~/.cache/pip`
- `~/.cache/uv`

The launcher may also seed missing guidance into the mounted home directories:

- `~/.claude/CX.md`
- `~/.claude/CLAUDE.md`
- `~/.codex/AGENTS.md`
- `~/.codex/skills/dclaude-cx-navigation`

Interactive login happens through the official CLIs inside the container. If you are not logged in yet, run the wrapper and complete the normal login flow there. The mounted state keeps you logged in across container restarts.

API-key auth is intentionally unsupported for both tools. This repo does not provide `.env`-driven auth, `ANTHROPIC_API_KEY`, or `OPENAI_API_KEY` workflow guidance.

## Optional GitHub SSH Access

`--ssh` enables forwarded SSH agent access without copying raw private keys into the image.

When enabled, the wrappers mount:

- `/run/host-services/ssh-auth.sock`
- `~/.ssh/known_hosts` read-only when present

They do not mount the full `~/.ssh` directory and they never copy private key files into the container.

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
- full `~/.ssh` mounts
- copied SSH key files
- API-key auth shortcuts

Verify the Desktop and Downloads mounts are read-only with:

```bash
touch ~/Desktop/test
touch ~/Downloads/test
```

Both commands should fail inside the container with the default config. If you override the folder config, test the paths listed in `dclaude.yaml` instead. Writing in the repo should still succeed.
