"""Generate browser-downloadable fixture bundles for all six target identities."""
import argparse
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from installer_tooling.bundle import assemble
from installer_tooling.common import TARGETS
from installer_tooling.demo import create
from installer_tooling.registry_updates import resolve, verified_record

p = argparse.ArgumentParser(description=__doc__)
p.add_argument("--ifw", type=Path, required=True)
p.add_argument("--output", type=Path, default=Path("build/platforms"))
args = p.parse_args()
for target in TARGETS:
    root = args.output / target
    create(root / "demo", target, args.ifw, enable_tuf=True)
    source = json.loads((root / "demo/registry-source.json").read_text())
    view = resolve(source, args.output / "cache")
    verified_record(view.record, source, mode="offline")
    archive = assemble(view.registry, root / "downloads", root / "demo/keys/bundle.pem", args.output / "cache",
                       registry_record=view.record, registry_evidence=view.tuf_evidence)
    print(archive)
