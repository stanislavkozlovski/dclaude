```bash
brew tap stanislavkozlovski/tap
brew install stanislavkozlovski/tap/dclaude
cd /path/to/the-folder-you-wanna-run-claude-on-top-of
dclaude
dcodex
dclaude --ssh
dcodex --ssh
dclaude --check-update
dclaude --update-launcher
dcodex --profile magi        # use a named Codex profile (~/.codex-magi)
dcodex --list-profiles        # list available Codex profiles
```

Old launcher images and the build cache they held are retired automatically after
each image build and at most once a day on launch. Storage commands run in the
host terminal from any directory. They require host Python 3; cleanup supports
local Docker Desktop on macOS.

```bash
dclaude --space                         # diagnose and preview images; no deletion
dclaude --space images --keep 2 --apply  # review and confirm old image deletion
dclaude --space cache                   # fresh cache preview after image cleanup
dclaude --space cache --apply           # separately confirm builder-wide cache cleanup
dclaude --space verify                  # remeasure the latest cleanup
dclaude --space retention status        # automatic retention is on by default (keeps 2 builds)
dclaude --space retention enable --keep 3
dclaude --space retention disable
dclaude --space --json                  # structured read-only report
dclaude --space --help
```

`dcodex --space` works identically. See [Recover Docker space on a Mac](SPACE.md)
for image protections, `--disk-image`, receipt recovery, cache budgets, and support
limits. Deletion requires its own interactive confirmation; `--yes` remains an
update option.
