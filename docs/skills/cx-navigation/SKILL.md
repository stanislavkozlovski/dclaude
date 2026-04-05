---
name: cx-navigation
description: Use when exploring, understanding, or refactoring code in a repo where the cx CLI is available. Prefer cx overview, symbols, definition, and references before reading whole files directly.
user_invocable: true
---

# /cx-navigation — Semantic Code Navigation

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

## Use Cases

- Before reading a large file, start with `cx overview`.
- Before editing a specific symbol, use `cx definition` to avoid loading unrelated code.
- Before renaming or refactoring, use `cx references` first.
- When you are not sure where something lives, use `cx symbols` before grepping and opening files.

## Missing Grammars

If `cx` reports a missing grammar:

1. run `cx lang list`
2. install the missing grammar with `cx lang add <language>`
3. retry the semantic navigation command

The container bootstraps `bash`, `python`, and `typescript` grammars on first use and persists them across sessions.
