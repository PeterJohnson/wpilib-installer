"""Artifactory REST contract and failure tests; no Artifactory credentials needed."""
import hashlib
import io
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from urllib.error import HTTPError
from urllib.parse import parse_qs, unquote, urlsplit

from installer_tooling.ci_publication import publisher_release, registry_release
from installer_tooling.common import atomic_json, keygen, verify
from installer_tooling.publication_storage import (ArtifactoryStore, NoRedirect,
    PublicationConflict, publication_key, require_serialized_writer, storage_from_config)


class FakeArtifactory:
    """Exercise the backend's real Request construction against REST semantics."""
    def __init__(self):
        self.repositories = {"staging-local", "release-local"}
        self.files, self.requests = {}, []
        self.before = None
        self.copy_mode = "normal"

    def open(self, request, timeout):
        url = urlsplit(request.full_url)
        path = unquote(url.path.removeprefix("/artifactory/"))
        method = request.get_method()
        data = request.data.read() if hasattr(request.data, "read") else request.data
        assert request.get_header("Authorization") == "Bearer test-token"
        self.requests.append((method, path, data))

        def error(code):
            raise HTTPError(request.full_url, code, "fixture", {}, io.BytesIO(b"sensitive server response"))

        if self.before:
            self.before(method, path)
        if method == "GET" and path.startswith("api/storage/"):
            name = path.removeprefix("api/storage/")
            if "/" not in name:
                if name not in self.repositories:
                    error(404)
                result = {"repo": name, "path": "/", "children": []}
            else:
                if name not in self.files:
                    error(404)
                body = self.files[name]
                result = {"size": str(len(body)), "checksums": {"sha256": hashlib.sha256(body).hexdigest()}}
            return io.BytesIO(json.dumps(result).encode())
        if method == "GET":
            if path not in self.files:
                error(404)
            return io.BytesIO(self.files[path])
        if method == "PUT":
            assert int(request.get_header("Content-length")) == len(data)
            assert request.get_header("X-checksum-sha256") == hashlib.sha256(data).hexdigest()
            self.files[path] = data
            return io.BytesIO(b"{}")
        if method == "POST" and path.startswith("api/copy/"):
            source = path.removeprefix("api/copy/")
            query = parse_qs(url.query)
            assert query["suppressLayouts"] == ["1"] and query["failFast"] == ["1"]
            target = query["to"][0].lstrip("/")
            if self.copy_mode == "fail":
                error(500)
            if self.copy_mode == "error-body":
                return io.BytesIO(b'{"messages":[{"level":"ERROR","message":"failed"}]}')
            if self.copy_mode != "missing":
                self.files[target] = b"corrupted" if self.copy_mode == "corrupt" else self.files[source]
            return io.BytesIO(b'{"messages":[{"level":"INFO","message":"copied"}]}')
        raise AssertionError((method, path))


class ArtifactoryTests(unittest.TestCase):
    def setUp(self):
        self.config = {"type": "artifactory", "api_url": "https://packages.example.org/artifactory",
                       "staging_repository": "staging-local", "release_repository": "release-local",
                       "prefix": "2027/stable/linux-x64", "writer_repository": "example/publisher"}
        self.env = patch.dict(os.environ, {"GITHUB_ACTIONS": "true", "GITHUB_REPOSITORY": "example/publisher",
                                         "PUBLICATION_SERIALIZATION_KEY": publication_key(self.config),
                                         "ARTIFACTORY_ACCESS_TOKEN": "test-token"})
        self.env.start()
        self.addCleanup(self.env.stop)
        self.server = FakeArtifactory()
        self.opener = patch("installer_tooling.publication_storage.build_opener", return_value=self.server)
        self.opener.start()
        self.addCleanup(self.opener.stop)
        self.store = ArtifactoryStore(self.config)
        self.prefix = self.config["prefix"] + "/"
        self.head = "release-local/" + self.prefix + "index.json"
        self.server.files[self.head] = b'{"revisions":[1]}'

    def files(self):
        return {"releases/2/Updates.xml": b"<Updates/>",
                "releases/2/org.example.tool/tool archive.7z": b"archive",
                "releases/2/release.json": b"signed manifest"}

    def test_stages_complete_set_promotes_exact_paths_then_updates_only_live_index(self):
        old = self.store.get("index.json")
        self.server.requests.clear()
        self.store.publish_immutable(self.files())
        self.assertEqual(self.server.files[self.head], old.data)
        self.store.put("index.json", b'{"revisions":[1,2]}', previous=old.etag)
        requests = self.server.requests
        staging_puts = [i for i, (method, path, _) in enumerate(requests) if method == "PUT" and path.startswith("staging-local/")]
        copies = [i for i, (method, _, _) in enumerate(requests) if method == "POST"]
        self.assertLess(max(staging_puts), min(copies))
        live_puts = [r for r in requests if r[0] == "PUT" and r[1].startswith("release-local/")]
        self.assertEqual(live_puts, [("PUT", self.head, b'{"revisions":[1,2]}')])
        self.assertNotIn("staging-local/" + self.prefix + "index.json", self.server.files)
        for name, data in self.files().items():
            self.assertEqual(self.server.files["release-local/" + self.prefix + name], data)
            self.assertEqual(self.server.files["staging-local/" + self.prefix + name], data)
        self.assertNotIn("ARTIFACTORY_ACCESS_TOKEN", os.environ)

    def test_staging_failure_never_promotes_or_advances_head(self):
        def fail(method, path):
            if method == "PUT" and path.endswith("release.json"):
                raise OSError("interrupted staging upload")

        self.server.before = fail
        with self.assertRaises(OSError):
            self.store.publish_immutable(self.files())
        self.assertFalse(any(method == "POST" for method, _, _ in self.server.requests))
        self.assertEqual(self.server.files[self.head], b'{"revisions":[1]}')
        with self.assertRaisesRegex(ValueError, "before advancing"):
            self.store.put("index.json", b"{}")

    def test_incomplete_corrupt_or_failed_promotion_never_advances_head(self):
        for mode in ("fail", "missing", "corrupt", "error-body"):
            with self.subTest(mode=mode):
                self.server.copy_mode = mode
                self.server.files = {self.head: b'{"revisions":[1]}'}
                with self.assertRaises(ValueError):
                    self.store.publish_immutable(self.files())
                self.assertEqual(self.server.files[self.head], b'{"revisions":[1]}')
                with self.assertRaisesRegex(ValueError, "before advancing"):
                    self.store.put("index.json", b"{}")

    def test_partial_promotion_retries_identical_bytes_without_reupload_or_overwrite(self):
        self.server.files["release-local/" + self.prefix + "releases/2/Updates.xml"] = b"<Updates/>"
        self.server.files["staging-local/" + self.prefix + "releases/2/Updates.xml"] = b"<Updates/>"
        self.server.requests.clear()
        self.store.publish_immutable(self.files())
        self.assertFalse(any(method in ("POST", "PUT") and path.endswith("Updates.xml")
                             for method, path, _ in self.server.requests))
        self.assertEqual(self.server.files[self.head], b'{"revisions":[1]}')

    def test_immutable_collision_is_rejected_before_any_promotion(self):
        for repo in ("staging-local", "release-local"):
            with self.subTest(repository=repo):
                self.server.files = {self.head: b'{"revisions":[1]}',
                                     repo + "/" + self.prefix + "releases/2/release.json": b"different"}
                self.server.requests.clear()
                with self.assertRaises(PublicationConflict):
                    self.store.publish_immutable(self.files())
                self.assertFalse(any(method == "POST" for method, _, _ in self.server.requests))

    def test_changed_head_is_detected_before_final_put(self):
        old = self.store.get("index.json")
        self.store.publish_immutable(self.files())
        self.server.files[self.head] = b'{"revisions":[1,3]}'
        with self.assertRaises(PublicationConflict):
            self.store.put("index.json", b'{"revisions":[1,2]}', previous=old.etag)
        self.assertEqual(self.server.files[self.head], b'{"revisions":[1,3]}')

    def test_index_cannot_be_included_in_promotion(self):
        with self.assertRaisesRegex(ValueError, "Only immutable"):
            self.store.publish_immutable({"index.json": b"{}"})

    def test_authentication_errors_and_redirects_fail_closed(self):
        for code in (401, 403, 302, 500):
            with self.subTest(code=code):
                def fail(_method, _path):
                    raise HTTPError("https://example.org", code, "fixture", {}, io.BytesIO(b"secret"))
                self.server.before = fail
                with self.assertRaisesRegex(ValueError, f"HTTP {code}") as raised:
                    self.store.get("missing.json")
                self.assertNotIn("secret", str(raised.exception))
        self.assertIsNone(NoRedirect().redirect_request(None, None, 302, "", {}, "https://elsewhere.example.org"))
        self.server.before = None
        self.assertIsNone(self.store.get("missing.json"))

    def test_missing_repository_is_not_an_empty_publication(self):
        self.server.repositories.remove("release-local")
        os.environ["ARTIFACTORY_ACCESS_TOKEN"] = "test-token"
        with self.assertRaisesRegex(ValueError, "HTTP 404"):
            ArtifactoryStore(self.config)

    def test_oversized_origin_metadata_rejected(self):
        self.server.files[self.head] = b"x" * (8 * 1024 * 1024 + 1)
        with self.assertRaisesRegex(ValueError, "8 MiB"):
            self.store.get("index.json")

    def test_serialization_requires_canonical_destination_and_authoritative_repository(self):
        alias = dict(self.config, public_url="https://cdn.example.org", staging_repository="other-staging")
        alias["api_url"] = "https://PACKAGES.example.org:443/artifactory/"
        self.assertEqual(publication_key(alias), publication_key(self.config))
        self.assertNotEqual(publication_key(dict(alias, prefix="2027/stable/windows-x64")), publication_key(self.config))
        for change in ({"GITHUB_REPOSITORY": "another/writer"}, {"PUBLICATION_SERIALIZATION_KEY": "wrong"},
                       {"GITHUB_ACTIONS": "false"}):
            with patch.dict(os.environ, change), self.assertRaisesRegex(ValueError, "serialized"):
                require_serialized_writer(self.config)
        require_serialized_writer(self.config)

    def credentials(self, root):
        key = root / "authority.pem"
        publisher_key = root / "publisher.pem"
        source = {"kind": "registry-source", "schema": 1, "registry_id": "test",
            "target": "linux-x64", "season": "2027", "channel": "stable",
            "public_key": keygen(key), "url": "https://example.org/registry.json"}
        registry = {"schema": 1, "target": "linux-x64", "season": "2027", "channel": "stable",
            "bundle_public_key": keygen(root / "bundle.pem"), "repositories": {"tools": {
                "url": "https://example.org/tools", "public_key": keygen(publisher_key),
                "components": ["org.example.tool"]}}}
        return key, publisher_key, source, registry

    def test_registry_renewal_skips_staged_revision_after_failed_promotion(self):
        with tempfile.TemporaryDirectory() as tmp:
            key, _, source, registry = self.credentials(Path(tmp))
            now = 1_800_000_000
            registry_release(source, self.store, key, approved=registry, initialize=True, now=now)
            head_name = "release-local/" + self.prefix + "registry.json"
            original = self.server.files[head_name]
            self.server.copy_mode = "fail"
            with self.assertRaises(ValueError):
                registry_release(source, self.store, key, now=now + 5 * 86400)
            self.assertEqual(self.server.files[head_name], original)
            self.assertIn("staging-local/" + self.prefix + "revisions/2.json", self.server.files)
            self.server.copy_mode = "normal"
            result = registry_release(source, self.store, key, now=now + 5 * 86400)
            self.assertEqual(result["revision"], 3)
            self.assertEqual(result["registry"]["signed"]["registry"], registry)
            verify(json.loads(self.server.files[head_name]), source["public_key"])

    @unittest.skipUnless(os.environ.get("IFW_PATH"), "Set IFW_PATH to Qt IFW 4.11")
    def test_native_publisher_releases_use_promotion_and_fetch_no_old_archives(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _, key, _, registry = self.credentials(root)
            payload = root / "payload"
            payload.mkdir()
            spec = root / "spec.json"
            del self.server.files[self.head]
            for revision in (1, 2):
                (payload / "tool").write_text(str(revision))
                atomic_json(spec, {"publisher": "tools", "target": "linux-x64", "season": "2027", "channel": "stable",
                    "revision": revision, "packages": [{"id": "org.example.tool", "name": "Tool", "payload": "payload",
                                                          "version": f"{revision}.0.0"}]})
                self.server.requests.clear()
                result = publisher_release(spec, registry, "tools", self.store, key, os.environ["IFW_PATH"], initialize=revision == 1)
                self.assertEqual(result["index"]["revisions"], list(range(1, revision + 1)))
                self.assertFalse(any(method == "GET" and path.endswith(".7z") and not path.startswith("api/storage/")
                                     for method, path, _ in self.server.requests))
                manifest = verify(result["release"], registry["repositories"]["tools"]["public_key"])
                for name, info in manifest["files"].items():
                    data = self.server.files["release-local/" + self.prefix + f"releases/{revision}/{name}"]
                    self.assertEqual(hashlib.sha256(data).hexdigest(), info["sha256"])
            self.assertIn("release-local/" + self.prefix + "releases/1/release.json", self.server.files)
            with self.assertRaisesRegex(ValueError, "exceed"):
                publisher_release(spec, registry, "tools", self.store, key, os.environ["IFW_PATH"])

    def test_storage_selection_preserves_existing_s3_configs_and_rejects_unknown_type(self):
        with patch("installer_tooling.publication_storage.S3Store") as s3:
            storage_from_config({"bucket": "existing", "prefix": "packages"})
            s3.assert_called_once()
        with self.assertRaisesRegex(ValueError, "Unknown"):
            storage_from_config({"type": "unknown"})
