"""Real Qt engine acceptance tests. Set IFW_PATH to enable; no external services."""
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import platform
import shutil
import subprocess
import sys
import tempfile
import threading
import unittest
import xml.etree.ElementTree as ET
import zipfile

from installer_prototype.bundle import assemble, load_bundle
from installer_prototype.common import atomic_json, digest, run
from installer_prototype.demo import create, write_spec
from installer_prototype.manager import build_installer, check_updates, inventory, launch
from installer_prototype.repository import publish, select, tool


@unittest.skipUnless(os.environ.get("IFW_PATH"), "Set IFW_PATH to Qt IFW 4.11")
class NativeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="wpilib-native-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.ifw = Path(os.environ["IFW_PATH"])
        self.target = {"Linux": "linux", "Darwin": "macos", "Windows": "windows"}[platform.system()]
        self.target += "-arm64" if platform.machine().lower() in ("arm64", "aarch64") else "-x64"
        self.cache = self.root / "cache"

    def publish_tool(self, revision, minimum="1.0.0"):
        spec = write_spec(self.root / "demo" / "specs", "elastic", self.target, revision, minimum)
        return publish(spec, self.root / "demo" / "store", self.root / "demo" / "keys" / "elastic.pem", self.registry, self.ifw)

    def unpack(self, archive):
        destination = self.root / "usb" / archive.stem
        if not destination.exists():
            with zipfile.ZipFile(archive) as stream:
                stream.extractall(self.root / "usb")
        return destination

    def offline(self, bundle, engine, root, command, *packages, registry_path=None):
        args = [sys.executable, "-m", "installer_prototype", "run", "--registry",
                str(registry_path or self.root / "demo" / "registry.json"), "--bundle", str(bundle),
                "--cache", str(self.root / "offline-cache"), "--engine", str(engine),
                "--root", str(root), "--command", command, "--headless", *packages]
        if os.environ.get("IFW_TEST_NETWORK_NAMESPACE") == "1":
            args = ["unshare", "-Urn", *args]
        run(args, timeout=180)

    def test_online_and_offline_lifecycle(self):
        requests = []

        class Handler(SimpleHTTPRequestHandler):
            def do_GET(self):
                requests.append(self.path)
                super().do_GET()

            def log_message(self, *_args):
                pass

        server = ThreadingHTTPServer(("127.0.0.1", 0), partial(Handler, directory=str(self.root / "demo" / "store")))
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        self.registry = create(self.root / "demo", self.target, self.ifw, f"http://127.0.0.1:{server.server_port}")
        suffix = ".exe" if os.name == "nt" else ".app" if sys.platform == "darwin" else ""
        engine = build_installer(self.ifw, self.root / ("installer" + suffix))
        installation = self.root / "installed"
        snapshots, held = select(self.registry)
        launch(engine, snapshots, self.cache, root=installation, command="install", profile="tools",
               profiles=self.registry["profiles"], headless=True)
        self.assertEqual(set(inventory(installation)), {"org.wpilib.elastic", "org.wpilib.advantagescope", "org.wpilib.runtime"})
        self.assertFalse(any("org.wpilib.cpp/" in path or "org.wpilib.java/" in path or "/org.robotpy." in path for path in requests))
        maintenance = installation / ("maintenancetool" + suffix)
        core_index = self.root / "demo" / "store" / self.target / "core" / "index.json"
        core_before = core_index.read_bytes()
        self.publish_tool(2)
        self.assertEqual(core_index.read_bytes(), core_before)
        snapshots, held = select(self.registry)
        updates = check_updates(installation, snapshots, held)
        self.assertEqual([p["id"] for p in updates["updates"]], ["org.wpilib.elastic"])
        requests.clear()
        launch(maintenance, snapshots, self.cache, command="update", packages=["org.wpilib.elastic"], headless=True)
        self.assertEqual(inventory(installation)["org.wpilib.elastic"], "2.0.0")
        self.assertFalse(any("org.wpilib.runtime/" in path for path in requests))
        bundle_key = self.root / "demo" / "keys" / "bundle.pem"
        old = self.unpack(assemble(self.registry, self.root / "bundles", bundle_key, self.cache))
        self.publish_tool(3)
        archive = assemble(self.registry, self.root / "bundles", bundle_key, self.cache)
        current = self.unpack(archive)
        self.assertEqual(archive, assemble(self.registry, self.root / "bundles", bundle_key, self.cache))
        # Incompatible tool release is held back; no other repository needs rewriting.
        self.publish_tool(4, "9.0.0")
        snapshots, held = select(self.registry)
        self.assertEqual(held[0]["selected"], 3)
        # Diagnostics are part of the bundle identity; the payloads remain cached.
        assemble(self.registry, self.root / "bundles", bundle_key, self.cache)
        server.shutdown()
        thread.join()
        self.offline(current, maintenance, installation, "update", "org.wpilib.elastic")
        self.assertEqual(inventory(installation)["org.wpilib.elastic"], "3.0.0")
        self.offline(old, maintenance, installation, "update", "org.wpilib.elastic")
        self.assertEqual(inventory(installation)["org.wpilib.elastic"], "3.0.0")
        fresh = self.root / "fresh"
        self.offline(current, engine, fresh, "install", "org.wpilib.java")
        self.assertEqual(set(inventory(fresh)), {"org.wpilib.runtime", "org.wpilib.java"})
        self.offline(current, maintenance, installation, "remove", "org.wpilib.elastic")
        self.assertNotIn("org.wpilib.elastic", inventory(installation))
        self.assertIn("org.wpilib.runtime", inventory(installation))
        self.offline(current, maintenance, installation, "install", "org.wpilib.elastic")
        self.assertEqual(inventory(installation)["org.wpilib.elastic"], "3.0.0")
        # Missing/corrupt media is rejected even with a populated download cache.
        snapshots, _ = load_bundle(current, self.registry, self.cache)
        file = next(n for n in snapshots[1].manifest["files"] if n.endswith(".7z"))
        (current / "repos" / "elastic" / file).write_bytes(b"corrupt")
        with self.assertRaisesRegex(ValueError, "Corrupt"):
            load_bundle(current, self.registry, self.cache)

    def test_sparse_repogen_needs_metadata_but_not_old_payloads(self):
        packages = self.root / "packages"
        for name in ("org.example.a", "org.example.b"):
            package = packages / name
            (package / "meta").mkdir(parents=True)
            (package / "data").mkdir()
            (package / "data" / f"{name}.txt").write_text(name)
            (package / "meta" / "package.xml").write_text(
                f"<Package><DisplayName>{name}</DisplayName><Description>Fixture</Description>"
                "<Version>1.0.0</Version><ReleaseDate>2026-11-26</ReleaseDate></Package>")
        original = self.root / "original"
        run([tool(self.ifw, "repogen"), "-p", packages, original])
        sparse = self.root / "sparse"
        sparse.mkdir()
        shutil.copyfile(original / "Updates.xml", sparse / "Updates.xml")
        metadata = ET.parse(original / "Updates.xml").findtext("MetadataName")
        self.assertTrue(metadata)
        shutil.copyfile(original / metadata, sparse / metadata)
        changed = self.root / "changed"
        shutil.copytree(packages / "org.example.a", changed / "org.example.a")
        xml = changed / "org.example.a" / "meta" / "package.xml"
        xml.write_text(xml.read_text().replace("1.0.0", "2.0.0"))
        run([tool(self.ifw, "repogen"), "--update", "-p", changed, sparse])
        entries = {p.findtext("Name"): p.findtext("Version") for p in ET.parse(sparse / "Updates.xml").findall("PackageUpdate")}
        self.assertEqual(entries, {"org.example.a": "2.0.0", "org.example.b": "1.0.0"})
        self.assertFalse((sparse / "org.example.b").exists())

    def test_failed_assembly_preserves_last_download(self):
        self.registry = create(self.root / "demo", self.target, self.ifw)
        destination = self.root / "downloads"
        key = self.root / "demo" / "keys" / "bundle.pem"
        archive = assemble(self.registry, destination, key, self.cache)
        previous = (destination / "latest.json").read_bytes()
        index = self.root / "demo" / "store" / self.target / "elastic" / "index.json"
        index.unlink()
        with self.assertRaises(Exception):
            assemble(self.registry, destination, key, self.cache)
        self.assertEqual(previous, (destination / "latest.json").read_bytes())
        self.assertTrue(archive.exists())
        self.assertTrue((destination / "index.html").exists())

    def test_fourth_repository_offline_python_profile_then_online_update(self):
        self.registry = create(self.root / "demo", self.target, self.ifw)
        self.assertEqual(len(self.registry["repositories"]), 4)
        snapshots, _ = select(self.registry)
        self.assertEqual([s.publisher for s in snapshots], self.registry["repository_order"])
        archive = assemble(self.registry, self.root / "bundles", self.root / "demo/keys/bundle.pem", self.cache)
        bundle = self.unpack(archive)
        self.assertTrue((bundle / "repos/robotpy/Updates.xml").is_file())
        suffix = ".exe" if os.name == "nt" else ".app" if sys.platform == "darwin" else ""
        engine = build_installer(self.ifw, self.root / ("installer" + suffix))
        installation = self.root / "python-install"
        # Test the CLI profile from registry, with networking disabled when enabled.
        self.offline(bundle, engine, installation, "install", "--profile", "python")
        installed = inventory(installation)
        self.assertEqual(set(installed), {"org.robotpy.python", "org.robotpy.sdk", "org.robotpy.deploy", "org.wpilib.runtime"})
        before = (self.root / "demo/store" / self.target / "core/index.json").read_bytes()
        spec = write_spec(self.root / "demo/specs", "robotpy", self.target, 2)
        publish(spec, self.root / "demo/store", self.root / "demo/keys/robotpy.pem", self.registry, self.ifw)
        self.assertEqual(before, (self.root / "demo/store" / self.target / "core/index.json").read_bytes())
        snapshots, held = select(self.registry)
        self.assertEqual(len(check_updates(installation, snapshots, held)["updates"]), 3)
        launch(installation / ("maintenancetool" + suffix), snapshots, self.cache, command="update", headless=True)
        self.assertEqual(inventory(installation)["org.robotpy.sdk"], "2.0.0")
        # An old three-repository bundle must not claim completeness for a four-repository registry.
        from installer_prototype.common import sign
        manifest_path = bundle / "bundle-manifest.json"
        manifest = json.loads(manifest_path.read_text())["signed"]
        del manifest["repositories"]["robotpy"]
        atomic_json(manifest_path, sign(manifest, self.root / "demo/keys/bundle.pem"))
        with self.assertRaisesRegex(ValueError, "Incomplete"):
            load_bundle(bundle, self.registry, self.cache)

    def test_single_custom_publisher_without_default_projects(self):
        from installer_prototype.common import keygen
        publisher = "vendor-tools"
        public = keygen(self.root / "vendor.pem")
        bundle_public = keygen(self.root / "bundle.pem")
        registry = {"schema": 1, "season": "2027", "channel": "prototype", "target": self.target,
            "bundle_public_key": bundle_public, "repositories": {publisher: {
                "url": (self.root / "store" / self.target / publisher).as_uri(), "public_key": public,
                "components": ["org.example.utility"]}}}
        (self.root / "payload").mkdir()
        (self.root / "payload/utility.txt").write_text("custom publisher")
        spec = self.root / "spec.json"
        atomic_json(spec, {"publisher": publisher, "season": "2027", "channel": "prototype", "target": self.target,
            "revision": 1, "packages": [{"id": "org.example.utility", "name": "Utility", "version": "1.0.0", "payload": "payload"}]})
        publish(spec, self.root / "store", self.root / "vendor.pem", registry, self.ifw)
        self.assertEqual([s.publisher for s in select(registry)[0]], [publisher])
        bundle = self.unpack(assemble(registry, self.root / "downloads", self.root / "bundle.pem", self.cache))
        self.assertEqual([s.publisher for s in load_bundle(bundle, registry, self.cache)[0]], [publisher])
        manifest = json.loads((bundle / "bundle-manifest.json").read_text())["signed"]
        manifest["repositories"]["untrusted"] = manifest["repositories"][publisher]
        from installer_prototype.common import sign
        atomic_json(bundle / "bundle-manifest.json", sign(manifest, self.root / "bundle.pem"))
        with self.assertRaisesRegex(ValueError, "Incomplete"):
            load_bundle(bundle, registry, self.cache)

    def test_authenticated_registry_addition_and_old_bundle_after_refresh(self):
        import copy
        from installer_prototype.registry_updates import cache_directory, publish_registry, resolve

        class Handler(SimpleHTTPRequestHandler):
            def log_message(self, *_args):
                pass

        server = ThreadingHTTPServer(("127.0.0.1", 0), partial(Handler, directory=str(self.root / "demo/store")))
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        self.registry = create(self.root / "demo", self.target, self.ifw, f"http://127.0.0.1:{server.server_port}")
        source = json.loads((self.root / "demo/registry-source.json").read_text())
        source["registry_id"] += "-dynamic"
        source["url"] = f"http://127.0.0.1:{server.server_port}/{self.target}/dynamic/registry.json"
        source_path = self.root / "demo/dynamic-source.json"
        atomic_json(source_path, source)
        bootstrap_bytes = source_path.read_bytes()
        initial = copy.deepcopy(self.registry)
        del initial["repositories"]["robotpy"]
        del initial["profiles"]["python"]
        initial["repository_order"].remove("robotpy")
        authority_key = self.root / "demo/keys/registry.pem"
        feed = self.root / "demo/store" / self.target / "dynamic"
        publish_registry(initial, source, authority_key, 1, feed)
        cache = self.root / "offline-cache"
        view = resolve(source, cache)
        archive = assemble(view.registry, self.root / "dynamic-bundles", self.root / "demo/keys/bundle.pem",
                           cache, registry_record=view.record)
        old_bundle = self.unpack(archive)
        suffix = ".exe" if os.name == "nt" else ".app" if sys.platform == "darwin" else ""
        engine = build_installer(self.ifw, self.root / ("installer" + suffix))
        from installer_prototype.manager import executable
        engine_hash = digest(executable(engine))
        installation = self.root / "dynamic-install"
        self.offline(old_bundle, engine, installation, "install", "--profile", "tools", registry_path=source_path)
        # Same client/bootstrap, newly authorized publisher and profile.
        publish_registry(self.registry, source, authority_key, 2, feed)
        cli = [sys.executable, "-m", "installer_prototype"]
        refreshed = json.loads(run([*cli, "refresh-registry", "--registry", source_path, "--cache", cache]))
        self.assertEqual(refreshed["revision"], 2)
        self.assertIn("robotpy", refreshed["publishers"])
        run([*cli, "run", "--registry", source_path, "--cache", cache, "--engine",
             installation / ("maintenancetool" + suffix), "--root", installation,
             "--command", "install", "--profile", "python", "--headless"])
        self.assertIn("org.robotpy.sdk", inventory(installation))
        self.assertEqual(engine_hash, digest(executable(engine)))
        self.assertEqual(bootstrap_bytes, source_path.read_bytes())
        saved = (cache_directory(source, cache) / "current.json").read_bytes()
        server.shutdown()
        thread.join()
        # Even after revision 2 was cached, old media must use its own three-publisher registry.
        self.offline(old_bundle, engine, self.root / "old-media-install", "install", "--profile", "tools", registry_path=source_path)
        old_view = resolve(source, cache, bundle=old_bundle)
        self.assertEqual(old_view.summary()["revision"], 1)
        self.assertNotIn("robotpy", old_view.registry["repositories"])
        self.assertEqual(saved, (cache_directory(source, cache) / "current.json").read_bytes())


if __name__ == "__main__":
    unittest.main()
