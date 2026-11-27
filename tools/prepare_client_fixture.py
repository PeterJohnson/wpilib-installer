"""Prepare signed fixture ZIPs (no Python runtime is shipped to the client)."""
import argparse
import json
from pathlib import Path
from installer_tooling.demo import create, write_spec
from installer_tooling.repository import publish
from installer_tooling.registry_updates import refresh
from installer_tooling.bundle import assemble
from installer_tooling.offline_acceptance import host_target


def prepare(ifw, work):
    work = Path(work).resolve()
    registry = create(work / 'demo', host_target(), ifw, enable_tuf=True)
    source = json.loads((work / 'demo/registry-source.json').read_text())
    view = refresh(source, work / 'cache')
    # refresh returns a registry envelope plus evidence; preserve the signed pin.
    for name, rev in [('baseline', 1), ('candidate', 2)]:
        if rev == 2:
            spec = write_spec(work / 'demo/specs', 'elastic', host_target(), 2)
            publish(spec, work / 'demo/store', work / 'demo/keys/elastic.pem', registry, ifw)
        assemble(registry, work / name, work / 'demo/keys/bundle.pem', work / 'cache',
                 registry_record=view.record, registry_evidence=view.tuf_evidence)
    return work


if __name__ == '__main__':
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--ifw', type=Path, required=True)
    p.add_argument('--work', type=Path, required=True)
    a = p.parse_args()
    prepare(a.ifw, a.work)
