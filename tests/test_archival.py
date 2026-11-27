import copy
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from installer_tooling.archival import INDEFINITE_POLICY, authorization, verify_archival
from installer_tooling.common import atomic_json, keygen, sign
from installer_tooling.registry_updates import (cache_directory, publish_registry, refresh,
    registry_identity, resolve, verified_record)
from installer_tooling.tuf_repository import demo_trust


class IndefiniteArchivalTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.authority = self.root / "authority.pem"
        self.bundle_key = self.root / "bundle.pem"
        self.source = {"kind": "registry-source", "schema": 1, "registry_id": "archive-test",
            "target": "linux-x64", "season": "2027", "channel": "stable",
            "url": (self.root / "feed/registry.json").as_uri(), "public_key": keygen(self.authority),
            "archive_policy": dict(INDEFINITE_POLICY),
            "tuf": demo_trust(self.root / "registry-tuf", "authority", (self.root / "feed/tuf").as_uri())}
        self.registry = {"schema": 1, "target": "linux-x64", "season": "2027", "channel": "stable",
            "online_trust": "tuf", "archive_policy": dict(INDEFINITE_POLICY),
            "bundle_public_key": keygen(self.bundle_key), "repositories": {"core": {
                "url": "https://example.org/core", "components": ["org.example.core"],
                "public_key": keygen(self.root / "publisher.pem"),
                "tuf": demo_trust(self.root / "core-tuf", "core", "https://example.org/core/tuf")}}}
        path = publish_registry(self.registry, self.source, self.authority, 1, self.root / "feed")
        self.record = json.loads(path.read_text())
        from types import SimpleNamespace
        evidence = {"repository_id": "authority", "catalog_sha256": "a" * 64}
        snapshots = [SimpleNamespace(publisher="core", tuf_evidence={"repository_id": "core", "catalog_sha256": "b" * 64})]
        self.manifest = {"schema": 3, "registry": registry_identity(self.record),
            "archival": authorization(self.registry, self.record, evidence, snapshots)}
        self.bundle = self.root / "usb"
        atomic_json(self.bundle / "registry.json", self.record)
        atomic_json(self.bundle / "bundle-manifest.json", sign(self.manifest, self.bundle_key))

    def test_offline_authorization_is_independent_of_age_and_wall_clock(self):
        cache = self.root / "cache"
        publish_registry(self.registry, self.source, self.authority, 2, self.root / "feed")
        refresh(self.source, cache)
        saved = {str(p.relative_to(cache)): p.read_bytes() for p in cache.rglob("*") if p.is_file()}
        for now in (0, self.record["signed"]["issued_at"] + 366 * 86400, 4_102_444_800):  # 2100
            for directory in (cache, self.root / "cold-cache"):
                with self.subTest(now=now, cache=directory.name), patch(
                    "installer_tooling.tuf_repository.Fetcher._fetch", side_effect=AssertionError("network used")
                ), patch("installer_tooling.archival.time.time", return_value=now):
                    view = resolve(self.source, directory, bundle=self.bundle)
                    self.assertEqual(view.record, self.record)
                    verify_archival(self.manifest, view.registry, view.record)
        self.assertFalse((self.root / "cold-cache").exists())
        self.assertEqual(saved, {str(p.relative_to(cache)): p.read_bytes() for p in cache.rglob("*") if p.is_file()})
        self.assertEqual(json.loads((cache_directory(self.source, cache) / "current.json").read_text())["record"]["signed"]["revision"], 2)

    def test_indefinite_archive_does_not_extend_online_registry_authorization(self):
        expiry = self.record["signed"]["expires_at"]
        with self.assertRaisesRegex(ValueError, "online authorization expired"):
            verified_record(self.record, self.source, now=expiry)
        self.assertEqual(verified_record(self.record, self.source, mode="offline", now=expiry), self.registry)
        with self.assertRaisesRegex(ValueError, "not yet valid"):
            verified_record(self.record, self.source, now=0)

    def test_indefinite_policy_and_null_expiry_must_be_explicitly_authorized(self):
        for mutation in ("missing", "finite", "different-policy"):
            record = copy.deepcopy(self.record["signed"])
            if mutation == "missing":
                del record["offline_expires_at"]
            elif mutation == "finite":
                record["offline_expires_at"] = record["issued_at"] + 365 * 86400
            else:
                record["registry"]["archive_policy"] = {"name": "wpilib-archive-v1", "max_days": 365}
            with self.subTest(mutation=mutation), self.assertRaises(ValueError):
                verified_record(sign(record, self.authority), self.source, mode="offline")
        bounded = {**self.source, "archive_policy": {"name": "wpilib-archive-v1", "max_days": 365}}
        with self.assertRaises(ValueError):
            verified_record(self.record, bounded, mode="offline")
        for policy in ({"name": "wpilib-archive-v2"}, {**INDEFINITE_POLICY, "max_days": 365},
                       {"name": "wpilib-archive-v2", "lifetime": "forever"}):
            with self.subTest(policy=policy), self.assertRaisesRegex(ValueError, "explicit archival"):
                verified_record(self.record, {**self.source, "archive_policy": policy}, mode="offline")

    def test_bundle_signer_cannot_change_authority_policy_or_provenance(self):
        for field, value in (("expires_at", 4_102_444_800), ("issued_at", 1),
                             ("policy", "wpilib-archive-v1"), ("not_before", 1), ("publishers_tuf", {})):
            manifest = copy.deepcopy(self.manifest)
            manifest["archival"][field] = value
            with self.subTest(field=field), self.assertRaises(ValueError):
                verify_archival(manifest, self.registry, self.record)
        manifest = copy.deepcopy(self.manifest)
        del manifest["archival"]["expires_at"]
        with self.assertRaises(ValueError):
            verify_archival(manifest, self.registry, self.record)
        forged = copy.deepcopy(self.record)
        forged["signed"]["registry"]["profiles"] = {"forged": ["org.example.core"]}
        with self.assertRaisesRegex(ValueError, "signature"):
            verified_record(forged, self.source, mode="offline", now=4_102_444_800)

    def test_old_bounded_authorization_still_expires_and_cannot_be_reinterpreted(self):
        source, registry = copy.deepcopy(self.source), copy.deepcopy(self.registry)
        source["archive_policy"] = registry["archive_policy"] = {"name": "wpilib-archive-v1", "max_days": 365}
        path = publish_registry(registry, source, self.authority, 1, self.root / "bounded", update_tuf=False)
        record = json.loads(path.read_text())
        manifest = copy.deepcopy(self.manifest)
        manifest["archival"].pop("issued_at")
        manifest["archival"].update(policy="wpilib-archive-v1", not_before=record["signed"]["issued_at"],
                                   expires_at=record["signed"]["offline_expires_at"])
        expiry = record["signed"]["offline_expires_at"]
        verify_archival(manifest, registry, record, now=expiry - 1)
        with self.assertRaisesRegex(ValueError, "expired"):
            verify_archival(manifest, registry, record, now=expiry)
        with self.assertRaisesRegex(ValueError, "expired"):
            verified_record(record, source, mode="offline", now=expiry)
        with self.assertRaises(ValueError):
            verified_record(record, self.source, mode="offline", now=expiry)

    def test_ci_renewal_keeps_indefinite_authorization_and_rejects_stale_day_setting(self):
        from installer_tooling.ci_publication import tuf_registry_release
        from installer_tooling.tuf_repository import DirectoryStore, ROLES
        keys = {role: [self.root / "registry-tuf" / (role + ".pem")] for role in ROLES}
        with self.assertRaisesRegex(ValueError, "offline_days conflicts"):
            publish_registry(self.registry, self.source, self.authority, 2, self.root / "feed", offline_days=365)
        with self.assertRaisesRegex(ValueError, "offline_days conflicts"):
            tuf_registry_release(self.source, DirectoryStore(self.root / "feed"), self.authority, keys, offline_days=365)
        result = tuf_registry_release(self.source, DirectoryStore(self.root / "feed"), self.authority, keys,
                                      now=self.record["signed"]["expires_at"] + 1)
        self.assertEqual(result["revision"], 2)
        renewed = json.loads((self.root / "feed/revisions/2.json").read_text())
        self.assertIsNone(renewed["signed"]["offline_expires_at"])
        self.assertEqual(verified_record(renewed, self.source, mode="offline", now=4_102_444_800), self.registry)

    def test_download_page_reports_indefinite_authorization_and_round_trips(self):
        from installer_tooling.bundle_publication import read_page, render_page
        publication = {"kind": "offline-download", "schema": 1, "id": "fixture",
            "target": "linux-x64", "season": "2027", "channel": "stable", "size": 100,
            "sha256": "c" * 64, "registry": registry_identity(self.record),
            "repositories": {"core": {"revision": 1, "sha256": "d" * 64}},
            "components": [], "held_back": [], "payload_kind": "prototype-fixtures"}
        state = {"current": {"registry_record": self.record, "publication": sign(publication, self.bundle_key)}}
        page = render_page(state)
        self.assertIn(b"offline installation has no expiration date", page)
        self.assertEqual(read_page(page, self.source), state)
