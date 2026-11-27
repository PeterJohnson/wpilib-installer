import copy
import hashlib
import importlib.util
import io
import json
import os
from pathlib import Path
import tarfile
import tempfile
import unittest
from unittest.mock import patch

from installer_prototype.ci_publication import (Object, PublicationConflict, S3Store,
    json_bytes, publisher_release, registry_release, secret_key)
from installer_prototype.common import atomic_json, keygen, sign, verify
from tools.unpack_release import unpack


class MemoryStore:
    """Atomic origin model with injectable failures, including competing writers."""
    def __init__(self):
        self.objects, self.reads, self.writes = {}, [], []
        self.before_put = None

    def get(self, name):
        self.reads.append(name)
        return self.objects.get(name)

    def reserved(self, name):
        return self.get(name)

    def publish_immutable(self, files):
        for name, value in files.items():
            self.put(name, value, immutable=True)

    def verify_immutable(self, files):
        for name, value in files.items():
            expected = value.read_bytes() if isinstance(value, Path) else value
            if self.objects[name].data != expected:
                raise ValueError("Corrupt published object")

    def seed(self, name, data):
        self.objects[name] = Object(data, hashlib.sha256(data).hexdigest())

    def put(self, name, value, *, previous=None, immutable=False):
        data = value.read_bytes() if isinstance(value, Path) else value
        if self.before_put:
            self.before_put(name)
        existing = self.objects.get(name)
        if (existing.etag if existing else None) != previous:
            if immutable and existing and existing.data == data:
                return
            raise PublicationConflict(name)
        self.seed(name, data)
        self.writes.append(name)


class PublicationTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.now = 1_800_000_000
        self.key = self.root / "authority.pem"
        self.publisher_key = self.root / "publisher.pem"
        self.source = {"kind": "registry-source", "schema": 1, "registry_id": "ci-test",
            "target": "linux-x64", "season": "2027", "channel": "stable",
            "url": "https://example.org/registry/registry.json", "public_key": keygen(self.key)}
        self.registry = {"schema": 1, "target": "linux-x64", "season": "2027", "channel": "stable",
            "bundle_public_key": keygen(self.root / "bundle.pem"), "repositories": {
                "tools": {"url": "https://example.org/tools", "public_key": keygen(self.publisher_key),
                          "components": ["org.example.tool"]}}}
        self.store = MemoryStore()

    def registry_publish(self, **kwargs):
        return registry_release(self.source, self.store, self.key, now=self.now, **kwargs)

    def initialize(self):
        return self.registry_publish(approved=self.registry, initialize=True)

    def test_initial_publication_requires_explicit_configuration_and_initialization(self):
        for kwargs in ({}, {"approved": self.registry}, {"initialize": True}):
            with self.assertRaisesRegex(ValueError, "initialize explicitly"):
                self.registry_publish(**kwargs)
        self.assertEqual(self.store.writes, [])
        result = self.initialize()
        self.assertEqual(result["revision"], 1)
        self.assertEqual(self.store.writes, ["revisions/1.json", "registry.json"])
        verify(result["registry"], self.source["public_key"])

    def test_renewal_uses_latest_approved_config_and_recovers_after_expiry(self):
        self.initialize()
        # Publish a reviewed change, then retain a stale local configuration.
        approved = copy.deepcopy(self.registry)
        approved["profiles"] = {"tools": ["org.example.tool"]}
        self.registry_publish(approved=approved)
        writes = list(self.store.writes)
        self.assertEqual(self.registry_publish()["status"], "not-due")
        self.assertEqual(self.store.writes, writes)
        self.now += 8 * 86400
        result = self.registry_publish()
        self.assertEqual(result["revision"], 3)
        self.assertEqual(result["registry"]["signed"]["registry"], approved)
        self.assertEqual(result["registry"]["signed"]["expires_at"], self.now + 7 * 86400)
        self.assertEqual(json.loads(self.store.objects["revisions/1.json"].data)["signed"]["registry"], self.registry)

    def test_invalid_origin_or_wrong_signing_key_never_advances_head(self):
        self.initialize()
        head = self.store.objects["registry.json"]
        with self.assertRaisesRegex(ValueError, "signature"):
            registry_release(self.source, self.store, self.publisher_key, approved=self.registry, now=self.now)
        self.assertEqual(self.store.objects["registry.json"], head)
        invalid = json.loads(head.data)
        invalid["signed"]["revision"] = 99
        self.store.seed("registry.json", json_bytes(invalid))
        writes = list(self.store.writes)
        with self.assertRaisesRegex(ValueError, "signature"):
            self.registry_publish()
        self.assertEqual(self.store.writes, writes)

    def test_failed_head_update_preserves_head_and_next_run_skips_orphan(self):
        self.initialize()
        head = self.store.objects["registry.json"]

        def fail(name):
            if name == "registry.json":
                raise OSError("connection failed")

        self.store.before_put = fail
        self.now += 5 * 86400
        with self.assertRaises(OSError):
            self.registry_publish()
        self.assertEqual(self.store.objects["registry.json"], head)
        self.assertIn("revisions/2.json", self.store.objects)
        self.store.before_put = None
        self.assertEqual(self.registry_publish()["revision"], 3)

    def test_competing_writer_cannot_be_overwritten(self):
        self.initialize()
        self.now += 5 * 86400
        winner = json.loads(self.store.objects["registry.json"].data)["signed"]
        winner["revision"] = 10
        signed = json_bytes(sign(winner, self.key))

        def compete(name):
            if name == "registry.json":
                self.store.seed(name, signed)

        self.store.before_put = compete
        with self.assertRaises(PublicationConflict):
            self.registry_publish()
        self.assertEqual(self.store.objects["registry.json"].data, signed)

    def test_origin_failure_is_not_first_publication(self):
        with patch.object(self.store, "get", side_effect=OSError("origin unavailable")):
            with self.assertRaises(OSError):
                self.initialize()
        self.assertFalse(self.store.writes)

    def test_secret_cleanup_on_failure_and_no_child_environment_key(self):
        for newline in ("\n", "\r\n"):
            pem = self.key.read_text().replace("\n", newline)
            with self.subTest(newline=repr(newline)), patch.dict(os.environ, {"TEST_SIGNING_KEY": pem}):
                with self.assertRaisesRegex(RuntimeError, "failed"):
                    with secret_key("TEST_SIGNING_KEY") as path:
                        self.assertNotIn("TEST_SIGNING_KEY", os.environ)
                        self.assertEqual(path.read_bytes(), pem.encode("ascii"))
                        if os.name != "nt":
                            self.assertEqual(path.stat().st_mode & 0o777, 0o600)
                        raise RuntimeError("failed")
                self.assertFalse(path.exists())

    def spec(self, revision):
        payload = self.root / "payload"
        payload.mkdir(exist_ok=True)
        executable = payload / "tool"
        executable.write_text(f"fixture {revision}")
        executable.chmod(0o755)
        spec = self.root / "spec.json"
        atomic_json(spec, {"publisher": "tools", "target": "linux-x64", "season": "2027", "channel": "stable",
                          "revision": revision, "packages": [{"id": "org.example.tool", "name": "Fixture",
                          "version": f"{revision}.0.0", "payload": "payload"}]})
        return spec

    @unittest.skipUnless(os.environ.get("IFW_PATH"), "Set IFW_PATH to Qt IFW 4.11")
    def test_publisher_fresh_workspace_fetches_only_metadata_and_preserves_history(self):
        def release(revision, **kwargs):
            return publisher_release(self.spec(revision), self.registry, "tools", self.store,
                                     self.publisher_key, os.environ["IFW_PATH"], **kwargs)

        with self.assertRaisesRegex(ValueError, "initialize explicitly"):
            release(1)
        first = release(1, initialize=True)
        original = dict(self.store.objects)
        self.store.reads.clear()
        second = release(2)
        self.assertEqual(self.store.reads, ["index.json", "releases/1/release.json", "releases/2/release.json"])
        self.assertEqual(second["index"], {"revisions": [1, 2]})
        for name, obj in original.items():
            if name != "index.json":
                self.assertEqual(self.store.objects[name], obj)
        self.assertEqual(self.store.writes[-2:], ["releases/2/release.json", "index.json"])
        for result in (first, second):
            manifest = verify(result["release"], self.registry["repositories"]["tools"]["public_key"])
            for name, info in manifest["files"].items():
                data = self.store.objects[f"releases/{result['revision']}/{name}"].data
                self.assertEqual(hashlib.sha256(data).hexdigest(), info["sha256"])
                self.assertEqual(len(data), info["size"])
        with self.assertRaisesRegex(ValueError, "exceed"):
            release(1)
        head = self.store.objects["index.json"]

        def fail(name):
            if name.endswith(".7z"):
                raise OSError("interrupted payload upload")

        self.store.before_put = fail
        with self.assertRaises(OSError):
            release(3)
        self.assertEqual(self.store.objects["index.json"], head)
        self.assertNotIn("releases/3/release.json", self.store.objects)


@unittest.skipUnless(importlib.util.find_spec("boto3"), "Install the ci extra for S3 contract tests")
class S3ContractTests(unittest.TestCase):
    def setUp(self):
        import boto3
        from botocore.stub import Stubber

        client = boto3.client("s3", region_name="us-east-1", aws_access_key_id="test", aws_secret_access_key="test")
        with patch("boto3.client", return_value=client):
            self.store = S3Store({"bucket": "test-bucket", "prefix": "publisher"})
        self.stub = Stubber(client)
        self.stub.activate()
        self.addCleanup(self.stub.deactivate)
        self.addCleanup(self.stub.assert_no_pending_responses)

    def test_only_missing_keys_are_empty_and_metadata_reads_are_bounded(self):
        from botocore.exceptions import ClientError
        from botocore.response import StreamingBody

        params = {"Bucket": "test-bucket", "Key": "publisher/index.json"}
        self.stub.add_client_error("get_object", "NoSuchKey", http_status_code=404, expected_params=params)
        self.assertIsNone(self.store.get("index.json"))
        self.stub.add_client_error("get_object", "AccessDenied", http_status_code=403, expected_params=params)
        with self.assertRaises(ClientError):
            self.store.get("index.json")
        data = b"x" * (8 * 1024 * 1024 + 1)
        self.stub.add_response("get_object", {"Body": StreamingBody(io.BytesIO(data), len(data)), "ETag": '"etag"'}, params)
        with self.assertRaisesRegex(ValueError, "8 MiB"):
            self.store.get("index.json")

    def test_mutable_pointer_uses_compare_and_swap_and_conflict_fails(self):
        params = {"Bucket": "test-bucket", "Key": "publisher/index.json", "Body": b"{}", "ContentLength": 2,
                  "ContentType": "application/json", "CacheControl": "no-cache,max-age=0", "IfMatch": '"old"',
                  "Metadata": {"sha256": hashlib.sha256(b"{}").hexdigest()}, "ContentMD5": "mZFLkyvTelC5g8XnyQrpOw=="}
        self.stub.add_client_error("put_object", "PreconditionFailed", http_status_code=412, expected_params=params)
        with self.assertRaises(PublicationConflict):
            self.store.put("index.json", b"{}", previous='"old"')

    def test_immutable_retry_checks_bytes_and_never_overwrites(self):
        from botocore.response import StreamingBody

        params = {"Bucket": "test-bucket", "Key": "publisher/release.json", "Body": b"{}", "ContentLength": 2,
                  "ContentType": "application/json", "CacheControl": "public,max-age=31536000,immutable", "IfNoneMatch": "*",
                  "Metadata": {"sha256": hashlib.sha256(b"{}").hexdigest()}, "ContentMD5": "mZFLkyvTelC5g8XnyQrpOw=="}
        for data in (b"{}", b"changed"):
            self.stub.add_client_error("put_object", "PreconditionFailed", http_status_code=412, expected_params=params)
            self.stub.add_response("get_object", {"Body": StreamingBody(io.BytesIO(data), len(data))},
                                   {"Bucket": "test-bucket", "Key": "publisher/release.json"})
        self.store.put("release.json", b"{}", immutable=True)
        with self.assertRaises(PublicationConflict):
            self.store.put("release.json", b"{}", immutable=True)


class ReleaseArchiveTests(unittest.TestCase):
    def test_preserves_executable_permissions_and_rejects_links_and_traversal(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            archive = root / "release.tar"
            info = tarfile.TarInfo("payload/tool")
            info.mode, info.size = 0o755, 3
            with tarfile.open(archive, "w") as stream:
                stream.addfile(info, io.BytesIO(b"abc"))
            unpack(archive, root / "good")
            self.assertEqual((root / "good/payload/tool").read_bytes(), b"abc")
            if os.name != "nt":
                self.assertTrue((root / "good/payload/tool").stat().st_mode & 0o111)
            for name, kind in (("../escape", tarfile.REGTYPE), ("payload/link", tarfile.SYMTYPE)):
                info = tarfile.TarInfo(name)
                info.type, info.linkname = kind, "../escape"
                with tarfile.open(archive, "w") as stream:
                    stream.addfile(info)
                with self.assertRaises(ValueError):
                    unpack(archive, root / "bad")
                self.assertFalse((root / "bad").exists())
