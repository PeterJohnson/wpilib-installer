"""Restore executable modes lost by Actions artifacts; reject links and traversal."""
import argparse
from pathlib import Path
import tarfile

from installer_prototype.common import safe_path


def unpack(archive, destination):
    destination = Path(destination)
    if destination.exists():
        raise ValueError("Release extraction needs a fresh directory")
    with tarfile.open(archive) as stream:
        members = stream.getmembers()
        names = set()
        for member in members:
            safe_path(member.name.rstrip("/"))
            if not (member.isfile() or member.isdir()) or member.name in names:
                raise ValueError("Release archives must contain only unique regular files/directories")
            names.add(member.name)
        destination.mkdir(parents=True)
        stream.extractall(destination, members=members, filter="data")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("archive", type=Path)
    parser.add_argument("destination", type=Path)
    args = parser.parse_args()
    unpack(args.archive, args.destination)
