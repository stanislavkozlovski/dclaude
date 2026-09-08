"""Readable summaries must not hide uncertainty or deletion consent."""

import contextlib
import io
import unittest

from test_space import cache, ident, image, inventory, measurement, space


def render(function, *args, **kwargs):
    output = io.StringIO()
    with contextlib.redirect_stdout(output):
        function(*args, **kwargs)
    return output.getvalue()


def missing_image_report():
    """The user's four-image report, with illustrative cache sizes."""
    images = [image("a", "0.1.81", created=1788416279, Size=1859748430),
              image("b", "0.1.76", created=1787821780, Size=1879354212),
              image("c", labelled=False, RepoTags=["postgres:16"], Size=663046595),
              image("d", labelled=False, RepoTags=["testcontainers/ryuk:0.5.1"], Size=19277768)]
    contents = inventory(images, [dict(Image=ident("a")), dict(Image=ident("b"))],
                         [cache(f"cache-{i:02}", Size=64 * 1024 ** 2) for i in range(58)] +
                         [cache(f"shared-{i}", Shared=True) for i in range(9)])
    return dict(inventory=contents, plan=space.image_plan(contents, "dclaude:0.1.85"),
                cache_plan=space.cache_plan(contents),
                host=measurement(free=31819055104, allocated=32058363904))


class OutputTests(unittest.TestCase):
    def test_user_report_is_short_and_has_an_actionable_cache_summary(self):
        output = render(space.print_report, missing_image_report(), "images")
        for text in ("29.9 GiB", "29.6 GiB", "2 dclaude images — used by containers",
                     "2 images (other projects)", "58 unused private records", "3.6 GiB",
                     "This checkout expects dclaude:0.1.85, which is not built.", "dclaude --space cache"):
            self.assertIn(text, output)
        for noise in ("sha256:", "Unix time", "apfs-1", "32058363904", "{'", "--apply"):
            self.assertNotIn(noise, output)
        self.assertLessEqual(len(output.splitlines()), 30)

    def test_cache_preview_is_bounded_but_apply_shows_every_exact_record(self):
        report = missing_image_report()
        report["plan"] = report["cache_plan"]
        preview = render(space.print_report, report, "cache", wrapper="dcodex")
        self.assertIn("53 more", preview)
        self.assertNotIn("cache-57", preview)
        self.assertIn("dcodex --space cache --apply", preview)
        applied = render(space.print_report, report, "cache", applying=True)
        for i in range(58):
            self.assertIn(f"cache-{i:02}", applied)
        self.assertNotIn("Preview only", applied)

    def test_image_apply_displays_all_aliases_and_full_immutable_ids(self):
        old = [image(c, f"0.0.{i}") for i, c in enumerate("abcdef", start=1)]
        old[0]["RepoTags"].append("dclaude:0.0.20")
        contents = inventory(old + [image("9"), image("0", "0.0.99", created=99)])
        report = dict(plan=space.image_plan(contents, "dclaude:0.0.99", keep=1), host=measurement())
        output = render(space.print_report, report, "images", applying=True)
        for candidate in report["plan"]["candidates"]:
            self.assertIn(candidate["id"], output)
            for target in candidate["targets"]:
                self.assertIn(target, output)
        self.assertNotIn("…", output)

    def test_unknown_measurement_is_not_zero_and_external_space_is_separate(self):
        report = missing_image_report()
        report["host"] = measurement(disk_image=None, complete=False,
                                     issues=[dict(message="Permission denied: /Docker.raw")])
        report["host"]["containers"] = [dict(roles=["docker"], free_bytes=10 * 1024 ** 3)]
        output = render(space.print_report, report, "images")
        self.assertIn("Disk used       Unmeasured", output)
        self.assertIn("Mac free        Unmeasured", output)
        self.assertIn("External free   10.0 GiB", output)
        self.assertIn("Permission denied", output)
        self.assertNotIn("--apply", output)

    def test_recovery_preserves_negative_deltas_and_external_disk_identity(self):
        delta = dict(measured=True, raw_allocated_bytes_reduction=-1024 ** 3, recovery_observed=False,
                     apfs=[dict(roles=["startup"], free_bytes_delta=-2 * 1024 ** 3),
                           dict(roles=["docker"], free_bytes_delta=-1024 ** 3)])
        output = render(space.print_recovery, delta, "/receipt.json", "dcodex")
        self.assertIn("1.0 GiB more allocated", output)
        self.assertIn("Mac free change -2.0 GiB", output)
        self.assertIn("External change -1.0 GiB", output)
        self.assertIn("Recovery not yet observed", output)
        self.assertIn("dcodex --space verify", output)
        self.assertIn("/receipt.json", output)

    def test_retention_status_is_readable_with_and_without_a_saved_policy(self):
        default = render(space.print_policy, None, "dclaude")
        for text in ("Image retention enabled.", "Enabled (default)", "2 distinct builds", "once a day", "build cache"):
            self.assertIn(text, default)
        self.assertNotIn("Docker context", default)
        disabled = render(space.print_policy, dict(enabled=False, keep=2, binding={}), "dclaude")
        self.assertIn("Image retention disabled.", disabled)
        self.assertIn("Status          Disabled", disabled)
        self.assertNotIn("Runs after", disabled)
        policy = dict(enabled=True, keep=4, binding=dict(context="desktop-linux", builder="default"))
        enabled = render(space.print_policy, policy, "dclaude")
        for text in ("Status          Enabled\n", "4 distinct builds", "desktop-linux", "default", "status --json"):
            self.assertIn(text, enabled)
        self.assertNotIn("(default)", enabled)
        self.assertNotIn("{", enabled)

    def test_followup_commands_preserve_keep_and_quote_a_moved_disk_path(self):
        report = missing_image_report()
        report["plan"]["keep"] = 4
        output = render(space.print_report, report, "images", wrapper="dcodex",
                        disk_image="/Volumes/External Disk/Docker.raw")
        self.assertIn("dcodex --space cache --disk-image '/Volumes/External Disk/Docker.raw'", output)
        self.assertIn("dcodex --space images --keep 4 --disk-image '/Volumes/External Disk/Docker.raw' --json", output)


class NextActionTests(unittest.TestCase):
    def test_eligible_preview_recommends_review_and_blocked_preview_does_not(self):
        contents = inventory()
        report = dict(plan=space.image_plan(contents, "dclaude:0.0.9", keep=1), host=measurement())
        hints = space.next_actions("dcodex", "images", report=report)
        self.assertEqual(hints[0][1], "dcodex --space images --keep 1 --apply")
        report["host"]["complete"] = False
        self.assertFalse(any("--apply" in command for _, command in space.next_actions("dcodex", "images", report=report)))

    def test_no_candidates_offers_details_without_inventing_cache_work(self):
        report = missing_image_report()
        report["plan"]["issues"] = []
        report["cache_plan"]["candidates"] = []
        hints = space.next_actions("dclaude", "images", report=report)
        self.assertEqual(len(hints), 1)
        self.assertTrue(hints[0][1].endswith("--json"))
        output = render(space.print_report, report, "images")
        self.assertIn("Nothing to remove.", output)
        self.assertNotIn("\nIssues", output)

    def test_recovery_prioritizes_verify_then_fresh_cache_and_preserves_scope(self):
        delta = dict(measured=True, recovery_observed=False)
        hints = space.next_actions("dcodex", "images", delta=delta, completed=True,
                                   disk_image="/External Disk/Docker.raw")
        self.assertEqual(hints[0][1], "dcodex --space verify")
        self.assertEqual(hints[1][1], "dcodex --space cache --disk-image '/External Disk/Docker.raw'")
        self.assertEqual(len(hints), 3)
        delta["recovery_observed"] = True
        self.assertEqual(space.next_actions("dcodex", "images", delta=delta, completed=True)[0][1], "dcodex --space cache")

    def test_unmeasured_recovery_does_not_promise_a_retry_will_fix_it(self):
        hints = space.next_actions("dclaude", "verify", delta=dict(measured=False))
        self.assertEqual(hints, [("Inspect recovery measurements", "dclaude --space verify --json")])

    def test_section_order_and_single_next_section(self):
        report = missing_image_report()
        report["plan"]["issues"] = ["A container image reference is unresolved."]
        output = render(space.print_report, report, "images", applying=True)
        self.assertLess(output.index("Result"), output.index("Docker storage"))
        self.assertLess(output.index("Docker storage"), output.index("\nIssues"))
        self.assertLess(output.index("\nIssues"), output.index("\nNext"))
        self.assertEqual(output.count("\nNext\n"), 1)
        for old in ("Next step", "Check later", "Details"):
            self.assertNotIn(old, output)


if __name__ == "__main__":
    unittest.main()
