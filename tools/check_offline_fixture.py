"""Required native lifecycle gate: two signed fixture bundles and a real version upgrade."""
import argparse
import json
from pathlib import Path
import shutil

from installer_prototype.bundle_publication import paths, prepare_bundle, render_page
from installer_prototype.demo import create, write_spec
from installer_prototype.offline_acceptance import check_candidate, host_target
from installer_prototype.publication_storage import Object
from installer_prototype.repository import publish


def fixture(ifw, root, report):
    root = Path(root).resolve()
    if root.exists():
        raise ValueError("Fixture workspace must be new")
    target = host_target()
    demo = root / "demo"
    registry = create(demo, target, ifw, enable_tuf=True)
    source_path = demo / "registry-source.json"
    source = json.loads(source_path.read_text())
    key = demo / "keys/bundle.pem"
    public = root / "public"
    public.mkdir()

    class Origin:
        def get(self, name):
            path = public / name
            return Object(path.read_bytes(), "fixture") if path.exists() else None

    initial = prepare_bundle(source, Origin(), key, root / "cache", root / "initial", public.as_uri(), initialize=True)
    record = initial["current"]
    publication = record["publication"]["signed"]
    destination = public / paths(publication)["bundle"]
    destination.parent.mkdir(parents=True)
    shutil.copyfile(root / "initial/current" / (publication["id"] + ".zip"), destination)
    (public / "index.html").write_bytes(render_page({"current": record}))
    spec = write_spec(demo / "specs", "elastic", target, 2)
    publish(spec, demo / "store", demo / "keys/elastic.pem", registry, ifw)
    candidate = root / "candidate"
    prepare_bundle(source, Origin(), key, root / "cache", candidate, public.as_uri())
    result = check_candidate(candidate, source_path, ifw, root / "native", report)
    if result["inventory"]["org.wpilib.elastic"] != "2.0.0":
        raise ValueError("Fixture did not exercise a real component version upgrade")
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--ifw", type=Path, required=True)
    parser.add_argument("--work", type=Path, required=True)
    parser.add_argument("--report", type=Path, required=True)
    args = parser.parse_args()
    fixture(args.ifw, args.work, args.report)


if __name__ == "__main__":
    main()
