#!/usr/bin/env bash

set -euo pipefail

TARGET_REPO_ROOT="${TARGET_REPO_ROOT:?TARGET_REPO_ROOT must be set}"
CX_BOOTSTRAP_LANGUAGES="${CX_BOOTSTRAP_LANGUAGES:-bash python typescript}"
CX_GRAMMAR_DIR="${HOME}/.cache/cx/grammars"
CX_SKILL_TEMPLATE_DIR="/opt/dclaude/skills/cx-navigation"
BOOTSTRAP_ONLY=0
TOOL_NAME=""

parse_args() {
  if [ "${1:-}" = "--bootstrap-only" ]; then
    TOOL_NAME="${2:-}"
    [ -n "$TOOL_NAME" ] || {
      echo "error: --bootstrap-only requires a tool name" >&2
      exit 1
    }
    BOOTSTRAP_ONLY=1
    shift 2
  else
    TOOL_NAME="${1:-}"
  fi

  REMAINING_ARGS=("$@")
}

log() {
  echo "$*" >&2
}

parse_args "$@"

render_cx_guidance() {
  if command -v cx >/dev/null 2>&1; then
    if cx skill 2>/dev/null; then
      return 0
    fi
  fi

  cat <<'EOF'
# cx - Semantic Code Navigation

When `cx` is available in the project, prefer it over reading files directly.

## Escalation hierarchy: overview -> symbols -> definition / references -> read

- Understand a file's structure -> `cx overview <file>`
- Find symbols across the project -> `cx symbols [--kind K] [--name GLOB] [--file PATH]`
- Read a specific function or type -> `cx definition --name <name>`
- Find all usages of a symbol -> `cx references --name <name>` before refactoring or tracing impact
- Fall back to a full-file read only when you need surrounding context that `cx` does not provide

## Quick reference

```bash
cx overview PATH
cx symbols [--kind K] [--name GLOB] [--file PATH]
cx definition --name NAME [--from PATH] [--kind K]
cx references --name NAME [--file PATH]
cx lang list
cx lang add LANG [LANG...]
```

Short aliases: `cx o`, `cx s`, `cx d`, `cx r`
EOF
}

render_codex_cx_skill() {
  cat <<'EOF'
---
name: cx-navigation
description: Use when exploring, understanding, or refactoring code in a repo where the cx CLI is available. Prefer cx overview, symbols, definition, and references before reading whole files directly.
user_invocable: true
---

# /cx-navigation - Semantic Code Navigation

When `cx` is available, prefer it over broad file reads.

## Workflow

1. Start with `cx overview PATH` before opening a source file.
2. Use `cx symbols` to find candidate definitions across the repo.
3. Use `cx definition --name NAME [--from PATH]` to read the exact function, method, type, or constant you need.
4. Use `cx references --name NAME` before refactors or when tracing impact.
5. Fall back to a full-file read only when you need surrounding context that `cx` does not provide.

## Command Guide

```bash
cx overview PATH
cx symbols [--kind KIND] [--name GLOB] [--file PATH]
cx definition --name NAME [--from PATH] [--kind KIND]
cx references --name NAME [--file PATH]
cx lang list
cx lang add bash python typescript
```

Short aliases:

- `cx o`
- `cx s`
- `cx d`
- `cx r`

## Missing Grammars

If `cx` reports a missing grammar:

1. run `cx lang list`
2. install the missing grammar with `cx lang add <language>`
3. retry the semantic navigation command

The container bootstraps `bash`, `python`, and `typescript` grammars on first use and persists them across sessions.
EOF
}

file_has_cx_guidance() {
  local target_file="$1"

  [ -f "$target_file" ] || return 1

  grep -Fq '@CX.md' "$target_file" 2>/dev/null \
    || grep -Fq 'cx overview PATH' "$target_file" 2>/dev/null \
    || grep -Fq 'Semantic Code Navigation' "$target_file" 2>/dev/null
}

ensure_claude_cx_guidance() {
  local claude_dir="$HOME/.claude"
  local cx_file="$claude_dir/CX.md"
  local claude_file="$claude_dir/CLAUDE.md"

  mkdir -p "$claude_dir"

  if [ ! -s "$cx_file" ]; then
    render_cx_guidance > "$cx_file"
    log "Seeded $cx_file"
  fi

  if [ ! -e "$claude_file" ]; then
    printf '@CX.md\n' > "$claude_file"
    log "Created $claude_file with @CX.md"
  elif ! file_has_cx_guidance "$claude_file"; then
    printf '\n@CX.md\n' >> "$claude_file"
    log "Appended @CX.md to $claude_file"
  fi
}

ensure_codex_cx_guidance() {
  local codex_home="$HOME/.codex"
  local agents_file="$codex_home/AGENTS.md"
  local skill_dir="$codex_home/skills/dclaude-cx-navigation"

  mkdir -p "$codex_home/skills"

  if [ ! -s "$skill_dir/SKILL.md" ]; then
    mkdir -p "$skill_dir"
    if [ -f "$CX_SKILL_TEMPLATE_DIR/SKILL.md" ]; then
      cp -R "$CX_SKILL_TEMPLATE_DIR/." "$skill_dir/"
      log "Seeded $skill_dir from $CX_SKILL_TEMPLATE_DIR"
    else
      render_codex_cx_skill > "$skill_dir/SKILL.md"
      log "Seeded $skill_dir/SKILL.md from built-in fallback"
    fi
  fi

  if [ ! -s "$agents_file" ]; then
    render_cx_guidance > "$agents_file"
    log "Created $agents_file with cx guidance"
  elif ! file_has_cx_guidance "$agents_file"; then
    printf '\n' >> "$agents_file"
    render_cx_guidance >> "$agents_file"
    printf '\n' >> "$agents_file"
    log "Appended cx guidance to $agents_file"
  fi
}

ln -sfn "$TARGET_REPO_ROOT" /var/run/dclaude/workspace

if command -v cx >/dev/null 2>&1; then
  log "cx available: $(cx --version)"

  if [ -n "$CX_BOOTSTRAP_LANGUAGES" ] && { [ ! -d "$CX_GRAMMAR_DIR" ] || ! find "$CX_GRAMMAR_DIR" -mindepth 1 -maxdepth 1 -print -quit 2>/dev/null | grep -q .; }; then
    log "Bootstrapping cx grammars: $CX_BOOTSTRAP_LANGUAGES"
    # shellcheck disable=SC2086
    if ! cx lang add $CX_BOOTSTRAP_LANGUAGES >/dev/null 2>&1; then
      log "warning: cx grammar bootstrap failed; cx will install grammars on demand"
    fi
  fi
else
  log "warning: cx not available in container"
fi

case "$TOOL_NAME" in
  claude)
    ensure_claude_cx_guidance
    ;;
  codex)
    ensure_codex_cx_guidance
    ;;
esac

if [ "$BOOTSTRAP_ONLY" -eq 1 ] || [ "${#REMAINING_ARGS[@]}" -eq 0 ]; then
  exit 0
fi

exec "${REMAINING_ARGS[@]}"
