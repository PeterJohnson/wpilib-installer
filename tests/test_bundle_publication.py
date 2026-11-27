import hashlib
import base64
import importlib.util
import io
import json
import os
import stat
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import zipfile
from email.message import Message

from installer_prototype.bundle_publication import (paths, prepare_bundle, publish_candidate, read_page,
                                                     verify_public_files, verify_record)
from installer_prototype.offline_acceptance import CHECKS, report_identity
from installer_prototype.common import atomic_json, canonical, keygen, sign
from installer_prototype.publication_storage import ArtifactoryStore, PublicationConflict, S3Store, publication_key
from installer_prototype.registry_updates import publish_registry, registry_identity
from tests.test_artifactory import FakeArtifactory
from tests.test_ci_publication import MemoryStore


class BundlePublicationTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.key = self.root / "bundle.pem"
        self.authority = self.root / "authority.pem"
        self.publisher = self.root / "publisher.pem"
        self.registry = {"schema": 1, "target": "linux-x64", "season": "2027", "channel": "stable",
            "bundle_public_key": keygen(self.key), "repositories": {"core": {
                "url": "https://example.org/core", "public_key": keygen(self.publisher), "components": ["org.example.tool"]}}}
        self.source = {"kind": "registry-source", "schema": 1, "registry_id": "test-linux-x64",
            "target": "linux-x64", "season": "2027", "channel": "stable", "public_key": keygen(self.authority),
            "url": (self.root / "feed/registry.json").as_uri()}
        publish_registry(self.registry, self.source, self.authority, 1, self.root / "feed")
        self.revision, self.corrupt = 1, False
        self.store = MemoryStore()
        self.assembler = patch("installer_prototype.bundle_publication.assemble", side_effect=self.archive)
        self.assembler.start()
        self.addCleanup(self.assembler.stop)

    def archive(self, registry, destination, key, cache, *, registry_record):
        identity = "fixture-" + str(self.revision)
        files = {"Updates.xml": b"<Updates/>", "tool.7z": b"payload " + str(self.revision).encode()}
        release = sign({"schema": 1, "publisher": "core", "target": "linux-x64", "season": "2027", "channel": "stable",
            "revision": self.revision, "packages": [{"id": "org.example.tool", "version": f"{self.revision}.0.0", "owned_files": ["tool"]}],
            "files": {n: {"sha256": hashlib.sha256(data).hexdigest(), "size": len(data)} for n, data in files.items()}}, self.publisher)
        manifest = {"schema": 2, "id": identity, "target": "linux-x64", "season": "2027", "channel": "stable",
            "registry": registry_identity(registry_record), "repositories": {"core": {"revision": self.revision,
                "sha256": hashlib.sha256(canonical(release)).hexdigest()}}, "held_back": [], "payload_kind": "prototype-fixtures"}
        contents = {"registry.json": canonical(registry_record), "bundle-manifest.json": canonical(sign(manifest, key)),
                    "repos/core/release.json": canonical(release), **{"repos/core/" + n: data for n, data in files.items()}}
        if self.corrupt:
            contents["repos/core/tool.7z"] = b"corrupt"
        destination = Path(destination)
        destination.mkdir(parents=True, exist_ok=True)
        path = destination / (identity + ".zip")
        with zipfile.ZipFile(path, "w") as stream:
            for name, data in contents.items():
                stream.writestr(zipfile.ZipInfo(identity + "/" + name, (2026, 1, 1, 0, 0, 0)), data)
        return path

    def public_check(self, url, files):
        self.assertEqual(url, "https://downloads.example.org/linux-x64")
        for name, value in files.items():
            data = value.read_bytes() if isinstance(value, Path) else value
            if isinstance(self.store, MemoryStore):
                self.assertEqual(self.store.objects[name].data, data)
            else:
                self.assertEqual(self.server.files["release-local/downloads/" + name], data)

    def run_publication(self, **kwargs):
        directory, plan = self.prepare(**kwargs)
        return self.commit(directory, self.passing_report(directory, plan))

    def prepare(self, **kwargs):
        directory = self.root / ("candidate-" + str(len(list(self.root.glob("candidate-*")))))

        def fetch(base, name, record, cache):
            data = (self.store.objects[name].data if isinstance(self.store, MemoryStore)
                    else self.server.files["release-local/downloads/" + name])
            path = self.root / "baseline.zip"
            path.write_bytes(data)
            return path

        with patch("installer_prototype.bundle_publication.cached_file", side_effect=fetch):
            plan = prepare_bundle(self.source, self.store, self.key, self.root / "cache", directory,
                                  "https://downloads.example.org/linux-x64", **kwargs)
        return directory, plan

    def passing_report(self, directory, plan):
        # Storage unit tests inject a report; real native tests execute the OS-isolated runner.
        return {**report_identity(directory, plan), "status": "passed", "host_target": "linux-x64",
                "checks": CHECKS, "network_isolation": "network-namespace"}

    def commit(self, directory, report):
        return publish_candidate(self.source, self.store, directory, report,
                                 "https://downloads.example.org/linux-x64", public_check=self.public_check)

    def test_native_gate_rejects_missing_failed_wrong_target_or_wrong_archive_report(self):
        self.run_publication(initialize=True)
        original = dict(self.store.objects)
        self.revision = 2
        directory, plan = self.prepare()
        report = self.passing_report(directory, plan)
        for bad in ({}, {**report, "status": "failed"}, {**report, "host_target": "windows-x64"},
                    {**report, "sha256": "0" * 64}, {**report, "baseline_sha256": "0" * 64},
                    {**report, "checks": []}, {**report, "candidate_sha256": "0" * 64}):
            with self.assertRaisesRegex(ValueError, "acceptance report"):
                self.commit(directory, bad)
            self.assertEqual(self.store.objects, original)

    def test_preparation_makes_no_remote_writes_and_stale_baseline_cannot_publish(self):
        self.run_publication(initialize=True)
        original = dict(self.store.objects)
        self.revision = 2
        directory, plan = self.prepare()
        self.assertEqual(self.store.objects, original)
        self.revision = 3
        self.run_publication()
        newer = dict(self.store.objects)
        with self.assertRaisesRegex(ValueError, "head changed"):
            self.commit(directory, self.passing_report(directory, plan))
        self.assertEqual(self.store.objects, newer)

    def test_modified_candidate_after_native_check_cannot_publish(self):
        directory, plan = self.prepare(initialize=True)
        report = self.passing_report(directory, plan)
        next((directory / "current").glob("*.zip")).write_bytes(b"changed")
        with self.assertRaisesRegex(ValueError, "hash/length"):
            self.commit(directory, report)
        self.assertFalse(self.store.writes)

    def test_successful_update_keeps_previous_bundle_and_has_one_commit_point(self):
        with self.assertRaisesRegex(ValueError, "initialize explicitly"):
            self.run_publication()
        self.run_publication(initialize=True)
        original = dict(self.store.objects)
        old = read_page(original["index.html"].data, self.source)["current"]
        self.revision = 2
        result = self.run_publication()
        self.assertEqual(result["status"], "published")
        state = read_page(self.store.objects["index.html"].data, self.source)
        self.assertEqual(state["previous"], old)
        self.assertIn(b"Previous successful bundle", self.store.objects["index.html"].data)
        self.assertEqual(self.store.writes[-1], "index.html")
        self.assertNotIn("latest.json", self.store.objects)
        for name, value in original.items():
            if name != "index.html":
                self.assertEqual(self.store.objects[name], value)
        current = verify_record(state["current"], self.source)
        self.assertEqual(current["repositories"]["core"]["revision"], 2)
        self.assertEqual(hashlib.sha256(self.store.objects[paths(current)["bundle"]].data).hexdigest(), current["sha256"])

    def test_unchanged_run_verifies_without_uploading_or_replacing_page(self):
        self.run_publication(initialize=True)
        writes, page = list(self.store.writes), self.store.objects["index.html"]
        self.assertEqual(self.run_publication()["status"], "unchanged")
        self.assertEqual(self.store.writes, writes)
        self.assertEqual(self.store.objects["index.html"], page)

    def test_assembly_or_archive_validation_failure_preserves_every_published_file(self):
        self.run_publication(initialize=True)
        original = dict(self.store.objects)
        with patch("installer_prototype.bundle_publication.assemble", side_effect=ValueError("assembly failed")):
            with self.assertRaisesRegex(ValueError, "assembly failed"):
                self.run_publication()
        self.assertEqual(self.store.objects, original)
        self.revision, self.corrupt = 2, True
        with self.assertRaises(ValueError):
            self.run_publication()
        self.assertEqual(self.store.objects, original)

    def test_symlink_archive_member_is_rejected_before_publication(self):
        archive = self.archive

        def linked(*args, **kwargs):
            path = archive(*args, **kwargs)
            with zipfile.ZipFile(path) as source:
                contents = [(entry, source.read(entry)) for entry in source.infolist()]
            contents[-1][0].external_attr = (stat.S_IFLNK | 0o777) << 16
            with zipfile.ZipFile(path, "w") as destination:
                for entry, data in contents:
                    destination.writestr(entry, data)
            return path

        with patch("installer_prototype.bundle_publication.assemble", side_effect=linked):
            with self.assertRaisesRegex(ValueError, "regular files"):
                self.run_publication(initialize=True)
        self.assertFalse(self.store.objects)

    def test_failed_upload_then_retry_keeps_live_page_until_complete(self):
        self.run_publication(initialize=True)
        original = dict(self.store.objects)
        self.revision = 2

        def fail(name):
            if name.startswith("bundles/") and name.endswith("index.html"):
                raise OSError("page upload failed")

        self.store.before_put = fail
        with self.assertRaisesRegex(OSError, "page upload failed"):
            self.run_publication()
        for name, value in original.items():
            self.assertEqual(self.store.objects[name], value)
        self.store.before_put = None
        self.assertEqual(self.run_publication()["status"], "published")

    def test_verification_or_public_routing_failure_preserves_live_page(self):
        self.run_publication(initialize=True)
        page = self.store.objects["index.html"]
        self.revision = 2
        with patch.object(self.store, "verify_immutable", side_effect=ValueError("corrupt origin")):
            with self.assertRaisesRegex(ValueError, "corrupt origin"):
                self.run_publication()
        self.assertEqual(self.store.objects["index.html"], page)
        with patch.object(self, "public_check", side_effect=ValueError("public URL invalid")):
            with self.assertRaisesRegex(ValueError, "public URL invalid"):
                self.run_publication()
        self.assertEqual(self.store.objects["index.html"], page)

    def test_failed_final_write_leaves_previous_page_and_lost_ack_is_recovered(self):
        self.run_publication(initialize=True)
        page = self.store.objects["index.html"]
        self.revision = 2

        def fail(name):
            if name == "index.html":
                raise OSError("before commit")

        self.store.before_put = fail
        with self.assertRaisesRegex(OSError, "before commit"):
            self.run_publication()
        self.assertEqual(self.store.objects["index.html"], page)
        self.store.before_put = None
        put = self.store.put

        def lost_ack(name, *args, **kwargs):
            put(name, *args, **kwargs)
            if name == "index.html":
                raise OSError("lost response after commit")

        with patch.object(self.store, "put", side_effect=lost_ack):
            self.assertEqual(self.run_publication()["status"], "published")
        self.assertNotEqual(self.store.objects["index.html"], page)

    def test_competing_writer_is_not_overwritten_or_rolled_back(self):
        self.run_publication(initialize=True)
        self.revision = 2

        def compete(name):
            if name == "index.html":
                self.store.seed(name, b"a competing publication")

        self.store.before_put = compete
        with self.assertRaises(PublicationConflict):
            self.run_publication()
        self.assertEqual(self.store.objects["index.html"].data, b"a competing publication")

    def test_older_package_set_or_tampered_head_cannot_replace_last_success(self):
        self.revision = 2
        self.run_publication(initialize=True)
        original = dict(self.store.objects)
        self.revision = 1
        with self.assertRaisesRegex(ValueError, "roll back"):
            self.run_publication()
        self.assertEqual(self.store.objects, original)
        self.store.seed("index.html", original["index.html"].data.replace(b"2.0.0", b"9.0.0"))
        with self.assertRaisesRegex(ValueError, "signature"):
            self.run_publication()

    def test_artifactory_bundle_promotion_failure_preserves_page_and_retry_succeeds(self):
        config = {"type": "artifactory", "api_url": "https://packages.example.org/artifactory",
                  "staging_repository": "staging-local", "release_repository": "release-local",
                  "prefix": "downloads", "writer_repository": "example/installer"}
        self.server = FakeArtifactory()
        env = {"GITHUB_ACTIONS": "true", "GITHUB_REPOSITORY": "example/installer", "ARTIFACTORY_ACCESS_TOKEN": "test-token",
               "PUBLICATION_SERIALIZATION_KEY": publication_key(config)}
        with patch.dict(os.environ, env), patch("installer_prototype.publication_storage.build_opener", return_value=self.server):
            self.store = ArtifactoryStore(config)
            self.run_publication(initialize=True)
            original = dict(self.server.files)
            self.revision = 2
            self.server.copy_mode = "missing"
            with self.assertRaisesRegex(ValueError, "incomplete"):
                self.run_publication()
            for name, value in original.items():
                self.assertEqual(self.server.files[name], value)
            self.server.copy_mode = "normal"
            self.assertEqual(self.run_publication()["status"], "published")
            self.assertIn("previous", read_page(self.server.files["release-local/downloads/index.html"], self.source))

    @unittest.skipUnless(os.environ.get("IFW_PATH"), "Set IFW_PATH to Qt IFW 4.11")
    def test_real_qt_repositories_assemble_publish_and_retain_pinned_registry(self):
        from installer_prototype.demo import create, write_spec
        from installer_prototype.repository import publish

        self.assembler.stop()
        demo = self.root / "native"
        registry = create(demo, "linux-x64", Path(os.environ["IFW_PATH"]))
        self.source = json.loads((demo / "registry-source.json").read_text())
        self.key = demo / "keys/bundle.pem"
        self.run_publication(initialize=True)
        before = read_page(self.store.objects["index.html"].data, self.source)
        spec = write_spec(demo / "specs", "elastic", "linux-x64", 2)
        publish(spec, demo / "store", demo / "keys/elastic.pem", registry, Path(os.environ["IFW_PATH"]))
        self.run_publication()
        after = read_page(self.store.objects["index.html"].data, self.source)
        self.assertEqual(after["previous"], before["current"])
        self.assertEqual(after["current"]["publication"]["signed"]["repositories"]["elastic"]["revision"], 2)
        self.assertEqual(after["current"]["registry_record"], before["current"]["registry_record"])


class PublicDownloadTests(unittest.TestCase):
    def test_public_checks_use_head_for_zip_and_require_inline_exact_html(self):
        with tempfile.TemporaryDirectory() as tmp:
            archive = Path(tmp) / "bundle.zip"
            archive.write_bytes(b"ZIP")
            files = {"bundles/test/bundle.zip": archive, "bundles/test/index.html": b"<html>page</html>"}
            requests = []

            def open_response(request, timeout):
                requests.append(request.get_method())
                page = request.full_url.endswith(".html")
                response = io.BytesIO(b"<html>page</html>" if page else b"")
                response.geturl = lambda: request.full_url
                response.headers = Message()
                response.headers["Content-Length"] = "3"
                response.headers["Content-Type"] = "text/html" if page else "application/zip"
                response.headers["Content-Disposition"] = "inline"
                return response

            with patch("installer_prototype.bundle_publication.urlopen", side_effect=open_response):
                verify_public_files("https://example.org", files)
            self.assertEqual(requests, ["HEAD", "GET"])

            def attachment(request, timeout):
                result = open_response(request, timeout)
                result.headers.replace_header("Content-Disposition", "attachment")
                return result

            with patch("installer_prototype.bundle_publication.urlopen", side_effect=attachment):
                with self.assertRaisesRegex(ValueError, "serve HTML inline"):
                    verify_public_files("https://example.org", files)


@unittest.skipUnless(importlib.util.find_spec("boto3"), "Install the ci extra")
class MultipartTests(unittest.TestCase):
    def setUp(self):
        import boto3
        from botocore.stub import Stubber

        client = boto3.client("s3", region_name="us-east-1", aws_access_key_id="test", aws_secret_access_key="test")
        with patch("boto3.client", return_value=client):
            self.store = S3Store({"bucket": "test-bucket", "prefix": "downloads"})
        self.store.multipart_threshold = 1
        self.store.multipart_part_size = 5 * 1024**2
        self.stub = Stubber(client)
        self.stub.activate()
        self.addCleanup(self.stub.deactivate)
        self.addCleanup(self.stub.assert_no_pending_responses)

    def setup_upload(self, path, *, fail_part=False):
        chunks = [b"x" * (5 * 1024**2), b"ZIP trailer"]
        data = b"".join(chunks)
        path.write_bytes(data)
        args = {"Bucket": "test-bucket", "Key": "downloads/bundles/test/bundle.zip"}
        self.stub.add_response("create_multipart_upload", {"UploadId": "upload"}, {
            **args, "ContentType": "application/zip", "Metadata": {"sha256": hashlib.sha256(data).hexdigest()},
            "CacheControl": "public,max-age=31536000,immutable"})
        for number, chunk in enumerate(chunks, 1):
            part = {**args, "UploadId": "upload", "PartNumber": number, "Body": chunk, "ContentLength": len(chunk),
                    "ContentMD5": base64.b64encode(hashlib.md5(chunk).digest()).decode()}
            if fail_part:
                self.stub.add_client_error("upload_part", "InternalError", http_status_code=500, expected_params=part)
                self.stub.add_response("abort_multipart_upload", {}, {**args, "UploadId": "upload"})
                return
            self.stub.add_response("upload_part", {"ETag": f'"part-{number}"'}, part)
        self.stub.add_response("complete_multipart_upload", {}, {**args, "UploadId": "upload", "IfNoneMatch": "*",
            "MultipartUpload": {"Parts": [{"PartNumber": 1, "ETag": '"part-1"'}, {"PartNumber": 2, "ETag": '"part-2"'}]}})
        self.stub.add_response("head_object", {"ContentLength": len(data), "Metadata": {"sha256": hashlib.sha256(data).hexdigest()}}, args)

    def test_multipart_completion_retains_condition_and_verifies_object_metadata(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "bundle.zip"
            self.setup_upload(path)
            self.store.publish_immutable({"bundles/test/bundle.zip": path})
            self.store.verify_immutable({"bundles/test/bundle.zip": path})

    def test_failed_part_aborts_upload_without_completing_object(self):
        from botocore.exceptions import ClientError

        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "bundle.zip"
            self.setup_upload(path, fail_part=True)
            with self.assertRaises(ClientError):
                self.store.publish_immutable({"bundles/test/bundle.zip": path})
