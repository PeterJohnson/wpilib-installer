"""Independent publisher snapshots and conservative cross-repository selection."""
from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
import itertools
import json
from pathlib import Path
import re
import shutil
import tempfile
import xml.etree.ElementTree as ET

from .common import (TARGETS, atomic_json, cached_file, digest, read_json_url,
                     resource_url, run, safe_path, sign, verify, version)
from .registry import validate_registry
from .payload import inventory as payload_inventory, validate_records, validate_ownership


@contextmanager
def publication_lock(directory):
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    lock = directory / ".publication-lock"
    try:
        lock.mkdir()
    except FileExistsError as exc:
        raise ValueError(f"Publication already in progress: {directory}") from exc
    try:
        yield
    finally:
        lock.rmdir()


def tool(ifw, name):
    directory = Path(ifw)
    if (directory / "bin").exists():
        directory /= "bin"
    exe = directory / (name + (".exe" if __import__("os").name == "nt" else ""))
    if not exe.is_file():
        raise ValueError(f"Missing Qt IFW tool: {exe}")
    return exe.resolve()


def xml_file(root, path):
    ET.indent(root)
    ET.ElementTree(root).write(path, encoding="utf-8", xml_declaration=True)


def validate_packages(packages, allowed):
    ids, files = set(), set()
    for p in packages:
        name = p["id"]
        if name not in allowed or name in ids or not re.fullmatch(r"[a-z][a-z0-9.]+", name):
            raise ValueError(f"Unauthorized or duplicate component: {name}")
        ids.add(name)
        version(p["version"])
        for dep, minimum in p.get("dependencies", {}).items():
            if not re.fullmatch(r"[a-z][a-z0-9.]+", dep):
                raise ValueError(f"Invalid dependency ID: {dep}")
            version(minimum)
        if "file_records" in p:
            validate_records(p["file_records"])
            if set(p["file_records"]) != set(p["owned_files"]):
                raise ValueError("Payload records differ from component ownership")
        for filename in p["owned_files"]:
            safe_path(filename)
            folded = filename.casefold()
            if folded in files:
                raise ValueError(f"Conflicting ownership: {filename}")
            files.add(folded)
    validate_ownership(files)


def publish(spec_path, store, key, registry, ifw, *, tuf_keys=None, update_tuf=True):
    """Build a complete immutable publisher revision; other publishers are untouched."""
    spec_path = Path(spec_path).resolve()
    spec = json.loads(spec_path.read_text())
    validate_registry(registry)
    publisher, target = spec["publisher"], spec["target"]
    if publisher not in registry["repositories"] or target not in TARGETS:
        raise ValueError("Unknown publisher or target")
    revision = spec["revision"]
    if type(revision) is not int or revision < 1:
        raise ValueError("Revision must be a positive integer")
    if (spec["season"], spec["channel"], target) != (registry["season"], registry["channel"], registry["target"]):
        raise ValueError("Release and registry target/channel/season differ")
    root = Path(store).resolve() / target / publisher
    with publication_lock(root), tempfile.TemporaryDirectory(prefix="ifw-publish-") as tmp:
        index_path = root / "index.json"
        index = json.loads(index_path.read_text()) if index_path.exists() else {"revisions": []}
        if revision <= max(index["revisions"], default=0):
            raise ValueError("Repository revisions must increase")
        packages_dir = Path(tmp) / "packages"
        metadata = []
        for package in spec["packages"]:
            p = dict(package)
            payload = spec_path.parent / p.pop("payload")
            if not payload.is_dir():
                raise ValueError(f"Missing payload directory: {payload}")
            p["file_records"] = payload_inventory(payload)
            if p.get("macos_xattrs"):
                from .macos_metadata import records, restore_script
                if not target.startswith("macos-"):
                    raise ValueError("macOS metadata requires a macOS package")
                attributes = json.loads((payload / str(safe_path(p["macos_xattrs"]))).read_text())
                script = payload / str(safe_path(p["macos_xattr_script"]))
                if script.read_text() != restore_script(attributes):
                    raise ValueError("macOS restore script differs from its declared metadata")
                for name, attrs in records(attributes).items():
                    if p["file_records"].get(name, {}).get("type") != "file":
                        raise ValueError("macOS metadata refers to an absent or non-regular payload")
                    p["file_records"][name]["xattrs"] = attrs
            p["owned_files"] = sorted(p["file_records"])
            metadata.append(p)
        validate_packages(metadata, registry["repositories"][publisher]["components"])
        for package, p in zip(spec["packages"], metadata):
            package_dir = packages_dir / p["id"]
            (package_dir / "meta").mkdir(parents=True)
            shutil.copytree(spec_path.parent / package["payload"], package_dir / "data", symlinks=True)
            xml = ET.Element("Package")
            for tag, value in {"DisplayName": p["name"], "Description": p.get("description", p["name"]),
                               "Version": p["version"], "ReleaseDate": p.get("release_date", "2026-11-26"), "Default": "false"}.items():
                ET.SubElement(xml, tag).text = value
            if p.get("macos_xattr_script"):
                # Qt owns the files and their uninstall/rollback operations. The
                # extra operation restores attributes attached to those files;
                # deleting the owned files also deletes their attributes.
                path = "@TargetDir@/" + str(safe_path(p["macos_xattr_script"]))
                script = ('function Component() {}\n'
                          'Component.prototype.createOperations = function() {\n'
                          '    component.createOperations();\n'
                          '    component.addOperation("Execute", "/bin/sh", ' + json.dumps(path) + ', "@TargetDir@");\n'
                          '};\n')
                (package_dir / "meta" / "install.js").write_text(script, encoding="utf-8")
                ET.SubElement(xml, "Script").text = "install.js"
            deps = p.get("dependencies", {})
            if deps:
                ET.SubElement(xml, "Dependencies").text = ",".join(f"{name}->={minimum}" for name, minimum in deps.items())
            xml_file(xml, package_dir / "meta" / "package.xml")
        repo = Path(tmp) / "repository"
        archive_format = spec.get("archive_format", "7z")
        if archive_format not in ("7z", "tar.gz"):
            raise ValueError("Unsupported package archive format")
        run([tool(ifw, "repogen"), "--archive-format", archive_format, "-p", packages_dir, repo], timeout=900)
        files = {f.relative_to(repo).as_posix(): {"sha256": digest(f), "size": f.stat().st_size}
                 for f in sorted(repo.rglob("*")) if f.is_file()}
        manifest = {"schema": 1, "publisher": publisher, "target": target,
                    "season": spec["season"], "channel": spec["channel"], "revision": revision,
                    "packages": metadata, "files": files}
        envelope = sign(manifest, key)
        verify(envelope, registry["repositories"][publisher]["public_key"])
        atomic_json(repo / "release.json", envelope)
        destination = root / "releases" / str(revision)
        destination.parent.mkdir(parents=True, exist_ok=True)
        if destination.exists():
            raise ValueError(f"Immutable release already exists: {destination}")
        # Local prototype storage. A remote publisher uploads files first, index last.
        shutil.copytree(repo, destination)
        index["revisions"].append(revision)
        atomic_json(index_path, index)
        if update_tuf and "tuf" in registry["repositories"][publisher]:
            from .tuf_repository import DirectoryStore, ROLES, publish_catalog
            keys = tuf_keys or {role: [Path(key).parent / (publisher + "-tuf") / (role + ".pem")] for role in ROLES}
            releases = [json.loads((root / "releases" / str(r) / "release.json").read_text())
                        for r in index["revisions"][-8:]]
            publish_catalog(DirectoryStore(root), registry["repositories"][publisher]["tuf"], keys,
                            publisher_catalog(registry, publisher, releases), initialize=len(index["revisions"]) == 1)
        return destination


@dataclass
class Snapshot:
    manifest: dict
    envelope: dict
    base: str
    tuf_evidence: dict | None = None

    @property
    def publisher(self):
        return self.manifest["publisher"]

    def file(self, name, cache):
        return cached_file(self.base, name, self.manifest["files"][name], cache)


def load_snapshot(envelope, base, publisher, registry):
    validate_registry(registry)
    trust = registry["repositories"][publisher]
    m = verify(envelope, trust["public_key"])
    if m["schema"] != 1 or m["publisher"] != publisher:
        raise ValueError("Wrong repository identity/schema")
    for field in ("target", "season", "channel"):
        if m[field] != registry[field]:
            raise ValueError(f"Repository {field} mismatch")
    if type(m["revision"]) is not int or m["revision"] < trust.get("minimum_revision", 1):
        raise ValueError("Repository revision below trusted floor")
    validate_packages(m["packages"], trust["components"])
    for path in m["files"]:
        safe_path(path)
    if "Updates.xml" not in m["files"]:
        raise ValueError("Missing IFW index")
    return Snapshot(m, envelope, base)


def publisher_catalog(registry, publisher, releases):
    return {"kind": "publisher-catalog", "schema": 1, "publisher": publisher,
            **{k: registry[k] for k in ("target", "season", "channel")}, "releases": releases}


def catalog_releases(catalog, registry, publisher):
    if (catalog.get("kind") != "publisher-catalog" or catalog.get("schema") != 1
            or catalog.get("publisher") != publisher
            or any(catalog.get(k) != registry[k] for k in ("target", "season", "channel"))):
        raise ValueError("TUF publisher catalog scope mismatch")
    releases = catalog["releases"]
    if not isinstance(releases, list) or not 1 <= len(releases) <= 8:
        raise ValueError("Invalid TUF publisher release history")
    # Authenticate retained history without requiring already-withdrawn revisions
    # to satisfy the current floor. Only candidates at/above the floor are usable.
    import copy
    history_registry = copy.deepcopy(registry)
    history_registry["repositories"][publisher]["minimum_revision"] = 1
    revisions = [load_snapshot(r, "", publisher, history_registry).manifest["revision"] for r in releases]
    if revisions != sorted(set(revisions)):
        raise ValueError("TUF release revisions must be unique and increasing")
    return releases


def candidates(registry, cache=None):
    result = {}
    for publisher in validate_registry(registry):
        repo = registry["repositories"][publisher]
        if "tuf" in repo:
            from .tuf_repository import fetch_catalog
            catalog, _, evidence = fetch_catalog(repo["tuf"], cache or Path("build/cache"))
            result[publisher] = []
            for envelope in reversed(catalog_releases(catalog, registry, publisher)):
                rev = envelope["signed"]["revision"]
                if rev < repo.get("minimum_revision", 1):
                    continue
                snapshot = load_snapshot(envelope, resource_url(repo["url"], f"releases/{rev}"), publisher, registry)
                snapshot.tuf_evidence = evidence
                result[publisher].append(snapshot)
            if not result[publisher]:
                raise ValueError(f"No authorized TUF revisions: {publisher}")
            continue
        index = read_json_url(resource_url(repo["url"], "index.json"))
        revisions = index["revisions"]
        if not revisions or len(revisions) > 256 or any(type(r) is not int or r < 1 for r in revisions):
            raise ValueError(f"Invalid or oversized revision index: {publisher}")
        result[publisher] = []
        # Keep the search bounded; production needs retention/selection policy.
        for rev in sorted(set(revisions), reverse=True)[:8]:
            if rev < repo.get("minimum_revision", 1):
                continue
            base = resource_url(repo["url"], f"releases/{rev}")
            envelope = read_json_url(resource_url(base, "release.json"))
            snapshot = load_snapshot(envelope, base, publisher, registry)
            if snapshot.manifest["revision"] != rev:
                raise ValueError("Index points to a different revision")
            result[publisher].append(snapshot)
        if not result[publisher]:
            raise ValueError(f"No authorized revisions: {publisher}")
    return result


def package_map(snapshots):
    packages, owned = {}, set()
    for snapshot in snapshots:
        for p in snapshot.manifest["packages"]:
            if p["id"] in packages:
                raise ValueError(f"Duplicate component: {p['id']}")
            packages[p["id"]] = p
            for name in p["owned_files"]:
                name = name.casefold()
                if name in owned:
                    raise ValueError(f"Cross-repository file collision: {name}")
                owned.add(name)
    validate_ownership(owned)
    return packages


def compatible(snapshots):
    packages = package_map(snapshots)
    for p in packages.values():
        for dep, minimum in p.get("dependencies", {}).items():
            if dep not in packages or version(packages[dep]["version"]) < version(minimum):
                return False
    return True


def select(registry, cache=None):
    available = candidates(registry, cache) if cache is not None else candidates(registry)
    # candidates() preserves explicit registry precedence (alphabetical by default).
    for attempt, choice in enumerate(itertools.product(*available.values())):
        if attempt >= 4096:
            raise ValueError("Compatibility search exceeded 4096 combinations; narrow repository history or constraints")
        if compatible(choice):
            held = [{"publisher": s.publisher, "selected": s.manifest["revision"],
                     "latest": available[s.publisher][0].manifest["revision"],
                     "reason": "Newer repository revision is incompatible with this set"}
                    for s in choice if s is not available[s.publisher][0]]
            return list(choice), held
    raise ValueError("No complete compatible repository set")


def validate_index(snapshot, cache):
    """Verify IFW metadata before exposing it; prevent redirects outside the adapter."""
    root = ET.parse(snapshot.file("Updates.xml", cache)).getroot()
    if root.find("RepositoryUpdate") is not None or root.find("RepositoryCategories") is not None:
        raise ValueError("Repository redirects/categories are not allowed")
    expected = {p["id"]: p for p in snapshot.manifest["packages"]}
    seen = set()
    for entry in root.findall("PackageUpdate"):
        name, rev = entry.findtext("Name"), entry.findtext("Version")
        if name not in expected or name in seen or rev != expected[name]["version"]:
            raise ValueError("IFW index differs from signed package manifest")
        seen.add(name)
        deps = ",".join(f"{key}->={value}" for key, value in expected[name].get("dependencies", {}).items())
        if set(filter(None, (entry.findtext("Dependencies") or "").split(","))) != set(filter(None, deps.split(","))):
            raise ValueError("IFW dependencies differ from signed package manifest")
        for archive in (entry.findtext("DownloadableArchives") or "").split(","):
            if archive and f"{name}/{rev}{archive}" not in snapshot.manifest["files"]:
                raise ValueError("Missing referenced package archive")
    if seen != set(expected):
        raise ValueError("IFW index omits signed components")
