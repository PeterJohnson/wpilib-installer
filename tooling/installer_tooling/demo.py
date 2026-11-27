"""Generate small synthetic packages; no application release is impersonated."""
import json
from pathlib import Path

from .common import TARGETS, atomic_json, keygen
from .repository import publish
from .registry_updates import publish_registry


def demo_config():
    return json.loads((Path(__file__).parent / "assets" / "demo.json").read_text())


def write_spec(directory, publisher, target, revision=1, minimum_runtime="1.0.0"):
    directory = Path(directory).resolve()
    packages = []
    for definition in demo_config()["publishers"][publisher]:
        component = definition["id"]
        short = component.split(".")[-1]
        payload = directory / "payloads" / f"{publisher}-{revision}" / short
        marker = payload / "prototype" / short / "version.txt"
        marker.parent.mkdir(parents=True, exist_ok=True)
        marker.write_text(f"TEST PAYLOAD ONLY\n{short} {revision}.0.0 for {target}\n")
        deps = dict(definition["dependencies"])
        if "org.wpilib.runtime" in deps:
            deps["org.wpilib.runtime"] = minimum_runtime
        packages.append({"id": component, "name": f"{short.title()} (test payload)",
                         "description": "Synthetic prototype data; not the application or SDK",
                         "version": f"{revision}.0.0", "dependencies": deps,
                         "payload": payload.relative_to(directory).as_posix()})
    spec = directory / f"{publisher}-{revision}.json"
    atomic_json(spec, {"publisher": publisher, "target": target, "season": "2027", "channel": "prototype",
                       "revision": revision, "packages": packages})
    return spec


def create(output, target, ifw, base_url=None, *, enable_tuf=False):
    output = Path(output).resolve()
    if target not in TARGETS:
        raise ValueError("Unsupported target")
    if (output / "registry.json").exists():
        raise ValueError("Demo already exists; use publish for subsequent releases")
    store = output / "store"
    config = demo_config()
    registry = {"schema": 1, "target": target, "season": "2027", "channel": "prototype", "repositories": {},
                "repository_order": config["repository_order"], "profiles": config["profiles"]}
    for publisher, definitions in config["publishers"].items():
        key = output / "keys" / f"{publisher}.pem"
        registry["repositories"][publisher] = {
            "url": (base_url.rstrip("/") if base_url else store.as_uri()) + f"/{target}/{publisher}",
            "components": [p["id"] for p in definitions], "public_key": keygen(key), "minimum_revision": 1}
    registry["bundle_public_key"] = keygen(output / "keys" / "bundle.pem")
    if enable_tuf:
        from .tuf_repository import demo_trust
        from .archival import INDEFINITE_POLICY
        registry["online_trust"] = "tuf"
        registry["archive_policy"] = dict(INDEFINITE_POLICY)
        for publisher, repo in registry["repositories"].items():
            repo["tuf"] = demo_trust(output / "keys" / (publisher + "-tuf"),
                                     f"demo-{target}-{publisher}", repo["url"] + "/tuf")
    atomic_json(output / "registry.json", registry)
    for publisher in registry["repositories"]:
        spec = write_spec(output / "specs", publisher, target)
        publish(spec, store, output / "keys" / f"{publisher}.pem", registry, ifw)
    source = {"kind": "registry-source", "schema": 1, "registry_id": f"wpilib-2027-prototype-{target}",
              "target": target, "season": "2027", "channel": "prototype", "minimum_revision": 1,
              "public_key": keygen(output / "keys/registry.pem"),
              "url": (base_url.rstrip("/") if base_url else store.as_uri()) + f"/{target}/registry/registry.json"}
    if enable_tuf:
        source["archive_policy"] = registry["archive_policy"]
        source["tuf"] = demo_trust(output / "keys/registry-tuf", f"demo-{target}-registry",
                                   source["url"].rsplit("/", 1)[0] + "/tuf")
    atomic_json(output / "registry-source.json", source)
    publish_registry(registry, source, output / "keys/registry.pem", 1, store / target / "registry")
    return registry
