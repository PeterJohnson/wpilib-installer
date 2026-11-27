import copy
from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from tuf.api import exceptions
from tuf.api.metadata import Metadata

from installer_prototype.common import canonical, keygen
from installer_prototype.tuf_repository import (DirectoryStore, ROLES, create_root, demo_trust,
    encoded, fetch_catalog, origin_state, publish_catalog)
from tests.test_ci_publication import MemoryStore


class TUFTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.origin = self.root / "origin"
        self.trust = demo_trust(self.root / "keys", "test-publisher", self.origin.as_uri() + "/tuf")
        self.keys = {role: [self.root / "keys" / (role + ".pem")] for role in ROLES}
        self.store = DirectoryStore(self.origin)
        self.cache = self.root / "cache"

    def publish(self, catalog=None, **kwargs):
        return publish_catalog(self.store, self.trust, self.keys, catalog, **kwargs)

    def fetch(self, **kwargs):
        return fetch_catalog(self.trust, self.cache, **kwargs)

    def test_real_updater_rejects_rollback_tampering_and_keeps_bootstrap_unchanged(self):
        original = copy.deepcopy(self.trust)
        self.publish({"revision": 1}, initialize=True)
        old = (self.origin / "tuf/metadata/timestamp.json").read_bytes()
        self.assertEqual(self.fetch()[0], {"revision": 1})
        self.publish({"revision": 2})
        self.assertEqual(self.fetch()[0], {"revision": 2})
        (self.origin / "tuf/metadata/timestamp.json").write_bytes(old)
        with self.assertRaises(exceptions.BadVersionNumberError):
            self.fetch(fallback=True)
        bad = json.loads(old)
        bad["signed"]["version"] = 100
        (self.origin / "tuf/metadata/timestamp.json").write_bytes(canonical(bad))
        with self.assertRaises(exceptions.UnsignedMetadataError):
            self.fetch(fallback=True)
        self.assertEqual(self.trust, original)

    def test_expired_online_metadata_cannot_be_used_or_fallback_but_renewal_recovers(self):
        self.publish({"revision": 1}, initialize=True,
                     now=datetime.now(timezone.utc) - timedelta(days=3))
        with self.assertRaises(exceptions.ExpiredMetadataError):
            self.fetch(fallback=True)
        self.publish()
        self.assertEqual(self.fetch()[0], {"revision": 1})

    def test_transport_fallback_reverifies_cache_and_never_ignores_expiry(self):
        self.publish({"revision": 1}, initialize=True)
        self.fetch()
        with patch("installer_prototype.tuf_repository.Fetcher._fetch",
                   side_effect=exceptions.DownloadError("offline")):
            self.assertEqual(self.fetch(fallback=True)[1], "verified-cache")
        for path in self.cache.rglob("timestamp.json"):
            bad = json.loads(path.read_text())
            bad["signed"]["expires"] = "2100-01-01T00:00:00Z"
            path.write_bytes(canonical(bad))
        with patch("installer_prototype.tuf_repository.Fetcher._fetch",
                   side_effect=exceptions.DownloadError("offline")):
            with self.assertRaises(exceptions.UnsignedMetadataError):
                self.fetch(fallback=True)

    def test_catalog_hash_and_snapshot_hash_are_enforced(self):
        self.publish({"revision": 1}, initialize=True)
        target = next((self.origin / "tuf/targets").glob("*.catalog.json"))
        original = target.read_bytes()
        target.write_bytes(b"bad")
        with self.assertRaises(exceptions.LengthOrHashMismatchError):
            self.fetch()
        target.write_bytes(original)
        snapshot = self.origin / "tuf/metadata/1.snapshot.json"
        snapshot.write_bytes(b"bad")
        with self.assertRaises(exceptions.LengthOrHashMismatchError):
            fetch_catalog(self.trust, self.root / "empty-cache")

    def test_each_repository_is_independent_and_key_substitution_is_rejected(self):
        self.publish({"publisher": "one"}, initialize=True)
        other = demo_trust(self.root / "other-keys", "other", (self.root / "other").as_uri() + "/tuf")
        other["url"] = self.trust["url"]
        with self.assertRaises(exceptions.UnsignedMetadataError):
            fetch_catalog(other, self.cache)

    def test_root_threshold_rotation_uses_old_and_new_signatures(self):
        self.publish({"revision": 1}, initialize=True)
        self.fetch()
        new_key = self.root / "new-root.pem"
        keygen(new_key)
        keys = {**self.keys, "root": [new_key], "old_root": [self.root / "keys/root.pem"]}
        rotated = create_root(keys, version=2, previous=self.trust["root"])
        self.store.publish_immutable({"tuf/metadata/2.root.json": encoded(Metadata.from_bytes(canonical(rotated)))})
        self.publish()
        self.assertEqual(self.fetch()[2]["root_version"], 2)
        bad = copy.deepcopy(rotated)
        bad["signed"]["version"] = 3
        self.store.publish_immutable({"tuf/metadata/3.root.json": canonical(bad)})
        with self.assertRaises(exceptions.UnsignedMetadataError):
            self.fetch()

    def test_failed_timestamp_commit_preserves_head_and_renewal_skips_orphans(self):
        store = MemoryStore()
        publish_catalog(store, self.trust, self.keys, {"revision": 1}, initialize=True)
        old = store.objects["tuf/metadata/timestamp.json"]
        def fail(name):
            if name == "tuf/metadata/timestamp.json":
                raise OSError("failed head")
        store.before_put = fail
        with self.assertRaisesRegex(OSError, "failed head"):
            publish_catalog(store, self.trust, self.keys, {"revision": 2})
        self.assertEqual(store.objects["tuf/metadata/timestamp.json"], old)
        store.before_put = None
        result = publish_catalog(store, self.trust, self.keys)
        self.assertEqual(result["tuf_revision"], 3)
        self.assertEqual(origin_state(store, self.trust)[3], {"revision": 1})

    def test_root_requires_two_distinct_signers_when_threshold_is_two(self):
        keys = {**self.keys, "root": [self.root / "keys/root.pem", self.root / "second-root.pem"]}
        keygen(keys["root"][1])
        root = create_root(keys, root_threshold=2)
        root["signatures"] = root["signatures"][:1]
        trust = {**self.trust, "root": root}
        with self.assertRaises(exceptions.UnsignedMetadataError):
            fetch_catalog(trust, self.cache)

    def test_archival_authority_cannot_exceed_bootstrap_policy(self):
        from installer_prototype.registry_updates import publish_registry, verified_record
        from installer_prototype.common import sign
        authority = self.root / "archive-authority.pem"
        source = {"kind": "registry-source", "schema": 1, "registry_id": "archive-test",
                  "target": "linux-x64", "season": "2027", "channel": "stable",
                  "public_key": keygen(authority), "url": "https://example.org/registry.json",
                  "tuf": self.trust, "archive_policy": {"name": "wpilib-archive-v1", "max_days": 365}}
        registry = {"schema": 1, "target": "linux-x64", "season": "2027", "channel": "stable",
                    "online_trust": "tuf", "archive_policy": source["archive_policy"],
                    "bundle_public_key": keygen(self.root / "bundle.pem"), "repositories": {"core": {
                        "tuf": self.trust, "url": "https://example.org/core",
                        "public_key": keygen(self.root / "archive-publisher.pem"), "components": ["org.example.core"]}}}
        with self.assertRaisesRegex(ValueError, "archival policy"):
            publish_registry(registry, source, authority, 1, self.root / "bad-feed", offline_days=366, update_tuf=False)
        path = publish_registry(registry, source, authority, 1, self.root / "feed", update_tuf=False)
        record = json.loads(path.read_text())
        self.assertEqual(verified_record(record, source, mode="offline"), registry)
        record["signed"]["offline_expires_at"] += 86400
        record = sign(record["signed"], authority)
        with self.assertRaisesRegex(ValueError, "archival policy"):
            verified_record(record, source, mode="offline")

    def test_online_signer_rotation_with_overlap_preserves_renewal_and_revokes_old_key(self):
        self.publish({"revision": 1}, initialize=True)
        self.fetch()
        old_timestamp = (self.origin / "tuf/metadata/timestamp.json").read_bytes()
        replacement = {}
        for role in ROLES:
            path = self.root / ("new-" + role + ".pem")
            keygen(path)
            replacement[role] = [path]
        roots = [self.root / "keys/root.pem"]
        overlap = create_root({"root": roots, **{r: self.keys[r] + replacement[r] for r in ROLES}},
                              version=2, previous=self.trust["root"])
        self.store.publish_immutable({"tuf/metadata/2.root.json": json.dumps(overlap, indent=2).encode() + b"\n"})
        self.keys = replacement
        self.publish()
        final = create_root({"root": roots, **replacement}, version=3, previous=overlap)
        self.store.publish_immutable({"tuf/metadata/3.root.json": canonical(final)})
        self.publish()
        self.assertEqual(self.fetch()[2]["root_version"], 3)
        (self.origin / "tuf/metadata/timestamp.json").write_bytes(old_timestamp)
        with self.assertRaises(exceptions.UnsignedMetadataError):
            self.fetch()

    def test_artifactory_promotes_tuf_metadata_before_timestamp_and_preserves_old_head_on_failure(self):
        from installer_prototype.publication_storage import ArtifactoryStore, publication_key
        from tests.test_artifactory import FakeArtifactory
        config = {"type": "artifactory", "api_url": "https://example.org/artifactory",
                  "staging_repository": "staging-local", "release_repository": "release-local",
                  "prefix": "test", "writer_repository": "test/publisher"}
        env = {"GITHUB_ACTIONS": "true", "GITHUB_REPOSITORY": "test/publisher",
               "PUBLICATION_SERIALIZATION_KEY": publication_key(config), "ARTIFACTORY_ACCESS_TOKEN": "test-token"}
        server = FakeArtifactory()
        with patch.dict(os.environ, env), patch("installer_prototype.publication_storage.build_opener", return_value=server):
            store = ArtifactoryStore(config)
            publish_catalog(store, self.trust, self.keys, {"revision": 1}, initialize=True)
            old = store.get("tuf/metadata/timestamp.json")
            live_writes = [r[1] for r in server.requests if r[0] == "PUT" and r[1].startswith("release-local/")]
            self.assertEqual(live_writes, ["release-local/test/tuf/metadata/timestamp.json"])
            server.copy_mode = "fail"
            with self.assertRaises(ValueError):
                publish_catalog(store, self.trust, self.keys, {"revision": 2})
            self.assertEqual(store.get("tuf/metadata/timestamp.json"), old)


@unittest.skipUnless(os.environ.get("IFW_PATH"), "Set IFW_PATH to exercise native TUF repositories")
class NativeTUFTests(unittest.TestCase):
    def test_http_tuf_update_then_offline_archive_after_online_expiry(self):
        from functools import partial
        from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
        import threading
        import zipfile
        import sys
        from installer_prototype.bundle import assemble, load_bundle
        from installer_prototype.demo import create, write_spec
        from installer_prototype.manager import build_installer, inventory, launch
        from installer_prototype.offline_acceptance import host_target
        from installer_prototype.registry_updates import resolve
        from installer_prototype.repository import publish, select
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            requests = []
            class Handler(SimpleHTTPRequestHandler):
                def do_GET(self):
                    requests.append(self.path)
                    super().do_GET()
                def log_message(self, *_):
                    pass
            server = ThreadingHTTPServer(("127.0.0.1", 0), partial(Handler, directory=str(root / "demo/store")))
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            self.addCleanup(server.server_close)
            self.addCleanup(server.shutdown)
            ifw = Path(os.environ["IFW_PATH"])
            target = host_target()
            registry = create(root / "demo", target, ifw, f"http://127.0.0.1:{server.server_port}", enable_tuf=True)
            source = json.loads((root / "demo/registry-source.json").read_text())
            cache = root / "cache"
            view = resolve(source, cache)
            archive = assemble(registry, root / "downloads", root / "demo/keys/bundle.pem", cache,
                               registry_record=view.record, registry_evidence=view.tuf_evidence)
            core_keys = {role: [root / "demo/keys/core-tuf" / (role + ".pem")] for role in ROLES}
            publish_catalog(DirectoryStore(root / "demo/store" / target / "core"),
                            registry["repositories"]["core"]["tuf"], core_keys)
            self.assertEqual(archive, assemble(registry, root / "downloads", root / "demo/keys/bundle.pem", cache,
                             registry_record=view.record, registry_evidence=view.tuf_evidence))
            with zipfile.ZipFile(archive) as stream:
                stream.extractall(root / "usb")
            bundle = root / "usb" / archive.stem
            suffix = ".exe" if os.name == "nt" else ".app" if sys.platform == "darwin" else ""
            engine = build_installer(ifw, root / ("installer" + suffix))
            install = root / "installed"
            launch(engine, select(registry, cache)[0], cache, root=install, command="install",
                   profile="tools", profiles=registry["profiles"], headless=True)
            spec = write_spec(root / "demo/specs", "elastic", target, 2)
            publish(spec, root / "demo/store", root / "demo/keys/elastic.pem", registry, ifw)
            launch(install / ("maintenancetool" + suffix), select(registry, cache)[0], cache,
                   root=install, command="update", headless=True)
            self.assertEqual(inventory(install)["org.wpilib.elastic"], "2.0.0")
            from installer_prototype.ci_publication import tuf_publisher_release, tuf_registry_release
            publisher_keys = {role: [root / "demo/keys/elastic-tuf" / (role + ".pem")] for role in ROLES}
            result = tuf_publisher_release(write_spec(root / "demo/specs", "elastic", target, 3), registry,
                "elastic", DirectoryStore(root / "demo/store" / target / "elastic"), root / "demo/keys/elastic.pem",
                ifw, publisher_keys)
            self.assertEqual(result["revision"], 3)
            launch(install / ("maintenancetool" + suffix), select(registry, cache)[0], cache,
                   root=install, command="update", headless=True)
            self.assertEqual(inventory(install)["org.wpilib.elastic"], "3.0.0")
            authority_keys = {role: [root / "demo/keys/registry-tuf" / (role + ".pem")] for role in ROLES}
            authority_store = DirectoryStore(root / "demo/store" / target / "registry")
            renewed = tuf_registry_release(source, authority_store, root / "demo/keys/registry.pem", authority_keys)
            self.assertEqual(renewed["revision"], 1)
            applied = tuf_registry_release(source, authority_store, root / "demo/keys/registry.pem", authority_keys,
                                           approved=registry)
            self.assertEqual(applied["revision"], 2)
            self.assertEqual(resolve(source, cache).record["signed"]["revision"], 2)
            self.assertFalse(any(p.endswith("/index.json") or p.endswith("/registry.json") for p in requests))
            for publisher in [*registry["repositories"], "registry"]:
                trust = source["tuf"] if publisher == "registry" else registry["repositories"][publisher]["tuf"]
                keys = {role: [root / "demo/keys" / (publisher + "-tuf") / (role + ".pem")] for role in ROLES}
                publish_catalog(DirectoryStore(root / "demo/store" / target / publisher), trust, keys,
                                now=datetime.now(timezone.utc) - timedelta(days=3))
            with self.assertRaises(exceptions.ExpiredMetadataError):
                resolve(source, cache)
            saved = {str(p.relative_to(cache)): p.read_bytes() for p in (cache / "tuf").rglob("*") if p.is_file()}
            with patch("installer_prototype.tuf_repository.Fetcher._fetch", side_effect=AssertionError("Offline must not fetch TUF")), \
                    patch("installer_prototype.archival.time.time", return_value=4_102_444_800):  # 2100
                cold = root / "offline-cache"
                offline = resolve(source, cold, bundle=bundle)
                snapshots, _ = load_bundle(bundle, offline.registry, cold)
                launch(engine, snapshots, cold, offline=True, root=root / "offline", command="install",
                       profile="tools", profiles=offline.registry["profiles"], headless=True)
                self.assertEqual(inventory(root / "offline")["org.wpilib.elastic"], "1.0.0")
            self.assertEqual(saved, {str(p.relative_to(cache)): p.read_bytes() for p in (cache / "tuf").rglob("*") if p.is_file()})
            self.assertIsNone(view.record["signed"]["offline_expires_at"])
            self.assertFalse((cold / "tuf").exists())
            for publisher in [*registry["repositories"], "registry"]:
                trust = source["tuf"] if publisher == "registry" else registry["repositories"][publisher]["tuf"]
                keys = {role: [root / "demo/keys" / (publisher + "-tuf") / (role + ".pem")] for role in ROLES}
                publish_catalog(DirectoryStore(root / "demo/store" / target / publisher), trust, keys)
            new_archive_key = root / "demo/keys/registry-next.pem"
            keygen(new_archive_key)
            self.assertEqual(tuf_registry_release(source, authority_store, new_archive_key, authority_keys)["revision"], 3)
            current = resolve(source, cold)
            self.assertEqual(current.record["signed"]["revision"], 3)
            launch(root / "offline" / ("maintenancetool" + suffix), select(current.registry, cold)[0], cold,
                   root=root / "offline", command="update", headless=True)
            self.assertEqual(inventory(root / "offline")["org.wpilib.elastic"], "3.0.0")


if __name__ == "__main__":
    unittest.main()
