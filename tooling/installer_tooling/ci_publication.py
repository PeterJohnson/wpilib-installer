"""Signed CI publication through S3 conditional writes or serialized Artifactory promotion."""
from __future__ import annotations

import argparse
from contextlib import contextmanager, ExitStack
import json
import os
from pathlib import Path
import tempfile
import time

from .common import atomic_json, canonical, public_key_from_private, safe_path, verify
from .registry_updates import publish_registry, resolve, validate_source, validated_tuf_record, verified_record
from .repository import catalog_releases, load_snapshot, publish, publisher_catalog
from .publication_storage import (Object, PublicationConflict, S3Store,
                                  publication_key, storage_from_config)


def json_bytes(value):
    return canonical(value) + b"\n"


def revisions_from(head):
    if head is None:
        return []
    index = json.loads(head.data)
    revisions = index.get("revisions")
    if (set(index) != {"revisions"} or not isinstance(revisions, list) or not revisions
            or len(revisions) > 256 or any(type(r) is not int or r < 1 for r in revisions)
            or revisions != sorted(set(revisions))):
        raise ValueError("Invalid origin revision index")
    return revisions


def publisher_release(spec_path, registry, publisher, store, key, ifw, *, initialize=False):
    """Build one complete publisher snapshot without downloading old payloads."""
    spec = json.loads(Path(spec_path).read_text())
    if spec["publisher"] != publisher:
        raise ValueError("Release publisher differs from publication configuration")
    head = store.get("index.json")
    revisions = revisions_from(head)
    if head is None and not initialize:
        raise ValueError("Missing publisher index; initialize explicitly for the first release")
    if head is not None:
        latest = store.get(f"releases/{revisions[-1]}/release.json")
        if latest is None:
            raise ValueError("Origin head references a missing signed release")
        snapshot = load_snapshot(json.loads(latest.data), "", publisher, registry)
        if snapshot.manifest["revision"] != revisions[-1]:
            raise ValueError("Origin index and signed release revisions differ")
    revision = spec["revision"]
    if type(revision) is not int or revision <= max(revisions, default=0):
        raise ValueError("Publisher revision must exceed the current origin head")
    if store.reserved(f"releases/{revision}/release.json") is not None:
        raise PublicationConflict("Release revision already staged; allocate a higher revision")
    with tempfile.TemporaryDirectory(prefix="ci-publisher-") as tmp:
        root = Path(tmp)
        # Only index metadata is hydrated into the fresh repogen workspace.
        atomic_json(root / registry["target"] / publisher / "index.json", {"revisions": revisions})
        release = publish(spec_path, root, key, registry, ifw)
        envelope = json.loads((release / "release.json").read_text())
        load_snapshot(envelope, "", publisher, registry)
        # Artifactory stages and promotes this exact file set; S3 uploads directly.
        files = {f"releases/{revision}/{name}": release / name for name in sorted(envelope["signed"]["files"])}
        files[f"releases/{revision}/release.json"] = release / "release.json"
        store.publish_immutable(files)
        # Client history search is bounded; old immutable objects remain available.
        index = {"revisions": (revisions + [revision])[-256:]}
        store.put("index.json", json_bytes(index), previous=head.etag if head else None)
    return {"revision": revision, "release": envelope, "index": index}


def registry_release(source, store, key, *, approved=None, initialize=False,
                     valid_days=7, offline_days=365, renew_before_days=3, now=None):
    """Renew the origin's approved config, or explicitly publish a reviewed change."""
    validate_source(source)
    now = int(time.time() if now is None else now)
    if not (type(renew_before_days) is int and 0 <= renew_before_days < valid_days):
        raise ValueError("Renewal window must be nonnegative and shorter than validity")
    head = store.get("registry.json")
    previous = json.loads(head.data) if head else None
    if previous is None:
        if not initialize or approved is None:
            raise ValueError("Missing registry; initialize explicitly with an approved configuration")
        registry, revision = approved, source.get("minimum_revision", 1)
    else:
        # An expired but authentic head still establishes history. It can be renewed.
        current = verified_record(previous, source, mode="historical", now=now)
        if previous["signed"]["revision"] < source.get("minimum_revision", 1):
            raise ValueError("Origin registry is below the bootstrap revision floor")
        if approved is None and previous["signed"]["expires_at"] > now + renew_before_days * 86400:
            return {"status": "not-due", "revision": previous["signed"]["revision"]}
        registry = current if approved is None else approved
        revision = previous["signed"]["revision"] + 1
    # Abandoned immutable revisions must never be overwritten or block renewal.
    for _ in range(256):
        orphan = store.reserved(f"revisions/{revision}.json")
        if orphan is None:
            break
        record = json.loads(orphan.data)
        verified_record(record, source, mode="historical", now=now)
        if record["signed"]["revision"] != revision:
            raise ValueError("Immutable registry path and signed revision differ")
        revision += 1
    else:
        raise ValueError("Too many abandoned registry revisions; investigate publication")
    with tempfile.TemporaryDirectory(prefix="ci-registry-") as tmp:
        path = publish_registry(registry, source, key, revision, tmp,
                                valid_days=valid_days, offline_days=offline_days, now=now)
        record = json.loads(path.read_text())
        store.publish_immutable({f"revisions/{revision}.json": json_bytes(record)})
        store.put("registry.json", json_bytes(record), previous=head.etag if head else None)
    return {"status": "published", "revision": revision, "registry": record}


def tuf_publisher_release(spec_path, registry, publisher, store, key, ifw, tuf_keys, *, initialize=False):
    from .tuf_repository import origin_state, publish_catalog
    trust = registry["repositories"][publisher]["tuf"]
    _, head, _, current = origin_state(store, trust)
    releases = catalog_releases(current, registry, publisher) if current else []
    if head is None and not initialize:
        raise ValueError("Initialize TUF publisher explicitly")
    spec = json.loads(Path(spec_path).read_text())
    revision = spec["revision"]
    revisions = [r["signed"]["revision"] for r in releases]
    if spec["publisher"] != publisher or type(revision) is not int or revision <= max(revisions, default=0):
        raise ValueError("Publisher revision must advance its authenticated TUF catalog")
    if store.reserved(f"releases/{revision}/release.json") is not None:
        raise PublicationConflict("Release already staged; allocate a higher revision")
    with tempfile.TemporaryDirectory(prefix="tuf-publisher-") as tmp:
        root = Path(tmp)
        atomic_json(root / registry["target"] / publisher / "index.json", {"revisions": revisions})
        release = publish(spec_path, root, key, registry, ifw, update_tuf=False)
        envelope = json.loads((release / "release.json").read_text())
        files = {f"releases/{revision}/{name}": release / name for name in envelope["signed"]["files"]}
        files[f"releases/{revision}/release.json"] = release / "release.json"
        store.publish_immutable(files)
        store.verify_immutable(files)
        result = publish_catalog(store, trust, tuf_keys,
                                 publisher_catalog(registry, publisher, (releases + [envelope])[-8:]),
                                 initialize=initialize)
    return {**result, "revision": revision}


def tuf_registry_release(source, store, key, tuf_keys, *, approved=None, initialize=False,
                         valid_days=7, offline_days=None, renew_before_days=3, now=None):
    from .archival import INDEFINITE_POLICY
    from .tuf_repository import origin_state, publish_catalog
    if source.get("archive_policy") == INDEFINITE_POLICY and offline_days is not None:
        raise ValueError("offline_days conflicts with the indefinite archival policy; remove this setting")
    _, head, _, previous = origin_state(store, source["tuf"])
    now = int(time.time() if now is None else now)
    if not 0 <= renew_before_days < valid_days:
        raise ValueError("Invalid registry renewal window")
    if previous:
        registry = validated_tuf_record(previous, source, historical=True, now=now)
        revision = previous["signed"]["revision"] + 1
        try:
            verify(previous, public_key_from_private(key))
            same_archival_key = True
        except ValueError:
            same_archival_key = False
        if approved is None and same_archival_key and previous["signed"]["expires_at"] > now + renew_before_days * 86400:
            return {**publish_catalog(store, source["tuf"], tuf_keys), "revision": revision - 1}
    else:
        if not initialize or approved is None:
            raise ValueError("Initialize TUF registry explicitly with approved configuration")
        registry, revision = approved, source.get("minimum_revision", 1)
    registry = registry if approved is None else approved
    for _ in range(256):
        if store.reserved(f"revisions/{revision}.json") is None:
            break
        revision += 1
    else:
        raise ValueError("Too many orphaned registry records")
    with tempfile.TemporaryDirectory(prefix="tuf-registry-") as tmp:
        path = publish_registry(registry, source, key, revision, tmp, valid_days=valid_days,
                                offline_days=offline_days, now=now, update_tuf=False)
        record = json.loads(path.read_text())
        files = {f"revisions/{revision}.json": json_bytes(record)}
        store.publish_immutable(files)
        store.verify_immutable(files)
        result = publish_catalog(store, source["tuf"], tuf_keys, record, initialize=initialize)
    return {**result, "revision": revision}


@contextmanager
def tuf_secrets():
    with ExitStack() as stack:
        yield {role: [stack.enter_context(secret_key("TUF_" + role.upper() + "_KEY"))]
               for role in ("targets", "snapshot", "timestamp")}


def contained(root, relative):
    root = Path(root).resolve()
    result = (root / str(safe_path(relative))).resolve()
    if not result.is_relative_to(root):
        raise ValueError("Input escapes its checkout or artifact directory")
    return result


@contextmanager
def secret_key(variable):
    # Remove the PEM from the environment before running any child processes.
    pem = os.environ.pop(variable, "")
    if not pem.strip():
        raise ValueError(f"Missing Actions secret {variable}")
    with tempfile.TemporaryDirectory(prefix="ci-signing-") as tmp:
        path = Path(tmp) / "signing.pem"
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        # Preserve the supplied PEM bytes instead of translating LF to CRLF on Windows.
        with os.fdopen(fd, "wb") as stream:
            stream.write(pem.encode("ascii"))
        try:
            yield path
        finally:
            path.unlink(missing_ok=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("publisher", "publisher-renew", "registry", "bundle-prepare", "bundle-publish", "identity"))
    parser.add_argument("--root", type=Path, default=Path("."))
    parser.add_argument("--config", required=True)
    parser.add_argument("--artifact", type=Path)
    parser.add_argument("--spec", default="spec.json")
    parser.add_argument("--ifw", type=Path)
    parser.add_argument("--mode", choices=("renew", "apply"), default="renew")
    parser.add_argument("--initialize", action="store_true")
    parser.add_argument("--output", type=Path, default=Path("build/publication.json"))
    parser.add_argument("--cache", type=Path, default=Path("build/cache"))
    parser.add_argument("--candidate", type=Path, default=Path("build/candidate"))
    parser.add_argument("--acceptance", type=Path)
    args = parser.parse_args()
    config = json.loads(contained(args.root, args.config).read_text())
    if args.command == "identity":
        print(publication_key(config["storage"]))
        return
    source = json.loads(contained(args.root, config["source"]).read_text())
    validate_source(source)
    if "tuf" not in source:
        raise ValueError("CI requires a TUF registry bootstrap; legacy fixtures cannot be published")
    storage = config["storage"]
    public_url = storage["public_url"].rstrip("/")
    if (not public_url.startswith("https://") or not source["url"].startswith("https://")
            or not source["tuf"]["url"].startswith("https://")):
        raise ValueError("CI publication requires public HTTPS URLs")
    if args.command == "bundle-prepare":
        from .bundle_publication import prepare_bundle
        with secret_key("BUNDLE_SIGNING_KEY") as key:
            plan = prepare_bundle(source, storage_from_config(storage), key, args.cache,
                                  args.candidate, public_url, initialize=args.initialize,
                                  destination_key=publication_key(storage))
        result = {"status": "prepared", "candidate": str(args.candidate),
                  "sha256": plan["current"]["publication"]["signed"]["sha256"]}
    elif args.command == "bundle-publish":
        from .bundle_publication import publish_candidate
        if args.acceptance is None:
            parser.error("bundle-publish requires --acceptance from the native validation job")
        result = publish_candidate(source, storage_from_config(storage), args.candidate,
                                   json.loads(args.acceptance.read_text()), public_url,
                                   destination_key=publication_key(storage))
    elif args.command in ("publisher", "publisher-renew"):
        if args.command == "publisher" and (args.artifact is None or args.ifw is None):
            parser.error("publisher requires --artifact and --ifw")
        # Release publication needs a fresh authenticated registry, never stale fallback.
        view = resolve(source, args.cache)
        if view.status != "verified-online":
            raise ValueError("Publication requires a fresh TUF registry")
        registry = view.registry
        publisher = config["publisher"]
        if public_url != registry["repositories"][publisher]["url"].rstrip("/"):
            raise ValueError("Storage public URL differs from the authorized repository")
        if registry["repositories"][publisher]["tuf"]["url"].rstrip("/") != public_url + "/tuf":
            raise ValueError("Publisher TUF URL differs from publication storage")
        with tuf_secrets() as keys:
            store = storage_from_config(storage)
            if args.command == "publisher-renew":
                from .tuf_repository import origin_state, publish_catalog
                trust = registry["repositories"][publisher]["tuf"]
                _, _, _, current = origin_state(store, trust)
                catalog_releases(current, registry, publisher)
                result = publish_catalog(store, trust, keys)
            else:
                spec = contained(args.artifact, args.spec)
                for package in json.loads(spec.read_text())["packages"]:
                    contained(spec.parent, package["payload"])
                with secret_key("PUBLISHER_SIGNING_KEY") as key:
                    result = tuf_publisher_release(spec, registry, publisher, store, key, args.ifw, keys,
                                                   initialize=args.initialize)
    else:
        if public_url + "/registry.json" != source["url"]:
            raise ValueError("Storage public URL differs from the bootstrap feed")
        if source["tuf"]["url"].rstrip("/") != public_url + "/tuf":
            raise ValueError("Registry TUF URL differs from publication storage")
        if args.initialize and args.mode != "apply":
            parser.error("registry initialization requires --mode apply")
        approved = (json.loads(contained(args.root, config["approved_registry"]).read_text())
                    if args.mode == "apply" else None)
        with secret_key("REGISTRY_SIGNING_KEY") as key, tuf_secrets() as keys:
            result = tuf_registry_release(source, storage_from_config(storage), key, keys, approved=approved,
                                      initialize=args.initialize,
                                      **{k: config[k] for k in ("valid_days", "offline_days", "renew_before_days") if k in config})
    atomic_json(args.output, result)
    print(json.dumps({k: result[k] for k in ("status", "revision") if k in result}))


if __name__ == "__main__":
    main()
