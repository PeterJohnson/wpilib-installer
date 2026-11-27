"""Restore release payloads, retaining executable modes and contained app symlinks."""
import argparse
from pathlib import Path



from installer_tooling.release_archive import extract as unpack


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("archive", type=Path)
    parser.add_argument("destination", type=Path)
    args = parser.parse_args()
    unpack(args.archive, args.destination)
