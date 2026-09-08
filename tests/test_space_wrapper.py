"""Exercise the shared Bash entry point without a live Docker daemon."""

import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]
IMAGE_ID = "sha256:" + "a" * 64


class SpaceWrapperTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.directory = Path(self.temp.name)
        self.home = self.directory / "home"
        self.home.mkdir()
        self.tool_home = self.directory / "launcher"
        (self.tool_home / "scripts").mkdir(parents=True)
        (self.tool_home / "docs").mkdir()
        for name in ("dclaude", "dcodex", "scripts/agent-common.sh", "scripts/space-lock.sh"):
            shutil.copy2(ROOT / name, self.tool_home / name)
        (self.tool_home / "docs/VERSION").write_text("1.2.3\n")
        self.bin = self.directory / "bin"
        self.bin.mkdir()
        self.log = self.directory / "calls"
        self.state = self.home / ".local/state/dclaude/space"
        self.env = os.environ.copy()
        for key in ("DCLAUDE_IMAGE_NAME", "DCLAUDE_VERSION", "BUILDX_BUILDER", "DOCKER_BUILDKIT"):
            self.env.pop(key, None)
        self.env.update(
            HOME=str(self.home),
            HOST_HOME=str(self.home),
            TOOL_HOME=str(self.tool_home),
            PATH=f"{self.bin}:{os.environ['PATH']}",
            TEST_LOG=str(self.log),
            TEST_IMAGE_ID=IMAGE_ID,
        )
        self.executable("python3", """#!/bin/bash
printf 'python' >> "$TEST_LOG"
printf ' <%s>' "$@" >> "$TEST_LOG"
printf '\\n' >> "$TEST_LOG"
if [ -d "$HOME/.local/state/dclaude/space/operation.lock" ]; then
  echo 'python-saw-lock' >> "$TEST_LOG"
fi
exit "${PYTHON_EXIT:-0}"
""")
        self.executable("docker", """#!/bin/bash
printf 'docker' >> "$TEST_LOG"
printf ' <%s>' "$@" >> "$TEST_LOG"
printf '\\n' >> "$TEST_LOG"
if [ "$1" = build ]; then
  [ -d "$HOME/.local/state/dclaude/space/operation.lock" ] || exit 91
  exit "${BUILD_EXIT:-0}"
fi
if [ "$1" = image ] && [ "$2" = inspect ]; then
  [ "${IMAGE_EXISTS:-1}" = 1 ] || exit 1
  printf '%s\\n' "$TEST_IMAGE_ID"
fi
""")

    def executable(self, name, contents):
        path = self.bin / name
        path.write_text(contents)
        path.chmod(0o755)

    def calls(self):
        return self.log.read_text() if self.log.exists() else ""

    def run_wrapper(self, *args, wrapper="dclaude"):
        return subprocess.run(
            [str(self.tool_home / wrapper), *args],
            cwd=self.directory, env=self.env, text=True, capture_output=True,
        )

    def run_shell(self, body, *args):
        return subprocess.run(
            ["bash", "-c", 'source "$TOOL_HOME/scripts/agent-common.sh"\n' + body, "test", *args],
            cwd=self.directory, env=self.env, text=True, capture_output=True,
        )

    def run_launch(self, *args):
        # Keep the real dispatcher, launch sequencing, build, and retention.
        # Stub the existing repo/auth/container machinery at its boundaries.
        return self.run_shell("""
ensure_target_repo() { TARGET_REPO_ROOT="$PWD"; TARGET_CWD="$PWD"; echo target >> "$TEST_LOG"; }
maybe_prompt_launcher_update() { echo update-check >> "$TEST_LOG"; }
ensure_docker() { echo ensure-docker >> "$TEST_LOG"; }
load_configured_home_mounts() { :; }
ensure_required_paths() { :; }
ensure_host_state() { :; }
ensure_warm_container() {
  [ -d "$SPACE_STATE_DIR/operation.lock" ] || return 92
  echo bootstrap >> "$TEST_LOG"
  WARM_CONTAINER_NAME=test-warm
  if [ "${FAIL_RETENTION_INSPECT:-0}" = 1 ]; then export IMAGE_EXISTS=0; fi
  return "${BOOTSTRAP_EXIT:-0}"
}
append_launch_command() { LAUNCH_CMD=(agent "${TOOL_ARGS[@]}"); }
emit_startup_banner() { :; }
launch_agent claude "$@"
""", *args)

    def enable_retention(self, enabled=True, image_id=IMAGE_ID, *, pending=True):
        self.state.mkdir(parents=True, exist_ok=True)
        self.state.joinpath("policy.json").write_text(
            '{"schema_version": 1, "enabled": ' + str(enabled).lower() + ', "keep": 2}\n'
        )
        if pending:
            self.state.joinpath("pending-build").write_text(image_id + "\n")
        else:
            self.state.joinpath("pending-build").unlink(missing_ok=True)

    def test_build_without_saved_policy_does_not_invoke_retention(self):
        result = self.run_launch("--rebuild")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertNotIn("python", self.calls())
        self.assertFalse((self.state / "policy.json").exists())
        self.assertTrue((self.state / "pending-build").exists())
        self.assertEqual({path.name for path in self.state.iterdir()}, {"pending-build"})

    def test_disabled_policy_skips_build_retention_without_python(self):
        self.env["PYTHON_EXIT"] = "98"
        self.enable_retention(enabled=False, pending=False)
        for args, expected_state in (((), {"policy.json"}),
                                     (("--rebuild",), {"policy.json", "pending-build"})):
            with self.subTest(args=args):
                result = self.run_launch(*args)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertNotIn("python", self.calls())
                self.assertEqual({path.name for path in self.state.iterdir()}, expected_state)

    def test_enabled_retention_failure_keeps_the_launch(self):
        self.enable_retention()
        self.env["PYTHON_EXIT"] = "2"
        result = self.run_launch()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("automatic image retention did not complete", result.stderr)
        self.assertIn("dclaude --space retention status", result.stderr)
        self.assertIn("<agent>", self.calls())
        self.assertTrue((self.state / "pending-build").exists())

    def test_both_wrappers_dispatch_space_before_repo_or_docker(self):
        for wrapper in ("dclaude", "dcodex"):
            with self.subTest(wrapper=wrapper):
                self.log.unlink(missing_ok=True)
                result = self.run_wrapper("--space", "images", "--keep", "2", wrapper=wrapper)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertIn(f"<--wrapper> <{wrapper}> <images> <--keep> <2>", self.calls())
                self.assertIn("<--current-image> <dclaude:1.2.3>", self.calls())
                self.assertNotIn("docker", self.calls())
                self.assertFalse(self.state.exists())

    def test_space_help_belongs_to_python(self):
        result = self.run_wrapper("--space", "--help")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("<--help>", self.calls())
        self.assertNotIn("usage: dclaude", result.stdout)

    def test_ordinary_help_explains_space_without_python(self):
        for wrapper in ("dclaude", "dcodex"):
            result = self.run_wrapper("--help", wrapper=wrapper)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn(f"{wrapper} --space --help", result.stdout)
            self.assertIn("docs/SPACE.md", result.stdout)
            self.assertIn("--yes only authorizes updates", result.stdout)
            self.assertEqual(self.calls(), "")

    def test_space_rejects_wrapper_combinations_even_with_help(self):
        for args in (
            ("--yes", "--space"), ("--help", "--space"), ("--profile", "work", "--space"),
            ("--space", "--yes"), ("--space", "--help", "--yes"),
            ("--space", "images", "--reset"), ("--space", "--update-tool"),
            ("--space", "--profile=work"), ("query", "--space"),
        ):
            with self.subTest(args=args):
                result = self.run_wrapper(*args)
                self.assertNotEqual(result.returncode, 0)
                self.assertIn("cannot be combined", result.stderr)
                self.assertEqual(self.calls(), "")

    def test_delimiter_preserves_space_as_agent_argument(self):
        result = self.run_launch("--", "--space", "--help")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("<agent> <--space> <--help>", self.calls())
        self.assertNotIn("python", self.calls())
        self.assertFalse((self.state / "operation.lock").exists())

    def test_public_space_cannot_override_internal_context_or_skip_confirmation(self):
        for option in ("--current-image", "--wrapper", "--auto-retain"):
            for form in (option, option + "=override"):
                with self.subTest(option=form):
                    result = self.run_wrapper("--space", form)
                    self.assertNotEqual(result.returncode, 0)
                    self.assertIn("internal Docker space option", result.stderr)
                    self.assertEqual(self.calls(), "")

    def test_default_build_is_labelled_and_locked_through_bootstrap(self):
        self.enable_retention(pending=False)
        result = self.run_launch("--rebuild")
        self.assertEqual(result.returncode, 0, result.stderr)
        calls = self.calls()
        self.assertIn("<--label> <com.dclaude.managed=true>", calls)
        self.assertIn("<--label> <com.dclaude.release=1.2.3>", calls)
        self.assertLess(calls.index("docker <build>"), calls.index("bootstrap"))
        self.assertLess(calls.index("bootstrap"), calls.index("<--auto-retain>"))
        self.assertNotIn("python-saw-lock", calls)
        self.assertFalse((self.state / "operation.lock").exists())
        self.assertEqual((self.state / "pending-build").read_text().strip(), IMAGE_ID)

    def test_custom_image_and_version_are_not_adopted(self):
        for key, value in (("DCLAUDE_IMAGE_NAME", "personal:test"), ("DCLAUDE_IMAGE_NAME", "dclaude:1.2.3"), ("DCLAUDE_VERSION", "1.2.4")):
            with self.subTest(key=key, value=value):
                self.log.unlink(missing_ok=True)
                self.env[key] = value
                self.enable_retention(pending=False)
                result = self.run_launch("--rebuild")
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertNotIn("<--label>", self.calls())
                self.assertNotIn("python", self.calls())
                del self.env[key]

    def test_normal_and_disabled_launches_do_not_use_python(self):
        self.env["PYTHON_EXIT"] = "98"
        result = self.run_launch("hello")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertNotIn("python", self.calls())
        self.enable_retention(enabled=False, pending=False)
        result = self.run_launch("--rebuild")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertNotIn("python", self.calls())

    def test_enabled_policy_without_pending_build_does_not_use_python(self):
        self.enable_retention(pending=False)
        result = self.run_launch("hello")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertNotIn("python", self.calls())
        self.assertEqual({path.name for path in self.state.iterdir()}, {"policy.json"})

    def test_failure_to_retain_does_not_block_agent(self):
        self.enable_retention()
        self.env["PYTHON_EXIT"] = "7"
        result = self.run_launch()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("continuing agent launch", result.stderr)
        self.assertIn("<agent>", self.calls())
        self.assertTrue((self.state / "pending-build").exists())

    def test_missing_python_only_blocks_explicit_space_commands(self):
        (self.bin / "python3").unlink()
        for name in ("bash", "tr", "dirname", "basename", "id", "mkdir", "chmod", "cat", "rm", "rmdir", "grep",
                     "date", "mv", "sed"):
            (self.bin / name).symlink_to(shutil.which(name))
        self.env["PATH"] = str(self.bin)
        result = self.run_launch()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.enable_retention()
        result = self.run_launch()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("host Python 3 is unavailable", result.stderr)
        result = self.run_wrapper("--space")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("missing required command: python3", result.stderr)

    def test_retention_inspect_failure_preserves_healthy_launch(self):
        self.enable_retention()
        self.env["FAIL_RETENTION_INSPECT"] = "1"
        result = self.run_launch()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("identity could not be verified", result.stderr)
        self.assertNotIn("python", self.calls())
        self.assertIn("<agent>", self.calls())

    def test_pending_build_must_match_bootstrapped_image(self):
        self.enable_retention(image_id="sha256:" + "b" * 64)
        result = self.run_launch()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertNotIn("python", self.calls())

    def test_failed_build_and_bootstrap_never_retain_and_release_lock(self):
        for failure in ("BUILD_EXIT", "BOOTSTRAP_EXIT"):
            with self.subTest(failure=failure):
                self.log.unlink(missing_ok=True)
                self.enable_retention(pending=False)
                self.env[failure] = "7"
                result = self.run_launch("--rebuild")
                self.assertEqual(result.returncode, 7, result.stderr)
                self.assertNotIn("python", self.calls())
                self.assertNotIn("<agent>", self.calls())
                self.assertFalse((self.state / "operation.lock").exists())
                self.assertEqual((self.state / "pending-build").exists(), failure == "BOOTSTRAP_EXIT")
                del self.env[failure]

    def test_failed_first_build_does_not_create_pending_marker(self):
        self.env["BUILD_EXIT"] = "7"
        result = self.run_launch("--rebuild")
        self.assertEqual(result.returncode, 7, result.stderr)
        self.assertFalse((self.state / "pending-build").exists())
        self.assertFalse((self.state / "operation.lock").exists())

    def test_update_tool_build_defers_retention_until_next_launch(self):
        self.enable_retention(pending=False)
        result = self.run_shell("""
WRAPPER_NAME=dclaude
HOST_HOME="$HOME"
ensure_docker() { :; }
load_tool_update_status() {
  TOOL_UPDATE_UP_TO_DATE=0
  TOOL_UPDATE_CURRENT=1
  TOOL_UPDATE_LATEST=2
}
prompt_confirm() { :; }
perform_tool_update claude
""")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("sync_tool_versions.py", self.calls())
        self.assertIn("docker <build>", self.calls())
        self.assertNotIn("<--auto-retain>", self.calls())
        self.assertEqual((self.state / "pending-build").read_text().strip(), IMAGE_ID)
        self.assertFalse((self.state / "operation.lock").exists())
        result = self.run_launch()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("<--auto-retain>", self.calls())

    def test_builder_override_disables_automatic_retention(self):
        self.enable_retention()
        self.env["BUILDX_BUILDER"] = "other-project"
        result = self.run_launch()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertNotIn("python", self.calls())

    def test_custom_builder_build_does_not_create_pending_retention(self):
        for key, value in (("BUILDX_BUILDER", "other-project"), ("DOCKER_BUILDKIT", "0")):
            with self.subTest(key=key):
                self.env[key] = value
                result = self.run_launch("--rebuild")
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertFalse((self.state / "pending-build").exists())
                del self.env[key]

    def test_busy_lock_is_never_stolen(self):
        lock = self.state / "operation.lock"
        lock.mkdir(parents=True)
        (lock / "owner").write_text("123456789\n")
        self.executable("sleep", "#!/bin/bash\nexit 0\n")
        result = self.run_launch("--rebuild")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("operation is busy", result.stderr)
        self.assertNotIn("docker <build>", self.calls())
        self.assertEqual((lock / "owner").read_text(), "123456789\n")

    def test_symlinked_state_is_rejected(self):
        self.state.parent.mkdir(parents=True)
        elsewhere = self.directory / "elsewhere"
        elsewhere.mkdir()
        self.state.symlink_to(elsewhere, target_is_directory=True)
        result = self.run_launch("--rebuild")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("must not be a symlink", result.stderr)
        self.assertNotIn("docker <build>", self.calls())

    def test_termination_releases_owned_lock(self):
        result = self.run_shell('acquire_space_lock\nkill -TERM "$$"\n')
        self.assertEqual(result.returncode, 143, result.stderr)
        self.assertFalse((self.state / "operation.lock").exists())


if __name__ == "__main__":
    unittest.main()
