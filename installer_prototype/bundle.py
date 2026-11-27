"""Prebuilt browser downloads containing the configured local IFW repositories."""
from __future__ import annotations

import hashlib
import html
import json
from pathlib import Path
import shutil
import tempfile
import zipfile

from .common import atomic_json, canonical, digest, safe_path, sign, verify
from .repository import (compatible, load_snapshot, publication_lock, select, validate_index)
from .registry import validate_registry
from .registry_updates import local_record, pinned_record, record_registry, registry_identity


def assemble(registry, destination, key, cache, *, registry_record=None, registry_evidence=None):
    registry_record = local_record(registry) if registry_record is None else registry_record
    if record_registry(registry_record) != registry:
        raise ValueError("Registry snapshot does not match the assembly registry")
    registry_ref = registry_identity(registry_record)
    destination = Path(destination).resolve()
    # Lock selection AND link advancement: an older job cannot replace a newer one.
    with publication_lock(destination):
        snapshots, held = select(registry, cache)
        from .archival import authorization
        archival = authorization(registry, registry_record, registry_evidence, snapshots)
        identity = {s.publisher: {"revision": s.manifest["revision"],
                                 "sha256": hashlib.sha256(canonical(s.envelope)).hexdigest()}
                    for s in snapshots}
        bundle_id = f"{registry['season']}-{registry['channel']}-{registry['target']}-" + hashlib.sha256(canonical({"registry": registry_ref, "repositories": identity, "held_back": held, "archival": archival})).hexdigest()[:16]
        safe_path(bundle_id)
        path = destination / f"{bundle_id}.zip"
        with tempfile.TemporaryDirectory(prefix="ifw-bundle-") as tmp:
            root = Path(tmp) / bundle_id
            for snapshot in snapshots:
                validate_index(snapshot, cache)
                repo = root / "repos" / snapshot.publisher
                for name in snapshot.manifest["files"]:
                    target = repo / name
                    target.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copyfile(snapshot.file(name, cache), target)
                atomic_json(repo / "release.json", snapshot.envelope)
            atomic_json(root / "registry.json", registry_record)
            content = {"schema": 2, "registry": registry_ref, "id": bundle_id, "target": registry["target"],
                       "season": registry["season"], "channel": registry["channel"],
                       "repositories": identity, "held_back": held,
                       "payload_kind": "prototype-fixtures"}
            if archival is not None:
                content.update(schema=3, archival=archival)
            envelope = sign(content, key)
            verify(envelope, registry["bundle_public_key"])
            atomic_json(root / "bundle-manifest.json", envelope)
            # Verify the entire copied set before making a download visible.
            load_bundle(root, registry, Path(tmp) / "verify-cache")
            pending = destination / f".{bundle_id}.pending"
            try:
                with zipfile.ZipFile(pending, "w", zipfile.ZIP_STORED, allowZip64=True) as archive:
                    for file in sorted(root.rglob("*")):
                        if file.is_file():
                            # Fixed timestamps make assembly reproducible from exact snapshots.
                            entry = zipfile.ZipInfo(file.relative_to(root.parent).as_posix(), (2026, 1, 1, 0, 0, 0))
                            with file.open("rb") as source, archive.open(entry, "w", force_zip64=True) as out:
                                shutil.copyfileobj(source, out)
                if path.exists():
                    if digest(path) != digest(pending):
                        raise ValueError("Bundle ID collision or non-reproducible assembly")
                    pending.unlink()
                else:
                    pending.replace(path)
            finally:
                pending.unlink(missing_ok=True)
        atomic_json(destination / "latest.json", {"bundle": path.name, "sha256": digest(path),
                    "registry": registry_ref, "repositories": identity, "held_back": held})
        page = ("<!doctype html><html lang='en'><meta charset='utf-8'>"
                "<meta name='viewport' content='width=device-width'><title>WPILib prototype packages</title>"
                f"<h1>WPILib prototype: {html.escape(registry['target'])}</h1>"
                "<p>Test payloads only. Download the installer separately.</p>"
                f"<p><a href='{path.name}'>Download this platform's complete package set</a></p>"
                f"<p>SHA-256: <code>{digest(path)}</code></p>"
                f"<pre>{html.escape(json.dumps({'registry': registry_ref, 'repositories': identity, 'held_back': held}, indent=2))}</pre></html>")
        pending_page = destination / ".index.html.pending"
        pending_page.write_text(page)
        pending_page.replace(destination / "index.html")
        return path


def load_bundle(root, registry, cache):
    publishers = validate_registry(registry)
    root = Path(root).resolve()
    manifest = verify(json.loads((root / "bundle-manifest.json").read_text()), registry["bundle_public_key"])
    if manifest["schema"] not in (1, 2, 3) or set(manifest["repositories"]) != set(publishers):
        raise ValueError("Incomplete or unsupported bundle")
    if manifest["schema"] in (2, 3) and record_registry(pinned_record(root)) != registry:
        raise ValueError("Bundle must use its pinned registry")
    if registry.get("online_trust") == "tuf":
        from .archival import verify_archival
        verify_archival(manifest, registry, pinned_record(root))
    for field in ("target", "season", "channel"):
        if manifest[field] != registry[field]:
            raise ValueError(f"Bundle {field} mismatch")
    snapshots = []
    for publisher in publishers:
        repo = root / "repos" / publisher
        envelope = json.loads((repo / "release.json").read_text())
        identity = manifest["repositories"][publisher]
        if hashlib.sha256(canonical(envelope)).hexdigest() != identity["sha256"]:
            raise ValueError("Bundle repository digest mismatch")
        snapshot = load_snapshot(envelope, repo.as_uri(), publisher, registry)
        if snapshot.manifest["revision"] != identity["revision"]:
            raise ValueError("Bundle repository revision mismatch")
        for name in snapshot.manifest["files"]:
            # Validate source bytes even if the user already has a cached copy.
            path = repo / name
            if path.is_symlink() or not path.is_file() or not path.resolve().is_relative_to(repo.resolve()):
                raise ValueError(f"Missing or escaping bundle file: {name}")
            record = snapshot.manifest["files"][name]
            if path.stat().st_size != record["size"] or digest(path) != record["sha256"]:
                raise ValueError(f"Corrupt bundle file: {name}")
        validate_index(snapshot, cache)
        snapshots.append(snapshot)
    if not compatible(snapshots):
        raise ValueError("Incompatible offline bundle")
    return snapshots, manifest.get("held_back", [])
