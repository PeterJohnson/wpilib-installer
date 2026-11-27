import copy
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import shutil
import tempfile
import unittest
from unittest.mock import patch

from tuf.api import exceptions
from tuf.api.metadata import Metadata

from installer_prototype.common import atomic_json, canonical, keygen, sign
from installer_prototype.registry_updates import (cache_directory, publish_registry, refresh, resolve,
    validated_tuf_record, verified_record)
from installer_prototype.tuf_repository import (DirectoryStore, Fetcher, ROLES, RootUpdatePending,
    create_root, encoded, fetch_catalog, origin_state, publish_catalog)


class LongRootHistoryTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.fixture = tempfile.TemporaryDirectory()
        cls.addClassCleanup(cls.fixture.cleanup)
        cls.base = Path(cls.fixture.name)
        cls.keys = []
        for group in ("old", "new"):
            keys = {role: [cls.base / group / (role + ".pem")] for role in ROLES}
            keys["root"] = [cls.base / group / f"root-{i}.pem" for i in range(3)]
            for paths in keys.values():
                for path in paths:
                    keygen(path)
            cls.keys.append(keys)
        roots = {}
        previous = None
        for version in range(1, 521):
            keys = cls.keys[version >= 257]
            if version == 257:
                keys = {**keys, "old_root": cls.keys[0]["root"]}
            with patch("installer_prototype.tuf_repository.datetime") as clock:
                clock.now.return_value = (datetime(2018, 1, 1, tzinfo=timezone.utc) if version < 520
                                         else datetime.now(timezone.utc))
                previous = create_root(keys, version=version, previous=previous, root_threshold=2)
            roots[f"tuf/metadata/{version}.root.json"] = canonical(previous)
        cls.bootstrap = json.loads(roots["tuf/metadata/1.root.json"])
        cls.last = previous
        store = DirectoryStore(cls.base / "origin")
        store.publish_immutable(roots)
        trust = {"repository_id": "long-root-test", "url": (cls.base / "origin/tuf").as_uri(), "root": cls.bootstrap}
        publish_catalog(store, trust, cls.keys[-1], {"release": "current"}, initialize=True)

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.origin = self.root / "origin"
        shutil.copytree(self.base / "origin", self.origin)
        self.trust = {"repository_id": "long-root-test", "url": (self.origin / "tuf").as_uri(), "root": self.bootstrap}
        self.cache = self.root / "cache"

    def test_expired_bootstrap_and_intermediates_cross_multiple_batches_and_key_rotations(self):
        catalog, status, evidence = fetch_catalog(self.trust, self.cache)
        self.assertEqual(catalog, {"release": "current"})
        self.assertEqual(status, "verified-online")
        self.assertEqual(evidence["root_version"], 520)
        with patch.object(Fetcher, "_fetch", side_effect=exceptions.DownloadError("offline")):
            self.assertEqual(fetch_catalog(self.trust, self.cache, fallback=True)[1], "verified-cache")

    def test_default_work_budget_saves_progress_and_next_updater_can_finish(self):
        store = DirectoryStore(self.origin)
        previous = self.last
        for version in range(521, 1031):
            previous = create_root(self.keys[-1], version=version, previous=previous, root_threshold=2)
            store.publish_immutable({f"tuf/metadata/{version}.root.json": canonical(previous)})
        publish_catalog(store, self.trust, self.keys[-1], {"release": "after-1030"})
        requested = []
        fetch = Fetcher._fetch
        def track(instance, url):
            requested.append(url)
            yield from fetch(instance, url)
        with patch.object(Fetcher, "_fetch", track):
            with self.assertRaises(RootUpdatePending):
                fetch_catalog(self.trust, self.cache, fallback=True)
        self.assertFalse(any(url.endswith("timestamp.json") for url in requested))
        self.assertEqual(len(list(self.cache.rglob("*.root.json"))), 1025)
        # A new Updater still replays every cached signature from the old pin.
        catalog, _, evidence = fetch_catalog(self.trust, self.cache)
        self.assertEqual(catalog, {"release": "after-1030"})
        self.assertEqual(evidence["root_version"], 1030)

    def test_intermediate_requires_both_thresholds_even_after_a_batch_boundary(self):
        path = self.origin / "tuf/metadata/257.root.json"
        original = json.loads(path.read_bytes())
        for side in ("old", "new"):
            metadata = Metadata.from_bytes(canonical(original))
            ids = list(metadata.signed.roles["root"].keyids)
            if side == "new":
                metadata.signatures = {k: v for k, v in metadata.signatures.items() if k not in ids[1:]}
            else:
                old = list(Metadata.from_bytes(canonical(self.bootstrap)).signed.roles["root"].keyids)
                metadata.signatures = {k: v for k, v in metadata.signatures.items() if k not in old[1:]}
            path.write_bytes(encoded(metadata))
            with self.subTest(side=side), self.assertRaises(exceptions.UnsignedMetadataError):
                fetch_catalog(self.trust, self.root / (side + "-cache"), fallback=True)

    def test_missing_skipped_and_expired_final_roots_fail_closed(self):
        path = self.origin / "tuf/metadata/300.root.json"
        original = path.read_bytes()
        path.unlink()
        with self.assertRaises(exceptions.ExpiredMetadataError):
            fetch_catalog(self.trust, self.cache, fallback=True)
        path.write_bytes((self.origin / "tuf/metadata/301.root.json").read_bytes())
        with self.assertRaises(exceptions.BadVersionNumberError):
            fetch_catalog(self.trust, self.cache, fallback=True)
        path.write_bytes(original)
        metadata = Metadata.from_file(str(self.origin / "tuf/metadata/520.root.json"))
        metadata.signed.expires = datetime.now(timezone.utc) - timedelta(days=1)
        from installer_prototype.tuf_repository import signer
        for key in self.keys[-1]["root"]:
            metadata.sign(signer(key), append=True)
        (self.origin / "tuf/metadata/520.root.json").write_bytes(encoded(metadata))
        with self.assertRaises(exceptions.ExpiredMetadataError):
            fetch_catalog(self.trust, self.root / "expired-cache", fallback=True)

    def test_cached_chain_is_reverified_and_origin_renewal_has_no_256_limit(self):
        fetch_catalog(self.trust, self.cache)
        publish_catalog(DirectoryStore(self.origin), self.trust, self.keys[-1])
        self.assertEqual(origin_state(DirectoryStore(self.origin), self.trust)[0].signed.version, 520)
        cached = next(self.cache.rglob("257.root.json"))
        bad = json.loads(cached.read_bytes())
        bad["signed"]["expires"] = "2999-01-01T00:00:00Z"
        cached.write_bytes(canonical(bad))
        (self.origin / "tuf/metadata/257.root.json").write_bytes(canonical(bad))
        with self.assertRaises(exceptions.UnsignedMetadataError):
            fetch_catalog(self.trust, self.cache, fallback=True)
        with self.assertRaises(exceptions.UnsignedMetadataError):
            origin_state(DirectoryStore(self.origin), self.trust)


class ArchivalKeyRotationTests(unittest.TestCase):
    def setUp(self):
        # Reuse only fixture setup, not the other class's test methods.
        from tests.test_archival import IndefiniteArchivalTests
        IndefiniteArchivalTests.setUp(self)
        self.cache = self.root / "cache"
        self.store = DirectoryStore(self.root / "feed")
        self.keys = {role: [self.root / "registry-tuf" / (role + ".pem")] for role in ROLES}
        self.new_key = self.root / "new-archive.pem"
        self.new_public = keygen(self.new_key)

    def rotate(self):
        from installer_prototype.ci_publication import tuf_registry_release
        return tuf_registry_release(self.source, self.store, self.new_key, self.keys)

    def test_online_cold_warm_and_fallback_survive_archival_key_rotation(self):
        refresh(self.source, self.cache)
        self.assertEqual(self.rotate()["revision"], 2)  # Rotates even when not due.
        for cache in (self.cache, self.root / "cold-cache"):
            view = refresh(self.source, cache)
            self.assertEqual(view.record["signed"]["revision"], 2)
            self.assertEqual(view.registry, self.registry)
            with self.assertRaisesRegex(ValueError, "signature"):
                verified_record(view.record, self.source, mode="offline")
            with patch.object(Fetcher, "_fetch", side_effect=exceptions.DownloadError("offline")):
                self.assertEqual(refresh(self.source, cache).status, "verified-cache")
        newer_bootstrap = {**self.source, "public_key": self.new_public}
        self.assertEqual(cache_directory(newer_bootstrap, self.cache), cache_directory(self.source, self.cache))
        self.assertEqual(refresh(newer_bootstrap, self.cache).record, view.record)
        self.assertEqual(verified_record(view.record, newer_bootstrap, mode="offline"), self.registry)
        self.assertEqual(resolve(self.source, self.cache, bundle=self.bundle).record, self.record)
        self.assertEqual(self.rotate()["revision"], 2)

    def test_archival_signer_alone_cannot_change_online_catalog_and_cache_proof(self):
        refresh(self.source, self.cache)
        path = cache_directory(self.source, self.cache) / "current.json"
        saved = json.loads(path.read_text())
        forged = copy.deepcopy(saved)
        forged["record"]["signed"]["revision"] = 999
        forged["record"] = sign(forged["record"]["signed"], self.authority)
        atomic_json(path, forged)
        with self.assertRaisesRegex(ValueError, "differs from its TUF evidence"):
            refresh(self.source, self.cache)
        atomic_json(path, saved)
        target = next((self.root / "feed/tuf/targets").glob("*.catalog.json"))
        target.write_bytes(canonical(sign({**self.record["signed"], "revision": 9}, self.authority)))
        with self.assertRaises(exceptions.LengthOrHashMismatchError):
            refresh(self.source, self.root / "empty-cache")

    def test_history_protects_revision_floor_and_online_expiry_after_key_rotation(self):
        self.rotate()
        refresh(self.source, self.cache)
        path = cache_directory(self.source, self.cache) / "current.json"
        saved = path.read_bytes()
        # Even a freshly TUF-signed accidental registry rollback must fail.
        publish_catalog(self.store, self.source["tuf"], self.keys, self.record)
        with self.assertRaisesRegex(ValueError, "rollback"):
            refresh(self.source, self.cache)
        self.assertEqual(path.read_bytes(), saved)
        # Fresh TUF metadata cannot make an expired registry payload valid.
        expired = copy.deepcopy(self.record)
        expired["signed"].update(revision=3, issued_at=1, expires_at=2)
        publish_catalog(self.store, self.source["tuf"], self.keys, expired)
        with self.assertRaisesRegex(ValueError, "online authorization expired"):
            refresh(self.source, self.cache)

    def test_legacy_cache_floor_migrates_without_resetting_online_history(self):
        legacy_source = {k: v for k, v in self.source.items() if k != "tuf"}
        legacy = cache_directory(legacy_source, self.cache) / "current.json"
        atomic_json(legacy, self.record)
        self.rotate()
        view = refresh(self.source, self.cache)
        saved = json.loads((cache_directory(self.source, self.cache) / "current.json").read_text())
        self.assertEqual(saved["record"], view.record)
        self.assertIn("proof", saved)
        self.assertEqual(json.loads(legacy.read_text()), self.record)

    def test_expired_cache_evidence_is_only_a_floor_and_all_hash_links_are_reverified(self):
        import base64
        refresh(self.source, self.cache)
        path = cache_directory(self.source, self.cache) / "current.json"
        saved = json.loads(path.read_text())
        publish_catalog(self.store, self.source["tuf"], self.keys,
                        now=datetime.now(timezone.utc) - timedelta(days=3))
        # Model metadata legitimately cached before it expired, retaining its
        # authentic signatures and exact bytes, rather than changing a clock.
        for role in ROLES:
            saved["proof"][role] = base64.b64encode((self.root / f"feed/tuf/metadata/2.{role}.json").read_bytes()).decode()
        atomic_json(path, saved)
        with self.assertRaises(exceptions.ExpiredMetadataError):
            refresh(self.source, self.cache)
        publish_catalog(self.store, self.source["tuf"], self.keys)
        self.assertEqual(refresh(self.source, self.cache).record, self.record)
        good = json.loads(path.read_text())
        for role in ("timestamp", "snapshot", "targets", "catalog"):
            bad = copy.deepcopy(good)
            bad["proof"][role] = base64.b64encode(b"{}").decode()
            atomic_json(path, bad)
            with self.subTest(role=role), self.assertRaises((ValueError, KeyError, exceptions.RepositoryError)):
                refresh(self.source, self.cache)
        atomic_json(path, good)

    def test_online_validation_does_not_adopt_an_offline_pin_or_policy(self):
        self.rotate()
        changed = {**self.source, "public_key": "obsolete archival key",
                   "archive_policy": {"name": "wpilib-archive-v1", "max_days": 365}}
        self.assertEqual(refresh(changed, self.cache).registry, self.registry)
        with self.assertRaises(ValueError):
            verified_record(refresh(changed, self.cache).record, changed, mode="offline")
        with self.assertRaisesRegex(ValueError, "requires TUF"):
            validated_tuf_record(self.record, {k: v for k, v in self.source.items() if k != "tuf"})

    def test_offline_bundle_publication_requires_its_own_matching_pin(self):
        from installer_prototype.bundle_publication import prepare_bundle
        from tests.test_ci_publication import MemoryStore
        self.rotate()
        with patch("installer_prototype.bundle_publication.assemble") as assemble:
            with self.assertRaisesRegex(ValueError, "signature"):
                prepare_bundle(self.source, MemoryStore(), self.bundle_key, self.cache,
                               self.root / "candidate", "https://example.org/downloads", initialize=True)
            assemble.assert_not_called()
