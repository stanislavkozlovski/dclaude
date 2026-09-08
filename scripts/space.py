#!/usr/bin/env python3
"""Host-only, fail-closed Docker Desktop storage inventory and cleanup.

The Engine API supplies structured byte counts and exact mutations. Receipts are
observations, never executable plans. See docs/SPACE.md for the user contract.
"""
from __future__ import annotations

import argparse
import contextlib
from concurrent.futures import ThreadPoolExecutor
import datetime as dt
import http.client
import json
import os
from pathlib import Path
import platform
import plistlib
import re
import shlex
import signal
import socket
import stat
import subprocess
import sys
import tempfile
import time
from urllib.parse import quote, urlencode

SCHEMA = 1
LABEL = "com.dclaude.managed"
RELEASE = re.compile(r"^dclaude:(\d+\.\d+\.\d+)$")
IMAGE_ID = re.compile(r"^sha256:[0-9a-f]{64}$")
DEFAULT_KEEP = 2



class SpaceError(Exception):
    pass


def now():
    return dt.datetime.now(dt.timezone.utc).isoformat()


def run(args, timeout=15):
    try:
        result = subprocess.run(args, capture_output=True, timeout=timeout, check=False)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise SpaceError(f"{args[0]} probe failed: {exc}") from exc
    if result.returncode:
        raise SpaceError(f"{' '.join(args[:3])}: {result.stderr.decode(errors='replace').strip()}")
    return result.stdout


def measure_file(path: Path):
    resolved = path.expanduser().resolve(strict=True)
    info = resolved.stat()
    if not stat.S_ISREG(info.st_mode):
        raise SpaceError(f"Not a regular disk image: {resolved}")
    return dict(path=str(resolved), device=info.st_dev, inode=info.st_ino,
                allocated_bytes=info.st_blocks * 512, apparent_bytes=info.st_size)


class HostProbe:
    def __init__(self, runner=None):
        self.runner = runner or run

    def plist(self, args):
        try:
            return plistlib.loads(self.runner(["diskutil", *args]))
        except (ValueError, plistlib.InvalidFileException) as exc:
            raise SpaceError(f"Invalid diskutil response: {exc}") from exc

    def snapshot(self, disk_image=None):
        result = dict(disk_image=None, containers=[], measured_at=now(), issues=[], complete=False)
        if platform.system() != "Darwin":
            result["issues"].append(dict(probe="host", message="Host measurements require macOS/APFS."))
            return result
        path = disk_image or (Path.home() / "Library/Containers/com.docker.docker/Data/vms/0/data/Docker.raw")
        try:
            result["disk_image"] = measure_file(Path(path))
        except (OSError, SpaceError) as exc:
            result["issues"].append(dict(probe="disk_image", path=str(path), message=str(exc)))
        try:
            listing = self.plist(["apfs", "list", "-plist"])
            containers = listing["Containers"]
            targets = [("startup", Path("/"))]
            if result["disk_image"]:
                mount = Path(result["disk_image"]["path"]).parent
                while not os.path.ismount(mount) and mount != mount.parent:
                    mount = mount.parent
                targets.append(("docker", mount))
            for role, target in targets:
                info = self.plist(["info", "-plist", str(target)])
                matches = [c for c in containers if
                           c.get("ContainerReference") == info.get("APFSContainerReference", "missing") or
                           c.get("APFSContainerUUID") == info.get("APFSContainerUUID", "missing") or
                           any(v.get("DeviceIdentifier") == info.get("DeviceIdentifier", "missing")
                               for v in c.get("Volumes", []))]
                if len(matches) != 1:
                    raise SpaceError(f"Cannot resolve {role} APFS container for {target}")
                item = matches[0]
                identity = item["APFSContainerUUID"]
                capacity, free = item["CapacityCeiling"], item["CapacityFree"]
                if not isinstance(capacity, int) or not isinstance(free, int) or not 0 <= free <= capacity:
                    raise SpaceError("Invalid APFS byte counters")
                previous = next((c for c in result["containers"] if c["uuid"] == identity), None)
                if previous:
                    previous["roles"].append(role)
                else:
                    result["containers"].append(dict(uuid=identity, device=item["ContainerReference"],
                                                      capacity_bytes=capacity, free_bytes=free, roles=[role]))
        except (OSError, KeyError, TypeError, SpaceError) as exc:
            result["issues"].append(dict(probe="apfs", message=str(exc)))
        result["complete"] = bool(result["disk_image"] and result["containers"] and not result["issues"])
        return result


def host_delta(before, after):
    if not before.get("complete") or not after.get("complete"):
        return dict(measured=False, reason="Host baseline or follow-up is unmeasured.")
    left, right = before["disk_image"], after["disk_image"]
    if any(left[k] != right[k] for k in ("path", "device", "inode")):
        return dict(measured=False, reason="Disk image identity changed.")
    old = {c["uuid"]: c for c in before["containers"]}
    new = {c["uuid"]: c for c in after["containers"]}
    if old.keys() != new.keys():
        return dict(measured=False, reason="APFS container identity changed.")
    deltas = [dict(uuid=k, roles=new[k]["roles"], free_bytes_delta=new[k]["free_bytes"] - old[k]["free_bytes"])
              for k in old]
    allocated = left["allocated_bytes"] - right["allocated_bytes"]
    observed = allocated > 0 or any(c["free_bytes_delta"] > 0 and "docker" in c["roles"] for c in deltas)
    return dict(measured=True, raw_allocated_bytes_reduction=allocated, apfs=deltas,
                recovery_observed=observed,
                note="Signed free-space deltas include other host writes; this is not deletion attribution.")


class UnixConnection(http.client.HTTPConnection):
    def __init__(self, path):
        super().__init__("localhost", timeout=30)
        self.path = path

    def connect(self):
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sock.settimeout(self.timeout)
        self.sock.connect(self.path)


class Docker:
    def __init__(self, runner=None):
        self.runner = runner or run
        self.binding = None
        self.path = None
        self.version = None
        self.deadline = None
        self.report_deadline = None

    def api(self, method, path, query=None):
        connection = UnixConnection(self.path)
        deadlines = [value for value in (self.deadline, self.report_deadline) if value is not None]
        if deadlines:
            remaining = min(deadlines) - time.monotonic()
            if remaining <= 0:
                raise SpaceError("Docker inventory exceeded its 55-second budget; mutation disabled.")
            connection.timeout = remaining
        url = (f"/v{self.version}" if self.version else "") + path
        if query:
            url += "?" + urlencode(query)
        try:
            connection.request(method, url)
            response = connection.getresponse()
            raw = response.read()
            if response.status >= 300:
                raise SpaceError(f"Docker {method} {path}: HTTP {response.status}: {raw.decode(errors='replace')}")
            return json.loads(raw) if raw else None
        except (OSError, ValueError, http.client.HTTPException) as exc:
            raise SpaceError(f"Docker {method} {path}: {exc}") from exc
        finally:
            connection.close()

    def connect(self):
        if platform.system() != "Darwin":
            raise SpaceError("Cleanup supports local macOS Docker Desktop only.")
        context = self.runner(["docker", "context", "show"]).decode().strip()
        contexts = json.loads(self.runner(["docker", "context", "inspect", context]))
        endpoint = contexts[0]["Endpoints"]["docker"]["Host"]
        override = os.environ.get("DOCKER_HOST")
        if override and not os.environ.get("DOCKER_CONTEXT") and override != endpoint:
            raise SpaceError("DOCKER_HOST overrides the context; use an unambiguous local Desktop context.")
        if not endpoint.startswith("unix://"):
            raise SpaceError("Remote Docker endpoints are read-only unsupported; no cleanup attempted.")
        self.path = endpoint[7:]
        desktop_socket = Path.home() / ".docker/run/docker.sock"
        if Path(self.path).resolve() != desktop_socket.resolve():
            raise SpaceError("Socket is not the local Docker Desktop socket (~/.docker/run/docker.sock); forwarded or unknown Unix endpoints are unsupported.")
        self.path = str(Path(self.path).resolve())
        version = self.api("GET", "/version")
        parts = tuple(int(p) for p in version["ApiVersion"].split("."))
        if parts < (1, 48):
            raise SpaceError("Docker Engine API 1.48 or newer is required for image-family safety.")
        self.version = "1.48" if parts == (1, 48) else "1.49"
        info = self.api("GET", "/info")
        if "docker desktop" not in info.get("OperatingSystem", "").lower() or info.get("OSType") != "linux":
            raise SpaceError("The selected daemon is not a local Linux Docker Desktop engine.")
        if os.environ.get("BUILDX_BUILDER") or os.environ.get("DOCKER_BUILDKIT") == "0":
            raise SpaceError("Custom or disabled builders are outside storage cleanup scope.")
        builders = [json.loads(line) for line in self.runner(
            ["docker", "buildx", "ls", "--format", '{"Current":{{json .Builder.Current}},"Builder":{{json .Builder}}}']).decode().splitlines() if line.strip()]
        selected = list({json.dumps(b["Builder"], sort_keys=True): b["Builder"] for b in builders if b.get("Current") is True}.values())
        if len(selected) != 1 or selected[0].get("Driver") != "docker":
            raise SpaceError("Select the default docker-driver builder; other builders are outside this cleanup scope.")
        builder = selected[0]
        nodes = builder.get("Nodes", [])
        if (builder.get("Name") not in (context, "default") or len(nodes) != 1
                or nodes[0].get("Status") != "running"):
            raise SpaceError("The selected builder must be the single running default docker-driver node.")
        node = nodes[0]
        endpoint_ref = node.get("Endpoint")
        if not isinstance(endpoint_ref, str) or not endpoint_ref:
            raise SpaceError("Builder node endpoint is unmeasured; cleanup disabled.")
        if "://" in endpoint_ref:
            node_endpoint = endpoint_ref
        else:
            node_context = json.loads(self.runner(["docker", "context", "inspect", endpoint_ref]))
            node_endpoint = node_context[0]["Endpoints"]["docker"]["Host"]
        if not node_endpoint.startswith("unix://") or str(Path(node_endpoint[7:]).resolve()) != self.path:
            raise SpaceError("The selected builder endpoint is not the pinned local Docker Desktop socket.")
        # The docker driver dials BuildKit through this daemon's /grpc API.
        # Containerd worker IDs are independent of Engine /info.ID, so compare
        # the daemon over the proven same socket and retain worker IDs separately.
        node_info = self.api("GET", "/info")
        if not info.get("ID") or node_info.get("ID") != info["ID"]:
            raise SpaceError("Docker daemon identity changed while binding its default builder.")
        workers = node.get("IDs")
        if not isinstance(workers, list) or not workers or any(not isinstance(w, str) or not w for w in workers):
            raise SpaceError("Builder worker identities are unmeasured; cleanup disabled.")
        self.binding = dict(context=context, endpoint=endpoint, socket=self.path, daemon_id=info["ID"],
                            builder=builder["Name"], builder_endpoint=node_endpoint, worker_ids=sorted(workers),
                            driver=info.get("Driver"), driver_status=info.get("DriverStatus"))
        return self.binding

    def inventory(self):
        self.deadline = time.monotonic() + 55
        try:
            return self._inventory()
        finally:
            self.deadline = None

    def _inventory(self):
        # These read-only requests are independent. Use the same deadline for
        # every connection, including inspections queued after the container list.
        # A complete observation still has no global Docker transaction; apply
        # validates a fresh observation before each exact mutation.
        with ThreadPoolExecutor(max_workers=4) as pool:
            images_future = pool.submit(self.api, "GET", "/images/json", dict(all="true", manifests="true"))
            cache_future = pool.submit(self.api, "GET", "/system/df", dict(type="build-cache"))
            containers = self.api("GET", "/containers/json", dict(all="true"))
            inspections = [pool.submit(self.api, "GET", f"/containers/{quote(c['Id'], safe='')}/json")
                           for c in containers]
            images = images_future.result()
            inspected = []
            for future in inspections:
                info = future.result()
                # Keep storage-reference metadata. Environment variables, command
                # lines, and health logs are unrelated and must not enter receipts.
                inspected.append({
                    "Id": info["Id"], "Name": info.get("Name"), "Image": info["Image"],
                    "ImageManifestDescriptor": info.get("ImageManifestDescriptor"),
                    "State": {key: info.get("State", {}).get(key) for key in
                              ("Status", "Running", "Paused", "Restarting", "Dead", "StartedAt", "FinishedAt")},
                    "HostConfig": {"Mounts": [mount for mount in info.get("HostConfig", {}).get("Mounts", []) or []
                                              if mount.get("Type") == "image"]},
                    "Mounts": [{key: mount.get(key) for key in ("Type", "Name", "Source", "Destination", "RW")}
                               for mount in info.get("Mounts", [])],
                })
            disk = cache_future.result()
        if not isinstance(images, list) or not isinstance(inspected, list) or ("BuildCache" not in disk or (disk["BuildCache"] is not None and not isinstance(disk["BuildCache"], list))):
            raise SpaceError("Incomplete Docker inventory; mutation disabled.")
        return dict(images=images, containers=inspected, cache=disk["BuildCache"] or [],
                    measured_at=now())

    def delete_image(self, target):
        return self.api("DELETE", f"/images/{quote(target, safe='')}", dict(force="false", noprune="true"))

    def delete_cache(self, record):
        filters = {"id": ["^" + re.escape(record) + "$"], "private": []}
        return self.api("POST", "/build/prune", dict(all="false", filters=json.dumps(filters)))


def image_plan(inventory, current_image, keep=DEFAULT_KEEP, automatic=False):
    """Group aliases by immutable ID; unresolved graph edges veto mutation."""
    families = {}
    issues = []
    notes = []
    for item in inventory["images"]:
        ident = item.get("Id")
        if not isinstance(ident, str) or not IMAGE_ID.fullmatch(ident):
            raise SpaceError("Image inventory contains an invalid immutable ID")
        if type(item.get("Created")) is not int or (type(item.get("Size")) is not int or item["Size"] < 0):
            raise SpaceError("Image inventory is missing creation/size metadata")
        family = families.setdefault(ident, dict(id=ident, tags=[], digests=[], members={ident},
            created=item["Created"], size_bytes=item["Size"], shared_bytes=item.get("SharedSize"),
            labelled=(item.get("Labels") or {}).get(LABEL) == "true", reasons=[]))
        family["tags"] = sorted(set(family["tags"]) | {t for t in item.get("RepoTags") or [] if t != "<none>:<none>"})
        family["digests"] = sorted(set(family["digests"]) | set(item.get("RepoDigests") or []))
        descriptor = item.get("Descriptor") or {}
        if descriptor.get("digest"):
            family["members"].add(descriptor["digest"])
        media = descriptor.get("mediaType", "")
        manifests = item.get("Manifests")
        if ("index" in media or "manifest.list" in media) and not manifests:
            family["reasons"].append("unresolved index/platform/attestation relationships")
        for manifest in manifests or []:
            if manifest.get("Available") is not True or manifest.get("Kind") not in ("image", "attestation"):
                family["reasons"].append("unavailable or unknown manifest relationship")
            if (manifest.get("Descriptor") or {}).get("digest"):
                family["members"].add(manifest["Descriptor"]["digest"])
            member = manifest.get("ID") or manifest.get("Id") or (manifest.get("Descriptor") or {}).get("digest")
            if not member or not IMAGE_ID.fullmatch(member):
                family["reasons"].append("unresolved manifest identity")
            else:
                family["members"].add(member)
            data = manifest.get("ImageData") or {}
            if data.get("Containers"):
                family["reasons"].append("manifest referenced by a running or stopped container")
            attestation = manifest.get("AttestationData") or {}
            if attestation.get("For"):
                family["members"].add(attestation["For"])
    # A member listed as another root is a single family. Protect both roots:
    # deleting two independently could otherwise bypass alias protection.
    for family in families.values():
        if any(other["id"] != family["id"] and family["members"] & other["members"] for other in families.values()):
            family["reasons"].append("overlapping image families require manual inspection")
    aliases = {tag: family for family in families.values() for tag in family["tags"] + family["digests"]}
    current = families.get(current_image) or aliases.get(current_image)
    current_families = [current] if current else [f for f in families.values() if current_image in f["members"]]
    if not current_families:
        notes.append(f"This checkout expects {current_image}, which is not built.")
    for family in current_families:
        family["reasons"].append("configured current launcher image")
    referenced = set()
    for container in inventory["containers"]:
        ident = container.get("Image")
        if not isinstance(ident, str) or not IMAGE_ID.fullmatch(ident):
            issues.append("Container image identity is unresolved.")
        else:
            referenced.add(ident)
        descriptor = container.get("ImageManifestDescriptor") or {}
        if descriptor.get("digest"):
            referenced.add(descriptor["digest"])
        mounts = (container.get("HostConfig") or {}).get("Mounts") or []
        for mount in mounts:
            if mount.get("Type") == "image":
                source = mount.get("Source", "")
                digest = source if IMAGE_ID.fullmatch(source) else source.rsplit("@", 1)[-1]
                if not IMAGE_ID.fullmatch(digest):
                    issues.append("A tag-based image mount hides its immutable image identity; image cleanup is disabled.")
                else:
                    referenced.add(digest)
        if any(m.get("Type") == "image" for m in container.get("Mounts", [])) and not any(m.get("Type") == "image" for m in mounts):
            issues.append("An image mount has incomplete reference metadata.")
    for ident in referenced:
        matched = [f for f in families.values() if ident in f["members"] or any(d.endswith("@" + ident) for d in f["digests"])]
        if not matched:
            issues.append(f"Container image reference {ident} has no resolved family.")
        for family in matched:
            family["reasons"].append("referenced by a running or stopped container or image mount")
    launcher = []
    for family in families.values():
        tags = family["tags"]
        releases = [tag for tag in tags if RELEASE.fullmatch(tag)]
        owned = family["labelled"] or bool(releases)
        family["owned"] = owned
        if owned:
            launcher.append(family)
        if not owned:
            family["reasons"].append("unknown ownership")
        if any(not RELEASE.fullmatch(tag) for tag in tags):
            family["reasons"].append("non-release or other-repository alias")
        if any(not digest.startswith("dclaude@") for digest in family["digests"]):
            family["reasons"].append("other-repository digest alias")
        if automatic and not family["labelled"]:
            family["reasons"].append("legacy unlabelled images require manual review")
    newest = sorted(launcher, key=lambda f: (f["created"], f["id"]), reverse=True)[:keep]
    for family in newest:
        family["reasons"].append(f"newest {keep} distinct launcher builds")
    candidates = []
    for family in sorted(families.values(), key=lambda f: (f["created"], f["id"])):
        family["members"] = sorted(family["members"])
        family["reasons"] = sorted(set(family["reasons"]))
        if not family["reasons"] and not issues:
            targets = family["tags"] or [family["id"]]
            candidates.append(dict(id=family["id"], targets=targets, size_bytes=family["size_bytes"],
                                   legacy=not family["labelled"], members=family["members"]))
    return dict(candidates=candidates, families=list(families.values()), issues=sorted(set(issues)), notes=notes, keep=keep)


def cache_plan(inventory):
    candidates, protected, issues = [], [], []
    for record in inventory["cache"]:
        if not isinstance(record.get("ID"), str) or not record["ID"] or (type(record.get("Size")) is not int or record["Size"] < 0):
            raise SpaceError("Incomplete build-cache identity/size metadata")
        if type(record.get("InUse")) is not bool or type(record.get("Shared")) is not bool:
            raise SpaceError("Incomplete build-cache sharing/use metadata")
        # The Engine serializes this list under " Parents": moby's JSON tag
        # carries a leading space. The deprecated singular "Parent" may also appear.
        parents = record.get("Parents", record.get(" Parents")) or []
        if not isinstance(parents, list):
            raise SpaceError("Incomplete build-cache dependency metadata")
        if record.get("Parent"):
            parents = [*parents, record["Parent"]]
        if any(not isinstance(parent, str) or not parent for parent in parents):
            raise SpaceError("Incomplete build-cache dependency metadata")
        parents = sorted(set(parents))
        item = dict(id=record["ID"], size_bytes=record["Size"], last_used=record.get("LastUsedAt"),
                    description=record.get("Description", ""), parents=parents, type=record.get("Type"), reasons=[])
        if record["InUse"]:
            item["reasons"].append("in use")
        if record["Shared"]:
            item["reasons"].append("shared with an image")
        if not record.get("Type") or record["Type"] in ("internal", "frontend"):
            item["reasons"].append("internal/frontend or unknown cache type")
        (protected if item["reasons"] else candidates).append(item)
    # Dependents first; exact filters never broaden to parents. Cycles/unreported
    # edges are safe: Engine may decline deletion, reported as a skipped target.
    candidate_ids = {c["id"] for c in candidates}
    ordered = []
    while candidates:
        parents = {p for c in candidates for p in c["parents"] if p in candidate_ids}
        leaves = [c for c in candidates if c["id"] not in parents]
        if not leaves:
            issues.append("Unresolved cache dependency cycle; mutation disabled.")
            ordered.extend(candidates)
            break
        for item in sorted(leaves, key=lambda c: c["id"]):
            ordered.append(item)
            candidates.remove(item)
    return dict(candidates=ordered, protected=protected, issues=issues,
                note="Cache is builder-wide. Private records are not proof of project ownership; shared byte counts overlap image accounting.")


def state_dir():
    return Path.home() / ".local/state/dclaude/space"


def secure_state(path):
    for directory in [path, *list(path.parents)[:3]]:
        if directory.is_symlink():
            raise SpaceError(f"Docker space state must not be a symlink: {directory}")
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    path.chmod(0o700)


def write_json(path, value):
    secure_state(path.parent)
    fd, temporary = tempfile.mkstemp(prefix=".space-", dir=path.parent)
    try:
        with os.fdopen(fd, "w") as handle:
            json.dump(value, handle, indent=2, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        directory_fd = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    finally:
        with contextlib.suppress(FileNotFoundError):
            os.unlink(temporary)


def read_json(path):
    try:
        with path.open() as handle:
            data = json.load(handle)
    except FileNotFoundError:
        return None
    except (OSError, ValueError) as exc:
        raise SpaceError(f"Cannot read {path}: {exc}") from exc
    if not isinstance(data, dict) or type(data.get("schema_version")) is not int or data["schema_version"] != SCHEMA:
        raise SpaceError(f"Unknown or invalid state schema at {path}; mutation disabled.")
    return data


def install_signal_handlers():
    def terminate(signum, _frame):
        raise SystemExit(128 + signum)

    signal.signal(signal.SIGTERM, terminate)


@contextlib.contextmanager
def operation_lock(directory, wait_seconds=30):
    secure_state(directory)
    lock = directory / "operation.lock"
    deadline = time.monotonic() + wait_seconds
    while True:
        try:
            lock.mkdir(mode=0o700)
            break
        except FileExistsError:
            if time.monotonic() >= deadline:
                raise SpaceError(f"Docker space operation is busy: {lock}; retry when its owner finishes. Stale locks require manual inspection.")
            time.sleep(0.2)
    owner = str(os.getpid())
    owner_path = lock / "owner"
    try:
        owner_path.write_text(owner + "\n")
    except BaseException:
        with contextlib.suppress(OSError):
            owner_path.unlink(missing_ok=True)
        with contextlib.suppress(OSError):
            lock.rmdir()
        raise
    try:
        yield
    finally:
        if owner_path.exists() and owner_path.read_text().strip() == owner:
            owner_path.unlink()
            lock.rmdir()


def load_policy(directory):
    policy = read_json(directory / "policy.json")
    if policy and (type(policy.get("enabled")) is not bool or type(policy.get("keep")) is not int or policy["keep"] < 1
                   or policy.get("repository") != "dclaude" or not isinstance(policy.get("binding"), dict)):
        raise SpaceError("Invalid retention policy; mutation disabled.")
    return policy


def disabled_policy():
    """Describe the unsaved state without granting automatic deletion authority."""
    return dict(schema_version=SCHEMA, enabled=False, keep=DEFAULT_KEEP, repository="dclaude", binding={})


def plan_inventory(inventory, args, automatic=False):
    """One planner per action; apply replans through this same function before each mutation."""
    if args.action == "cache":
        return cache_plan(inventory)
    return image_plan(inventory, args.current_image, args.keep, automatic)


def cleanup_blocked(report):
    """Deletion needs a plan without issues and, once anything is eligible, a complete host baseline."""
    plan, host = report["plan"], report["host"]
    return bool(plan["issues"] or (plan["candidates"] and not host["complete"]))


def collect(docker, host, args, automatic=False):
    inventory = docker.inventory()
    plan = plan_inventory(inventory, args, automatic)
    measurement = host.snapshot(args.disk_image)
    return dict(schema_version=SCHEMA, binding=docker.binding, inventory=inventory, plan=plan, host=measurement)


def human_bytes(value):
    if value is None:
        return "Unmeasured"
    amount = abs(value)
    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if amount < 1024 or unit == "TiB":
            return f"{amount:.0f} B" if unit == "B" else f"{amount:.1f} {unit}"
        amount /= 1024


def row(label, value):
    print(f"  {label:<16}{value}")


def kept_reason(family):
    reasons = family["reasons"]
    if any("container" in reason or "image mount" in reason for reason in reasons):
        return "used by containers"
    if "configured current launcher image" in reasons:
        return "current launcher image"
    if any(reason.startswith("newest ") for reason in reasons):
        return "keep-newest policy"
    if any("alias" in reason for reason in reasons):
        return "tags used elsewhere"
    return "needs inspection" if reasons else "cleanup blocked"


def next_actions(wrapper, action, *, report=None, delta=None, completed=False, disk_image=None):
    """Choose useful commands from observations; never invent cleanup work."""
    command = f"{wrapper} --space"
    location = f" --disk-image {shlex.quote(str(disk_image))}" if disk_image else ""
    hints = []
    if report is not None:
        plan = report["plan"]
        keep = f" --keep {plan['keep']}" if action == "images" else ""
        if plan["candidates"] and not cleanup_blocked(report):
            purpose = "Review image deletion" if action == "images" else "Review cache deletion (future builds may need downloads)"
            hints.append((purpose, f"{command} {action}{keep}{location} --apply"))
        else:
            cache = report.get("cache_plan")
            if action == "images" and cache and cache["candidates"] and not cache["issues"]:
                hints.append(("Review unused build cache", f"{command} cache{location}"))
        hints.append(("Inspect image protections" if action == "images" else "Inspect all cache records",
                      f"{command} {action}{keep}{location} --json"))
    elif delta is not None:
        if delta["measured"] and not delta.get("recovery_observed"):
            hints.append(("Check for delayed recovery later", f"{command} verify"))
        if completed and action == "images":
            hints.append(("Review cache after image cleanup", f"{command} cache{location}"))
        hints.append(("Inspect recovery measurements", f"{command} verify --json"))
    elif action == "retention":
        hints.append(("Inspect the policy as JSON", f"{command} retention status --json"))
    return hints[:3]


def print_next(hints):
    if hints:
        print("\nNext")
        for purpose, command in hints:
            print(f"  {purpose}:\n    {command}")


def print_result(message):
    print(f"Result\n  {message}")


class ReportedError(SpaceError):
    """The human report already displayed the specific blocking issues."""


def print_report(report, action, wrapper="dclaude", applying=False, disk_image=None):
    plan, host = report["plan"], report["host"]
    blocked = cleanup_blocked(report)
    if blocked:
        result = "Cleanup blocked; nothing deleted." if applying else "Preview only; cleanup is blocked."
    elif not plan["candidates"]:
        result = "Nothing to remove."
        if action == "images":
            launcher = [family for family in plan["families"] if family["owned"]]
            if launcher and all(kept_reason(family) == "used by containers" for family in launcher):
                result += " Both dclaude images are used by containers." if len(launcher) == 2 else " All dclaude images are used by containers."
    else:
        result = f"{'Review' if applying else 'Preview'} {len(plan['candidates'])} {action} candidates; nothing deleted."
    print_result(result)
    print("\nDocker storage")
    raw = host["disk_image"]
    row("Disk used", human_bytes(raw["allocated_bytes"] if raw else None))
    startup = next((c for c in host["containers"] if "startup" in c["roles"]), None)
    row("Mac free", human_bytes(startup["free_bytes"] if startup else None))
    for container in host["containers"]:
        if "docker" in container["roles"] and "startup" not in container["roles"]:
            row("External free", human_bytes(container["free_bytes"]))

    candidates = plan["candidates"]
    if action == "images":
        print("\nImages")
        row("Removable", f"{len(candidates)} {'build' if len(candidates) == 1 else 'builds'}" if candidates else "None")
        candidate_ids = {c["id"] for c in candidates}
        groups = {}
        for family in plan["families"]:
            if family["id"] in candidate_ids:
                continue
            if family["owned"]:
                category = "dclaude", kept_reason(family)
            else:
                category = ("other projects", "") if family["tags"] and all(not t.startswith("dclaude:") for t in family["tags"]) else ("unclassified", "")
            groups[category] = groups.get(category, 0) + 1
        for index, ((category, reason), count) in enumerate(sorted(groups.items(), key=lambda item: (item[0][0] != "dclaude", item[0]))):
            noun = "image" if count == 1 else "images"
            description = f"{count} dclaude {noun}" if category == "dclaude" else f"{count} {noun} ({category})"
            row("Kept" if index == 0 else "", description + (f" — {reason}" if reason else ""))
        if candidates:
            print("\n  Removable builds (Docker-reported sizes)" if applying else "\n  Largest removable builds (Docker-reported sizes)")
            families = {f["id"]: f for f in plan["families"]}
            shown = candidates if applying else sorted(candidates, key=lambda c: c["size_bytes"], reverse=True)[:5]
            for candidate in shown:
                family = families[candidate["id"]]
                name = ", ".join(family["tags"]) or f"<dangling {candidate['id'][7:19]}>"
                age = max(0, time.time() - family["created"]) / 86400
                print(f"    {human_bytes(candidate['size_bytes']):>10}  {age:.0f}d  {name}")
                if applying:
                    print(f"      Image ID: {candidate['id']}")
                    if not family["tags"]:
                        print(f"      Delete: {candidate['id']}")
                if candidate.get("legacy"):
                    print("      Legacy tag — review ownership before deletion.")
            if len(shown) < len(candidates):
                print(f"    … {len(candidates) - len(shown)} more; use --json for the full list.")

    cache = plan if action == "cache" else report.get("cache_plan")
    if cache is not None:
        print("\nBuild cache")
        records = cache["candidates"]
        row("Needs review", f"{len(records)} unused private records" if records else "None")
        row("Reported size", human_bytes(sum(c["size_bytes"] for c in records)))
        row("Kept", f"{len(cache['protected'])} shared, in-use or internal records")
        if action == "cache" and records:
            print("\n  Exact cache targets" if applying else "\n  Largest unused records")
            shown = records if applying else sorted(records, key=lambda c: c["size_bytes"], reverse=True)[:5]
            for record in shown:
                description = " ".join(record["description"].split())
                if len(description) > 60:
                    description = description[:57] + "…"
                print(f"    {human_bytes(record['size_bytes']):>10}  {record['id']}  {description}")
            if len(shown) < len(records):
                print(f"    … {len(records) - len(shown)} more; --apply lists every target before confirmation.")
        if records:
            print("  Reported sizes may overlap; actual disk recovery can differ.")

    if plan.get("notes"):
        print("\nNote")
        for note in plan["notes"]:
            print(f"  {note}")
    issues = list(plan["issues"])
    issues.extend(issue["message"] for issue in host["issues"])
    if not host["complete"]:
        issues.append("Check Docker.raw path and terminal access; use --disk-image PATH if the file moved.")
    if cache is not None and action != "cache":
        issues.extend(cache["issues"])
    if issues:
        print("\nIssues")
        for issue in issues:
            print(f"  {issue}")
    if not applying or blocked or not candidates:
        print_next(next_actions(wrapper, action, report=report, disk_image=disk_image))


def print_recovery(delta, receipt_path, wrapper, title="Verification", *, action=None, disk_image=None, skipped=0):
    print_result(title + (" — recovery measured." if delta["measured"] else " — recovery unmeasured."))
    print("\nMeasurements")
    issues = []
    if delta["measured"]:
        change = delta["raw_allocated_bytes_reduction"]
        row("Disk change", "Unchanged" if change == 0 else f"{human_bytes(change)} {'less' if change > 0 else 'more'} allocated")
        for container in delta["apfs"]:
            free = container["free_bytes_delta"]
            label = "Mac free change" if "startup" in container["roles"] else "External change"
            row(label, f"{'+' if free >= 0 else '-'}{human_bytes(free)}")
        print("  Free-space changes include other host activity.")
        if not delta.get("recovery_observed"):
            issues.append("Recovery not yet observed.")
    else:
        row("Disk change", "Unmeasured")
        issues.append(delta["reason"])
    row("Receipt", receipt_path)
    if skipped:
        row("Retained", f"{skipped} cache records still referenced by Docker")
    if issues:
        print("\nIssues")
        for issue in issues:
            print(f"  {issue}")
    print_next(next_actions(wrapper, action, delta=delta, completed=title == "Cleanup complete", disk_image=disk_image))


def print_policy(policy, wrapper):
    saved = policy is not None
    policy = policy or disabled_policy()
    print_result("Image retention enabled." if policy["enabled"] else "Image retention disabled.")
    print("\nPolicy")
    row("Status", ("Enabled" if policy["enabled"] else "Disabled") + ("" if saved else " (not configured)"))
    row("Keep newest", f"{policy['keep']} distinct builds")
    binding = policy.get("binding") or {}
    if binding.get("context"):
        row("Docker context", binding["context"])
    if binding.get("builder"):
        row("Builder", binding["builder"])
    if policy["enabled"]:
        print("\nRuns after a launcher image build once warm-container bootstrap succeeds.")
        print("Retires labelled dclaude builds beyond the newest ones.")
        print("Current images, container references and tags used elsewhere stay protected.")
    print_next(next_actions(wrapper, "retention"))


def confirm(action, count):
    if not sys.stdin.isatty():
        raise SpaceError("--apply requires interactive confirmation in a terminal; --yes cannot authorize storage deletion.")
    if action == "cache":
        print("\nBuilder-wide cache deletion: any project's next build may need downloads or recompilation.")
    else:
        print("\nImage deletion has no undo; a rebuild may produce a different image.")
    print("Keep other Docker clients and older launchers idle during cleanup.")
    answer = input(f"Delete the {count} exact {action} candidate(s) listed above? Type yes: ")
    if answer != "yes":
        raise SpaceError("Cancelled; nothing deleted.")


def image_revalidation_key(candidate, completed=()):
    """Only image identity, family edges, and remaining reviewed aliases authorize removal."""
    completed = set(completed)
    return dict(id=candidate["id"],
                targets=sorted(target for target in candidate["targets"] if target not in completed),
                members=sorted(candidate["members"]))


def cache_revalidation_key(candidate):
    """Eligibility comes from the fresh plan; dependency identity must also stay stable."""
    return dict(id=candidate["id"], parents=sorted(candidate["parents"]), type=candidate["type"])


def revalidate_candidate(reviewed, plan, action, completed=()):
    if plan["issues"]:
        raise SpaceError("Docker eligibility is incomplete; stopped before mutation.")
    eligible = next((candidate for candidate in plan["candidates"] if candidate["id"] == reviewed["id"]), None)
    if eligible is None:
        raise SpaceError(f"Target {reviewed['id']} changed or became protected; stopped without a broader fallback.")
    if action == "images":
        expected = image_revalidation_key(reviewed, completed)
        observed = image_revalidation_key(eligible)
    else:
        expected = cache_revalidation_key(reviewed)
        observed = cache_revalidation_key(eligible)
    if expected != observed:
        raise SpaceError(f"Target {reviewed['id']} metadata changed since confirmation; stopped.")
    return eligible


def load_latest_receipt(directory):
    """Resolve latest.json to its receipt, or fail closed before cleanup and verification alike."""
    latest = read_json(directory / "latest.json")
    if not latest:
        return None
    receipt_path = Path(latest.get("receipt", ""))
    if receipt_path.parent.resolve() != (directory / "receipts").resolve():
        raise SpaceError("Invalid latest receipt location; mutation disabled.")
    receipt = read_json(receipt_path)
    if not receipt or not isinstance(receipt.get("steps"), list) or not {"baseline", "disk_image"} <= receipt.keys():
        raise SpaceError("Incomplete latest receipt; mutation disabled.")
    return receipt_path, receipt


def validate_automatic(docker, args, directory, binding):
    """A saved policy and matching build marker are the only automatic authority."""
    policy = load_policy(directory)
    if not policy or not policy["enabled"]:
        raise SpaceError("Retention has not been enabled; no cleanup performed.")
    if policy["binding"] != binding:
        raise SpaceError("The enabled retention policy belongs to a different Docker binding; run retention enable again.")
    if not RELEASE.fullmatch(args.current_image):
        raise SpaceError("Retention only manages default dclaude release images")
    pending = directory / "pending-build"
    pending_id = pending.read_text().strip() if pending.exists() else ""
    if not IMAGE_ID.fullmatch(pending_id):
        raise SpaceError("A verified completed-build marker is required for automatic retention.")
    current = docker.api("GET", f"/images/{quote(args.current_image, safe='')}/json")
    if current.get("Id") != pending_id:
        raise SpaceError("The completed-build marker does not match the configured launcher image.")
    labels = (current.get("Config") or {}).get("Labels") or {}
    if labels.get(LABEL) != "true":
        raise SpaceError("The completed launcher image is not positively labelled for retention.")
    args.keep = policy["keep"]
    args.disk_image = Path(policy["disk_image"]) if policy.get("disk_image") else None


def consume_pending_build(directory):
    """Only a completed automatic pass retires the marker; failures keep it for a later launch."""
    (directory / "pending-build").unlink(missing_ok=True)


def apply_cleanup(docker, host, args, directory, automatic=False):
    with operation_lock(directory):
        binding = docker.connect()
        # Unknown state cannot be overwritten into a valid-looking receipt.
        if automatic:
            validate_automatic(docker, args, directory, binding)
        else:
            load_policy(directory)
        load_latest_receipt(directory)
        report = collect(docker, host, args, automatic)
        if not automatic:
            if args.action == "images":
                # Cache hints are presentation only; cache metadata cannot alter image eligibility.
                try:
                    report["cache_plan"] = cache_plan(report["inventory"])
                except SpaceError as exc:
                    report["cache_plan"] = dict(candidates=[], protected=[], issues=[str(exc)])
            print_report(report, args.action, args.wrapper, applying=True, disk_image=args.disk_image)
        candidates = report["plan"]["candidates"]
        if cleanup_blocked(report):
            error = SpaceError if automatic else ReportedError
            raise error("Cleanup blocked: complete image/cache and host baselines are required.")
        if not candidates:
            if automatic:
                consume_pending_build(directory)
            return None
        if not automatic:
            confirm(args.action, len(candidates))
        receipt = dict(schema_version=SCHEMA, started_at=now(), action=args.action, status="in_progress",
                       binding=docker.binding, disk_image=report["host"]["disk_image"]["path"],
                       baseline=report["host"], steps=[], reviewed=candidates,
                       authority="enabled retention policy" if automatic else "interactive exact target confirmation")
        receipt_path = directory / "receipts" / (dt.datetime.now(dt.timezone.utc).strftime("%Y%m%dT%H%M%S.%fZ") + ".json")

        def save():
            write_json(receipt_path, receipt)
            write_json(directory / "latest.json", dict(schema_version=SCHEMA, receipt=str(receipt_path)))

        save()  # No mutation unless the durable receipt is writable.
        try:
            for candidate in candidates:
                targets = candidate.get("targets", [candidate["id"]])
                for target in targets:
                    binding = docker.connect()
                    if binding != receipt["binding"]:
                        raise SpaceError("Docker context/engine/builder/store changed; stopped before next mutation.")
                    fresh_inventory = docker.inventory()
                    fresh_plan = plan_inventory(fresh_inventory, args, automatic)
                    completed = {step["target"] for step in receipt["steps"]
                                 if step.get("candidate_id") == candidate["id"] and step.get("status") == "completed"}
                    eligible = revalidate_candidate(candidate, fresh_plan, args.action, completed)
                    if args.action == "images" and target not in eligible["targets"]:
                        raise SpaceError(f"Target {target} changed or became protected; stopped without a broader fallback.")
                    step = dict(candidate_id=candidate["id"], target=target,
                                status="started", started_at=now())
                    receipt["steps"].append(step)
                    save()
                    try:
                        response = docker.delete_image(target) if args.action == "images" else docker.delete_cache(target)
                        step["response"] = response
                        # Docker may release coupled cache twins; record actual
                        # fresh inventory and stop if public records outside
                        # consent vanish.
                        if args.action == "cache":
                            after_inventory = docker.inventory()
                            old_ids = {r["ID"] for r in fresh_inventory["cache"]}
                            new_ids = {r["ID"] for r in after_inventory["cache"]}
                            step["cache_records_disappeared"] = sorted(old_ids - new_ids)
                            if (old_ids - new_ids) - {candidate["id"]}:
                                raise SpaceError("Cache records outside this exact target changed; stopped. See receipt.")
                    except BaseException as exc:
                        # An error may follow a partial mutation. Persist it
                        # before any optional observation, without retrying or
                        # widening scope.
                        step["status"] = "error"
                        step["error"] = str(exc) or type(exc).__name__
                        step["finished_at"] = now()
                        save()
                        if "response" not in step:
                            with contextlib.suppress(Exception):
                                step["after_inventory"] = docker.inventory()
                                save()
                        raise
                    step["status"] = "completed"
                    if args.action == "cache" and target not in step["cache_records_disappeared"]:
                        step["status"] = "skipped"
                        step["note"] = "Engine retained this record (for example, a dependent still references it); no wider prune attempted."
                    step["finished_at"] = now()
                    save()
            receipt["status"] = "completed"
        except BaseException as exc:
            receipt["status"] = "partial"
            receipt["error"] = str(exc) or type(exc).__name__
            receipt["finished_at"] = now()
            save()
            with contextlib.suppress(Exception):
                receipt["after"] = host.snapshot(args.disk_image)
                receipt["delta"] = host_delta(receipt["baseline"], receipt["after"])
                save()
            raise
        receipt["finished_at"] = now()
        receipt["after"] = host.snapshot(args.disk_image)
        receipt["delta"] = host_delta(receipt["baseline"], receipt["after"])
        save()
        if automatic:
            consume_pending_build(directory)
        else:
            skipped = sum(step["status"] == "skipped" for step in receipt["steps"])
            print_recovery(receipt["delta"], receipt_path, args.wrapper, "Cleanup complete",
                           action=args.action, disk_image=args.disk_image, skipped=skipped)
        return receipt


def automatic_retention(docker, host, args, directory):
    """After an enabled launcher build, retire only old labelled image history."""
    policy = load_policy(directory)
    if not policy or not policy["enabled"]:
        return 0
    pending = directory / "pending-build"
    if not pending.exists():
        return 0
    if platform.system() != "Darwin":
        # Automatic cleanup supports local macOS Docker Desktop; elsewhere the launcher just runs.
        consume_pending_build(directory)
        return 0
    args.action = "images"
    images = apply_cleanup(docker, host, args, directory, automatic=True)
    if images is None:
        return 0
    removed = [step["target"] for step in images["steps"] if step.get("status") == "completed"]
    reported = sum(candidate["size_bytes"] for candidate in images["reviewed"])
    print(f"Retention removed {len(removed)} old dclaude build{'' if len(removed) == 1 else 's'}: "
          f"{', '.join(removed)} ({human_bytes(reported)} reported)", file=sys.stderr)
    delta = images.get("delta") or {}
    startup = next((c for c in delta.get("apfs", []) if "startup" in c["roles"]), None) if delta.get("measured") else None
    receipt_path, _ = load_latest_receipt(directory)
    if startup:
        free = startup["free_bytes_delta"]
        print(f"Mac free space change {'+' if free >= 0 else '-'}{human_bytes(free)}; receipt {receipt_path}", file=sys.stderr)
    else:
        print(f"Recovery unmeasured; run {args.wrapper} --space verify later. Receipt {receipt_path}", file=sys.stderr)
    return 0


def parser():
    result = argparse.ArgumentParser(allow_abbrev=False, prog="dclaude --space", formatter_class=argparse.RawDescriptionHelpFormatter, description="Measure and explicitly reclaim local macOS Docker Desktop storage. Read-only by default.",
        epilog="""Commands (also available through dcodex):
  images                 Preview launcher image families and their protections.
  images --keep 2 --apply Review and confirm exact old image targets.
  cache                  Preview private unused default-builder cache records.
  cache --apply          Confirm separate builder-wide cache deletion.
  verify                 Remeasure the latest receipt; never delete anything.
  retention status       Show whether automatic image retention is configured.
  retention enable       Save a keep count, Docker binding, and disk-image path.
  retention disable      Stop automatic image cleanup; retain all history.

Retention is opt-in. Once enabled, it keeps the newest 2 launcher builds and runs
after a successful image build and warm-container bootstrap. It retires only older
labelled dclaude images. Images built by older launchers carry no label; retire
them once with images --apply. Cache cleanup always remains a separate choice.
Requires host Python 3 and local macOS Docker Desktop with Engine API 1.48+
for storage operations.
Runs before repository lookup, builds, updates, or agent startup. Launches
without host Python 3 skip enabled build-triggered retention with a warning.
--apply requires a terminal; --yes only authorizes launcher updates. Other Docker
clients must be idle.
For moved Docker.raw files use --disk-image PATH from Desktop Settings.
Default output uses Result, measurements, Issues (when needed), and Next commands.
--json includes full IDs, references, and bytes.
--apply lists every exact target before asking for confirmation.
See docs/SPACE.md for examples, protections, receipts, and native cache GC setup.""")
    result.add_argument("action", nargs="?", choices=["images", "cache", "verify", "retention"], default="images")
    result.add_argument("retention_action", nargs="?", choices=["enable", "status", "disable"])
    result.add_argument("--keep", type=int, help=f"distinct newest launcher builds to preserve (default: {DEFAULT_KEEP}; minimum: 1)")
    result.add_argument("--apply", action="store_true", help="print exact targets and ask for interactive deletion consent")
    result.add_argument("--json", action="store_true", help="read-only structured inventory with byte counts and protection reasons")
    result.add_argument("--disk-image", type=Path, help="Docker.raw path from Docker Desktop Settings when moved")
    result.add_argument("--current-image", default="dclaude:unknown", help=argparse.SUPPRESS)
    result.add_argument("--wrapper", default="dclaude", help=argparse.SUPPRESS)
    result.add_argument("--auto-retain", action="store_true", help=argparse.SUPPRESS)
    return result


def main(argv=None):
    install_signal_handlers()
    args = parser().parse_args(argv)
    try:
        if args.keep is not None and args.keep < 1:
            raise SpaceError("--keep must be at least 1")
        if args.keep is not None and args.action != "images" and not (args.action == "retention" and args.retention_action == "enable"):
            raise SpaceError("--keep belongs to images or retention enable")
        if args.disk_image and (args.action == "verify" or (args.action == "retention" and args.retention_action != "enable")):
            raise SpaceError("--disk-image belongs to diagnosis, cleanup, or retention enable; verify uses the receipt path")
        if args.json and args.action == "retention" and args.retention_action in ("enable", "disable"):
            raise SpaceError("--json is read-only; use retention status --json")
        if args.json and args.apply:
            raise SpaceError("--json is read-only; run --apply separately for interactive review")
        if args.retention_action and args.action != "retention":
            raise SpaceError("enable/status/disable belong to retention")
        if args.action in ("retention", "verify") and args.apply:
            raise SpaceError("--apply only belongs to images or cache")
        if args.keep is None:
            args.keep = DEFAULT_KEEP
        directory = state_dir()
        host = HostProbe()
        if args.action == "retention" and args.retention_action != "enable":
            saved = load_policy(directory)
            if args.retention_action == "disable":
                with operation_lock(directory):
                    policy = load_policy(directory) or disabled_policy()
                    policy["enabled"] = False
                    policy["updated_at"] = now()
                    write_json(directory / "policy.json", policy)
                print_result("Image retention disabled. Nothing deleted.")
                print_next(next_actions(args.wrapper, "retention"))
            elif args.json:
                print(json.dumps(dict(saved or disabled_policy(), saved=saved is not None), indent=2))
            else:
                print_policy(saved, args.wrapper)
            return 0
        if args.action == "verify":
            latest = load_latest_receipt(directory)
            if not latest:
                raise SpaceError("No cleanup receipt to verify")
            receipt_path, receipt = latest
            after = host.snapshot(Path(receipt["disk_image"]))
            result = dict(schema_version=SCHEMA, receipt=str(receipt_path), after=after,
                          delta=host_delta(receipt["baseline"], after))
            if args.json:
                print(json.dumps(result, indent=2))
            else:
                print_recovery(result["delta"], receipt_path, args.wrapper)
            return 0 if result["delta"]["measured"] else 2
        docker = Docker()
        if args.action in ("images", "cache") and not args.apply and not args.auto_retain:
            deadline = time.monotonic() + 55

            def bounded_run(arguments, timeout=15):
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise SpaceError("Diagnosis exceeded its 55-second probe budget; unmeasured, mutation disabled.")
                return run(arguments, timeout=min(timeout, remaining))

            docker.runner = bounded_run
            docker.report_deadline = deadline
            host.runner = bounded_run
        if args.auto_retain:
            return automatic_retention(docker, host, args, directory)
        docker.connect()
        if args.action == "retention":
            baseline = host.snapshot(args.disk_image)
            if not baseline["complete"]:
                raise SpaceError(f"Retention needs a complete host baseline: {baseline['issues']}")
            if not RELEASE.fullmatch(args.current_image):
                raise SpaceError("Retention is limited to default dclaude release images")
            with operation_lock(directory):
                load_policy(directory)
                load_latest_receipt(directory)
                policy = dict(schema_version=SCHEMA, enabled=True, keep=args.keep, repository="dclaude",
                              binding=docker.binding, disk_image=baseline["disk_image"]["path"], updated_at=now())
                write_json(directory / "policy.json", policy)
            print_policy(policy, args.wrapper)
            return 0
        if args.apply:
            apply_cleanup(docker, host, args, directory)
        else:
            report = collect(docker, host, args)
            # Diagnosis always includes separate cache accounting and relationships.
            report["cache_plan"] = cache_plan(report["inventory"])
            if args.json:
                print(json.dumps(report, indent=2, sort_keys=True))
            else:
                print_report(report, args.action, args.wrapper, disk_image=args.disk_image)
        return 0
    except (SpaceError, OSError, ValueError, KeyError, TypeError) as exc:
        if args.json:
            print(json.dumps(dict(schema_version=SCHEMA, error=str(exc), mutation_allowed=False)))
        elif args.auto_retain:
            print(f"Automatic retention stopped: {exc}", file=sys.stderr)
        elif not isinstance(exc, ReportedError):
            print(f"Result\n  Command failed.\n\nIssues\n  {exc}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        print("Interrupted; any partial cleanup is recorded in the receipt.", file=sys.stderr)
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
