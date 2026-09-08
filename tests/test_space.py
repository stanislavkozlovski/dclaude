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
import threading
import unittest
from unittest.mock import Mock, patch


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
                containers=containers or [], cache=records or [])


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


def reverse_inventory_lists(value):
    """Model Docker returning the same sets in a different enumeration order."""
    if isinstance(value, list):
        return [reverse_inventory_lists(item) for item in reversed(value)]
    if isinstance(value, dict):
        return {key: reverse_inventory_lists(item) for key, item in value.items()}
    return value


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
        self.held = {}  # image ID -> cache record IDs whose layers that image references

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
                    self.release_cache()
                return [{"Untagged": target}]
            if target == item["Id"]:
                self.contents["images"].remove(item)
                self.release_cache()
                return [{"Deleted": target}]
        raise space.SpaceError("missing image")

    def release_cache(self):
        """Model BuildKit: a record stays shared while any remaining image references its layers."""
        remaining = {item["Id"] for item in self.contents["images"]}
        for record in self.contents["cache"]:
            holders = {image_id for image_id, ids in self.held.items() if record["ID"] in ids}
            if holders and not holders & remaining:
                record["Shared"] = False

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

    def test_missing_current_image_is_a_note_and_preserves_newest(self):
        plan = space.image_plan(inventory(), "dclaude:missing", 1)
        self.assertEqual([c["id"] for c in plan["candidates"]], [ident("a")])
        self.assertEqual(plan["issues"], [])
        self.assertEqual(plan["notes"], ["This checkout expects dclaude:missing, which is not built."])

    def test_current_manifest_member_protects_its_existing_family(self):
        contents = inventory([
            image("a", "0.0.1", Manifests=[dict(ID=ident("b"), Available=True, Kind="image")]),
            image("f", "0.0.9", created=9)])
        plan = space.image_plan(contents, ident("b"), 1)
        self.assertEqual(plan["candidates"], [])
        self.assertEqual(plan["notes"], [])
        self.assertIn("configured current launcher image", plan["families"][0]["reasons"])

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
            dict(ID=ident("b"), Available=True, Kind="image", ImageData=dict(Containers=["stopped"])),
            dict(ID=ident("c"), Available=True, Kind="attestation", AttestationData=dict(For=ident("b"))),
        ):
            with self.subTest(manifest=manifest):
                contents = inventory(containers=[dict(Image=ident("b"))])
                contents["images"][0]["Manifests"] = [manifest]
                self.assertEqual(self.plan(contents)["candidates"], [])

    def test_overlapping_family_roots_cannot_bypass_alias_protection(self):
        contents = inventory([image("a", "0.0.1", Manifests=[dict(ID=ident("b"), Available=True, Kind="image")]),
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

    def host_probe(self, denied=False, external=False):
        startup = dict(APFSContainerUUID="startup", ContainerReference="disk3", CapacityCeiling=1000,
                       CapacityFree=100, Volumes=[dict(DeviceIdentifier="disk3s1")])
        external_disk = dict(APFSContainerUUID="external", ContainerReference="disk8", CapacityCeiling=2000,
                             CapacityFree=200, Volumes=[dict(DeviceIdentifier="disk8s1")])
        probe = space.HostProbe()
        def plist(args):
            if args[0] == "apfs":
                return dict(Containers=[startup, external_disk] if external else [startup])
            if args[-1] == "/Volumes/External":
                return dict(APFSContainerReference="disk8")
            return dict(APFSContainerReference="disk3")
        raw = measurement()["disk_image"]
        raw["path"] = "/Volumes/External/Docker.raw" if external else "/Docker.raw"
        with patch.object(space.platform, "system", return_value="Darwin"), patch.object(probe, "plist", side_effect=plist), \
                patch.object(space, "measure_file", side_effect=PermissionError("privacy denied") if denied else None, return_value=raw), \
                patch.object(space.os.path, "ismount", side_effect=lambda p: str(p) in ("/", "/Volumes/External")):
            return probe.snapshot(Path(raw["path"]))

    def test_same_apfs_container_is_counted_once_with_both_roles(self):
        result = self.host_probe()
        self.assertTrue(result["complete"])
        self.assertEqual(len(result["containers"]), 1)
        self.assertEqual(result["containers"][0]["roles"], ["startup", "docker"])
        self.assertEqual(result["containers"][0]["free_bytes"], 100)

    def test_external_raw_keeps_separate_apfs_containers(self):
        result = self.host_probe(external=True)
        self.assertTrue(result["complete"])
        self.assertEqual([c["roles"] for c in result["containers"]], [["startup"], ["docker"]])

    def test_denied_disk_image_is_unmeasured_but_startup_counter_survives(self):
        result = self.host_probe(denied=True)
        self.assertFalse(result["complete"])
        self.assertIsNone(result["disk_image"])
        self.assertEqual(result["containers"][0]["free_bytes"], 100)
        self.assertEqual(result["issues"][0]["path"], "/Docker.raw")
        self.assertIn("privacy denied", result["issues"][0]["message"])


class DockerBoundaryTests(unittest.TestCase):
    def setUp(self):
        self.context = "desktop-linux"
        self.endpoint = "unix://" + str(Path.home() / ".docker/run/docker.sock")
        self.version = "1.49"
        self.info = dict(ID="desktop-engine", OperatingSystem="Docker Desktop", OSType="linux", Driver="overlayfs")
        self.context_endpoints = {}
        self.builder = dict(Name="desktop-linux", Driver="docker",
                            Nodes=[dict(Status="running", Endpoint="desktop-linux", IDs=["desktop-worker"])])
        self.envelope = dict(Current=True, Builder=self.builder)
        self.docker = space.Docker(runner=self.runner)
        self.api_patch = patch.object(self.docker, "api", side_effect=self.api)
        self.api_patch.start()
        self.addCleanup(self.api_patch.stop)
        self.platform_patch = patch.object(space.platform, "system", return_value="Darwin")
        self.platform_patch.start()
        self.addCleanup(self.platform_patch.stop)
        self.env_patch = patch.dict(os.environ)
        self.env_patch.start()
        self.addCleanup(self.env_patch.stop)
        for key in ("DOCKER_HOST", "DOCKER_CONTEXT", "BUILDX_BUILDER", "DOCKER_BUILDKIT"):
            os.environ.pop(key, None)

    def runner(self, args):
        if args[:3] == ["docker", "context", "show"]:
            return self.context.encode()
        if args[:3] == ["docker", "context", "inspect"]:
            endpoint = self.endpoint if args[3] == self.context else self.context_endpoints[args[3]]
            return json.dumps([dict(Endpoints=dict(docker=dict(Host=endpoint)))]).encode()
        if args[:3] == ["docker", "buildx", "ls"]:
            return json.dumps(self.envelope).encode()
        raise AssertionError(args)

    def api(self, method, path, query=None):
        if path == "/version":
            return dict(ApiVersion=self.version)
        if path == "/info":
            return self.info
        raise AssertionError((method, path, query))

    def test_local_desktop_binding_requires_selected_builder_same_engine(self):
        result = self.docker.connect()
        self.assertEqual(result["daemon_id"], "desktop-engine")
        self.assertEqual(result["context"], "desktop-linux")
        self.assertEqual(result["endpoint"], self.endpoint)
        self.assertEqual(result["worker_ids"], ["desktop-worker"])

    def test_default_named_builder_requires_one_running_node_on_selected_desktop_engine(self):
        self.builder["Name"] = "default"
        binding = self.docker.connect()
        self.assertEqual(binding["context"], "desktop-linux")
        self.assertEqual(binding["builder"], "default")
        self.assertEqual(binding["daemon_id"], "desktop-engine")
        invalid_nodes = (
            [dict(Status="running", Endpoint="unix:///other.sock", IDs=["desktop-engine"])],
            [dict(Status="stopped", Endpoint="desktop-linux", IDs=["desktop-worker"])],
            [dict(Status="running", Endpoint="desktop-linux", IDs=["desktop-worker"]),
             dict(Status="running", Endpoint="desktop-linux", IDs=["desktop-worker"])],
        )
        for nodes in invalid_nodes:
            with self.subTest(nodes=nodes):
                self.builder["Nodes"] = nodes
                with self.assertRaises(space.SpaceError):
                    self.docker.connect()

    def test_tcp_ssh_and_forwarded_unix_sockets_are_refused(self):
        for endpoint in ("tcp://remote:2375", "ssh://server", "unix:///tmp/forwarded.sock"):
            with self.subTest(endpoint=endpoint):
                self.endpoint = endpoint
                with self.assertRaises(space.SpaceError):
                    self.docker.connect()

    def test_other_builder_driver_or_daemon_is_refused(self):
        for key, value in (("Driver", "docker-container"), ("Name", "other-context"),
                           ("Nodes", [dict(Status="running", Endpoint="unix:///other.sock", IDs=["desktop-engine"])])):
            with self.subTest(key=key):
                old = self.builder[key]
                self.builder[key] = value
                with self.assertRaises(space.SpaceError):
                    self.docker.connect()
                self.builder[key] = old

    def test_worker_id_can_differ_only_when_node_endpoint_proves_same_desktop_socket(self):
        self.context_endpoints["default"] = self.endpoint
        for endpoint in ("default", self.endpoint):
            with self.subTest(endpoint=endpoint):
                self.builder["Nodes"][0]["Endpoint"] = endpoint
                binding = self.docker.connect()
                self.assertEqual(binding["daemon_id"], "desktop-engine")
                self.assertEqual(binding["worker_ids"], ["desktop-worker"])
                self.assertEqual(binding["builder_endpoint"], self.endpoint)

    def test_wrong_remote_or_missing_node_endpoint_refused_even_if_worker_matches_daemon(self):
        node = self.builder["Nodes"][0]
        node["IDs"] = ["desktop-engine"]
        self.context_endpoints["remote-context"] = "tcp://other:2375"
        self.context_endpoints["other-context"] = "unix:///other.sock"
        for endpoint in ("unix:///other.sock", "tcp://other:2375", "ssh://other", "remote-context", "other-context", None, ""):
            with self.subTest(endpoint=endpoint):
                node["Endpoint"] = endpoint
                with self.assertRaises(space.SpaceError):
                    self.docker.connect()
        del node["Endpoint"]
        with self.assertRaises(space.SpaceError):
            self.docker.connect()

    def test_daemon_change_during_builder_endpoint_binding_is_refused(self):
        info_calls = 0
        def change_daemon(method, path, query=None):
            nonlocal info_calls
            if path == "/info":
                info_calls += 1
                return dict(self.info, ID="desktop-engine" if info_calls == 1 else "replacement-engine")
            return self.api(method, path, query)
        with patch.object(self.docker, "api", side_effect=change_daemon):
            with self.assertRaisesRegex(space.SpaceError, "identity changed"):
                self.docker.connect()
        self.assertEqual(info_calls, 2)

    def test_no_selected_builder_and_stopped_builder_are_refused(self):
        self.envelope["Current"] = False
        with self.assertRaises(space.SpaceError):
            self.docker.connect()
        self.envelope["Current"] = True
        self.builder["Nodes"][0]["Status"] = "stopped"
        with self.assertRaises(space.SpaceError):
            self.docker.connect()

    def test_docker_host_override_does_not_silently_change_target(self):
        os.environ["DOCKER_HOST"] = "unix:///other.sock"
        with self.assertRaises(space.SpaceError):
            self.docker.connect()

    def test_custom_builder_environment_is_refused(self):
        for key, value in (("BUILDX_BUILDER", "custom"), ("DOCKER_BUILDKIT", "0")):
            with self.subTest(key=key):
                os.environ[key] = value
                with self.assertRaises(space.SpaceError):
                    self.docker.connect()
                del os.environ[key]

    def test_old_engine_api_or_non_desktop_engine_is_refused(self):
        self.version = "1.47"
        with self.assertRaises(space.SpaceError):
            self.docker.connect()
        self.version = "1.49"
        self.info["OperatingSystem"] = "Ubuntu"
        with self.assertRaises(space.SpaceError):
            self.docker.connect()

    def test_inventory_includes_stopped_containers_and_inspects_references(self):
        responses = {
            "/images/json": [], "/containers/json": [dict(Id="stopped-container")],
            "/containers/stopped-container/json": dict(Id="stopped-container", Image=ident("a"), State=dict(Running=False)),
            "/system/df": dict(BuildCache=None, LayersSize=0),
        }
        with patch.object(self.docker, "api", side_effect=lambda method, path, query=None: responses[path]) as api:
            result = self.docker.inventory()
        self.assertEqual(result["containers"][0]["Image"], ident("a"))
        self.assertEqual(result["cache"], [])
        api.assert_any_call("GET", "/containers/json", dict(all="true"))
        api.assert_any_call("GET", "/images/json", dict(all="true", manifests="true"))

    def test_container_inventory_omits_environment_secrets_and_preserves_image_references(self):
        source = dict(
            Id="container-one", Image=ident("a"),
            ImageManifestDescriptor=dict(digest=ident("b")),
            Config=dict(Env=["PRIVATE_TOKEN=do-not-store-this"], Cmd=["private-command"]),
            HostConfig=dict(Mounts=[dict(Type="image", Source="dclaude@" + ident("c"), Target="/image-mount")],
                            Binds=["/private/host:/private/container"], RestartPolicy=dict(Name="always")),
            Mounts=[dict(Type="image", Source="dclaude@" + ident("c"), Destination="/image-mount")],
            State=dict(Running=False), NetworkSettings=dict(IPAddress="10.1.2.3"),
        )
        responses = {"/images/json": [], "/containers/json": [dict(Id="container-one")],
                     "/containers/container-one/json": source, "/system/df": dict(BuildCache=[], LayersSize=0)}
        with patch.object(self.docker, "api", side_effect=lambda method, path, query=None: responses[path]):
            result = self.docker.inventory()
        serialized = json.dumps(result)
        self.assertNotIn("PRIVATE_TOKEN", serialized)
        self.assertNotIn("do-not-store-this", serialized)
        self.assertNotIn("private-command", serialized)
        observed = result["containers"][0]
        self.assertNotIn("Config", observed)
        self.assertNotIn("NetworkSettings", observed)
        self.assertNotIn("Binds", observed["HostConfig"])
        self.assertEqual(observed["Id"], "container-one")
        self.assertEqual(observed["Image"], ident("a"))
        self.assertEqual(observed["ImageManifestDescriptor"]["digest"], ident("b"))
        self.assertEqual(observed["HostConfig"]["Mounts"][0]["Source"], "dclaude@" + ident("c"))
        self.assertEqual(observed["Mounts"][0]["Type"], "image")

    def test_missing_cache_inventory_fails_closed(self):
        responses = {"/images/json": [], "/containers/json": [], "/system/df": dict(LayersSize=0)}
        with patch.object(self.docker, "api", side_effect=lambda method, path, query=None: responses[path]):
            with self.assertRaises(space.SpaceError):
                self.docker.inventory()

    def test_independent_inventory_probes_overlap_and_omit_global_disk_accounting(self):
        # All three requests must start before any returns. This detects an
        # accidental return to serial probes without a wall-clock speed guess.
        started = threading.Barrier(3)
        images = [image("a", "0.0.1", Descriptor=dict(mediaType="application/vnd.oci.image.index.v1+json"),
                        Manifests=[dict(ID=ident("b"), Available=True, Kind="image")])]
        def respond(method, path, query=None):
            if path in ("/images/json", "/system/df", "/containers/json"):
                started.wait(timeout=3)
            if path == "/images/json":
                self.assertEqual(query, dict(all="true", manifests="true"))
                return images
            if path == "/system/df":
                self.assertEqual(query, dict(type="build-cache"))
                return dict(BuildCache=[cache("private")])
            if path == "/containers/json":
                self.assertEqual(query, dict(all="true"))
                return [dict(Id="stopped")]
            if path == "/containers/stopped/json":
                return dict(Id="stopped", Image=ident("b"), State=dict(Running=False))
            raise AssertionError((method, path, query))
        with patch.object(self.docker, "api", side_effect=respond):
            result = self.docker.inventory()
        self.assertEqual(result["images"][0]["Manifests"][0]["ID"], ident("b"))
        self.assertEqual(result["containers"][0]["Image"], ident("b"))
        self.assertEqual(result["cache"][0]["ID"], "private")
        self.assertEqual(set(result), {"images", "containers", "cache", "measured_at"})


class DockerDeadlineTests(unittest.TestCase):
    def setUp(self):
        self.docker = space.Docker()
        self.docker.path = "/docker.sock"
        self.connection = Mock()
        self.connection.getresponse.return_value.status = 200
        self.connection.getresponse.return_value.read.return_value = b"[]"

    def test_api_uses_earliest_shared_deadline_and_allows_more_than_thirty_seconds(self):
        for report, inventory_deadline, expected_timeout in ((155, 170, 55), (170, 145, 45), (120, 155, 20)):
            with self.subTest(report=report, inventory=inventory_deadline):
                self.docker.report_deadline = report
                self.docker.deadline = inventory_deadline
                with patch.object(space.time, "monotonic", return_value=100), \
                        patch.object(space, "UnixConnection", return_value=self.connection):
                    self.assertEqual(self.docker.api("GET", "/images/json"), [])
                self.assertEqual(self.connection.timeout, expected_timeout)

    def test_expired_report_or_inventory_budget_issues_no_request(self):
        for report, inventory_deadline in ((99, 155), (155, 100)):
            with self.subTest(report=report, inventory=inventory_deadline):
                self.docker.report_deadline = report
                self.docker.deadline = inventory_deadline
                with patch.object(space.time, "monotonic", return_value=100), \
                        patch.object(space, "UnixConnection", return_value=self.connection):
                    with self.assertRaises(space.SpaceError):
                        self.docker.api("GET", "/images/json")
                self.connection.request.assert_not_called()

    def test_slow_response_failure_is_not_retried_or_replaced_by_broader_probe(self):
        self.docker.report_deadline = 155
        self.connection.getresponse.side_effect = TimeoutError("read deadline")
        with patch.object(space.time, "monotonic", return_value=100), \
                patch.object(space, "UnixConnection", return_value=self.connection):
            with self.assertRaises(space.SpaceError):
                self.docker.api("GET", "/images/json", dict(all="true", manifests="true"))
        self.connection.request.assert_called_once_with("GET", "/images/json?all=true&manifests=true")
        self.connection.close.assert_called_once()


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

    def test_changed_followup_disk_identity_reports_unmeasured_recovery(self):
        def replace(count, result):
            if count > 1:
                result["disk_image"]["inode"] = 99
            return result
        self.host.hook = replace
        result = self.apply()
        self.assertEqual(self.docker.deleted, ["dclaude:0.0.1"])
        self.assertFalse(result["delta"]["measured"])
        self.assertIn("identity changed", result["delta"]["reason"])

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

    def test_api_error_after_partial_mutation_preserves_fresh_inventory(self):
        def mutate_then_fail(docker, target):
            docker.contents["images"] = [i for i in docker.contents["images"] if target not in i["RepoTags"]]
            raise space.SpaceError("connection dropped after image removal")
        self.docker.delete_hook = mutate_then_fail
        with self.assertRaises(space.SpaceError):
            self.apply()
        saved = self.receipt()
        observed = saved["steps"][0]["after_inventory"]["images"]
        self.assertNotIn(ident("a"), [i["Id"] for i in observed])
        self.assertEqual(saved["status"], "partial")
        self.assertEqual(self.docker.deleted, ["dclaude:0.0.1"])

    def test_target_error_is_durable_when_followup_probes_also_fail(self):
        self.docker.delete_hook = lambda *_: (_ for _ in ()).throw(space.SpaceError("delete failed"))
        self.docker.inventory_hooks[3] = lambda *_: (_ for _ in ()).throw(space.SpaceError("inventory failed"))
        self.host.hook = lambda count, result: (result if count == 1 else
                                                 (_ for _ in ()).throw(RuntimeError("host failed")))
        with self.assertRaises(space.SpaceError):
            self.apply()
        saved = self.receipt()
        self.assertEqual(saved["status"], "partial")
        self.assertEqual(saved["steps"][0]["status"], "error")
        self.assertIn("delete failed", saved["steps"][0]["error"])

    def test_cache_refusal_is_recorded_as_skipped_without_wider_prune(self):
        self.args.action = "cache"
        self.docker.contents["cache"] = [cache("a")]
        with patch.object(self.docker, "delete_cache", return_value=dict(CachesDeleted=None, SpaceReclaimed=0)) as delete:
            self.apply()
        delete.assert_called_once_with("a")
        self.assertEqual(self.receipt()["steps"][0]["status"], "skipped")

    def test_unrelated_image_addition_does_not_block_reviewed_image(self):
        self.docker.inventory_hooks[2] = lambda d: d.contents["images"].append(image("b", RepoTags=["other:project"]))
        result = self.apply()
        self.assertEqual(result["status"], "completed")
        self.assertEqual(self.docker.deleted, ["dclaude:0.0.1"])

    def test_unrelated_cache_addition_does_not_block_reviewed_image(self):
        self.docker.inventory_hooks[2] = lambda d: d.contents["cache"].append(cache("other-project"))
        result = self.apply()
        self.assertEqual(result["status"], "completed")
        self.assertEqual(self.docker.deleted, ["dclaude:0.0.1"])

    def test_unrelated_image_addition_does_not_block_reviewed_cache(self):
        self.args.action = "cache"
        self.docker.contents["cache"] = [cache("approved")]
        self.docker.inventory_hooks[2] = lambda d: d.contents["images"].append(image("b", RepoTags=["other:project"]))
        result = self.apply()
        self.assertEqual(result["status"], "completed")
        self.assertEqual(self.docker.deleted, ["approved"])

    def test_unrelated_cache_addition_does_not_block_reviewed_cache(self):
        self.args.action = "cache"
        self.docker.contents["cache"] = [cache("approved")]
        self.docker.inventory_hooks[2] = lambda d: d.contents["cache"].append(cache("other-project"))
        result = self.apply()
        self.assertEqual(result["status"], "completed")
        self.assertEqual(self.docker.deleted, ["approved"])

    def test_reordered_images_tags_manifests_and_container_mounts_allow_reviewed_cleanup(self):
        self.docker.contents["images"][0]["RepoTags"].append("dclaude:0.0.2")
        self.docker.contents["images"][0]["Manifests"] = [
            dict(ID=ident("b"), Available=True, Kind="image"),
            dict(ID=ident("c"), Available=True, Kind="attestation", AttestationData=dict(For=ident("b"))),
        ]
        mounts = [dict(Type="bind", Source="/repo"), dict(Type="volume", Source="auth")]
        self.docker.contents["containers"] = [
            dict(Id="first", Image=ident("f"), HostConfig=dict(Mounts=mounts), Mounts=copy.deepcopy(mounts)),
            dict(Id="second", Image=ident("f")),
        ]
        self.docker.contents["cache"] = [cache("cache-a"), cache("cache-b")]
        self.docker.inventory_hooks[2] = lambda docker: setattr(docker, "contents", reverse_inventory_lists(docker.contents))
        result = self.apply()
        self.assertEqual(result["status"], "completed")
        self.assertEqual(self.docker.deleted, ["dclaude:0.0.1", "dclaude:0.0.2"])

    def test_reordered_cache_parent_relationships_allow_only_reviewed_cache_cleanup(self):
        self.args.action = "cache"
        self.docker.contents["cache"] = [cache("child", Parents=["parent-a", "parent-b"]),
                                         cache("parent-a", Shared=True), cache("parent-b", Shared=True)]
        self.docker.inventory_hooks[2] = lambda docker: setattr(docker, "contents", reverse_inventory_lists(docker.contents))
        result = self.apply()
        self.assertEqual(result["status"], "completed")
        self.assertEqual(self.docker.deleted, ["child"])

    def test_reordered_cache_with_changed_parent_reference_still_stops_before_delete(self):
        self.args.action = "cache"
        self.docker.contents["cache"] = [cache("child", Parents=["parent-a", "parent-b"]),
                                         cache("parent-a", Shared=True), cache("parent-b", Shared=True)]
        def change_reference(docker):
            docker.contents = reverse_inventory_lists(docker.contents)
            next(record for record in docker.contents["cache"] if record["ID"] == "child")["Parents"].append("new-parent")
        self.docker.inventory_hooks[2] = change_reference
        with self.assertRaises(space.SpaceError):
            self.apply()
        self.assertEqual(self.docker.deleted, [])

    def test_changed_image_family_members_stop_before_delete(self):
        self.docker.contents["images"][0]["Manifests"] = [
            dict(ID=ident("b"), Available=True, Kind="image")]
        self.docker.inventory_hooks[2] = lambda d: d.contents["images"][0]["Manifests"].append(
            dict(ID=ident("c"), Available=True, Kind="attestation", AttestationData=dict(For=ident("b"))))
        with self.assertRaises(space.SpaceError):
            self.apply()
        self.assertEqual(self.docker.deleted, [])

    def test_cache_presentation_changes_do_not_block_eligible_target(self):
        self.args.action = "cache"
        self.docker.contents["cache"] = [cache("a")]
        self.docker.inventory_hooks[2] = lambda d: d.contents["cache"][0].update(
            Size=200, Description="updated display text", LastUsedAt="2026-02-02T00:00:00Z")
        result = self.apply()
        self.assertEqual(result["status"], "completed")
        self.assertEqual(self.docker.deleted, ["a"])

    def test_cache_shared_after_confirmation_is_never_deleted(self):
        self.args.action = "cache"
        self.docker.contents["cache"] = [cache("a")]
        self.docker.inventory_hooks[2] = lambda d: d.contents["cache"][0].update(Shared=True)
        with self.assertRaises(space.SpaceError):
            self.apply()
        self.assertEqual(self.docker.deleted, [])

    def test_cache_in_use_after_confirmation_is_never_deleted(self):
        self.args.action = "cache"
        self.docker.contents["cache"] = [cache("a")]
        self.docker.inventory_hooks[2] = lambda d: d.contents["cache"][0].update(InUse=True)
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
        self.assertEqual(self.receipt()["steps"][0]["status"], "error")

    def test_host_measurement_count_is_constant_across_many_targets(self):
        self.docker.contents["images"].insert(1, image("b", "0.0.2", created=2))
        result = self.apply()
        self.assertEqual(result["status"], "completed")
        self.assertEqual(self.docker.deleted, ["dclaude:0.0.1", "dclaude:0.0.2"])
        self.assertEqual(self.host.count, 2)

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

    def test_owner_write_failure_removes_new_lock_and_propagates(self):
        failure = OSError("disk full")
        def leave_partial_owner(path, *_args, **_kwargs):
            path.write_bytes(b"partial owner")
            raise failure
        with patch.object(Path, "write_text", autospec=True, side_effect=leave_partial_owner):
            with self.assertRaises(OSError) as caught:
                with space.operation_lock(self.directory):
                    self.fail("owner write failure yielded the lock")
        self.assertIs(caught.exception, failure)
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

    def test_boolean_schema_version_is_not_version_one(self):
        self.directory.mkdir()
        path = self.directory / "policy.json"
        path.write_text('{"schema_version": true}')
        with self.assertRaises(space.SpaceError):
            space.read_json(path)


class CommandTests(SpaceFixture):
    def run_main(self, *args):
        with patch.object(space, "Docker", return_value=self.docker), patch.object(space, "HostProbe", return_value=self.host), \
                patch.object(space, "state_dir", return_value=self.directory), contextlib.redirect_stdout(self.output), \
                contextlib.redirect_stderr(self.output), \
                patch.object(space.platform, "system", return_value=getattr(self, "platform", "Darwin")):
            return space.main(["--current-image", "dclaude:0.0.9", *args])

    def test_missing_current_with_container_protections_is_successful_noop(self):
        self.docker.contents = inventory(
            [image("a", "0.1.81"), image("b", "0.1.76")],
            [dict(Image=ident("a")), dict(Image=ident("b"), State=dict(Running=False))])
        self.assertEqual(self.run_main("images", "--apply"), 0)
        output = self.output.getvalue()
        self.assertIn("Nothing to remove. Both dclaude images are used by containers.", output)
        self.assertIn("This checkout expects dclaude:0.0.9, which is not built.", output)
        self.assertNotIn("\nIssues", output)
        self.assertEqual(output.count("\nNext\n"), 1)
        self.confirm_mock.assert_not_called()
        self.assertEqual(self.docker.deleted, [])
        self.assertFalse((self.directory / "latest.json").exists())

    def test_missing_current_still_allows_confirmed_eligible_deletion(self):
        self.docker.contents = inventory([
            image("a", "0.0.1"), image("b", "0.0.2", created=2),
            image("c", "0.0.3", created=3)])
        self.assertEqual(self.run_main("images", "--keep", "2", "--apply"), 0)
        self.confirm_mock.assert_called_once()
        self.assertEqual(self.docker.deleted, ["dclaude:0.0.1"])
        self.assertTrue((self.directory / "latest.json").exists())

    def test_missing_current_does_not_bypass_unknown_container_reference(self):
        self.docker.contents = inventory([image("a", "0.0.1")], [dict(Image=ident("b"))])
        self.assertEqual(self.run_main("images", "--apply"), 2)
        self.assertIn("\nIssues", self.output.getvalue())
        self.confirm_mock.assert_not_called()
        self.assertEqual(self.docker.deleted, [])

    def test_empty_inventory_needs_no_host_baseline_or_confirmation(self):
        self.docker.contents = inventory([])
        self.host.hook = lambda count, result: measurement(complete=False)
        self.assertEqual(self.run_main("images", "--apply"), 0)
        self.assertIn("Nothing to remove.", self.output.getvalue())
        self.confirm_mock.assert_not_called()
        self.assertEqual(self.docker.deleted, [])
        self.assertFalse((self.directory / "latest.json").exists())

    def test_missing_current_json_separates_notes_from_blockers(self):
        self.docker.contents = inventory([])
        self.assertEqual(self.run_main("images", "--json"), 0)
        report = json.loads(self.output.getvalue())
        self.assertEqual(report["plan"]["issues"], [])
        self.assertEqual(report["plan"]["notes"], ["This checkout expects dclaude:0.0.9, which is not built."])
        self.assertFalse(self.directory.exists())

    def test_json_preview_is_readonly_and_machine_parseable(self):
        self.assertEqual(self.run_main("--json"), 0)
        report = json.loads(self.output.getvalue())
        self.assertEqual(report["schema_version"], 1)
        self.assertIn("cache_plan", report)
        self.assertEqual(self.docker.deleted, [])
        self.assertFalse(self.directory.exists())

    def test_abbreviated_hidden_flags_cannot_bypass_wrapper_rejection(self):
        for args in (("--current-im", "other:tag"), ("--wrapp", "other"), ("--auto-ret",)):
            with self.subTest(args=args), contextlib.redirect_stderr(io.StringIO()):
                with self.assertRaises(SystemExit) as caught:
                    space.parser().parse_args(list(args))
                self.assertEqual(caught.exception.code, 2)

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
        self.assertEqual(self.run_main("verify", "--json"), 0)
        self.assertTrue(json.loads(self.output.getvalue())["delta"]["measured"])
        self.assertEqual(self.docker.deleted, [])

    def test_verify_rejects_receipt_outside_own_directory(self):
        space.write_json(self.directory / "latest.json", dict(schema_version=1, receipt="/tmp/untrusted.json"))
        self.assertEqual(self.run_main("verify"), 2)
        self.assertIn("Invalid receipt location", self.output.getvalue())

    def test_retention_is_disabled_until_a_policy_is_saved(self):
        self.assertEqual(self.run_main("retention", "status", "--json"), 0)
        status = json.loads(self.output.getvalue())
        self.assertEqual((status["enabled"], status["keep"], status["saved"]), (False, 2, False))
        self.assertFalse((self.directory / "policy.json").exists())
        self.output = io.StringIO()
        self.assertEqual(self.run_main("retention", "status"), 0)
        self.assertIn("Disabled (not configured)", self.output.getvalue())
        self.assertEqual(self.docker.connect_count, 0)

    def test_retention_disable_without_a_saved_policy_persists_and_stops_automatic_runs(self):
        self.assertEqual(self.run_main("retention", "disable"), 0)
        policy = space.load_policy(self.directory)
        self.assertEqual((policy["enabled"], policy["keep"], policy["repository"]), (False, 2, "dclaude"))
        self.docker.contents = inventory([image("a", "0.0.1"), image("b", "0.0.2", created=2), image("f", "0.0.9", created=9)])
        (self.directory / "pending-build").write_text(ident("f") + "\n")
        self.assertEqual(self.run_main("--auto-retain"), 0)
        self.assertEqual(self.docker.deleted, [])
        self.assertEqual(self.docker.connect_count, 0)
        self.assertTrue((self.directory / "pending-build").exists())
        self.assertEqual(self.run_main("retention", "enable"), 0)
        self.assertTrue(space.load_policy(self.directory)["enabled"])

    def test_missing_policy_never_connects_or_deletes_automatically(self):
        self.docker.contents = inventory([image("a", "0.0.1"), image("b", "0.0.2", created=2), image("f", "0.0.9", created=9)])
        self.directory.mkdir()
        (self.directory / "pending-build").write_text(ident("f") + "\n")
        self.assertEqual(self.run_main("--auto-retain"), 0)
        self.assertEqual(self.docker.connect_count, 0)
        self.assertEqual(self.docker.deleted, [])
        self.assertTrue((self.directory / "pending-build").exists())
        self.assertFalse((self.directory / "policy.json").exists())

    def test_enabled_auto_retention_reports_removed_images(self):
        self.docker.contents = inventory([image("a", "0.0.1"), image("b", "0.0.2", created=2), image("f", "0.0.9", created=9)])
        self.policy(keep=2)
        (self.directory / "pending-build").write_text(ident("f") + "\n")
        self.assertEqual(self.run_main("--auto-retain"), 0)
        self.assertEqual(self.docker.deleted, ["dclaude:0.0.1"])
        self.confirm_mock.assert_not_called()
        self.assertFalse((self.directory / "pending-build").exists())
        output = self.output.getvalue()
        self.assertIn("Retention removed 1 old dclaude build: dclaude:0.0.1", output)
        self.assertIn("receipt", output)
        self.assertEqual(self.receipt()["authority"], "enabled retention policy")

    def test_automatic_retention_rejects_a_different_saved_binding(self):
        self.policy(binding=dict(self.docker.binding, daemon_id="reinstalled-desktop"))
        (self.directory / "pending-build").write_text(ident("f") + "\n")
        self.assertEqual(self.run_main("--auto-retain"), 2)
        self.assertEqual(self.docker.deleted, [])
        self.assertTrue((self.directory / "pending-build").exists())
        self.assertIn("different Docker binding", self.output.getvalue())

    def test_automatic_retention_requires_the_pending_build_to_match_current_image(self):
        self.policy()
        (self.directory / "pending-build").write_text(ident("b") + "\n")
        self.assertEqual(self.run_main("--auto-retain"), 2)
        self.assertEqual(self.docker.deleted, [])
        self.assertTrue((self.directory / "pending-build").exists())
        self.assertIn("does not match", self.output.getvalue())

    def test_unknown_policy_schema_fails_closed_before_connecting(self):
        self.directory.mkdir()
        (self.directory / "policy.json").write_text('{"schema_version": 2, "enabled": true}\n')
        (self.directory / "pending-build").write_text(ident("f") + "\n")
        self.assertEqual(self.run_main("--auto-retain"), 2)
        self.assertEqual(self.docker.connect_count, 0)
        self.assertEqual(self.docker.deleted, [])

    def test_enabled_policy_without_pending_build_never_connects(self):
        self.policy()
        self.assertEqual(self.run_main("--auto-retain"), 0)
        self.assertEqual(self.docker.connect_count, 0)
        self.assertEqual(self.docker.deleted, [])

    def test_auto_retention_is_a_silent_noop_off_macos(self):
        self.platform = "Linux"
        self.policy()
        (self.directory / "pending-build").write_text(ident("f") + "\n")
        self.assertEqual(self.run_main("--auto-retain"), 0)
        self.assertEqual(self.output.getvalue(), "")
        self.assertEqual(self.docker.connect_count, 0)
        self.assertFalse((self.directory / "pending-build").exists())

    def test_auto_retention_leaves_all_build_cache_untouched(self):
        records = [cache("child-a", Shared=True, Parents=["held-a"]), cache("held-a", Shared=True),
                   cache("held-f", Shared=True), cache("private-x"), cache("busy-a", Shared=True, InUse=True)]
        self.docker.contents = inventory(records=records)
        self.docker.held = {ident("a"): ["child-a", "held-a", "busy-a"], ident("f"): ["held-f"]}
        self.policy()
        (self.directory / "pending-build").write_text(ident("f") + "\n")
        self.assertEqual(self.run_main("--auto-retain"), 0)
        self.assertEqual(self.docker.deleted, ["dclaude:0.0.1"])
        self.assertEqual({record["ID"] for record in self.docker.contents["cache"]},
                         {"child-a", "held-a", "held-f", "private-x", "busy-a"})
        receipt = self.receipt()
        self.assertEqual(receipt["action"], "images")
        self.assertEqual(len(list((self.directory / "receipts").glob("*.json"))), 1)
        self.assertFalse((self.directory / "pending-build").exists())
        output = self.output.getvalue()
        self.assertIn("Retention removed 1 old dclaude build: dclaude:0.0.1", output)
        self.assertNotIn("Retention cleared", output)

    def test_auto_retention_consumes_pending_only_after_success(self):
        self.policy()
        (self.directory / "pending-build").write_text(ident("f") + "\n")
        self.assertEqual(self.run_main("--auto-retain"), 0)
        self.assertEqual(self.docker.deleted, ["dclaude:0.0.1"])
        self.confirm_mock.assert_not_called()
        self.assertFalse((self.directory / "pending-build").exists())

    def test_failed_auto_retention_keeps_pending_for_later_review(self):
        self.policy()
        (self.directory / "pending-build").write_text(ident("f") + "\n")
        self.docker.delete_hook = lambda *_: (_ for _ in ()).throw(space.SpaceError("busy image"))
        self.assertEqual(self.run_main("--auto-retain"), 2)
        self.assertTrue((self.directory / "pending-build").exists())
        self.assertEqual(self.receipt()["status"], "partial")

    def test_noop_auto_retention_consumes_pending_after_protection_checks(self):
        self.policy(keep=2)
        (self.directory / "pending-build").write_text(ident("f") + "\n")
        self.assertEqual(self.run_main("--auto-retain"), 0)
        self.assertEqual(self.docker.deleted, [])
        self.assertFalse((self.directory / "pending-build").exists())

    def test_json_retention_status_is_one_json_document(self):
        self.policy()
        self.assertEqual(self.run_main("retention", "status", "--json"), 0)
        self.assertTrue(json.loads(self.output.getvalue())["enabled"])

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
