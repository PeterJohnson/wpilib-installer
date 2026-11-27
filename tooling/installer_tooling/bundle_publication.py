"""Publish complete offline downloads with one final, static HTML commit point."""
from datetime import datetime, timezone
import hashlib
import html
import json
from pathlib import Path
import re
import stat
import shutil
import zipfile
from urllib.request import Request

from .bundle import assemble
from .common import atomic_json, cached_file, canonical, check_url, digest, resource_url, safe_path, sign, urlopen, verify
from .registry_updates import RegistryView, registry_identity, resolve, verified_record
from .repository import compatible, load_snapshot

MARKER = b'<script id="bundle-publication" type="application/json">'
END_MARKER = b'</script>'


def archive_metadata(path, view):
    """Verify archive members directly, without extracting or trusting ZIP paths."""
    path = Path(path)
    root = path.stem + "/"
    with zipfile.ZipFile(path) as archive:
        names = archive.namelist()
        if len(names) != len(set(names)):
            raise ValueError("Duplicate bundle archive members")
        for name in names:
            safe_path(name)
            kind = stat.S_IFMT(archive.getinfo(name).external_attr >> 16)
            if kind not in (0, stat.S_IFREG):
                raise ValueError("Bundle archive members must be regular files")

        def metadata(relative):
            name = root + relative
            if archive.getinfo(name).file_size > 8 * 1024 * 1024:
                raise ValueError("Bundle archive metadata exceeds 8 MiB")
            return json.loads(archive.read(name))

        if metadata("registry.json") != view.record:
            raise ValueError("Archive differs from its selected signed registry")
        manifest = verify(metadata("bundle-manifest.json"), view.registry["bundle_public_key"])
        if (manifest.get("schema") not in (2, 3) or manifest["id"] != path.stem
                or manifest["registry"] != registry_identity(view.record)
                or set(manifest["repositories"]) != set(view.registry["repositories"])):
            raise ValueError("Archive identity or publisher set mismatch")
        for field in ("target", "season", "channel"):
            if manifest[field] != view.registry[field]:
                raise ValueError("Archive scope mismatch")
        from .archival import verify_archival
        verify_archival(manifest, view.registry, view.record)
        expected = {root + "registry.json", root + "bundle-manifest.json"}
        snapshots, components = [], []
        for publisher, identity in manifest["repositories"].items():
            release_name = f"repos/{publisher}/release.json"
            envelope = metadata(release_name)
            snapshot = load_snapshot(envelope, "", publisher, view.registry)
            if (snapshot.manifest["revision"] != identity["revision"]
                    or hashlib.sha256(canonical(envelope)).hexdigest() != identity["sha256"]):
                raise ValueError("Archive publisher identity mismatch")
            snapshots.append(snapshot)
            components.extend({"publisher": publisher, "id": p["id"], "version": p["version"]}
                              for p in snapshot.manifest["packages"])
            expected.add(root + release_name)
            for name, info in snapshot.manifest["files"].items():
                member = root + f"repos/{publisher}/" + name
                expected.add(member)
                if archive.getinfo(member).file_size != info["size"]:
                    raise ValueError("Bundle archive file size mismatch")
                with archive.open(member) as stream:
                    if hashlib.file_digest(stream, "sha256").hexdigest() != info["sha256"]:
                        raise ValueError("Bundle archive file digest mismatch")
        if set(names) != expected or not compatible(snapshots):
            raise ValueError("Incomplete, unexpected, or incompatible bundle archive contents")
    return manifest, sorted(components, key=lambda p: (p["publisher"], p["id"]))


def verify_record(record, source):
    registry = verified_record(record["registry_record"], source, mode="historical")
    publication = verify(record["publication"], registry["bundle_public_key"])
    if publication.get("kind") != "offline-download" or publication.get("schema") != 1:
        raise ValueError("Unsupported download publication")
    for field in ("target", "season", "channel"):
        if publication[field] != registry[field]:
            raise ValueError("Download publication scope mismatch")
    if publication["registry"] != registry_identity(record["registry_record"]):
        raise ValueError("Download registry binding mismatch")
    if (not re.fullmatch(r"[0-9a-f]{64}", publication["sha256"])
            or type(publication["size"]) is not int or publication["size"] < 1):
        raise ValueError("Invalid download digest or size")
    if not re.fullmatch(r"[A-Za-z0-9_-]+", publication["id"]):
        raise ValueError("Invalid download identity")
    if set(publication["repositories"]) != set(registry["repositories"]):
        raise ValueError("Download publisher set mismatch")
    for identity in publication["repositories"].values():
        if (type(identity["revision"]) is not int or identity["revision"] < 1
                or not re.fullmatch(r"[0-9a-f]{64}", identity["sha256"])):
            raise ValueError("Invalid download repository identity")
    return publication


def paths(publication):
    prefix = "bundles/" + publication["sha256"] + "/"
    return {"bundle": prefix + publication["id"] + ".zip", "metadata": prefix + "bundle.json",
            "page": prefix + "index.html"}


def render_page(state, *, archived=False):
    publication = state["current"]["publication"]["signed"]
    links = {k: ("../../" if archived else "") + v for k, v in paths(publication).items()}
    esc = html.escape
    rows = "".join(f"<tr><td>{esc(p['publisher'])}</td><td>{esc(p['id'])}</td><td>{esc(p['version'])}</td></tr>"
                   for p in publication["components"])
    held = publication["held_back"]
    held_html = ("<h2>Compatibility holdbacks</h2><ul>" + "".join(
        f"<li>{esc(p['publisher'])}: revision {esc(str(p['selected']))} selected; latest is {esc(str(p['latest']))}. "
        f"{esc(p['reason'])}</li>" for p in held) + "</ul>") if held else ""
    previous = ""
    if state.get("previous"):
        previous = f"<p><a href='{paths(state['previous']['publication']['signed'])['page']}'>Previous successful bundle</a></p>"
    record = state["current"]["registry_record"]["signed"]
    if record["offline_expires_at"] is None:
        from .archival import INDEFINITE_POLICY
        if record["registry"].get("archive_policy") != INDEFINITE_POLICY:
            raise ValueError("Missing indefinite archival policy")
        authorization = "offline installation has no expiration date"
    else:
        expiry = datetime.fromtimestamp(record["offline_expires_at"], timezone.utc).date()
        authorization = f"offline authorization expires {expiry} UTC"
    warning = "<p><strong>Prototype test payloads only.</strong></p>" if publication["payload_kind"] == "prototype-fixtures" else ""
    encoded = canonical(state).replace(b"<", b"\\u003c")
    body = ("<!doctype html><html lang='en'><head><meta charset='utf-8'>"
            "<meta name='viewport' content='width=device-width, initial-scale=1'>"
            "<title>WPILib offline downloads</title><style>"
            "body{font:16px/1.55 system-ui,sans-serif;color:#1f2937;background:#f6f8fa;margin:0}"
            "main{max-width:960px;margin:32px auto;padding:24px;background:white}"
            "h1{font-size:1.8rem;line-height:1.25}h2{font-size:1.25rem;margin-top:2rem}"
            "a{color:#075fbe}code,td{overflow-wrap:anywhere}table{width:100%;border-collapse:collapse;table-layout:fixed}"
            "th,td{text-align:left;vertical-align:top;border-bottom:1px solid #d8dee4;padding:8px}"
            "@media(max-width:600px){main{margin:0;padding:16px}table{font-size:13px}}"
            "</style></head><body><main>"
            f"<h1>WPILib offline packages: {esc(publication['target'])}</h1>"
            f"<p>Season {esc(publication['season'])} · {esc(publication['channel'])}</p>{warning}"
            f"<p><a href='{links['bundle']}'>Download complete package set ({publication['size']:,} bytes)</a></p>"
            "<p>Download the matching installer separately. Extract the ZIP and copy the package set and installer to your offline machine.</p>"
            f"<p>SHA-256: <code>{publication['sha256']}</code></p>"
            f"<p>Registry revision {publication['registry']['revision']}; {authorization}.</p>"
            f"<p><a href='{links['metadata']}'>Signed bundle details</a> · <a href='{links['page']}'>Permanent download page</a></p>"
            f"{previous}<h2>Included packages</h2><table><thead><tr><th>Publisher</th><th>Package</th><th>Version</th></tr></thead>"
            f"<tbody>{rows}</tbody></table>{held_html}</main>")
    # This is inert metadata; the page has no executable JavaScript or external assets.
    return body.encode() + MARKER + encoded + END_MARKER + b"</body></html>\n"


def read_page(page, source):
    if page.count(MARKER) != 1:
        raise ValueError("Existing download page is not a recognized publication")
    state = json.loads(page.split(MARKER, 1)[1].split(END_MARKER, 1)[0])
    verify_record(state["current"], source)
    if state.get("previous"):
        verify_record(state["previous"], source)
    if render_page(state) != page:
        raise ValueError("Existing download page differs from its verified metadata")
    return state


def prevent_rollback(previous, current):
    old, new = previous["publication"]["signed"], current["publication"]["signed"]
    a, b = old["registry"], new["registry"]
    if b["revision"] < a["revision"] or (a["revision"] == b["revision"] and a != b):
        raise ValueError("Bundle publication would roll back or change the registry revision")
    for name in old["repositories"].keys() & new["repositories"].keys():
        a, b = old["repositories"][name], new["repositories"][name]
        if b["revision"] < a["revision"] or (a["revision"] == b["revision"] and a != b):
            raise ValueError(f"Bundle publication would roll back or change {name}")


def verify_public_files(base, files):
    """Check anonymous download routing and HTML rendering before the live switch."""
    for name, value in files.items():
        url = resource_url(base, name)
        expected_size = value.stat().st_size if isinstance(value, Path) else len(value)
        request = Request(url, method="HEAD" if isinstance(value, Path) else "GET", headers={"Cache-Control": "no-cache"})
        with urlopen(request, timeout=120) as response:
            check_url(response.geturl())
            if isinstance(value, Path):
                if response.headers.get("Content-Length") != str(expected_size):
                    raise ValueError("Public bundle URL has a missing or incorrect Content-Length")
            else:
                if response.read(len(value) + 1) != value:
                    raise ValueError("Public metadata/page does not match the uploaded bytes")
                if name.endswith(".html") and (response.headers.get_content_type() != "text/html"
                        or response.headers.get_content_disposition() == "attachment"):
                    raise ValueError("Public host must serve HTML inline; configure Artifactory content browsing or a download frontend")


def prepare_bundle(source, store, key, cache, destination, public_url, *, initialize=False,
                   destination_key="local"):
    """Freeze a signed candidate and its previous successful bundle; no remote writes."""
    destination = Path(destination)
    if destination.exists():
        raise ValueError("Candidate directory must be new")
    head = store.get("index.html")
    previous = read_page(head.data, source) if head else None
    if head is None and not initialize:
        raise ValueError("Missing download page; initialize explicitly for first publication")
    view = resolve(source, cache)
    if view.status != "verified-online":
        raise ValueError("Bundle publication requires a fresh authenticated registry")
    verified_record(view.record, source, mode="offline")
    options = {"registry_evidence": view.tuf_evidence} if view.tuf_evidence else {}
    archive = assemble(view.registry, destination / "current", key, cache, registry_record=view.record, **options)
    manifest, components = archive_metadata(archive, view)
    publication = {"kind": "offline-download", "schema": 1,
        **{k: manifest[k] for k in ("id", "target", "season", "channel", "registry", "repositories", "held_back", "payload_kind")},
        "sha256": digest(archive), "size": archive.stat().st_size, "components": components}
    record = {"registry_record": view.record, "publication": sign(publication, key)}
    verify_record(record, source)
    if previous:
        prevent_rollback(previous["current"], record)
        baseline = previous["current"]
        old = verify_record(baseline, source)
        downloaded = cached_file(public_url, paths(old)["bundle"], old, cache)
        baseline_path = destination / "previous" / (old["id"] + ".zip")
        baseline_path.parent.mkdir()
        shutil.copyfile(downloaded, baseline_path)
        validate_archive(baseline_path, baseline, source)
    else:
        baseline = None
    plan = {"kind": "offline-candidate", "schema": 1, "current": record, "previous": baseline,
            "destination_key": destination_key, "public_url": public_url,
            "origin_sha256": hashlib.sha256(head.data).hexdigest() if head else None}
    atomic_json(destination / "candidate.json", sign(plan, key))
    # Local assembly drafts are not publication heads or inputs to acceptance.
    for name in ("index.html", "latest.json"):
        (destination / "current" / name).unlink(missing_ok=True)
    return plan


def validate_archive(archive, record, source):
    publication = verify_record(record, source)
    if archive.stat().st_size != publication["size"] or digest(archive) != publication["sha256"]:
        raise ValueError("Candidate archive hash/length mismatch")
    registry = verified_record(record["registry_record"], source, mode="offline")
    manifest, components = archive_metadata(archive, RegistryView(registry, record["registry_record"], "verified-bundle"))
    if components != publication["components"] or any(manifest[k] != publication[k] for k in
            ("id", "target", "season", "channel", "registry", "repositories", "held_back", "payload_kind")):
        raise ValueError("Candidate archive differs from its signed download record")
    return publication


def load_candidate(directory, source):
    directory = Path(directory)
    envelope = json.loads((directory / "candidate.json").read_text())
    registry = verified_record(envelope["signed"]["current"]["registry_record"], source, mode="offline")
    plan = verify(envelope, registry["bundle_public_key"])
    if plan.get("kind") != "offline-candidate" or plan.get("schema") != 1:
        raise ValueError("Unsupported bundle candidate")
    for role in ("current", "previous"):
        if plan[role] is not None:
            record = plan[role]
            publication = verify_record(record, source)
            validate_archive(directory / role / (publication["id"] + ".zip"), record, source)
    if plan["previous"]:
        prevent_rollback(plan["previous"], plan["current"])
    return plan


def publish_candidate(source, store, directory, report, public_url, *, destination_key="local",
                      public_check=verify_public_files):
    """Commit the exact tested ZIP, rejecting stale baselines and failed/mismatched reports."""
    from .offline_acceptance import verify_acceptance
    directory = Path(directory)
    plan = load_candidate(directory, source)
    verify_acceptance(report, directory, plan)
    if plan["destination_key"] != destination_key or plan["public_url"] != public_url:
        raise ValueError("Candidate publication destination changed")
    # A delayed candidate must still have live online authorization at publication.
    verified_record(plan["current"]["registry_record"], source)
    head = store.get("index.html")
    if (hashlib.sha256(head.data).hexdigest() if head else None) != plan["origin_sha256"]:
        raise ValueError("Download head changed since native validation; prepare and test again")
    previous = read_page(head.data, source) if head else None
    if (previous["current"] if previous else None) != plan["previous"]:
        raise ValueError("Candidate upgrade baseline differs from current download")
    record = plan["current"]
    publication = verify_record(record, source)
    archive = directory / "current" / (publication["id"] + ".zip")
    locations = paths(publication)
    files = {locations["bundle"]: archive, locations["metadata"]: canonical(record) + b"\n",
             locations["page"]: render_page({"current": record}, archived=True)}
    unchanged = previous is not None and previous["current"] == record
    if not unchanged:
        store.publish_immutable(files)
    store.verify_immutable(files)
    public_check(public_url, files)
    status = "unchanged"
    if not unchanged:
        state = {"current": record}
        if previous:
            state["previous"] = previous["current"]
        page = render_page(state)
        try:
            store.put("index.html", page, previous=head.etag if head else None)
        except Exception:
            # A lost acknowledgement after a successful commit is recoverable.
            # Never write a rollback over a possibly newer competing publication.
            committed = store.get("index.html")
            if committed is None or committed.data != page:
                raise
        status = "published"
    return {"status": status, "target": publication["target"], "sha256": publication["sha256"],
            "acceptance": report,
            "registry": publication["registry"], "repositories": publication["repositories"],
            "held_back": publication["held_back"], "index_url": resource_url(public_url, "index.html"),
            **{k + "_url": resource_url(public_url, v) for k, v in locations.items()}}
