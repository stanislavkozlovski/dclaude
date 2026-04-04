# Plan: Add `cx` To The Docker Workflow

## Goal

Make `cx` available inside the `dclaude` and `dcodex` containers so the agents can do semantic code navigation with lower token usage than naive full-file reads.

Target outcome:

- `cx` is installed in the shared image
- `cx` works against the target repo mounted into the container
- `cx` indexes and grammars persist across runs
- Claude and Codex are both nudged to prefer `cx` before broad file reads
- the repo docs explain how it works and what is persisted

## What We Know

From the host machine:

- `cx` exists at `/opt/homebrew/bin/cx`
- installed version is `0.6.0`
- `cx skill` already emits usable agent guidance
- installed grammars on the host are:
  - `typescript`
  - `python`
  - `bash`
- host cache path is macOS-specific:
  - `~/Library/Caches/cx/grammars`
  - `~/Library/Caches/cx/indexes`

Important implication:

- do not mirror the host macOS cache path directly into the Linux container
- give the container its own Linux-friendly persisted cache path

## Design Decisions

### 1. `cx` belongs in the shared image

Reason:

- both `dclaude` and `dcodex` should have it
- it is core repo-navigation tooling, not per-project app code
- installing it at image build time avoids per-run bootstrap noise

### 2. Container `cx` state should be persisted separately from the host macOS cache

Reason:

- host `cx` currently uses `~/Library/Caches/cx`, which is macOS-specific
- container `cx` should use a Linux path
- mixing host-native and container-native cache layouts is fragile and unnecessary

Proposed shape:

- host source of truth for container cache:
  - `~/.cache/dclaude/cx`
- mount into container at:
  - `~/.cache/cx`

This keeps persistence stable without coupling to macOS internals.

### 3. `cx` guidance should be explicit for both agents

Reason:

- simply installing the binary is not enough
- the agent needs a clear preference order: `cx overview` / `symbols` / `definition` / `references` before broad reads

### 4. Reuse `cx skill` instead of inventing a second guidance document

Reason:

- the built-in guidance is already good
- it keeps the repo aligned with the tool itself
- if we want a repo-specific wrapper around that guidance, it should be thin

## Implementation Plan

### Phase 1. Decide the Linux install route for `cx`

Need to answer:

- what is the canonical Linux installation path for `cx 0.6.0`
- can we install from an official release binary
- if not, do we build from source during image build

Preferred order:

1. official Linux binary or release artifact
2. official package manager route
3. source build pinned to `0.6.0`

Success criteria:

- install path is reproducible in `Dockerfile`
- version is pinned, not floating

### Phase 2. Add `cx` to the shared image

Update [Dockerfile](/Users/stanislavkozlovski/code/dclaude/Dockerfile):

- install `cx`
- ensure it is on `PATH`
- optionally preinstall a baseline grammar set:
  - `typescript`
  - `python`
  - `bash`

Open question:

- whether grammar install is cheap and stable enough to do at image build time
- if not, defer grammar materialization to first run with a persisted cache

Success criteria:

- `cx --version` works in the container
- `cx lang list` works in the container

### Phase 3. Add persisted `cx` cache mounts

Update [scripts/agent-common.sh](/Users/stanislavkozlovski/code/dclaude/scripts/agent-common.sh):

- create a host cache directory for container `cx`
- mount it into the container

Recommended host path:

- `~/.cache/dclaude/cx`

Recommended container path:

- `~/.cache/cx`

Also update [docker-compose.yml](/Users/stanislavkozlovski/code/dclaude/docker-compose.yml) to mirror that mount.

If `cx` honors XDG cache environment:

- set `XDG_CACHE_HOME=$HOST_HOME/.cache`

If `cx` needs a dedicated override env var:

- set that explicitly once verified

Success criteria:

- container-created `cx` indexes survive container restarts
- container-created grammars survive container restarts

### Phase 4. Add Claude guidance

Current state:

- `~/.claude/CX.md` already exists on the host
- `~/.claude` is already mounted into the container

Decision:

- first verify whether Claude Code auto-discovers that file in the mounted home inside the container
- if yes, do not duplicate the guidance in this repo
- if no, add a repo-managed Claude guidance file and document it

Preferred approach:

- rely on the mounted `~/.claude/CX.md` if discovery works

Fallback:

- copy or generate a repo-level guidance file only if Claude does not reliably pick up the mounted one

Success criteria:

- Claude has visible guidance telling it to prefer `cx` over broad reads

### Phase 5. Add Codex guidance

Codex is different:

- it will not automatically inherit Claude’s home-file conventions
- this repo should provide an explicit Codex-compatible skill or instruction source

Preferred approach:

- create a repo skill that is derived from `cx skill`
- keep it thin and tool-specific

Likely file shape:

- a repo skill under a dedicated skill directory
- the skill body should mostly mirror `cx skill`
- any repo-specific additions should be minimal

If you want to help here, this is the right collaboration point:

- you can shape how opinionated the skill should be
- we can decide whether it should be global, repo-local, or both

Success criteria:

- Codex receives stable guidance to use `cx overview`, `symbols`, `definition`, and `references` first

### Phase 6. Decide whether the wrappers should self-check `cx`

Options:

1. no wrapper check
2. soft banner
3. hard failure if `cx` is missing

Recommendation:

- soft check only

Reason:

- `dclaude` and `dcodex` should still work if `cx` is temporarily unavailable
- but a startup banner like `cx available` / `cx missing` is useful for debugging

Success criteria:

- the startup path makes it obvious whether `cx` is present in the container

### Phase 7. Document it

Update:

- [README.md](/Users/stanislavkozlovski/code/dclaude/README.md)
- [ARCHITECTURE.md](/Users/stanislavkozlovski/code/dclaude/ARCHITECTURE.md)

The docs should cover:

- that `cx` is included in the image
- what problem it solves
- where its cache and grammars persist
- whether baseline grammars are baked into the image or warmed on first use
- how Claude gets its guidance
- how Codex gets its guidance

If the `cx` cache mount introduces new persistent state, keep `ARCHITECTURE.md` aligned.

## Validation Plan

### 1. Binary presence

Inside both wrappers:

- `cx --version`
- `which cx`

Expected:

- `cx 0.6.0` or the pinned chosen version

### 2. Grammar availability

Inside the container:

- `cx lang list`

Expected minimum:

- `typescript`
- `python`
- `bash`

### 3. Cache persistence

Run in a target repo twice:

- `cx overview some/file`
- `cx cache path`

Expected:

- cache path resolves into the mounted persistent location
- second run reuses the index instead of rebuilding from scratch

### 4. Cross-repo launch model still holds

From repo `A`, with launcher in repo `B`:

- `cd A/repo`
- `dcodex`

Expected:

- `cx` runs against `A/repo`
- image/build assets still come from `B/dclaude`

### 5. Agent guidance actually lands

For Claude:

- inspect whether the mounted `CX.md` is visible and used

For Codex:

- confirm the repo skill is discoverable

Expected:

- both agents know to prefer `cx` before broad reads

## Risks

### 1. Linux install path for `cx` may be less clean than the Homebrew path

Response:

- verify the official Linux install route before editing the image

### 2. Grammar bootstrap may slow down image builds

Response:

- if needed, persist grammars in the mounted cache and install lazily

### 3. Agent guidance may drift from the tool

Response:

- derive from `cx skill`
- avoid writing a second, divergent instruction set by hand

### 4. Extra cache mounts add state surface area

Response:

- keep it narrow
- document it
- avoid mounting broad host directories unnecessarily

## Files Likely To Change

- [Dockerfile](/Users/stanislavkozlovski/code/dclaude/Dockerfile)
- [scripts/agent-common.sh](/Users/stanislavkozlovski/code/dclaude/scripts/agent-common.sh)
- [docker-compose.yml](/Users/stanislavkozlovski/code/dclaude/docker-compose.yml)
- [README.md](/Users/stanislavkozlovski/code/dclaude/README.md)
- [ARCHITECTURE.md](/Users/stanislavkozlovski/code/dclaude/ARCHITECTURE.md)
- a new repo-local Codex skill file or skill directory

## Recommendation

Do this in two commits:

1. runtime support
   - install `cx`
   - mount persistent cache
   - verify container behavior

2. agent guidance
   - wire Claude/Codex instructions
   - document the workflow

That split keeps runtime failures separate from prompt/skill changes.
