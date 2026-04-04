# Claude Instructions

- When you create a Markdown file that the user is expected to open or edit, reply with exactly one line:

```text
zed /absolute/path/to/file.md
```

- Use the real absolute host path, not a relative path.
- Do not say `Created ...`, `I wrote ...`, or add any extra commentary around that line.
- If you create more than one Markdown file, output one `zed /absolute/path` line per file.
- For plans in this repo, prefer the repo-root `plan.md` unless the user asks for a different location.
