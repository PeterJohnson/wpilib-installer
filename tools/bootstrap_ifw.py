"""Download exact public Qt IFW 4.11 tools; requires 7z or 7zz on PATH."""
import argparse
import hashlib
import json
from pathlib import Path
import platform
import shutil
import subprocess
import tempfile
from urllib.request import urlopen


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--output", type=Path, default=Path(".tools/ifw"))
    args = p.parse_args()
    system = platform.system()
    if system in ("Linux", "Windows") and platform.machine().lower() not in ("x86_64", "amd64"):
        raise SystemExit("This tool lock covers Linux/Windows x64 and universal macOS. Supply a native IFW build on other hosts.")
    lock = json.loads(Path(__file__).with_name("ifw-lock.json").read_text())[system]
    extractor = shutil.which("7zz") or shutil.which("7z")
    if not extractor:
        raise SystemExit("Install 7-Zip (7z or 7zz) before running bootstrap_ifw.py")
    output = args.output.resolve()
    if output.exists():
        raise SystemExit(f"Output already exists; use it or choose a new directory: {output}")
    with tempfile.TemporaryDirectory(prefix="qt-ifw-") as tmp:
        archive = Path(tmp) / "ifw.7z"
        with urlopen(lock["url"], timeout=180) as response, archive.open("wb") as stream:
            shutil.copyfileobj(response, stream)
        with archive.open("rb") as stream:
            actual = hashlib.file_digest(stream, "sha256").hexdigest()
        if actual != lock["sha256"]:
            raise SystemExit("Qt IFW archive does not match the checked-in SHA-256")
        subprocess.run([extractor, "x", "-y", f"-o{output}", str(archive)], check=True)
    print(output)


if __name__ == "__main__":
    main()
