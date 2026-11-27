import argparse
import json
from pathlib import Path
import sys

from .bundle import assemble, load_bundle
from .common import TARGETS
from .demo import create
from .qt_engine import build_installer
from .reference_client import check_updates, launch
from .repository import publish, select
from .registry_updates import publish_registry, resolve


def parser():
    p = argparse.ArgumentParser(description="WPILib package publishing and development tools")
    sub = p.add_subparsers(dest="action", required=True)
    demo = sub.add_parser("demo", help="Generate synthetic repositories and throwaway signing keys")
    demo.add_argument("--output", type=Path, required=True)
    demo.add_argument("--target", choices=TARGETS, required=True)
    demo.add_argument("--ifw", type=Path, required=True)
    demo.add_argument("--base-url", help="URL serving the generated store directory")
    build = sub.add_parser("build-installer")
    build.add_argument("--ifw", type=Path, required=True)
    build.add_argument("--output", type=Path, required=True)
    pub = sub.add_parser("publish")
    pub.add_argument("--cache", type=Path, default=Path("build/cache"))
    for name in ("spec", "store", "key", "registry", "ifw"):
        pub.add_argument(f"--{name}", type=Path, required=True)
    registry_pub = sub.add_parser("publish-registry", help="Sign and publish an authority-approved registry revision")
    for name in ("registry", "source", "key", "output"):
        registry_pub.add_argument(f"--{name}", type=Path, required=True)
    registry_pub.add_argument("--revision", type=int, required=True)
    registry_pub.add_argument("--valid-days", type=int, default=7)
    registry_pub.add_argument("--offline-days", type=int, help="Bounded legacy archival policy only (default: 365); omit for indefinite policy")
    for name in ("bundle", "check-updates", "run", "refresh-registry"):
        command = sub.add_parser(name)
        command.add_argument("--registry", type=Path, required=True)
        command.add_argument("--cache", type=Path, default=Path("build/cache"))
        if name == "bundle":
            command.add_argument("--output", type=Path, required=True)
            command.add_argument("--key", type=Path, required=True)
        elif name != "refresh-registry":
            command.add_argument("--bundle", type=Path, help="Extracted offline bundle directory")
            command.add_argument("--root", type=Path, required=name == "check-updates")
        if name == "run":
            command.add_argument("--engine", type=Path, required=True)
            command.add_argument("--command", choices=("install", "update", "remove", "list", "check-updates", "search"))
            command.add_argument("--profile", help="Profile name defined in the trusted registry")
            command.add_argument("--headless", action="store_true")
            command.add_argument("packages", nargs="*")
    return p


def main():
    args = parser().parse_args()
    try:
        if args.action == "demo":
            create(args.output, args.target, args.ifw, args.base_url, enable_tuf=True)
            print(args.output.resolve() / "registry-source.json")
            return
        if args.action == "build-installer":
            print(build_installer(args.ifw, args.output))
            return
        config = json.loads(args.registry.read_text())
        if args.action == "publish-registry":
            source = json.loads(args.source.read_text())
            print(publish_registry(config, source, args.key, args.revision, args.output,
                                   valid_days=args.valid_days, offline_days=args.offline_days))
            return
        view = resolve(config, args.cache, bundle=getattr(args, "bundle", None))
        registry = view.registry
        if view.warning:
            print(view.warning, file=sys.stderr)
        if args.action == "refresh-registry":
            print(json.dumps({**view.summary(), "publishers": list(registry["repositories"]),
                              "profiles": registry.get("profiles", {})}, indent=2))
            return
        if args.action == "publish":
            print(publish(args.spec, args.store, args.key, registry, args.ifw))
            return
        if args.action == "bundle":
            if config.get("kind") == "registry-source":
                from .registry_updates import verified_record
                verified_record(view.record, config, mode="offline")
            print(assemble(registry, args.output, args.key, args.cache, registry_record=view.record,
                           registry_evidence=view.tuf_evidence))
            return
        if args.bundle:
            snapshots, held = load_bundle(args.bundle, registry, args.cache)
        else:
            snapshots, held = select(registry, args.cache)
        if args.action == "check-updates":
            print(json.dumps({**check_updates(args.root, snapshots, held), "registry": view.summary()}, indent=2))
        else:
            print(launch(args.engine, snapshots, args.cache, offline=bool(args.bundle), root=args.root,
                         command=args.command, packages=args.packages, profile=args.profile,
                         profiles=registry.get("profiles", {}), headless=args.headless))
    except Exception as exc:
        if args.action in ("check-updates", "refresh-registry"):
            from .tuf_repository import RootUpdatePending
            pending = isinstance(exc, RootUpdatePending)
            print(json.dumps({"protocol_version": 1, "status": "root-update-pending" if pending else "error",
                              "error": str(exc), "retryable": pending}))
        else:
            print(f"error: {exc}", file=sys.stderr)
        sys.exit(1)
