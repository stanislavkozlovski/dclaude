"""Real filesystem/APFS checks; no Docker daemon or fixture stubs required."""

import importlib.util
import os
from pathlib import Path
import platform
import sys
import tempfile
import unittest


SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "space.py"
SPEC = importlib.util.spec_from_file_location("dclaude_space_macos", SCRIPT)
space = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = space
SPEC.loader.exec_module(space)


@unittest.skipUnless(platform.system() == "Darwin", "requires real macOS/APFS")
class MacOSStorageProbes(unittest.TestCase):
    def test_sparse_capacity_is_not_reported_as_occupied_space(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "sparse.raw"
            with path.open("wb") as handle:
                handle.seek(64 * 1024 * 1024 - 1)
                handle.write(b"x")
                handle.flush()
                os.fsync(handle.fileno())
            measurement = space.measure_file(path)

            self.assertEqual(measurement["apparent_bytes"], 64 * 1024 * 1024)
            self.assertGreater(measurement["allocated_bytes"], 0)
            self.assertLess(measurement["allocated_bytes"], measurement["apparent_bytes"])

    def test_hardlink_preserves_disk_image_identity(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "disk.raw"
            alias = Path(directory) / "disk-alias.raw"
            path.write_bytes(os.urandom(8192))
            os.link(path, alias)

            original = space.measure_file(path)
            linked = space.measure_file(alias)

            self.assertEqual(original["device"], linked["device"])
            self.assertEqual(original["inode"], linked["inode"])
            self.assertEqual(original["allocated_bytes"], linked["allocated_bytes"])

    def test_snapshot_resolves_apfs_without_docker(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "disk.raw"
            path.write_bytes(os.urandom(8192))
            snapshot = space.HostProbe().snapshot(path)

            self.assertTrue(snapshot["complete"], snapshot["issues"])
            self.assertFalse(snapshot["issues"])
            self.assertEqual(snapshot["disk_image"]["inode"], path.stat().st_ino)
            self.assertTrue(snapshot["measured_at"])
            containers = snapshot["containers"]
            self.assertTrue(containers)
            identities = [container["uuid"] for container in containers]
            self.assertEqual(len(identities), len(set(identities)))
            roles = {role for container in containers for role in container["roles"]}
            self.assertEqual(roles, {"startup", "docker"})
            for container in containers:
                self.assertGreater(container["capacity_bytes"], 0)
                self.assertGreaterEqual(container["free_bytes"], 0)
                self.assertLessEqual(container["free_bytes"], container["capacity_bytes"])

    def test_missing_image_is_unmeasured_not_zero(self):
        with tempfile.TemporaryDirectory() as directory:
            missing = Path(directory) / "missing.raw"
            snapshot = space.HostProbe().snapshot(missing)

            self.assertFalse(snapshot["complete"])
            self.assertIsNone(snapshot["disk_image"])
            self.assertTrue(snapshot["issues"])
            self.assertTrue(any(str(missing) in str(issue) for issue in snapshot["issues"]))

    @unittest.skipIf(os.geteuid() == 0, "root bypasses filesystem permissions")
    def test_denied_path_is_unmeasured_not_zero(self):
        with tempfile.TemporaryDirectory() as directory:
            private = Path(directory) / "private"
            private.mkdir()
            path = private / "disk.raw"
            path.write_bytes(b"allocated data")
            private.chmod(0)
            try:
                snapshot = space.HostProbe().snapshot(path)
            finally:
                private.chmod(0o700)

            self.assertFalse(snapshot["complete"])
            self.assertIsNone(snapshot["disk_image"])
            self.assertTrue(any("denied" in str(issue).lower() for issue in snapshot["issues"]))


if __name__ == "__main__":
    unittest.main()
