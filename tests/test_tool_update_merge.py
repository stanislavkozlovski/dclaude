import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]
REPOSITORY = "stanislavkozlovski/dclaude"
HEAD_SHA = "a" * 40


class ToolUpdateMergeTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.directory = Path(temp.name)
        self.log = self.directory / "calls.jsonl"
        gh = self.directory / "gh"
        gh.write_text("""#!/usr/bin/env python3
import json
import os
import sys
args = sys.argv[1:]
with open(os.environ['GH_TEST_LOG'], 'a') as log:
    log.write(json.dumps(args) + '\\n')
if args[:2] == ['pr', 'list']:
    print(os.environ['GH_TEST_PRS'])
elif args[0] == 'api':
    print(os.environ['GH_TEST_PR'])
elif args[:2] == ['pr', 'merge']:
    sys.exit(int(os.environ.get('GH_TEST_MERGE_STATUS', '0')))
elif args[:2] == ['workflow', 'run']:
    sys.exit(int(os.environ.get('GH_TEST_DISPATCH_STATUS', '0')))
else:
    sys.exit(99)
""")
        gh.chmod(0o755)
        self.pr = {
            "state": "open", "draft": False, "title": "chore: codex 1.0->1.1",
            "head": {"ref": "chore/tool-version-updates", "sha": HEAD_SHA,
                     "repo": {"full_name": REPOSITORY}},
            "base": {"ref": "main", "repo": {"full_name": REPOSITORY}},
        }
        self.env = {
            **os.environ,
            "PATH": f"{self.directory}:{os.environ['PATH']}",
            "GH_TEST_LOG": str(self.log),
            "GH_TEST_PRS": json.dumps([{"number": 104}]),
            "GITHUB_REPOSITORY": REPOSITORY, "BASE_BRANCH": "main",
            "HEAD_REPOSITORY": REPOSITORY, "HEAD_SHA": HEAD_SHA,
        }

    def run_merge(self):
        self.env["GH_TEST_PR"] = json.dumps(self.pr)
        return subprocess.run(
            ["bash", str(ROOT / "scripts/merge_tool_update.sh")],
            env=self.env, capture_output=True, text=True,
        )

    def calls(self):
        if not self.log.exists():
            return []
        return [json.loads(line) for line in self.log.read_text().splitlines()]

    def assert_no_writes(self):
        self.assertFalse(any(args[:2] in (["pr", "merge"], ["workflow", "run"])
                             for args in self.calls()))

    def test_dispatched_ci_resolves_pr_and_releases_after_matching_merge(self):
        result = self.run_merge()
        self.assertEqual(result.returncode, 0, result.stderr)
        calls = self.calls()
        self.assertEqual(calls[0], [
            "pr", "list", "--repo", REPOSITORY, "--base", "main", "--head",
            "chore/tool-version-updates", "--state", "open", "--limit", "2", "--json", "number",
        ])
        self.assertEqual(calls[1], ["api", f"repos/{REPOSITORY}/pulls/104"])
        self.assertEqual(calls[2], [
            "pr", "merge", "104", "--repo", REPOSITORY, "--squash", "--delete-branch",
            "--match-head-commit", HEAD_SHA,
        ])
        self.assertEqual(calls[3], ["workflow", "run", "ci.yml", "--repo", REPOSITORY, "--ref", "main"])

    def test_unsafe_or_stale_pr_never_merges(self):
        cases = [
            ("state", "closed"), ("draft", True), ("title", "feat: unrelated change"),
            ("head.ref", "other-branch"), ("head.sha", "b" * 40),
            ("head.repo.full_name", "someone/dclaude"),
            ("base.ref", "other-base"), ("base.repo.full_name", "someone/dclaude"),
        ]
        for path, value in cases:
            with self.subTest(path=path):
                self.log.unlink(missing_ok=True)
                target = self.pr
                keys = path.split(".")
                for key in keys[:-1]:
                    target = target[key]
                previous = target[keys[-1]]
                target[keys[-1]] = value
                self.assertNotEqual(self.run_merge().returncode, 0)
                self.assert_no_writes()
                target[keys[-1]] = previous

    def test_fork_ci_is_rejected_before_reading_pr(self):
        self.env["HEAD_REPOSITORY"] = "someone/dclaude"
        self.assertNotEqual(self.run_merge().returncode, 0)
        self.assertEqual(self.calls(), [])

    def test_missing_or_ambiguous_pr_never_merges(self):
        for prs in ([], [{"number": 104}, {"number": 105}]):
            with self.subTest(prs=prs):
                self.log.unlink(missing_ok=True)
                self.env["GH_TEST_PRS"] = json.dumps(prs)
                self.assertNotEqual(self.run_merge().returncode, 0)
                self.assert_no_writes()

    def test_merge_race_does_not_dispatch_release(self):
        self.env["GH_TEST_MERGE_STATUS"] = "17"
        self.assertEqual(self.run_merge().returncode, 17)
        self.assertFalse(any(args[:2] == ["workflow", "run"] for args in self.calls()))

    def test_failed_release_dispatch_is_reported(self):
        self.env["GH_TEST_DISPATCH_STATUS"] = "19"
        self.assertEqual(self.run_merge().returncode, 19)


if __name__ == "__main__":
    unittest.main()
