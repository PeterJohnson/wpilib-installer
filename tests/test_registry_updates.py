import copy
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from urllib.error import URLError

from installer_tooling.common import atomic_json, keygen, sign
from installer_tooling.registry_updates import (cache_directory, cache_lock, publish_registry,
                                                refresh, registry_identity, resolve)


class RegistryUpdateTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.now = 1_800_000_000
        self.key = self.root / "authority.pem"
        authority = keygen(self.key)
        self.publisher_key = self.root / "publisher.pem"
        publisher = keygen(self.publisher_key)
        self.bundle_key = self.root / "bundle.pem"
        bundle = keygen(self.bundle_key)
        self.cache = self.root / "cache"
        self.feed = self.root / "feed"
        self.source = {"kind": "registry-source", "schema": 1, "registry_id": "test-registry",
            "target": "linux-x64", "season": "2027", "channel": "prototype", "public_key": authority,
            "url": (self.feed / "registry.json").as_uri(), "minimum_revision": 1}
        self.registry = {"schema": 1, "target": "linux-x64", "season": "2027", "channel": "prototype",
            "bundle_public_key": bundle, "repositories": {"core": {"url": "https://example.org/core",
            "public_key": publisher, "components": ["org.example.core"]}}}

    def publish(self, revision, **kwargs):
        publish_registry(self.registry, self.source, self.key, revision, self.feed, now=self.now, **kwargs)
        return json.loads((self.feed / "registry.json").read_text())

    def current(self):
        return cache_directory(self.source, self.cache) / "current.json"

    def test_cache_refresh_lock_excludes_concurrent_writers_and_releases(self):
        directory = cache_directory(self.source, self.cache)
        with cache_lock(directory):
            with self.assertRaisesRegex(ValueError, "in progress"):
                with cache_lock(directory):
                    self.fail("Concurrent refresh acquired the lock")
        with cache_lock(directory):
            pass

    def test_refresh_adds_publisher_and_profile_without_changing_bootstrap(self):
        source_before = copy.deepcopy(self.source)
        self.publish(1)
        self.assertEqual(refresh(self.source, self.cache, now=self.now).summary()["revision"], 1)
        self.registry["repositories"]["robotpy"] = {
            "url": "https://example.org/robotpy", "public_key": keygen(self.root / "robotpy.pem"),
            "components": ["org.robotpy.python"]}
        self.registry["profiles"] = {"python": ["org.robotpy.python"]}
        self.publish(2)
        view = refresh(self.source, self.cache, now=self.now)
        self.assertEqual(view.registry["profiles"]["python"], ["org.robotpy.python"])
        self.assertEqual(view.summary()["revision"], 2)
        self.assertEqual(self.source, source_before)

    def test_network_failure_uses_only_unexpired_verified_cache(self):
        self.publish(1, valid_days=1, offline_days=30)
        refresh(self.source, self.cache, now=self.now)
        with patch("installer_tooling.registry_updates.read_json_url", side_effect=URLError("unavailable")):
            view = refresh(self.source, self.cache, now=self.now + 10)
            self.assertEqual(view.status, "verified-cache")
            self.assertTrue(view.warning)
            with self.assertRaisesRegex(ValueError, "expired"):
                refresh(self.source, self.cache, now=self.now + 86400)
            with self.assertRaisesRegex(ValueError, "no verified cache"):
                refresh(self.source, self.root / "empty-cache", now=self.now)

    def test_invalid_signature_does_not_fall_back_or_overwrite_cache(self):
        record = self.publish(1)
        refresh(self.source, self.cache, now=self.now)
        saved = self.current().read_bytes()
        record["signed"]["revision"] = 2
        atomic_json(self.feed / "registry.json", record)
        with self.assertRaisesRegex(ValueError, "signature"):
            refresh(self.source, self.cache, now=self.now)
        self.assertEqual(self.current().read_bytes(), saved)
        # Cached bytes are reverified, even with a healthy feed.
        atomic_json(self.current(), record)
        atomic_json(self.feed / "registry.json", json.loads((self.feed / "revisions/1.json").read_text()))
        self.publish(3)
        with self.assertRaisesRegex(ValueError, "signature"):
            refresh(self.source, self.cache, now=self.now)

    def test_publisher_key_cannot_authorize_registry(self):
        record = self.publish(1)
        atomic_json(self.feed / "registry.json", sign(record["signed"], self.publisher_key))
        with self.assertRaisesRegex(ValueError, "signature"):
            refresh(self.source, self.cache, now=self.now)

    def test_rollback_and_equivocation_rejected_across_calls(self):
        old = self.publish(1)
        current = self.publish(2)
        refresh(self.source, self.cache, now=self.now)
        saved = self.current().read_bytes()
        atomic_json(self.feed / "registry.json", old)
        with self.assertRaisesRegex(ValueError, "rollback"):
            resolve(self.source, self.cache, now=self.now)
        changed = copy.deepcopy(current["signed"])
        changed["registry"]["profiles"] = {"tools": ["org.example.core"]}
        atomic_json(self.feed / "registry.json", sign(changed, self.key))
        with self.assertRaisesRegex(ValueError, "without increasing"):
            refresh(self.source, self.cache, now=self.now)
        self.assertEqual(self.current().read_bytes(), saved)

    def test_scope_identity_and_expiry_checked_even_with_valid_signature(self):
        record = self.publish(1)
        for field, value in (("registry_id", "other"), ("expires_at", self.now - 1)):
            changed = copy.deepcopy(record["signed"])
            changed[field] = value
            atomic_json(self.feed / "registry.json", sign(changed, self.key))
            with self.subTest(field=field), self.assertRaises(ValueError):
                refresh(self.source, self.cache, now=self.now)
        for field, value in (("target", "windows-x64"), ("season", "2028"), ("channel", "stable")):
            changed = copy.deepcopy(record["signed"])
            changed["registry"][field] = value
            atomic_json(self.feed / "registry.json", sign(changed, self.key))
            with self.subTest(field=field), self.assertRaisesRegex(ValueError, "mismatch"):
                refresh(self.source, self.cache, now=self.now)

    def test_expired_cache_retains_floor_but_accepts_new_revision(self):
        old = self.publish(1, valid_days=1)
        refresh(self.source, self.cache, now=self.now)
        self.now += 2 * 86400
        self.publish(2)
        self.source["minimum_revision"] = 2
        self.assertEqual(refresh(self.source, self.cache, now=self.now).summary()["revision"], 2)
        # Moving the URL preserves the same cache namespace.
        moved = dict(self.source, url="https://example.org/moved.json")
        self.assertEqual(cache_directory(moved, self.cache), cache_directory(self.source, self.cache))

    def test_archival_bundle_uses_pinned_registry_without_network_or_cache_rollback(self):
        old = self.publish(1, valid_days=1, offline_days=30)
        bundle = self.root / "bundle"
        bundle.mkdir()
        atomic_json(bundle / "registry.json", old)
        manifest = {"schema": 2, "registry": registry_identity(old)}
        atomic_json(bundle / "bundle-manifest.json", sign(manifest, self.bundle_key))
        self.registry["profiles"] = {"new-profile": ["org.example.core"]}
        self.publish(2)
        refresh(self.source, self.cache, now=self.now)
        saved = self.current().read_bytes()
        with patch("installer_tooling.registry_updates.read_json_url", side_effect=AssertionError("network used")):
            view = resolve(self.source, self.cache, bundle=bundle, now=self.now + 2 * 86400)
            self.assertEqual(view.summary()["revision"], 1)
            self.assertNotIn("new-profile", view.registry.get("profiles", {}))
            self.assertEqual(self.current().read_bytes(), saved)
            with self.assertRaisesRegex(ValueError, "expired"):
                resolve(self.source, self.cache, bundle=bundle, now=self.now + 30 * 86400)
        atomic_json(bundle / "registry.json", json.loads((self.feed / "registry.json").read_text()))
        with self.assertRaisesRegex(ValueError, "identity/hash"):
            resolve(self.source, self.cache, bundle=bundle, now=self.now)

    def test_unsigned_registry_or_forged_bundle_cannot_bootstrap_trust(self):
        old = self.publish(1)
        bundle = self.root / "bundle"
        bundle.mkdir()
        atomic_json(bundle / "registry.json", old)
        atomic_json(bundle / "bundle-manifest.json", sign({"schema": 2, "registry": registry_identity(old)}, self.publisher_key))
        with self.assertRaisesRegex(ValueError, "signature"):
            resolve(self.source, self.cache, bundle=bundle, now=self.now)
        atomic_json(self.feed / "registry.json", self.registry)
        with self.assertRaisesRegex(ValueError, "signature"):
            resolve(self.source, self.cache, now=self.now)


if __name__ == "__main__":
    unittest.main()
