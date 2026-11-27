"""Authenticated registry refresh and archival offline registry authorization.

TUF authenticates online catalogs. The separate authority signature authorizes
archival registry snapshots under an explicit offline policy.
"""
from dataclasses import dataclass
from contextlib import contextmanager
import hashlib
import json
import os
from pathlib import Path
import time
from urllib.error import URLError

from .common import atomic_json, canonical, check_url, public_key_from_private, read_json_url, sign, verify
from .archival import INDEFINITE_POLICY, validate_policy
from .registry import validate_registry
from .repository import publication_lock


@dataclass
class RegistryView:
    registry: dict
    record: dict
    status: str
    warning: str | None = None
    tuf_evidence: dict | None = None

    def summary(self):
        signed = self.record.get("signed", {})
        return {"status": self.status, "registry_id": signed.get("registry_id"),
                "revision": signed.get("revision"), "sha256": record_digest(self.record),
                "warning": self.warning}


def record_digest(record):
    return hashlib.sha256(canonical(record)).hexdigest()


def local_record(registry):
    return {"kind": "local-registry", "schema": 1, "registry": registry}


def record_registry(record):
    if "signed" in record:
        return record["signed"]["registry"]
    if record.get("kind") != "local-registry" or record.get("schema") != 1:
        raise ValueError("Unsupported pinned registry record")
    return record["registry"]


def registry_identity(record):
    signed = record.get("signed", {})
    return {"registry_id": signed.get("registry_id"), "revision": signed.get("revision"),
            "sha256": record_digest(record)}


def validate_source(source):
    if source.get("kind") != "registry-source" or source.get("schema") != 1:
        raise ValueError("Unsupported registry bootstrap")
    if not isinstance(source.get("registry_id"), str) or not source["registry_id"]:
        raise ValueError("Missing registry identity")
    check_url(source["url"])
    if type(source.get("minimum_revision", 1)) is not int or source.get("minimum_revision", 1) < 1:
        raise ValueError("Invalid registry revision floor")
    for field in ("public_key", "season", "channel", "target"):
        if not isinstance(source.get(field), str) or not source[field]:
            raise ValueError(f"Missing registry bootstrap {field}")
    if "tuf" in source:
        from .tuf_repository import validate_trust
        validate_trust(source["tuf"])
        validate_policy(source.get("archive_policy", {}))


def verified_record(record, source, *, mode="online", now=None):
    """Verify the pinned archival signature (also used by legacy local feeds)."""
    return _record(record, source, mode=mode, now=now, archival=True)


def validated_tuf_record(record, source, *, historical=False, now=None):
    """Validate a record ALREADY authenticated by TUF, never a raw remote JSON.

    Only fetch_catalog, verified cache evidence, or authenticated publication
    origin state may supply this input. Archival keys/policy are not online pins.
    """
    if "tuf" not in source:
        raise ValueError("Online record requires TUF authentication")
    return _record(record, source, mode="historical" if historical else "online", now=now, archival=False)


def _record(record, source, *, mode, now, archival):
    validate_source(source)
    if mode not in ("online", "offline", "historical"):
        raise ValueError("Unknown registry verification mode")
    if set(record) != {"signed", "signature"}:
        raise ValueError("Invalid manifest signature envelope")
    signed = verify(record, source["public_key"]) if archival else record["signed"]
    if signed.get("kind") != "registry" or signed.get("schema") != 1:
        raise ValueError("Unsupported signed registry")
    if signed.get("registry_id") != source["registry_id"]:
        raise ValueError("Registry identity mismatch")
    floor = 1 if mode == "historical" else source.get("minimum_revision", 1)
    if type(signed.get("revision")) is not int or signed["revision"] < floor:
        raise ValueError("Registry revision below bootstrap floor")
    registry = signed["registry"]
    validate_registry(registry)
    for field in ("target", "season", "channel"):
        if registry[field] != source[field]:
            raise ValueError(f"Registry {field} mismatch")
    if not archival:
        issued, expires = signed.get("issued_at"), signed.get("expires_at")
        if (type(issued) is not int or issued < 1 or type(expires) is not int or expires <= issued
                or registry.get("online_trust") != "tuf"):
            raise ValueError("Invalid online registry authorization")
        if mode != "historical":
            now = time.time() if now is None else now
            if issued > now:
                raise ValueError("Online registry authorization is not yet valid")
            if expires <= now:
                raise ValueError("Registry online authorization expired")
        return registry
    indefinite = "tuf" in source and source["archive_policy"] == INDEFINITE_POLICY
    for field in ("expires_at",) if indefinite else ("expires_at", "offline_expires_at"):
        if type(signed.get(field)) is not int or signed[field] < 1:
            raise ValueError(f"Invalid registry {field}")
    if indefinite:
        if "offline_expires_at" not in signed or signed["offline_expires_at"] is not None:
            raise ValueError("Indefinite archival authorization must explicitly have no expiry")
    elif signed["offline_expires_at"] < signed["expires_at"]:
        raise ValueError("Offline authorization cannot expire before online authorization")
    if "tuf" in source:
        issued = signed.get("issued_at")
        if (type(issued) is not int or issued < 1
                or signed["expires_at"] <= issued
                or (not indefinite and signed["offline_expires_at"] > issued + source["archive_policy"]["max_days"] * 86400)
                or registry.get("archive_policy") != source["archive_policy"]
                or registry.get("online_trust") != "tuf"):
            raise ValueError("Registry violates the bootstrap archival policy")
        if (mode == "online" or (mode == "offline" and not indefinite)) and issued > (time.time() if now is None else now):
            raise ValueError("Registry archival authorization is not yet valid")
    if mode == "online" or (mode == "offline" and not indefinite):
        expiry = signed["offline_expires_at"] if mode == "offline" else signed["expires_at"]
        if expiry <= (time.time() if now is None else now):
            raise ValueError(f"Registry {mode} authorization expired")
    return registry


def cache_directory(source, cache):
    # Moving the endpoint or increasing a bootstrap floor must not reset history.
    fields = ("registry_id", "target", "season", "channel")
    identity = {key: source[key] for key in fields}
    if "tuf" in source:
        identity["tuf"] = {key: source["tuf"][key] for key in ("repository_id", "root")}
    else:
        identity["public_key"] = source["public_key"]
    return Path(cache) / "registries" / record_digest(identity)


@contextmanager
def cache_lock(directory):
    """OS-owned lock releases on process exit, including a crash during refresh."""
    directory.mkdir(parents=True, exist_ok=True)
    with (directory / ".refresh-lock").open("a+b") as stream:
        if stream.seek(0, 2) == 0:
            stream.write(b"\0")
            stream.flush()
        stream.seek(0)
        try:
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            raise ValueError("Another registry refresh is in progress; retry shortly") from exc
        try:
            yield
        finally:
            stream.seek(0)
            if os.name == "nt":
                msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(stream.fileno(), fcntl.LOCK_UN)


def refresh(source, cache, *, now=None):
    validate_source(source)
    if source["url"].startswith("https://") and "tuf" not in source:
        raise ValueError("Online HTTPS registry bootstrap requires TUF; legacy mode is for local fixtures")
    directory = cache_directory(source, cache)
    with cache_lock(directory):
        path = directory / "current.json"
        saved = json.loads(path.read_text()) if path.exists() else None
        previous = saved
        if "tuf" in source:
            from .tuf_repository import fetch_catalog, verify_catalog_history
            if saved is not None:
                if saved.get("kind") != "tuf-registry-cache" or saved.get("schema") != 1:
                    raise ValueError("Invalid TUF registry cache")
                previous = saved["record"]
                verify_catalog_history(source["tuf"], cache, previous, saved["proof"])
                validated_tuf_record(previous, source, historical=True, now=now)
            else:
                # One-time upgrade of the old key-namespaced cache: authenticate
                # its historical floor with the OLD pin, never use it online.
                legacy_source = {k: v for k, v in source.items() if k != "tuf"}
                legacy = cache_directory(legacy_source, cache) / "current.json"
                if legacy.exists():
                    previous = json.loads(legacy.read_text())
                    verified_record(previous, source, mode="historical", now=now)
        elif previous is not None:
            # Expired metadata still establishes the previously observed revision.
            # It is never used as an online view without a separate expiry check.
            verified_record(previous, source, mode="historical", now=now)
        try:
            evidence = None
            status = "verified-online"
            if "tuf" in source:
                candidate, status, evidence = fetch_catalog(source["tuf"], cache, fallback=True, with_proof=True)
            else:
                candidate = read_json_url(source["url"])
        except (URLError, TimeoutError, OSError) as exc:
            if "tuf" in source:
                raise  # TUF fallback must itself pass the standard TUF client.
            if previous is None:
                raise ValueError("Registry unavailable and no verified cache exists") from exc
            registry = verified_record(previous, source, now=now)
            return RegistryView(registry, previous, "verified-cache",
                                "Registry refresh unavailable; using an unexpired verified cache")
        registry = (validated_tuf_record(candidate, source, now=now) if "tuf" in source
                    else verified_record(candidate, source, now=now))
        if previous is not None:
            revision = candidate["signed"]["revision"]
            old_revision = previous["signed"]["revision"]
            if revision < old_revision:
                raise ValueError("Registry rollback rejected")
            if revision == old_revision and candidate != previous:
                raise ValueError("Registry changed without increasing its revision")
        if "tuf" in source:
            proof = evidence.pop("proof")
            atomic_json(path, {"kind": "tuf-registry-cache", "schema": 1, "record": candidate, "proof": proof})
        else:
            atomic_json(path, candidate)
        warning = "TUF transport unavailable; using unexpired verified cache" if status == "verified-cache" else None
        return RegistryView(registry, candidate, status, warning, evidence)


def pinned_record(bundle):
    root = Path(bundle)
    manifest = json.loads((root / "bundle-manifest.json").read_text())["signed"]
    if manifest.get("schema") not in (2, 3):
        raise ValueError("Bundle has no pinned registry; use its original local registry for legacy media")
    record = json.loads((root / "registry.json").read_text())
    if registry_identity(record) != manifest.get("registry"):
        raise ValueError("Bundle registry identity/hash mismatch")
    return record


def resolve(config, cache, *, bundle=None, now=None):
    """Resolve before repository access; offline resolution performs no fetches."""
    if config.get("kind") != "registry-source":
        validate_registry(config)
        return RegistryView(config, local_record(config), "local")
    if bundle is None:
        return refresh(config, cache, now=now)
    record = pinned_record(bundle)
    registry = verified_record(record, config, mode="offline", now=now)
    # Authenticate the association, not just the registry and bundle independently.
    manifest = verify(json.loads((Path(bundle) / "bundle-manifest.json").read_text()), registry["bundle_public_key"])
    if manifest.get("registry") != registry_identity(record):
        raise ValueError("Bundle registry identity/hash mismatch")
    # Intentionally do not lower or replace the online cache with archival media.
    return RegistryView(registry, record, "verified-bundle")


def publish_registry(registry, source, key, revision, destination, *, valid_days=7, offline_days=None, now=None,
                     tuf_keys=None, update_tuf=True):
    validate_source(source)
    indefinite = "tuf" in source and source["archive_policy"] == INDEFINITE_POLICY
    if indefinite and offline_days is not None:
        raise ValueError("offline_days conflicts with the indefinite archival policy; remove this setting")
    offline_days = 365 if offline_days is None and not indefinite else offline_days
    if (type(revision) is not int or revision < 1 or type(valid_days) is not int or valid_days < 1
            or (not indefinite and (type(offline_days) is not int or valid_days > offline_days))):
        raise ValueError("Invalid revision or registry validity intervals")
    now = int(time.time() if now is None else now)
    record = sign({"kind": "registry", "schema": 1, "registry_id": source["registry_id"],
                   "revision": revision, "issued_at": now, "expires_at": now + valid_days * 86400,
                   "offline_expires_at": None if indefinite else now + offline_days * 86400, "registry": registry}, key)
    # A newly selected archival signing key does not need the old offline pin's
    # consent. TUF authorizes its online publication; new offline media needs a
    # matching independently trusted bootstrap.
    signing_source = {**source, "public_key": public_key_from_private(key)} if "tuf" in source else source
    verified_record(record, signing_source, now=now)
    destination = Path(destination)
    with publication_lock(destination):
        latest = destination / "registry.json"
        if latest.exists():
            previous = json.loads(latest.read_text())
            if "tuf" in source:
                from .tuf_repository import DirectoryStore, origin_state
                authenticated = origin_state(DirectoryStore(destination), source["tuf"])[3]
                if previous != authenticated:
                    raise ValueError("Local registry differs from its authenticated TUF origin")
                validated_tuf_record(previous, source, historical=True, now=now)
            else:
                verified_record(previous, source, mode="historical", now=now)
            if revision <= previous["signed"]["revision"]:
                raise ValueError("Registry publication revisions must increase")
        immutable = destination / "revisions" / f"{revision}.json"
        if immutable.exists():
            raise ValueError("Immutable registry revision already exists")
        atomic_json(immutable, record)
        atomic_json(latest, record)
        if update_tuf and "tuf" in source:
            from .tuf_repository import DirectoryStore, ROLES, publish_catalog
            keys = tuf_keys or {role: [Path(key).parent / "registry-tuf" / (role + ".pem")] for role in ROLES}
            publish_catalog(DirectoryStore(destination), source["tuf"], keys, record,
                            initialize=revision == source.get("minimum_revision", 1))
    return latest
