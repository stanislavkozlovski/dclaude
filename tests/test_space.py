"""Safety contracts for the host storage planner and its mutation boundary."""

import argparse
import contextlib
import copy
import importlib.util
import io
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch


SPEC = importlib.util.spec_from_file_location("dclaude_space", Path(__file__).resolve().parents[1] / "scripts/space.py")
space = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(space)


def ident(letter):
    return "sha256:" + letter * 64


def image(letter, version=None, created=1, labelled=True, **extra):
    result = dict(Id=ident(letter), RepoTags=[f"dclaude:{version}"] if version else [], RepoDigests=[],
                  Created=created, Size=1000, SharedSize=600,
                  Labels={space.LABEL: "true"} if labelled else {})
    result.update(extra)
    return result


def cache(record, **extra):
    result = dict(ID=record, Size=100, InUse=False, Shared=False, Type="regular",
                  Description=f"build {record}", LastUsedAt="2026-01-01T00:00:00Z", Parents=[])
    result.update(extra)
    return result


def inventory(images=None, containers=None, records=None):
    return dict(images=images if images is not None else [image("a", "0.0.1"), image("f", "0.0.9", created=9)],
                containers=containers or [], cache=records or [], layers_size=2000)


def measurement(free=100, allocated=1000, **extra):
    result = dict(complete=True, measured_at="2026-09-07T00:00:00Z", issues=[],
                  disk_image=dict(path="/Docker.raw", device=1, inode=2, allocated_bytes=allocated, apparent_bytes=100000),
                  containers=[dict(uuid="apfs-1", roles=["startup", "docker"], free_bytes=free,
                                   capacity_bytes=100000, device="disk3")])
    result.update(extra)
    return result


def arguments(**extra):
    result = dict(action="images", keep=1, current_image="dclaude:0.0.9", disk_image=None,
                  apply=True, json=False, wrapper="dclaude", auto_retain=False)
    result.update(extra)
    return argparse.Namespace(**result)


class FakeDocker:
    def __init__(self, contents=None):
        self.contents = contents or inventory()
        self.binding = dict(daemon_id="desktop-engine", context="desktop-linux", builder="desktop-linux",
                            endpoint="unix:///docker.sock", driver="overlayfs", driver_status=[])
        self.deleted = []
        self.inventory_count = 0
        self.connect_count = 0
        self.inventory_hooks = {}
        self.connect_hook = None
        self.delete_hook = None

    def connect(self):
        self.connect_count += 1
        if self.connect_hook:
            self.connect_hook(self)
        return self.binding

    def inventory(self):
        self.inventory_count += 1
        if self.inventory_count in self.inventory_hooks:
            self.inventory_hooks[self.inventory_count](self)
        return copy.deepcopy(self.contents)

    def delete_image(self, target):
        self.deleted.append(target)
        if self.delete_hook:
            self.delete_hook(self, target)
        for item in list(self.contents["images"]):
            if target in item["RepoTags"]:
                item["RepoTags"].remove(target)
                if not item["RepoTags"]:
                    self.contents["images"].remove(item)
                return [{"Untagged": target}]
            if target == item["Id"]:
                self.contents["images"].remove(item)
                return [{"Deleted": target}]
        raise space.SpaceError("missing image")

    def delete_cache(self, target):
        self.deleted.append(target)
        if self.delete_hook:
            self.delete_hook(self, target)
        self.contents["cache"] = [r for r in self.contents["cache"] if r["ID"] != target]
        return dict(CachesDeleted=[target], SpaceReclaimed=100)

    def api(self, method, path, query=None):
        if path.startswith("/images/") and path.endswith("/json"):
            current = self.contents["images"][-1]
            return dict(Id=current["Id"], Config=dict(Labels=current["Labels"]))
        raise AssertionError((method, path, query))


class FakeHost:
    def __init__(self):
        self.count = 0
        self.hook = None

    def snapshot(self, disk_image=None):
        self.count += 1
        result = measurement(free=100 + self.count * 10, allocated=1000 - self.count * 5)
        return self.hook(self.count, result) if self.hook else result


class ImagePlanningTests(unittest.TestCase):
    def plan(self, contents, **kwargs):
        return space.image_plan(contents, "dclaude:0.0.9", keep=1, **kwargs)

    def test_release_aliases_count_once_for_retention(self):
        old = image("a", "0.0.1", created=1)
        old["RepoTags"].append("dclaude:0.0.2")
        contents = inventory([old, image("b", "0.0.3", created=3), image("f", "0.0.9", created=9)])
        plan = space.image_plan(contents, "dclaude:0.0.9", keep=2)
        self.assertEqual([p["id"] for p in plan["candidates"]], [ident("a")])
        self.assertEqual(plan["candidates"][0]["targets"], ["dclaude:0.0.1", "dclaude:0.0.2"])

    def test_current_old_build_is_protected_in_addition_to_newest(self):
        contents = inventory([image("a", "0.0.1"), image("b", "0.0.2", created=2), image("f", "0.0.9", created=9)])
        plan = space.image_plan(contents, "dclaude:0.0.1", keep=1)
        self.assertEqual([c["id"] for c in plan["candidates"]], [ident("b")])

    def test_creation_ties_use_identity_and_not_release_number(self):
        contents = inventory([image("a", "9.9.9", created=5), image("b", "0.0.2", created=5), image("f", "0.0.9", created=1)])
        plan = self.plan(contents)
        self.assertEqual([c["id"] for c in plan["candidates"]], [ident("a")])

    def test_stopped_containers_protect_old_builds(self):
        contents = inventory(containers=[dict(Image=ident("a"), State=dict(Running=False))])
        self.assertEqual(self.plan(contents)["candidates"], [])

    def test_unknown_container_identity_disables_all_image_cleanup(self):
        for reference in ("dclaude:0.0.1", ident("b"), None):
            with self.subTest(reference=reference):
                plan = self.plan(inventory(containers=[dict(Image=reference)]))
                self.assertTrue(plan["issues"])
                self.assertEqual(plan["candidates"], [])

    def test_missing_current_image_disables_cleanup(self):
        plan = space.image_plan(inventory(), "dclaude:missing", 1)
        self.assertEqual(plan["candidates"], [])
        self.assertIn("unresolved", plan["issues"][0])

    def test_nonrelease_and_foreign_aliases_protect_the_whole_identity(self):
        for alias in ("dclaude:latest", "personal:0.0.1", "dclaude:1.2.3-beta"):
            with self.subTest(alias=alias):
                contents = inventory()
                contents["images"][0]["RepoTags"].append(alias)
                self.assertEqual(self.plan(contents)["candidates"], [])
        contents = inventory()
        contents["images"][0]["RepoDigests"] = ["foreign@" + ident("c")]
        self.assertEqual(self.plan(contents)["candidates"], [])

    def test_legacy_releases_are_manual_only_and_dangling_unknowns_are_never_adopted(self):
        contents = inventory([image("a", "0.0.1", labelled=False), image("b", labelled=False), image("f", "0.0.9", created=9)])
        manual = self.plan(contents)
        self.assertEqual([p["id"] for p in manual["candidates"]], [ident("a")])
        self.assertTrue(manual["candidates"][0]["legacy"])
        self.assertEqual(self.plan(contents, automatic=True)["candidates"], [])

    def test_labelled_dangling_build_is_a_candidate_by_immutable_id(self):
        contents = inventory([image("a"), image("f", "0.0.9", created=9)])
        self.assertEqual(self.plan(contents, automatic=True)["candidates"][0]["targets"], [ident("a")])

    def test_index_without_platform_metadata_is_protected(self):
        contents = inventory()
        contents["images"][0]["Descriptor"] = dict(mediaType="application/vnd.oci.image.index.v1+json")
        plan = self.plan(contents)
        self.assertEqual(plan["candidates"], [])
        self.assertIn("unresolved", " ".join(plan["families"][0]["reasons"]))

    def test_platform_and_attestation_references_protect_the_index(self):
        for manifest in (
            dict(ID=ident("b"), ImageData=dict(Containers=["stopped"])),
            dict(ID=ident("c"), AttestationData=dict(For=ident("b"))),
        ):
            with self.subTest(manifest=manifest):
                contents = inventory(containers=[dict(Image=ident("b"))])
                contents["images"][0]["Manifests"] = [manifest]
                self.assertEqual(self.plan(contents)["candidates"], [])

    def test_overlapping_family_roots_cannot_bypass_alias_protection(self):
        contents = inventory([image("a", "0.0.1", Manifests=[dict(ID=ident("b"))]),
                              image("b", "0.0.2", RepoTags=["personal:keep"]), image("f", "0.0.9", created=9)])
        self.assertEqual(self.plan(contents)["candidates"], [])

    def test_immutable_image_mount_is_a_protected_reference(self):
        mounts = [dict(Type="image", Source="dclaude@" + ident("a"))]
        contents = inventory(containers=[dict(Image=ident("f"), HostConfig=dict(Mounts=mounts))])
        self.assertEqual(self.plan(contents)["candidates"], [])

    def test_unresolved_image_mount_vetoes_image_cleanup(self):
        contents = inventory(containers=[dict(Image=ident("f"), HostConfig=dict(Mounts=[dict(Type="image", Source="dclaude:0.0.1")]))])
        plan = self.plan(contents)
        self.assertTrue(plan["issues"])
        self.assertEqual(plan["candidates"], [])

    def test_invalid_image_metadata_fails_closed(self):
        for field, value in (("Id", "short-id"), ("Created", None), ("Size", "1000")):
            with self.subTest(field=field):
                contents = inventory()
                contents["images"][0][field] = value
                with self.assertRaises(space.SpaceError):
                    self.plan(contents)


class CachePlanningTests(unittest.TestCase):
    def test_only_private_unused_regular_records_are_eligible(self):
        records = [cache("private"), cache("shared", Shared=True), cache("used", InUse=True),
                   cache("internal", Type="internal"), cache("frontend", Type="frontend")]
        plan = space.cache_plan(inventory(records=records))
        self.assertEqual([c["id"] for c in plan["candidates"]], ["private"])
        self.assertEqual(len(plan["protected"]), 4)
        self.assertIn("builder-wide", plan["note"])

    def test_dependency_order_deletes_children_before_parents(self):
        records = [cache("parent"), cache("child", Parents=["parent"]), cache("grandchild", Parent="child")]
        plan = space.cache_plan(inventory(records=records))
        self.assertEqual([c["id"] for c in plan["candidates"]], ["grandchild", "child", "parent"])

    def test_dependency_cycles_disable_mutation(self):
        plan = space.cache_plan(inventory(records=[cache("a", Parents=["b"]), cache("b", Parents=["a"])]))
        self.assertTrue(plan["issues"])

    def test_missing_sharing_or_usage_is_not_assumed_reclaimable(self):
        for field, value in (("Shared", None), ("InUse", 0), ("Size", None), ("ID", "")):
            with self.subTest(field=field):
                record = cache("a")
                record[field] = value
                with self.assertRaises(space.SpaceError):
                    space.cache_plan(inventory(records=[record]))

    def test_cache_delete_uses_anchored_escaped_id_and_never_force(self):
        docker = space.Docker()
        with patch.object(docker, "api") as api:
            docker.delete_cache("record.with+regex")
        method, path, query = api.call_args.args
        self.assertEqual((method, path), ("POST", "/build/prune"))
        self.assertEqual(query["all"], "false")
        self.assertEqual(json.loads(query["filters"])["id"], [r"^record\.with\+regex$"])

    def test_image_delete_is_nonforced_and_disables_parent_pruning(self):
        docker = space.Docker()
        with patch.object(docker, "api") as api:
            docker.delete_image("dclaude:0.0.1")
        self.assertEqual(api.call_args.args, ("DELETE", "/images/dclaude%3A0.0.1", dict(force="false", noprune="true")))


class MeasurementTests(unittest.TestCase):
    def test_signed_host_counters_are_separate_from_disk_allocation(self):
        result = space.host_delta(measurement(100, 1000), measurement(90, 800))
        self.assertEqual(result["raw_allocated_bytes_reduction"], 200)
        self.assertEqual(result["apfs"][0]["free_bytes_delta"], -10)
        self.assertTrue(result["recovery_observed"])

    def test_unmeasured_is_not_zero(self):
        result = space.host_delta(measurement(), measurement(complete=False))
        self.assertFalse(result["measured"])
        self.assertNotIn("raw_allocated_bytes_reduction", result)

    def test_changed_file_or_apfs_identity_invalidates_comparison(self):
        for kind in ("file", "apfs"):
            after = measurement()
            if kind == "file":
                after["disk_image"]["inode"] = 3
            else:
                after["containers"][0]["uuid"] = "different-apfs"
            self.assertFalse(space.host_delta(measurement(), after)["measured"])

    def test_external_disk_recovery_does_not_claim_startup_disk_recovery(self):
        before = measurement()
        before["containers"][0]["roles"] = ["startup"]
        before["containers"].append(dict(uuid="external", roles=["docker"], free_bytes=400))
        after = copy.deepcopy(before)
        after["containers"][1]["free_bytes"] += 100
        result = space.host_delta(before, after)
        self.assertEqual([c["free_bytes_delta"] for c in result["apfs"]], [0, 100])


class SpaceFixture(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.directory = Path(self.temp.name) / "space"
        self.docker = FakeDocker()
        self.host = FakeHost()
        self.args = arguments()
        self.output = io.StringIO()
        self.confirm = patch.object(space, "confirm")
        self.confirm_mock = self.confirm.start()
        self.addCleanup(self.confirm.stop)

    def apply(self, **kwargs):
        with contextlib.redirect_stdout(self.output):
            return space.apply_cleanup(self.docker, self.host, self.args, self.directory, **kwargs)

    def receipt(self):
        latest = json.loads((self.directory / "latest.json").read_text())
        return json.loads(Path(latest["receipt"]).read_text())

    def policy(self, **kwargs):
        value = dict(schema_version=1, enabled=True, keep=1, repository="dclaude",
                     binding=self.docker.binding, disk_image="/Docker.raw")
        value.update(kwargs)
        space.write_json(self.directory / "policy.json", value)
        return value


class StateAndApplyTests(SpaceFixture):
    def test_apply_saves_receipt_before_first_deletion_and_records_measured_result(self):
        def require_saved(docker, target):
            saved = self.receipt()
            self.assertEqual(saved["status"], "in_progress")
            self.assertEqual(saved["steps"][-1]["target"], target)
            self.assertEqual(saved["steps"][-1]["status"], "started")
        self.docker.delete_hook = require_saved
        result = self.apply()
        self.assertEqual(self.docker.deleted, ["dclaude:0.0.1"])
        self.assertEqual(result["status"], "completed")
        self.assertTrue(result["delta"]["measured"])
        self.assertEqual(self.receipt()["status"], "completed")
        self.assertFalse((self.directory / "operation.lock").exists())

    def test_all_reviewed_aliases_can_be_removed_without_false_drift(self):
        self.docker.contents["images"][0]["RepoTags"].append("dclaude:0.0.2")
        self.apply()
        self.assertEqual(self.docker.deleted, ["dclaude:0.0.1", "dclaude:0.0.2"])

    def test_read_only_receipt_failure_prevents_any_delete(self):
        with patch.object(space, "write_json", side_effect=PermissionError("receipt denied")):
            with self.assertRaises(PermissionError):
                self.apply()
        self.assertEqual(self.docker.deleted, [])

    def test_unknown_policy_or_latest_schema_prevents_apply(self):
        for name in ("policy.json", "latest.json"):
            with self.subTest(name=name):
                self.directory.mkdir(exist_ok=True)
                path = self.directory / name
                path.write_text('{"schema_version": 2}')
                with self.assertRaises(space.SpaceError):
                    self.apply()
                self.assertEqual(self.docker.deleted, [])
                path.unlink()

    def test_missing_host_baseline_prevents_confirmation_and_deletion(self):
        self.host.hook = lambda count, result: measurement(complete=False)
        with self.assertRaises(space.SpaceError):
            self.apply()
        self.confirm_mock.assert_not_called()
        self.assertEqual(self.docker.deleted, [])

    def test_declined_confirmation_does_not_write_a_receipt_or_delete(self):
        self.confirm_mock.side_effect = space.SpaceError("cancelled")
        with self.assertRaises(space.SpaceError):
            self.apply()
        self.assertEqual(self.docker.deleted, [])
        self.assertFalse((self.directory / "latest.json").exists())

    def test_new_container_reference_between_preview_and_delete_stops_apply(self):
        self.docker.inventory_hooks[2] = lambda d: d.contents["containers"].append(dict(Image=ident("a")))
        with self.assertRaises(space.SpaceError):
            self.apply()
        self.assertEqual(self.docker.deleted, [])
        self.assertEqual(self.receipt()["status"], "partial")

    def test_new_alias_between_preview_and_delete_stops_apply(self):
        self.docker.inventory_hooks[2] = lambda d: d.contents["images"][0]["RepoTags"].append("dclaude:0.0.2")
        with self.assertRaises(space.SpaceError):
            self.apply()
        self.assertEqual(self.docker.deleted, [])

    def test_retag_to_different_identity_stops_apply(self):
        self.docker.inventory_hooks[2] = lambda d: d.contents["images"][0].update(Id=ident("b"))
        with self.assertRaises(space.SpaceError):
            self.apply()
        self.assertEqual(self.docker.deleted, [])

    def test_docker_binding_change_stops_before_mutation(self):
        def change_after_review(docker):
            if docker.connect_count > 1:
                docker.binding = dict(docker.binding, daemon_id="other-engine")
        self.docker.connect_hook = change_after_review
        with self.assertRaises(space.SpaceError):
            self.apply()
        self.assertEqual(self.docker.deleted, [])

    def test_disk_replacement_between_preview_and_apply_stops_mutation(self):
        def replace(count, result):
            if count > 1:
                result["disk_image"]["inode"] = 99
            return result
        self.host.hook = replace
        with self.assertRaises(space.SpaceError):
            self.apply()
        self.assertEqual(self.docker.deleted, [])

    def test_error_stops_without_global_fallback_and_saves_partial_receipt(self):
        self.docker.contents["images"].insert(1, image("b", "0.0.2", created=2))
        def fail_second(docker, target):
            if target == "dclaude:0.0.2":
                raise space.SpaceError("daemon conflict")
        self.docker.delete_hook = fail_second
        with self.assertRaises(space.SpaceError):
            self.apply()
        saved = self.receipt()
        self.assertEqual(self.docker.deleted, ["dclaude:0.0.1", "dclaude:0.0.2"])
        self.assertEqual(saved["status"], "partial")
        self.assertIn("daemon conflict", saved["error"])
        self.assertEqual(saved["steps"][0]["status"], "completed")
        self.assertIn("finished_at", saved)
        self.assertFalse((self.directory / "operation.lock").exists())

    def test_keyboard_interrupt_keeps_partial_receipt_and_releases_lock(self):
        self.docker.delete_hook = lambda *_: (_ for _ in ()).throw(KeyboardInterrupt())
        with self.assertRaises(KeyboardInterrupt):
            self.apply()
        self.assertEqual(self.receipt()["status"], "partial")
        self.assertFalse((self.directory / "operation.lock").exists())

    def test_cache_shared_after_confirmation_is_never_deleted(self):
        self.args.action = "cache"
        self.docker.contents["cache"] = [cache("a")]
        self.docker.inventory_hooks[2] = lambda d: d.contents["cache"][0].update(Shared=True)
        with self.assertRaises(space.SpaceError):
            self.apply()
        self.assertEqual(self.docker.deleted, [])

    def test_unapproved_cache_dependency_disappearance_stops_next_delete(self):
        self.args.action = "cache"
        self.docker.contents["cache"] = [cache("a"), cache("b", Shared=True), cache("c")]
        def remove_extra(docker, target):
            docker.contents["cache"] = [r for r in docker.contents["cache"] if r["ID"] != "b"]
        self.docker.delete_hook = remove_extra
        with self.assertRaises(space.SpaceError):
            self.apply()
        self.assertEqual(self.docker.deleted, ["a"])
        self.assertEqual(self.receipt()["steps"][0]["status"], "unexpected_dependency_change")

    def test_lock_never_steals_a_stale_owner(self):
        self.directory.mkdir()
        lock = self.directory / "operation.lock"
        lock.mkdir()
        (lock / "owner").write_text("999999999\n")
        with self.assertRaises(space.SpaceError):
            with space.operation_lock(self.directory, wait_seconds=0):
                self.fail("stale lock was stolen")
        self.assertEqual((lock / "owner").read_text(), "999999999\n")

    def test_lock_is_removed_on_exception_and_matches_shell_owner_format(self):
        with self.assertRaisesRegex(RuntimeError, "test"):
            with space.operation_lock(self.directory):
                self.assertEqual((self.directory / "operation.lock/owner").read_text(), str(os.getpid()) + "\n")
                raise RuntimeError("test")
        self.assertFalse((self.directory / "operation.lock").exists())

    def test_symlink_state_is_rejected(self):
        target = Path(self.temp.name) / "target"
        target.mkdir()
        self.directory.symlink_to(target, target_is_directory=True)
        with self.assertRaises(space.SpaceError):
            with space.operation_lock(self.directory):
                self.fail("symlink state was accepted")

    def test_policy_schema_rejects_bool_keep_and_unowned_repository(self):
        for change in (dict(keep=True), dict(keep=0), dict(enabled="true"), dict(repository="other"), dict(binding="desktop")):
            with self.subTest(change=change):
                self.policy(**change)
                with self.assertRaises(space.SpaceError):
                    space.load_policy(self.directory)


class CommandTests(SpaceFixture):
    def run_main(self, *args):
        with patch.object(space, "Docker", return_value=self.docker), patch.object(space, "HostProbe", return_value=self.host), \
                patch.object(space, "state_dir", return_value=self.directory), contextlib.redirect_stdout(self.output), \
                contextlib.redirect_stderr(self.output):
            return space.main(["--current-image", "dclaude:0.0.9", *args])

    def test_json_preview_is_readonly_and_machine_parseable(self):
        self.assertEqual(self.run_main("--json"), 0)
        report = json.loads(self.output.getvalue())
        self.assertEqual(report["schema_version"], 1)
        self.assertIn("cache_plan", report)
        self.assertEqual(self.docker.deleted, [])
        self.assertFalse(self.directory.exists())

    def test_json_apply_is_rejected(self):
        self.assertEqual(self.run_main("images", "--apply", "--json"), 2)
        self.assertFalse(json.loads(self.output.getvalue())["mutation_allowed"])
        self.assertEqual(self.docker.deleted, [])

    def test_json_cannot_enable_retention(self):
        self.assertEqual(self.run_main("retention", "enable", "--json"), 2)
        self.assertFalse((self.directory / "policy.json").exists())

    def test_json_cannot_disable_retention(self):
        self.policy()
        self.assertEqual(self.run_main("retention", "disable", "--json"), 2)
        self.assertTrue(space.load_policy(self.directory)["enabled"])

    def test_retention_enable_then_disable_preserves_policy_and_other_state(self):
        self.assertEqual(self.run_main("retention", "enable", "--keep", "3"), 0)
        enabled = space.load_policy(self.directory)
        self.assertTrue(enabled["enabled"])
        self.assertEqual(enabled["keep"], 3)
        self.assertEqual(enabled["binding"], self.docker.binding)
        self.assertEqual(self.run_main("retention", "disable"), 0)
        self.assertFalse(space.load_policy(self.directory)["enabled"])
        self.assertEqual(self.docker.deleted, [])

    def test_unknown_policy_schema_cannot_be_enabled_over(self):
        self.directory.mkdir()
        (self.directory / "policy.json").write_text('{"schema_version": 999}')
        self.assertEqual(self.run_main("retention", "enable"), 2)
        self.assertEqual(json.loads((self.directory / "policy.json").read_text())["schema_version"], 999)

    def test_verify_measures_only_receipt_baseline_and_never_executes_receipt_targets(self):
        self.apply()
        receipt = self.receipt()
        path = Path(json.loads((self.directory / "latest.json").read_text())["receipt"])
        receipt["reviewed"] = [dict(targets=["dangerous:target"])]
        space.write_json(path, receipt)
        self.docker.deleted.clear()
        self.output = io.StringIO()
        self.assertEqual(self.run_main("verify"), 0)
        self.assertTrue(json.loads(self.output.getvalue())["delta"]["measured"])
        self.assertEqual(self.docker.deleted, [])

    def test_verify_rejects_receipt_outside_own_directory(self):
        space.write_json(self.directory / "latest.json", dict(schema_version=1, receipt="/tmp/untrusted.json"))
        self.assertEqual(self.run_main("verify"), 2)
        self.assertIn("Invalid receipt location", self.output.getvalue())

    def test_auto_retention_needs_matching_pending_build_and_policy_binding(self):
        self.policy(binding=dict(self.docker.binding, daemon_id="other"))
        (self.directory / "pending-build").write_text(ident("f") + "\n")
        self.assertEqual(self.run_main("--auto-retain"), 2)
        self.assertEqual(self.docker.deleted, [])
        self.policy()
        (self.directory / "pending-build").write_text(ident("b") + "\n")
        self.assertEqual(self.run_main("--auto-retain"), 2)
        self.assertEqual(self.docker.deleted, [])

    def test_auto_retention_consumes_pending_only_after_success(self):
        self.policy()
        (self.directory / "pending-build").write_text(ident("f") + "\n")
        self.assertEqual(self.run_main("--auto-retain"), 0)
        self.assertEqual(self.docker.deleted, ["dclaude:0.0.1"])
        self.confirm_mock.assert_not_called()
        self.assertFalse((self.directory / "pending-build").exists())

    def test_auto_retention_cannot_outlive_policy_disabled_before_lock(self):
        self.policy()
        (self.directory / "pending-build").write_text(ident("f") + "\n")
        real_lock = space.operation_lock
        @contextlib.contextmanager
        def disable_before_lock(directory, **kwargs):
            policy = space.load_policy(directory)
            policy["enabled"] = False
            space.write_json(directory / "policy.json", policy)
            with real_lock(directory, **kwargs):
                yield
        with patch.object(space, "operation_lock", disable_before_lock):
            result = self.run_main("--auto-retain")
        self.assertIn(result, (0, 2))
        self.assertEqual(self.docker.deleted, [])
        self.assertTrue((self.directory / "pending-build").exists())

    def test_ignored_keep_arguments_are_rejected(self):
        for args in (("cache", "--keep", "2"), ("verify", "--keep", "2"), ("retention", "status", "--keep", "2")):
            with self.subTest(args=args):
                self.assertEqual(self.run_main(*args), 2)


if __name__ == "__main__":
    unittest.main()
