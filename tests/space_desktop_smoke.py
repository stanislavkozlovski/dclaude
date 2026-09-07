#!/usr/bin/env python3
"""Destructive integration proof, exclusively on a fresh hosted Desktop runner.

This does not use mocked Docker, APFS, or allocation measurements. The runner
must opt in and its daemon must initially have no images or containers.
"""

import importlib.util
import io
import json
import os
from pathlib import Path
import platform
import pty
import select
import subprocess
import sys
import tarfile
import tempfile
import time
from concurrent.futures import ThreadPoolExecutor


ROOT = Path(__file__).resolve().parents[1]
MIB = 1024 * 1024
CURRENT = "dclaude:90.0.6"
EVIDENCE = {}


def save_evidence():
    destination = Path(os.environ.get("RUNNER_TEMP", tempfile.gettempdir())) / "space-desktop-evidence.json"
    destination.write_text(json.dumps(EVIDENCE, indent=2, sort_keys=True) + "\n")
    return destination


def phase(message):
    EVIDENCE.setdefault("phases", []).append({"message": message, "time": time.time()})
    save_evidence()
    print(f"Desktop proof: {message}", flush=True)


def command(*args, timeout=180):
    result = subprocess.run(args, text=True, stdout=subprocess.PIPE,
                            stderr=subprocess.STDOUT, timeout=timeout)
    if result.returncode:
        raise AssertionError(f"Command failed ({result.returncode}): {args}\n{result.stdout}")
    return result.stdout


def space(*args, confirm=False, timeout=600):
    argv = [sys.executable, str(ROOT / "scripts/space.py"),
            "--current-image", CURRENT, "--tool-home", str(ROOT),
            "--wrapper", "dclaude", *args]
    if not confirm:
        return command(*argv, timeout=timeout)
    # A real terminal provides the same input contract as an interactive user.
    master, slave = pty.openpty()
    process = subprocess.Popen(argv, stdin=slave, stdout=subprocess.PIPE,
                               stderr=subprocess.STDOUT)
    os.close(slave)
    chunks = bytearray()
    answered = False
    deadline = time.monotonic() + timeout
    try:
        while True:
            if time.monotonic() >= deadline:
                raise TimeoutError(f"Storage command exceeded {timeout}s: {args}")
            if select.select([process.stdout], [], [], 1)[0]:
                block = os.read(process.stdout.fileno(), 65536)
                if not block:
                    break
                chunks.extend(block)
                if not answered and b"Type yes:" in chunks:
                    os.write(master, b"yes\n")
                    answered = True
            elif process.poll() is not None:
                chunks.extend(process.stdout.read())
                break
        process.wait(timeout=10)
    except BaseException:
        process.kill()
        process.communicate()
        raise
    finally:
        os.close(master)
        process.stdout.close()
    output = chunks.decode(errors="replace")
    print(output, flush=True)
    if process.returncode:
        raise AssertionError(f"Storage command failed ({process.returncode}): {args}")
    return output


def diagnosis(action="images"):
    started = time.monotonic()
    report = json.loads(space(action, "--json"))
    duration = time.monotonic() - started
    EVIDENCE.setdefault("diagnosis_reports", []).append({
        "action": action, "seconds": duration, "report": report,
    })
    assert "error" not in report, report
    assert duration < 60, f"Diagnosis exceeded one minute: {duration:.2f}s"
    assert report["host"]["complete"], report["host"]
    assert not report["plan"]["issues"], report["plan"]["issues"]
    EVIDENCE.setdefault("diagnosis_seconds", []).append(duration)
    return report


def image_id(tag):
    return command("docker", "image", "inspect", "--format", "{{.Id}}", tag).strip()


def latest_receipt():
    directory = Path.home() / ".local/state/dclaude/space"
    latest = json.loads((directory / "latest.json").read_text())
    receipt = json.loads(Path(latest["receipt"]).read_text())
    assert receipt["status"] == "completed", receipt
    return receipt


def make_image(context, release, size_mib, *, labelled=True, tagged=True):
    # Fresh, incompressible bytes ensure real reclaimable blocks, not a sparse
    # zero fixture that passes Docker accounting while occupying little APFS.
    for filename in ("payload-a", "payload-b"):
        with (context / filename).open("wb") as stream:
            for _ in range(size_mib // 2):
                stream.write(os.urandom(MIB))
    (context / "Dockerfile").write_text(
        "FROM scratch\nCOPY payload-a /payload-a\nCOPY payload-b /payload-b\n")
    tag = "dclaude:" + release
    argv = ["docker", "build", "--no-cache"]
    iid_file = context / "untagged-image-id"
    if tagged:
        argv.extend(["-t", tag])
    else:
        argv.extend(["--iidfile", str(iid_file)])
    if labelled:
        argv.extend(["--label", "com.dclaude.managed=true",
                     "--label", "com.dclaude.release=" + release])
    argv.append(str(context))
    output = command(*argv, timeout=300)
    print(f"Built {tag if tagged else 'labelled image without a tag'}: {size_mib} MiB\n{output[-500:]}", flush=True)
    # Docker's list API uses seconds; avoid ambiguous creation-order fixtures.
    time.sleep(1.1)
    return image_id(tag if tagged else iid_file.read_text().strip())


def load_helper():
    spec = importlib.util.spec_from_file_location("space_desktop_under_test", ROOT / "scripts/space.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def cache_map(inventory):
    return {record["ID"]: record for record in inventory["cache"]}


def prove_exact_cache_selection(helper, report):
    """Delete one approved leaf; prove other public records/parents survive."""
    candidates = report["plan"]["candidates"]
    known = cache_map(report["inventory"])
    parent_ids = {parent for candidate in candidates for parent in candidate["parents"]}
    leaves = [candidate for candidate in candidates
              if candidate["id"] not in parent_ids
              and any(parent in known for parent in candidate["parents"])]
    assert leaves, "Fixture did not produce a reclaimable cache leaf with a visible parent"
    selected = max(leaves, key=lambda candidate: candidate["size_bytes"])
    engine = helper.Docker()
    engine.connect()
    before = cache_map(engine.inventory())
    assert selected["id"] in before
    assert not before[selected["id"]]["Shared"] and not before[selected["id"]]["InUse"]
    response = engine.delete_cache(selected["id"])
    after = cache_map(engine.inventory())
    removed = set(before) - set(after)
    assert removed == {selected["id"]}, (selected, response, removed)
    assert set(response.get("CachesDeleted") or []) == removed, response
    assert all(parent in after for parent in selected["parents"] if parent in before)
    assert all(ident in after for ident, record in before.items()
               if record["Shared"] or record["InUse"])
    EVIDENCE["exact_cache_selection"] = {
        "selected": selected, "response": response,
        "public_records_removed": sorted(removed),
        "other_public_records_preserved": len(after),
        "parents_preserved": [parent for parent in selected["parents"] if parent in before],
    }


def prove_incident_scale():
    """Time a real inventory with hundreds of unrelated immutable identities."""
    phase("creating 280 unrelated images for the one-minute diagnosis test")
    tar_buffer = io.BytesIO()
    with tarfile.open(fileobj=tar_buffer, mode="w"):
        pass
    empty_tar = tar_buffer.getvalue()
    tags = [f"space-unrelated:fixture-{index:03d}" for index in range(280)]

    def import_image(tag):
        result = subprocess.run(
            ["docker", "image", "import", "--change", "LABEL space.fixture=" + tag, "-", tag],
            input=empty_tar, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, timeout=60)
        assert result.returncode == 0, (tag, result.stdout.decode(errors="replace"))
        return result.stdout.decode().strip()

    with ThreadPoolExecutor(max_workers=4) as executor:
        imported_ids = set(executor.map(import_image, tags))
    assert len(imported_ids) == len(tags), "Fixture imports must have distinct image identities"
    phase("280 unrelated images imported; creating 20 additional stopped containers")
    for index in range(20):
        command("docker", "create", "--name", f"space-proof-scale-{index:02d}", CURRENT, "/not-executed")
    phase("timing complete inventory of more than 280 images and 21 stopped containers")
    report = diagnosis()
    actual_images = {image["Id"] for image in report["inventory"]["images"]}
    assert imported_ids <= actual_images, "The inventory omitted unrelated imported images"
    assert len(report["inventory"]["containers"]) >= 21, "The inventory omitted stopped containers"
    assert not report["plan"]["candidates"], report["plan"]["candidates"]
    unrelated_families = [family for family in report["plan"]["families"] if family["id"] in imported_ids]
    assert len(unrelated_families) == len(imported_ids)
    assert all(family["reasons"] and not family["owned"] for family in unrelated_families)
    EVIDENCE["incident_scale"] = {
        "imported_unrelated_images": len(imported_ids),
        "distinct_inventory_images": len(actual_images),
        "stopped_containers": len(report["inventory"]["containers"]),
        "diagnosis_seconds": EVIDENCE["diagnosis_seconds"][-1],
        "unrelated_images_protected": len(unrelated_families),
        "image_cleanup_candidates": len(report["plan"]["candidates"]),
    }
    print(json.dumps({"incident_scale": EVIDENCE["incident_scale"]}, indent=2), flush=True)


def main():
    assert platform.system() == "Darwin", "This proof requires actual macOS"
    assert os.environ.get("GITHUB_ACTIONS") == "true", "Use a fresh GitHub-hosted runner"
    assert os.environ.get("DCLAUDE_SPACE_DISPOSABLE_TEST") == "1", "Disposable test opt-in is required"
    assert os.environ.get("RUNNER_ENVIRONMENT") == "github-hosted", "A disposable hosted runner is required"
    assert not command("docker", "ps", "-aq").strip(), "Refusing a daemon with existing containers"
    assert not command("docker", "image", "ls", "-aq").strip(), "Refusing a daemon with existing images"
    state = Path.home() / ".local/state/dclaude/space"
    assert not state.exists(), "Refusing existing storage policy/receipt state"
    EVIDENCE["versions"] = {
        "macos": command("sw_vers"), "docker": json.loads(command("docker", "version", "--format", "{{json .}}")),
        "buildx": command("docker", "buildx", "version"),
        "desktop": command("defaults", "read", "/Applications/Docker.app/Contents/Info", "CFBundleShortVersionString"),
    }
    selected_context = command("docker", "context", "show").strip()
    raw_info = json.loads(command("docker", "info", "--format", "{{json .}}"))
    EVIDENCE["connection_diagnostics"] = {
        "context": selected_context,
        "contexts": json.loads(command("docker", "context", "inspect", selected_context, "default")),
        "builders_json": command("docker", "buildx", "ls", "--format", "{{json .}}"),
        "builders_selection": command("docker", "buildx", "ls", "--format",
                                      '{"Current":{{json .Builder.Current}},"Builder":{{json .Builder}}}'),
        "engine": {key: raw_info.get(key) for key in (
            "ID", "Name", "OperatingSystem", "OSType", "ServerVersion", "Driver", "DriverStatus")},
    }
    print(json.dumps({"connection_diagnostics": EVIDENCE["connection_diagnostics"]}, indent=2), flush=True)
    helper = load_helper()
    engine = helper.Docker()
    EVIDENCE["preflight_binding"] = engine.connect()
    EVIDENCE["preflight_host"] = helper.HostProbe().snapshot()
    assert EVIDENCE["preflight_host"]["complete"], EVIDENCE["preflight_host"]
    phase("Desktop binding and APFS preflight passed; building cleanup fixtures")
    with tempfile.TemporaryDirectory(prefix="dclaude-space-fixture-") as temporary:
        context = Path(temporary)
        # An unnamed export creates a genuine labelled dangling reference even
        # on stores whose named export replaces old references immediately.
        dangling = make_image(context, "90.0.0", 2, tagged=False)
        rebuilt = make_image(context, "90.0.1", 128)
        old_one = make_image(context, "90.0.1", 64)
        old_two = make_image(context, "90.0.2", 64)
        command("docker", "tag", "dclaude:90.0.2", "dclaude:90.0.20")
        stopped = make_image(context, "90.0.3", 8)
        container_id = command("docker", "create", "--name", "space-proof-stopped",
                               "dclaude:90.0.3", "/not-executed").strip()
        aliased = make_image(context, "90.0.4", 8)
        command("docker", "tag", "dclaude:90.0.4", "another-project:preserve")
        recent = make_image(context, "90.0.5", 8)
        current = make_image(context, "90.0.6", 8)

        EVIDENCE["raw_fixture_inventory"] = engine.inventory()
        phase("cleanup fixtures built; verifying exact image candidates and protections")
        expected_candidates = {dangling, old_one, old_two}
        listed_ids = {image["Id"] for image in EVIDENCE["raw_fixture_inventory"]["images"]}
        if rebuilt in listed_ids:
            expected_candidates.add(rebuilt)
            EVIDENCE["rebuilt_tag_disposition"] = {"id": rebuilt, "disposition": "retained_as_dangling_image"}
        else:
            # A missing row alone does not prove absence: inspect must also
            # report 404, or this is an inventory bug and the test must fail.
            probe = helper.UnixConnection(engine.path)
            try:
                probe.request("GET", f"/v{engine.version}/images/{rebuilt}/json")
                response = probe.getresponse()
                body = response.read().decode(errors="replace")
                EVIDENCE["rebuilt_tag_disposition"] = {
                    "id": rebuilt, "disposition": "engine_already_retired_reference",
                    "inspect_http_status": response.status, "inspect_response": body,
                }
                assert response.status == 404, EVIDENCE["rebuilt_tag_disposition"]
            finally:
                probe.close()
        initial = diagnosis()
        candidates = {candidate["id"] for candidate in initial["plan"]["candidates"]}
        assert candidates == expected_candidates, initial["plan"]
        dangling_candidate = next(candidate for candidate in initial["plan"]["candidates"] if candidate["id"] == dangling)
        assert dangling_candidate["targets"] == [dangling] and not dangling_candidate["legacy"], dangling_candidate
        alias_candidate = next(candidate for candidate in initial["plan"]["candidates"] if candidate["id"] == old_two)
        assert set(alias_candidate["targets"]) == {"dclaude:90.0.2", "dclaude:90.0.20"}
        assert initial["host"]["disk_image"]["allocated_bytes"] >= 256 * MIB
        EVIDENCE["image_preview"] = initial
        EVIDENCE["stopped_container"] = json.loads(command("docker", "inspect", container_id))[0]
        baseline = initial["host"]

        stopped_family = next(family for family in initial["plan"]["families"] if family["id"] == stopped)
        assert stopped_family["reasons"], stopped_family
        store = initial["binding"].get("driver_status") or []
        if any("containerd" in str(value) for value in store):
            # Modern Desktop's default store exposes actual child manifests.
            assert len(stopped_family["members"]) >= 2, stopped_family
            mounted_id = command("docker", "create", "--name", "space-proof-image-mount",
                                 "--mount", "type=image,source=dclaude:90.0.3,target=/mounted",
                                 CURRENT, "/not-executed").strip()
            mounted_report = json.loads(space("images", "--json"))
            assert any("image mount" in issue for issue in mounted_report["plan"]["issues"]), mounted_report
            assert not mounted_report["plan"]["candidates"], mounted_report
            EVIDENCE["unresolved_image_mount_refused"] = mounted_report["plan"]["issues"]
            # Remove only the explicitly created test container to continue.
            command("docker", "rm", mounted_id)

        space("images", "--keep", "2", "--apply", confirm=True)
        image_receipt = latest_receipt()
        EVIDENCE["image_receipt"] = image_receipt
        phase("image cleanup completed and receipt persisted")
        assert {candidate["id"] for candidate in image_receipt["reviewed"]} == candidates
        after_images = diagnosis()
        present = {image["Id"] for image in after_images["inventory"]["images"]}
        assert not present.intersection(candidates), (candidates, present)
        protected_tags = {"dclaude:90.0.3": stopped, "dclaude:90.0.4": aliased,
                          "another-project:preserve": aliased, "dclaude:90.0.5": recent, CURRENT: current}
        for tag, ident in protected_tags.items():
            assert image_id(tag) == ident, f"Protected alias changed: {tag}"
        assert command("docker", "inspect", "--format", "{{.Image}}", container_id).strip() == stopped
        assert command("docker", "inspect", "--format", "{{.State.Status}}", container_id).strip() == "created"

        cache_preview = diagnosis("cache")
        assert cache_preview["plan"]["candidates"], "Image fixture failed to leave retained private cache"
        assert cache_preview["plan"]["protected"], "Fixture failed to retain shared cache beside targets"
        EVIDENCE["cache_preview_after_images"] = cache_preview
        prove_exact_cache_selection(helper, cache_preview)
        phase("exact cache leaf selection and parent preservation passed; reviewing remaining cache")
        space("cache", "--apply", confirm=True)
        cache_receipt = latest_receipt()
        EVIDENCE["cache_receipt"] = cache_receipt
        phase("cache cleanup completed and receipt persisted")
        assert cache_receipt["action"] == "cache"
        assert cache_receipt["started_at"] != image_receipt["started_at"]
        after_cache = diagnosis("cache")
        for tag, ident in protected_tags.items():
            assert image_id(tag) == ident
        assert command("docker", "inspect", "--format", "{{.Image}}", container_id).strip() == stopped

        verification = json.loads(space("verify"))
        assert verification["delta"]["measured"], verification
        EVIDENCE["verification"] = verification
        # Receipts may observe concurrent host writes. Require an actual sparse
        # file reduction as well as recording signed APFS deltas, not merely a
        # positive counter anywhere on the Mac.
        delta = helper.host_delta(baseline, after_cache["host"])
        deadline = time.monotonic() + 60
        while delta.get("raw_allocated_bytes_reduction", 0) < 64 * MIB and time.monotonic() < deadline:
            time.sleep(2)
            final_host = helper.HostProbe().snapshot(Path(baseline["disk_image"]["path"]))
            delta = helper.host_delta(baseline, final_host)
        EVIDENCE["whole_cleanup_delta"] = delta
        assert delta["measured"] and delta["raw_allocated_bytes_reduction"] >= 64 * MIB, delta

        space("retention", "enable", "--keep", "2", confirm=True)
        policy = json.loads((state / "policy.json").read_text())
        assert policy["enabled"] and policy["keep"] == 2 and policy["repository"] == "dclaude", policy
        assert "true" in space("retention", "status")
        space("retention", "disable")
        disabled = json.loads((state / "policy.json").read_text())
        assert disabled["enabled"] is False
        EVIDENCE["retention_policy_disabled"] = disabled
        EVIDENCE["cleanup_checks_passed"] = True
        phase("cleanup, protected objects, host recovery, verification, and retention checks passed")
        print(json.dumps({"whole_cleanup_delta": delta}, indent=2), flush=True)
        prove_incident_scale()
        EVIDENCE["passed"] = True
        phase("all Desktop checks passed, including incident-scale diagnosis")
        print(json.dumps({"passed": True, "whole_cleanup_delta": delta,
                          "exact_cache_selection": EVIDENCE["exact_cache_selection"]}, indent=2), flush=True)


if __name__ == "__main__":
    try:
        main()
    finally:
        destination = save_evidence()
        print(f"Desktop integration evidence: {destination}", flush=True)
