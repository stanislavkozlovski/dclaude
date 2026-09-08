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
        self.report_deadline = None

    def api(self, method, path, query=None):
        connection = UnixConnection(self.path)
        deadlines = [value for value in (getattr(self, "deadline", None), self.report_deadline) if value is not None]
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
                    layers_size=None, binding=self.binding, measured_at=now())

    def delete_image(self, target):
        return self.api("DELETE", f"/images/{quote(target, safe='')}", dict(force="false", noprune="true"))

    def delete_cache(self, record):
        filters = {"id": ["^" + re.escape(record) + "$"], "private": []}
        return self.api("POST", "/build/prune", dict(all="false", filters=json.dumps(filters)))


def image_plan(inventory, current_image, keep=2, automatic=False):
    """Group aliases by immutable ID; unresolved graph edges veto mutation."""
    families = {}
    issues = []
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
    if not current:
        issues.append(f"Configured current image {current_image} is unresolved; build it before image cleanup.")
    else:
        current["reasons"].append("configured current launcher image")
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
    return dict(candidates=candidates, families=list(families.values()), issues=sorted(set(issues)), keep=keep)


def cache_plan(inventory):
    candidates, protected, issues = [], [], []
    for record in inventory["cache"]:
        if not isinstance(record.get("ID"), str) or not record["ID"] or (type(record.get("Size")) is not int or record["Size"] < 0):
            raise SpaceError("Incomplete build-cache identity/size metadata")
        if type(record.get("InUse")) is not bool or type(record.get("Shared")) is not bool:
            raise SpaceError("Incomplete build-cache sharing/use metadata")
        parents = record.get("Parents", record.get(" Parents", [])) or []
        if record.get("Parent"):
            parents = list(parents) + [record["Parent"]]
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
    try:
        (lock / "owner").write_text(owner + "\n")
        yield
    finally:
        if (lock / "owner").exists() and (lock / "owner").read_text().strip() == owner:
            (lock / "owner").unlink()
            lock.rmdir()


def load_policy(directory):
    policy = read_json(directory / "policy.json")
    if policy and (type(policy.get("enabled")) is not bool or type(policy.get("keep")) is not int or policy["keep"] < 1
                   or policy.get("repository") != "dclaude" or not isinstance(policy.get("binding"), dict)):
        raise SpaceError("Invalid retention policy; mutation disabled.")
    return policy


def collect(docker, host, args, automatic=False):
    inventory = docker.inventory()
    plan = image_plan(inventory, args.current_image, args.keep, automatic) if args.action != "cache" else cache_plan(inventory)
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


def print_report(report, action, wrapper="dclaude", applying=False, disk_image=None):
    plan, host = report["plan"], report["host"]
    command = f"{wrapper} --space"
    location = f" --disk-image {shlex.quote(str(disk_image))}" if disk_image else ""
    print("Docker storage")
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

    print()
    for issue in plan["issues"]:
        missing = re.fullmatch(r"Configured current image (.+) is unresolved; build it before image cleanup\.", issue)
        message = f"this checkout's image ({missing[1]}) is not built." if missing else issue
        print(f"{'Image' if action == 'images' else 'Cache'} cleanup blocked: {message}")
    for issue in host["issues"]:
        print(f"Host measurement unavailable: {issue['message']}")
    if not host["complete"]:
        print("Check Docker.raw path and terminal access; use --disk-image PATH if the file moved.")
    if cache is not None and action != "cache" and cache["issues"]:
        for issue in cache["issues"]:
            print(f"Cache cleanup blocked: {issue}")
    if not applying:
        if candidates and not plan["issues"] and host["complete"]:
            keep = f" --keep {plan['keep']}" if action == "images" else ""
            row("Next step", f"{command} {action}{keep}{location} --apply")
        elif action == "images" and cache is not None and cache["candidates"] and not cache["issues"]:
            row("Next step", f"{command} cache{location}")
        keep = f" --keep {plan['keep']}" if action == "images" and plan["keep"] != 2 else ""
        row("Details", f"{command} {action}{keep}{location} --json")
        print("\nPreview only — nothing deleted.")


def print_recovery(delta, receipt_path, wrapper, title="Verification"):
    print(f"\n{title}")
    if delta["measured"]:
        change = delta["raw_allocated_bytes_reduction"]
        row("Disk change", f"{human_bytes(change)} {'less' if change >= 0 else 'more'} allocated")
        for container in delta["apfs"]:
            free = container["free_bytes_delta"]
            label = "Mac free change" if "startup" in container["roles"] else "External change"
            row(label, f"{'+' if free >= 0 else '-'}{human_bytes(free)}")
        print("  Free-space changes include other host activity.")
        if not delta.get("recovery_observed"):
            print("  Recovery not yet observed.")
            row("Check later", f"{wrapper} --space verify")
    else:
        row("Disk change", "Unmeasured")
        print(f"  {delta['reason']}")
    row("Receipt", receipt_path)


def print_policy(policy, wrapper):
    print("Image retention")
    row("Status", "Enabled" if policy and policy["enabled"] else "Disabled")
    if policy:
        row("Keep newest", f"{policy['keep']} distinct builds")
        row("Docker context", policy["binding"]["context"])
        row("Builder", policy["binding"]["builder"])
    if policy and policy["enabled"]:
        print("\nRuns after successful build and startup. Only labelled dclaude images are eligible.")
        print("Current images, container references and tags used elsewhere stay protected.")
    row("Details", f"{wrapper} --space retention status --json")
    print("Native cache GC setup: docs/SPACE.md")


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


def canonical(value):
    """Inventory arrays describe sets of records/edges, not execution order."""
    if isinstance(value, dict):
        return {key: canonical(item) for key, item in value.items()}
    if isinstance(value, list):
        return sorted((canonical(item) for item in value), key=lambda item: json.dumps(item, sort_keys=True))
    return value


def same_selection(left, right):
    return json.dumps(canonical(left), sort_keys=True) == json.dumps(canonical(right), sort_keys=True)


def validate_latest(directory):
    latest = read_json(directory / "latest.json")
    if latest:
        receipt_path = Path(latest.get("receipt", ""))
        if receipt_path.parent.resolve() != (directory / "receipts").resolve():
            raise SpaceError("Invalid latest receipt location; mutation disabled.")
        receipt = read_json(receipt_path)
        if not receipt or not isinstance(receipt.get("steps"), list) or "baseline" not in receipt:
            raise SpaceError("Incomplete latest receipt; mutation disabled.")
    return latest


def inventory_signature(inventory):
    return json.dumps(canonical({key: inventory[key] for key in ("images", "containers", "cache")}), sort_keys=True)


def validate_automatic(docker, args, directory):
    policy = load_policy(directory)
    if not policy or not policy["enabled"] or policy["binding"] != docker.binding:
        raise SpaceError("Retention was disabled or its Docker binding changed; no cleanup performed.")
    if not RELEASE.fullmatch(args.current_image):
        raise SpaceError("Retention only manages default dclaude release images")
    pending = directory / "pending-build"
    if not pending.exists():
        raise SpaceError("No successful build is pending retention")
    pending_id = pending.read_text().strip()
    current = docker.api("GET", f"/images/{quote(args.current_image, safe='')}/json")
    if current.get("Id") != pending_id or (current.get("Config", {}).get("Labels") or {}).get(LABEL) != "true":
        raise SpaceError("Pending build does not match the current labelled launcher image")
    args.keep = policy["keep"]
    args.disk_image = Path(policy["disk_image"])
    return pending_id


def apply_cleanup(docker, host, args, directory, automatic=False):
    with operation_lock(directory):
        docker.connect()
        # Unknown state cannot be overwritten into a valid-looking receipt.
        if automatic:
            pending_id = validate_automatic(docker, args, directory)
        else:
            load_policy(directory)
        validate_latest(directory)
        report = collect(docker, host, args, automatic)
        if not automatic:
            print_report(report, args.action, args.wrapper, applying=True, disk_image=args.disk_image)
        if report["plan"]["issues"] or not report["host"]["complete"]:
            raise SpaceError("Cleanup blocked: complete image/cache and host baselines are required.")
        candidates = report["plan"]["candidates"]
        if not candidates:
            if not automatic:
                print("No eligible targets; nothing deleted.")
            if automatic:
                (directory / "pending-build").unlink()
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
        expected_inventory = report["inventory"]
        try:
            for candidate in candidates:
                targets = candidate.get("targets", [candidate["id"]])
                for target in targets:
                    binding = docker.connect()
                    if binding != receipt["binding"]:
                        raise SpaceError("Docker context/engine/builder/store changed; stopped before next mutation.")
                    fresh = collect(docker, host, args, automatic)
                    if inventory_signature(expected_inventory) != inventory_signature(fresh["inventory"]):
                        raise SpaceError("Docker inventory changed since review or the previous step; stopped.")
                    if fresh["plan"]["issues"] or not fresh["host"]["complete"]:
                        raise SpaceError("Inventory or host measurement became incomplete; stopped.")
                    delta = host_delta(report["host"], fresh["host"])
                    if not delta["measured"]:
                        raise SpaceError(delta["reason"])
                    eligible = next((c for c in fresh["plan"]["candidates"] if c["id"] == candidate["id"]), None)
                    if not eligible or (args.action == "images" and target not in eligible["targets"]):
                        raise SpaceError(f"Target {target} changed or became protected; stopped without a broader fallback.")
                    # Remaining aliases must be exactly the reviewed ones, minus
                    # aliases already successfully untagged by this receipt.
                    done = {s["target"] for s in receipt["steps"] if s.get("status") == "completed"}
                    expected = dict(candidate)
                    if args.action == "images":
                        expected["targets"] = [t for t in candidate["targets"] if t not in done]
                    comparable = dict(eligible)
                    if args.action == "cache":
                        # Releasing a reviewed child can update its parent's
                        # last-use timestamp. Full inventory drift is checked
                        # against our own preceding post-mutation observation.
                        expected.pop("last_used", None)
                        comparable.pop("last_used", None)
                    if not same_selection(expected, comparable):
                        raise SpaceError(f"Target {target} metadata changed since confirmation; stopped.")
                    step = dict(target=target, before=fresh["host"], status="started", started_at=now())
                    receipt["steps"].append(step)
                    save()
                    try:
                        response = docker.delete_image(target) if args.action == "images" else docker.delete_cache(target)
                        step["response"] = response
                    except Exception:
                        # An API error may follow a partial mutation. Preserve a
                        # fresh observation without retrying or widening scope.
                        with contextlib.suppress(Exception):
                            step["after_inventory"] = docker.inventory()
                            step["after"] = host.snapshot(args.disk_image)
                        raise
                    # Docker may release coupled cache twins; record actual fresh
                    # inventory and stop if public records outside consent vanish.
                    after_inventory = docker.inventory()
                    if args.action == "cache":
                        old_ids = {r["ID"] for r in fresh["inventory"]["cache"]}
                        new_ids = {r["ID"] for r in after_inventory["cache"]}
                        step["cache_records_disappeared"] = sorted(old_ids - new_ids)
                        if (old_ids - new_ids) - {candidate["id"]}:
                            step["status"] = "unexpected_dependency_change"
                            save()
                            raise SpaceError("Cache records outside this exact target changed; stopped. See receipt.")
                    expected_inventory = after_inventory
                    step["after"] = host.snapshot(args.disk_image)
                    step["delta"] = host_delta(step["before"], step["after"])
                    step["status"] = "completed"
                    if args.action == "cache" and target not in step["cache_records_disappeared"]:
                        step["status"] = "skipped"
                        step["note"] = "Engine retained this record (for example, a dependent still references it); no wider prune attempted."
                    save()
            # Recheck cache after image deletion: shared/private status changes.
            receipt["after_inventory"] = docker.inventory()
            deadline = time.monotonic() + 60
            while True:
                receipt["after"] = host.snapshot(args.disk_image)
                receipt["delta"] = host_delta(receipt["baseline"], receipt["after"])
                save()
                if receipt["delta"].get("recovery_observed") or time.monotonic() >= deadline:
                    break
                time.sleep(min(2, max(0, deadline - time.monotonic())))
            receipt["status"] = "completed"
        except BaseException as exc:
            receipt["status"] = "partial"
            receipt["error"] = str(exc) or type(exc).__name__
            receipt["finished_at"] = now()
            with contextlib.suppress(Exception):
                save()
            raise
        finally:
            receipt["finished_at"] = now()
        save()
        if not automatic:
            print_recovery(receipt["delta"], receipt_path, args.wrapper, "Cleanup complete")
            skipped = sum(step["status"] == "skipped" for step in receipt["steps"])
            if skipped:
                row("Retained", f"{skipped} cache records still referenced by Docker")
            if args.action == "images":
                location = f" --disk-image {shlex.quote(str(args.disk_image))}" if args.disk_image else ""
                row("Next step", f"{args.wrapper} --space cache{location}")
        if automatic:
            pending = directory / "pending-build"
            if pending.read_text().strip() == pending_id:
                pending.unlink()
        return receipt


def parser():
    result = argparse.ArgumentParser(allow_abbrev=False, prog="dclaude --space", formatter_class=argparse.RawDescriptionHelpFormatter, description="Measure and explicitly reclaim local macOS Docker Desktop storage. Read-only by default.",
        epilog="""Commands (also available through dcodex):
  images                 Preview launcher image families and their protections.
  images --keep 2 --apply Review and confirm exact old image targets.
  cache                  Preview private unused default-builder cache records.
  cache --apply          Confirm separate builder-wide cache deletion.
  verify                 Remeasure the latest receipt; never delete anything.
  retention enable       Enable labelled-image retention after build/bootstrap.
  retention status       Show the saved image policy.
  retention disable      Stop automatic image cleanup; retain all history.

Requires host Python 3 and local macOS Docker Desktop with Engine API 1.48+
for storage operations.
Runs before repository lookup, builds, updates, or agent startup. Ordinary
launches need no new Python installation. --apply requires a terminal; --yes
only authorizes launcher updates. Other Docker clients must be idle.
For moved Docker.raw files use --disk-image PATH from Desktop Settings.
Default output is a short summary; --json includes full IDs, references, and bytes.
--apply lists every exact target before asking for confirmation.
See docs/SPACE.md for examples, protections, receipts, and native cache GC setup.""")
    result.add_argument("action", nargs="?", choices=["images", "cache", "verify", "retention"], default="images")
    result.add_argument("retention_action", nargs="?", choices=["enable", "status", "disable"])
    result.add_argument("--keep", type=int, default=2, help="distinct newest launcher builds to preserve (default: 2; minimum: 1)")
    result.add_argument("--apply", action="store_true", help="print exact targets and ask for interactive deletion consent")
    result.add_argument("--json", action="store_true", help="read-only structured inventory with byte counts and protection reasons")
    result.add_argument("--disk-image", type=Path, help="Docker.raw path from Docker Desktop Settings when moved")
    result.add_argument("--tool-home", default=str(Path(__file__).resolve().parents[1]), help=argparse.SUPPRESS)
    result.add_argument("--current-image", default="dclaude:unknown", help=argparse.SUPPRESS)
    result.add_argument("--wrapper", default="dclaude", help=argparse.SUPPRESS)
    result.add_argument("--auto-retain", action="store_true", help=argparse.SUPPRESS)
    return result


def main(argv=None):
    arguments = list(sys.argv[1:] if argv is None else argv)
    args = parser().parse_args(arguments)
    try:
        if args.keep < 1:
            raise SpaceError("--keep must be at least 1")
        explicit_keep = any(a == "--keep" or a.startswith("--keep=") for a in arguments)
        if explicit_keep and args.action != "images" and not (args.action == "retention" and args.retention_action == "enable"):
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
        directory = state_dir()
        host = HostProbe()
        if args.action == "retention" and args.retention_action != "enable":
            policy = load_policy(directory)
            if args.retention_action == "disable":
                if policy:
                    with operation_lock(directory):
                        policy = load_policy(directory)
                        policy["enabled"] = False
                        write_json(directory / "policy.json", policy)
                print("Image retention disabled. Nothing deleted.")
            elif args.json:
                print(json.dumps(policy or dict(schema_version=SCHEMA, enabled=False), indent=2))
            else:
                print_policy(policy, args.wrapper)
            return 0
        if args.action == "verify":
            latest = read_json(directory / "latest.json")
            if not latest:
                raise SpaceError("No cleanup receipt to verify")
            receipt_path = Path(latest["receipt"])
            if receipt_path.parent.resolve() != (directory / "receipts").resolve():
                raise SpaceError("Invalid receipt location")
            receipt = read_json(receipt_path)
            if not receipt or "baseline" not in receipt or "disk_image" not in receipt:
                raise SpaceError("Invalid receipt; verification cannot infer a baseline")
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
        docker.connect()
        if args.action == "retention":
            baseline = host.snapshot(args.disk_image)
            if not baseline["complete"]:
                raise SpaceError(f"Retention needs a complete host baseline: {baseline['issues']}")
            if not RELEASE.fullmatch(args.current_image):
                raise SpaceError("Retention is limited to default dclaude release images")
            with operation_lock(directory):
                load_policy(directory)
                validate_latest(directory)
                policy = dict(schema_version=SCHEMA, enabled=True, keep=args.keep, repository="dclaude",
                              binding=docker.binding, disk_image=baseline["disk_image"]["path"], updated_at=now())
                write_json(directory / "policy.json", policy)
            print_policy(policy, args.wrapper)
            return 0
        if args.auto_retain:
            policy = load_policy(directory)
            if not policy or not policy["enabled"]:
                return 0
            args.action = "images"
            apply_cleanup(docker, host, args, directory, automatic=True)
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
        else:
            print(f"{args.wrapper} --space: {exc}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        print("Interrupted; any partial cleanup is recorded in the receipt.", file=sys.stderr)
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
