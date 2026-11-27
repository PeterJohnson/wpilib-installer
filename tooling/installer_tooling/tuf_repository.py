"""Independent TUF repositories: python-tuf clients and timestamp-last publication.

Each repository has one TUF target, catalog.json. Its authenticated release
manifests bind the hashes/lengths of the existing immutable Qt archive paths.
"""
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
import base64
import hashlib
import json
from pathlib import Path
import re
import shutil
from urllib.error import HTTPError, URLError

from cryptography.hazmat.primitives.serialization import load_pem_private_key
from securesystemslib.signer import CryptoSigner
from tuf.api import exceptions
from tuf.api.metadata import Metadata, Root, Targets, Snapshot, Timestamp, TargetFile, MetaFile
from tuf.api.serialization.json import JSONSerializer
from tuf.ngclient import Updater, UpdaterConfig
from tuf.ngclient.fetcher import FetcherInterface

from .common import atomic_json, canonical, check_url, keygen, resource_url, urlopen

SERIALIZER = JSONSerializer(compact=True)
ROLES = ("targets", "snapshot", "timestamp")
MAX_CATALOG = 8 * 1024 * 1024
ROOT_BATCH_SIZE = 256
ROOT_BATCHES_PER_REFRESH = 4


class RootUpdatePending(exceptions.DownloadError):
    """Verified root progress is saved, but no catalog is authorized yet."""


class RootBatchFetcher(FetcherInterface):
    """Bound remote work without treating a batch boundary as the latest root."""
    def __init__(self, fetcher):
        self.fetcher = fetcher
        self.downloads = 0
        self.complete = False

    def _fetch(self, url):
        if re.fullmatch(r"[1-9][0-9]*\.root\.json", url.rsplit("/", 1)[-1]):
            if self.downloads >= ROOT_BATCH_SIZE:
                raise RootUpdatePending("TUF root catch-up is incomplete; verified progress was saved. Retry the operation.")
            self.downloads += 1
            try:
                yield from self.fetcher.fetch(url)
            except exceptions.DownloadHTTPError as exc:
                if exc.status_code in (403, 404):
                    self.complete = True
                raise
        else:
            if not self.complete:
                raise RootUpdatePending("TUF root catch-up has not reached the end of the chain; retry the operation.")
            yield from self.fetcher.fetch(url)


def encoded(metadata):
    return metadata.to_bytes(SERIALIZER)


def signer(path):
    return CryptoSigner(load_pem_private_key(Path(path).read_bytes(), None))


def authorized(root, role, metadata):
    root.signed.verify_delegate(role, metadata.signed_bytes, metadata.signatures)


def create_root(keys, *, version=1, previous=None, days=730, root_threshold=1):
    """Administrative operation: keys maps roles to lists of private PEM paths.

For rotation include old root signers as well as the new role's signers under
old_root. All other role lists describe the replacement trust configuration.
"""
    if type(root_threshold) is not int or root_threshold < 1 or type(days) is not int or days < 1:
        raise ValueError("Root threshold and lifetime must be positive integers")
    root = Metadata(Root(version=version, expires=datetime.now(timezone.utc) + timedelta(days=days)))
    for role in ("root", *ROLES):
        for key in keys[role]:
            root.signed.add_key(signer(key).public_key, role)
    root.signed.roles["root"].threshold = root_threshold
    for key in [*keys["root"], *keys.get("old_root", [])]:
        root.sign(signer(key), append=True)
    authorized(root, "root", root)
    if previous is not None:
        previous = Metadata.from_bytes(canonical(previous))
        if version != previous.signed.version + 1:
            raise ValueError("Root rotations must advance exactly one version")
        authorized(previous, "root", root)
    elif version != 1:
        raise ValueError("Initial root must be version 1")
    return root.to_dict()


def demo_trust(directory, repository_id, base_url):
    directory = Path(directory)
    keys = {}
    for role in ("root", *ROLES):
        path = directory / (role + ".pem")
        keygen(path)
        keys[role] = [path]
    return {"repository_id": repository_id, "url": base_url.rstrip("/"), "root": create_root(keys)}


def validate_trust(trust):
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,160}", trust.get("repository_id", "")):
        raise ValueError("Invalid TUF repository identity")
    check_url(trust["url"])
    root = Metadata.from_bytes(canonical(trust["root"]))
    if not isinstance(root.signed, Root) or not root.signed.consistent_snapshot:
        raise ValueError("Expected TUF root with consistent snapshots")
    authorized(root, "root", root)
    return root


def cache_directory(trust, cache):
    identity = {"id": trust["repository_id"], "root": trust["root"]}
    return Path(cache) / "tuf" / hashlib.sha256(canonical(identity)).hexdigest()


def next_root(root, data):
    if len(data) > UpdaterConfig().root_max_length:
        raise ValueError("Oversized TUF root")
    new = Metadata.from_bytes(data)
    if not isinstance(new.signed, Root) or new.signed.version != root.signed.version + 1:
        raise ValueError("Invalid TUF root chain")
    authorized(root, "root", new)
    authorized(new, "root", new)
    return new


class Fetcher(FetcherInterface):
    def _fetch(self, url):
        check_url(url)
        try:
            with urlopen(url, timeout=60) as response:
                check_url(response.geturl())
                while chunk := response.read(64 * 1024):
                    yield chunk
        except HTTPError as exc:
            status = exc.code
            exc.close()
            raise exceptions.DownloadHTTPError("TUF HTTP request failed", status) from exc
        except URLError as exc:
            if isinstance(exc.reason, FileNotFoundError):
                raise exceptions.DownloadHTTPError("TUF file not found", 404) from exc
            raise exceptions.DownloadError("TUF transport unavailable") from exc
        except OSError as exc:
            raise exceptions.DownloadError("TUF transport unavailable") from exc


class CachedMetadataFetcher(FetcherInterface):
    """Reverify cached metadata with the normal current-clock TUF updater."""
    def __init__(self, directory):
        self.directory = directory

    def _fetch(self, url):
        name = url.rsplit("/", 1)[-1]
        if name.endswith(".root.json"):
            path = self.directory / "root_history" / name
        elif name == "timestamp.json":
            path = self.directory / name
        elif re.fullmatch(r"[1-9][0-9]*\.(targets|snapshot)\.json", name):
            path = self.directory / name.split(".", 1)[1]
        else:
            raise exceptions.DownloadError("No cached TUF download available")
        if not path.exists():
            raise exceptions.DownloadHTTPError("No cached metadata", 404)
        yield path.read_bytes()


@contextmanager
def client(trust, cache, *, fallback=False):
    from .registry_updates import cache_lock
    root = validate_trust(trust)
    # A root is rotated through its chain, not by editing this bootstrap identity.
    directory = cache_directory(trust, cache)
    with cache_lock(directory):
        metadata, targets = directory / "metadata", directory / "targets"
        metadata.mkdir(exist_ok=True)
        targets.mkdir(exist_ok=True)

        def refresh(fetcher):
            for _ in range(ROOT_BATCHES_PER_REFRESH):
                # File names only size the replay budget; Updater verifies every
                # byte/signature from the ORIGINAL bootstrap on every attempt.
                cached = 0
                while (metadata / "root_history" / f"{root.signed.version + cached + 1}.root.json").is_file():
                    cached += 1
                instance = Updater(str(metadata), resource_url(trust["url"], "metadata") + "/",
                    str(targets), resource_url(trust["url"], "targets") + "/", fetcher=RootBatchFetcher(fetcher),
                    config=UpdaterConfig(targets_max_length=MAX_CATALOG,
                        max_root_rotations=cached + ROOT_BATCH_SIZE + 1), bootstrap=encoded(root))
                try:
                    instance.refresh()
                    return instance
                except RootUpdatePending:
                    continue
            raise RootUpdatePending("TUF root catch-up is incomplete; verified progress was saved. Retry the operation.")

        status = "verified-online"
        try:
            instance = refresh(Fetcher())
        except exceptions.DownloadError as exc:
            # Corruption, expiry, signatures, rollback and oversized responses
            # are not connectivity failures and must never trigger fallback.
            transport = (type(exc) is exceptions.DownloadError or
                         isinstance(exc, exceptions.DownloadHTTPError) and exc.status_code >= 500)
            if not fallback or not transport:
                raise
            instance, status = refresh(CachedMetadataFetcher(metadata)), "verified-cache"
        yield instance, status, metadata


def fetch_catalog(trust, cache, *, fallback=False, with_proof=False):
    with client(trust, cache, fallback=fallback) as (updater, status, directory):
        info = updater.get_targetinfo("catalog.json")
        if info is None or info.length > MAX_CATALOG:
            raise ValueError("Missing or oversized TUF catalog")
        path = updater.find_cached_target(info)
        if path is None:
            if status != "verified-online":
                raise ValueError("Verified TUF catalog is not cached")
            path = updater.download_target(info)
        evidence = {"repository_id": trust["repository_id"], "catalog_sha256": info.hashes["sha256"],
                    **{role + "_version": Metadata.from_file(str(directory / (role + ".json"))).signed.version
                       for role in ("root", *ROLES)}}
        data = Path(path).read_bytes()
        if with_proof:
            evidence["proof"] = {"root_version": evidence["root_version"],
                "catalog": base64.b64encode(data).decode(), **{role: base64.b64encode(
                    (directory / (role + ".json")).read_bytes()).decode() for role in ROLES}}
        return json.loads(data), status, evidence


def verify_catalog_history(trust, cache, record, proof):
    """Authenticate a cached revision floor, NEVER authorize an online plan.

    Expiry is deliberately irrelevant to historical rollback evidence. Every
    usable online/cache-fallback catalog still goes through Updater.refresh().
    """
    root = validate_trust(trust)
    version = proof.get("root_version")
    if type(version) is not int or version < root.signed.version:
        raise ValueError("Invalid historical TUF root version")
    directory = cache_directory(trust, cache) / "metadata/root_history"
    while root.signed.version < version:
        with (directory / f"{root.signed.version + 1}.root.json").open("rb") as stream:
            root = next_root(root, stream.read(UpdaterConfig().root_max_length + 1))

    def decode(name, limit):
        value = proof[name]
        if not isinstance(value, str) or len(value) > 4 * ((limit + 2) // 3):
            raise ValueError("Oversized historical TUF evidence")
        data = base64.b64decode(value, validate=True)
        if len(data) > limit:
            raise ValueError("Oversized historical TUF evidence")
        return data

    config = UpdaterConfig(targets_max_length=MAX_CATALOG)
    raw = {role: decode(role, getattr(config, role + "_max_length")) for role in ROLES}
    metadata = {role: Metadata.from_bytes(data) for role, data in raw.items()}
    for role, cls in (("timestamp", Timestamp), ("snapshot", Snapshot), ("targets", Targets)):
        if not isinstance(metadata[role].signed, cls):
            raise ValueError("Wrong historical TUF metadata type")
        authorized(root, role, metadata[role])
    for child, info in (("snapshot", metadata["timestamp"].signed.snapshot_meta),
                        ("targets", metadata["snapshot"].signed.meta["targets.json"])):
        info.verify_length_and_hashes(raw[child])
        if info.version != metadata[child].signed.version:
            raise ValueError("Historical TUF metadata version mismatch")
    catalog = decode("catalog", MAX_CATALOG)
    metadata["targets"].signed.targets["catalog.json"].verify_length_and_hashes(catalog)
    if json.loads(catalog) != record:
        raise ValueError("Cached registry differs from its TUF evidence")


class DirectoryStore:
    """Local fixture backend with the same immutable / conditional-head contract."""
    def __init__(self, root):
        self.root = Path(root)

    def get(self, name):
        from .publication_storage import Object
        path = self.root / name
        return Object(path.read_bytes(), hashlib.sha256(path.read_bytes()).hexdigest()) if path.exists() else None

    reserved = get

    def publish_immutable(self, files):
        from .publication_storage import PublicationConflict, content_info
        for name, data in files.items():
            path = self.root / name
            if path.exists():
                if content_info(path) != content_info(data):
                    raise PublicationConflict("Immutable TUF object differs")
                continue
            path.parent.mkdir(parents=True, exist_ok=True)
            if isinstance(data, Path):
                shutil.copyfile(data, path)
            else:
                path.write_bytes(data)

    def verify_immutable(self, files):
        from .publication_storage import content_info
        for name, data in files.items():
            if content_info(self.root / name) != content_info(data):
                raise ValueError("Corrupt TUF publication")

    def put(self, name, data, *, previous=None):
        from .publication_storage import PublicationConflict
        current = self.get(name)
        if (current.etag if current else None) != previous:
            raise PublicationConflict("TUF head changed")
        atomic_json(self.root / name, json.loads(data))


def origin_state(store, trust):
    """Verify origin signatures/chains even when expired, for renewal only.

This is a publisher operation. Clients always use Updater with current time.
"""
    root = validate_trust(trust)
    while True:
        item = store.get(f"tuf/metadata/{root.signed.version + 1}.root.json")
        if item is None:
            break
        root = next_root(root, item.data)
    if root.signed.is_expired():
        raise ValueError("TUF root expired; rotate root through the administrative process")
    head = store.get("tuf/metadata/timestamp.json")
    if head is None:
        return root, None, None, None
    timestamp = Metadata.from_bytes(head.data)
    authorized(root, "timestamp", timestamp)
    snap_info = timestamp.signed.snapshot_meta
    snap_bytes = store.get(f"tuf/metadata/{snap_info.version}.snapshot.json")
    if snap_bytes is None:
        raise ValueError("Missing origin snapshot")
    snap_info.verify_length_and_hashes(snap_bytes.data)
    snapshot = Metadata.from_bytes(snap_bytes.data)
    authorized(root, "snapshot", snapshot)
    target_info = snapshot.signed.meta["targets.json"]
    targets_bytes = store.get(f"tuf/metadata/{target_info.version}.targets.json")
    if targets_bytes is None:
        raise ValueError("Missing origin targets")
    target_info.verify_length_and_hashes(targets_bytes.data)
    targets = Metadata.from_bytes(targets_bytes.data)
    authorized(root, "targets", targets)
    if snapshot.signed.version != snap_info.version or targets.signed.version != target_info.version:
        raise ValueError("Origin metadata versions disagree")
    info = targets.signed.targets["catalog.json"]
    if info.length > MAX_CATALOG:
        raise ValueError("Oversized origin catalog")
    catalog = store.get(f"tuf/targets/{info.hashes['sha256']}.catalog.json")
    if catalog is None:
        raise ValueError("Missing origin catalog")
    info.verify_length_and_hashes(catalog.data)
    return root, head, (timestamp, snapshot, targets), json.loads(catalog.data)


def publish_catalog(store, trust, keys, catalog=None, *, initialize=False, now=None,
                    timestamp_days=2, snapshot_days=7, targets_days=30):
    """Publish a new catalog or renew the current one, with timestamp as sole head."""
    root, head, previous, current = origin_state(store, trust)
    if head is None and (not initialize or catalog is None):
        raise ValueError("Initialize TUF explicitly with a catalog")
    if catalog is None:
        catalog = current
    data = canonical(catalog)
    if len(data) > MAX_CATALOG:
        raise ValueError("TUF catalog exceeds 8 MiB")
    if not 0 < timestamp_days <= snapshot_days <= targets_days <= 365:
        raise ValueError("Invalid TUF role lifetimes")
    now = datetime.now(timezone.utc) if now is None else now
    revision = max((m.signed.version for m in previous or ()), default=0) + 1
    for _ in range(256):
        if all(store.reserved(f"tuf/metadata/{revision}.{role}.json") is None for role in ROLES):
            break
        revision += 1
    else:
        raise ValueError("Too many orphaned TUF revisions")

    def signed(role, value):
        metadata = Metadata(value)
        for path in keys[role]:
            metadata.sign(signer(path), append=True)
        authorized(root, role, metadata)
        return encoded(metadata)

    def expiry(days):
        return min(now + timedelta(days=days), root.signed.expires)

    info = TargetFile.from_data("catalog.json", data)
    targets = signed("targets", Targets(version=revision, expires=expiry(targets_days), targets={"catalog.json": info}))
    snapshot = signed("snapshot", Snapshot(version=revision, expires=expiry(snapshot_days),
                       meta={"targets.json": MetaFile.from_data(revision, targets, ["sha256"])}))
    timestamp = signed("timestamp", Timestamp(version=revision, expires=expiry(timestamp_days),
                        snapshot_meta=MetaFile.from_data(revision, snapshot, ["sha256"])))
    root_path = f"tuf/metadata/{root.signed.version}.root.json"
    existing_root = store.get(root_path)
    if existing_root is not None and Metadata.from_bytes(existing_root.data).to_dict() != root.to_dict():
        raise ValueError("Published bootstrap root differs from configured trust")
    files = {root_path: existing_root.data if existing_root is not None else encoded(root),
             f"tuf/targets/{info.hashes['sha256']}.catalog.json": data,
             f"tuf/metadata/{revision}.targets.json": targets,
             f"tuf/metadata/{revision}.snapshot.json": snapshot,
             f"tuf/metadata/{revision}.timestamp.json": timestamp}
    store.publish_immutable(files)
    store.verify_immutable(files)
    try:
        store.put("tuf/metadata/timestamp.json", timestamp, previous=head.etag if head else None)
    except Exception:
        committed = store.get("tuf/metadata/timestamp.json")
        if committed is None or Metadata.from_bytes(committed.data).to_dict() != Metadata.from_bytes(timestamp).to_dict():
            raise
    return {"status": "published", "tuf_revision": revision, "catalog_sha256": info.hashes["sha256"]}
