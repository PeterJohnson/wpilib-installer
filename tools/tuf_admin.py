"""Offline TUF root setup/rotation. Never put root private keys in release CI."""
import argparse
import json
from pathlib import Path

from installer_tooling.common import atomic_json, keygen
from installer_tooling.tuf_repository import ROLES, create_root


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    init = commands.add_parser("init")
    init.add_argument("--repository-id", required=True)
    init.add_argument("--url", required=True, help="Public repository URL ending in /tuf")
    init.add_argument("--output", type=Path, required=True)
    rotate = commands.add_parser("rotate")
    rotate.add_argument("--previous", type=Path, required=True)
    rotate.add_argument("--keys", type=Path, required=True, help="JSON role -> PEM path list; include old_root signers")
    rotate.add_argument("--root-threshold", type=int, default=2)
    rotate.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if args.command == "init":
        if args.output.exists():
            raise ValueError("Root setup output must be new")
        keys = {}
        for role in ("root", *ROLES):
            keys[role] = []
            for index in range(3 if role == "root" else 1):
                path = args.output / "keys" / f"{role}-{index + 1}.pem"
                keygen(path)
                keys[role].append(path)
        root = create_root(keys, root_threshold=2)
        atomic_json(args.output / "1.root.json", root)
        atomic_json(args.output / "trust.json", {"repository_id": args.repository_id, "url": args.url, "root": root})
    else:
        if args.output.exists():
            raise ValueError("Refusing to replace a root file")
        previous = json.loads(args.previous.read_text())
        keys = json.loads(args.keys.read_text())
        root = create_root(keys, previous=previous, version=previous["signed"]["version"] + 1,
                           root_threshold=args.root_threshold)
        atomic_json(args.output, root)


if __name__ == "__main__":
    main()
