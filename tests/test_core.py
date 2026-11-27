import copy
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from installer_tooling.common import cached_file, digest, keygen, safe_path, sign, verify, version
from installer_tooling.repository import Snapshot, compatible, select, validate_packages
from installer_tooling.registry import validate_registry


class CoreTests(unittest.TestCase):
    def registry(self):
        return {"schema": 1, "season": "2027", "channel": "prototype", "target": "linux-x64",
                "repositories": {name: {"url": "https://example.com/" + name,
                    "public_key": "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA=",
                    "components": ["org.example." + name]}
                    for name in ("zulu", "alpha")}}

    def test_registry_accepts_arbitrary_publishers_and_explicit_order(self):
        registry = self.registry()
        self.assertEqual(validate_registry(registry), ["alpha", "zulu"])
        registry["repository_order"] = ["zulu", "alpha"]
        registry["profiles"] = {"python": ["org.example.zulu"]}
        self.assertEqual(validate_registry(registry), ["zulu", "alpha"])
        # Object serialization/reordering cannot change precedence.
        self.assertEqual(validate_registry(json.loads(json.dumps(registry, sort_keys=True))), ["zulu", "alpha"])

    def test_registry_rejects_unsafe_publishers_duplicates_and_bad_profiles(self):
        for publisher in ("../robotpy", "robotpy/other", "C:", "ROBOTPY", "nul"):
            registry = self.registry()
            registry["repositories"][publisher] = registry["repositories"].pop("zulu")
            with self.subTest(publisher=publisher), self.assertRaisesRegex(ValueError, "Unsafe publisher"):
                validate_registry(registry)
        registry = self.registry()
        registry["repositories"]["zulu"]["components"] = ["org.example.alpha"]
        with self.assertRaisesRegex(ValueError, "multiple owners"):
            validate_registry(registry)
        for order in (["alpha"], ["alpha", "alpha"], ["alpha", "unknown"]):
            registry = self.registry()
            registry["repository_order"] = order
            with self.assertRaisesRegex(ValueError, "exactly once"):
                validate_registry(registry)
        registry = self.registry()
        registry["profiles"] = {"python": ["org.unknown.component"]}
        with self.assertRaisesRegex(ValueError, "unowned"):
            validate_registry(registry)
        registry["repositories"] = {}
        with self.assertRaisesRegex(ValueError, "at least one"):
            validate_registry(registry)

    def test_selection_obeys_configured_precedence_and_bounds_search(self):
        first = Snapshot({"publisher": "vendor", "revision": 1, "packages": []}, {}, "")
        second = Snapshot({"publisher": "robotpy", "revision": 1, "packages": []}, {}, "")
        with patch("installer_tooling.repository.candidates", return_value={"vendor": [first], "robotpy": [second]}):
            self.assertEqual(select({})[0], [first, second])
        candidates = {f"vendor{i}": [first] * 8 for i in range(5)}
        with patch("installer_tooling.repository.candidates", return_value=candidates), patch("installer_tooling.repository.compatible", return_value=False):
            with self.assertRaisesRegex(ValueError, "4096"):
                select({})

    def test_signature_rejects_tampering_and_wrong_publisher(self):
        with tempfile.TemporaryDirectory() as tmp:
            key = Path(tmp) / "one.pem"
            public = keygen(key)
            other = keygen(Path(tmp) / "two.pem")
            envelope = sign({"revision": 1}, key)
            self.assertEqual(verify(envelope, public), {"revision": 1})
            with self.assertRaisesRegex(ValueError, "signature"):
                verify(envelope, other)
            envelope["signed"]["revision"] = 2
            with self.assertRaisesRegex(ValueError, "signature"):
                verify(envelope, public)

    def test_paths_are_portable_and_cannot_escape(self):
        for value in ("../x", "/x", "C:/x", "a\\b", "a//b", "a/./b", "a/%2e%2e", "x?y", ""):
            with self.subTest(value=value), self.assertRaises(ValueError):
                safe_path(value)
        self.assertEqual(str(safe_path("org.wpilib.elastic/1.0.0data.7z")), "org.wpilib.elastic/1.0.0data.7z")

    def test_cache_verifies_payload_and_repairs_corruption(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            payload = root / "data"
            payload.write_bytes(b"payload")
            record = {"sha256": digest(payload), "size": 7}
            cached = cached_file(root.as_uri(), "data", record, root / "cache")
            cached.write_bytes(b"corrupt")
            self.assertEqual(cached_file(root.as_uri(), "data", record, root / "cache").read_bytes(), b"payload")
            cached.unlink()
            payload.write_bytes(b"changed")
            with self.assertRaisesRegex(ValueError, "mismatch"):
                cached_file(root.as_uri(), "data", record, root / "cache")

    def test_ownership_and_namespace_validation(self):
        package = {"id": "org.wpilib.elastic", "version": "1.0.0", "owned_files": ["tools/app"]}
        with self.assertRaisesRegex(ValueError, "Unauthorized"):
            validate_packages([package], ["org.wpilib.runtime"])
        other = dict(package, id="org.wpilib.other", owned_files=["TOOLS/App"])
        with self.assertRaisesRegex(ValueError, "ownership"):
            validate_packages([package, other], [package["id"], other["id"]])

    def test_joint_dependency_upgrade_and_holdback(self):
        def snapshot(publisher, rev, dependency=None):
            name = "org.wpilib." + publisher
            package = {"id": name, "version": f"{rev}.0.0", "owned_files": [publisher],
                       "dependencies": {"org.wpilib.core": dependency} if dependency else {}}
            return Snapshot({"publisher": publisher, "revision": rev, "packages": [package]}, {}, "")
        c1, c2 = snapshot("core", 1), snapshot("core", 2)
        e1, e2, e3 = snapshot("elastic", 1), snapshot("elastic", 2, "2.0.0"), snapshot("elastic", 3, "9.0.0")
        a1 = snapshot("advantagescope", 1)
        with patch("installer_tooling.repository.candidates", return_value={
            "core": [c2, c1], "elastic": [e3, e2, e1], "advantagescope": [a1]
        }):
            selected, held = select({})
        self.assertEqual(selected, [c2, e2, a1])
        self.assertEqual(held[0]["latest"], 3)
        self.assertFalse(compatible([c1, e2, a1]))

    def test_numeric_versions(self):
        self.assertGreater(version("10.0"), version("2.9"))
        self.assertEqual(version("1"), version("1.0.0"))
        with self.assertRaises(ValueError):
            version("v1.0.0-beta")


if __name__ == "__main__":
    unittest.main()
