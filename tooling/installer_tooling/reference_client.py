"""Python reference adapter for publication gates and tests, not the installed client."""
from contextlib import contextmanager
import json
from pathlib import Path
import platform
import shutil
import tempfile
from .common import run, version
from .gateway import gateway
from .repository import package_map, validate_index
from .qt_engine import executable, inventory


def check_updates(root, snapshots, held):
    installed, offered = inventory(root), package_map(snapshots)
    updates, blocked = [], []
    for name, current in installed.items():
        if name not in offered or version(offered[name]["version"]) <= version(current):
            continue
        updates.append({"id": name, "installed": current, "available": offered[name]["version"]})
    return {"protocol_version": 1, "status": "ok", "installation": str(Path(root).resolve()),
            "repositories": [{"publisher": s.publisher, "revision": s.manifest["revision"], "status": "verified"} for s in snapshots],
            "updates": updates, "held_back": held, "blocked": blocked}


@contextmanager
def local_repositories(snapshots, cache):
    # Copy verified bytes to a private workspace; IFW never reads mutable USB paths.
    with tempfile.TemporaryDirectory(prefix="ifw-offline-") as tmp:
        urls = []
        for snapshot in snapshots:
            validate_index(snapshot, cache)
            root = Path(tmp) / snapshot.publisher
            for name in snapshot.manifest["files"]:
                path = root / name
                path.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(snapshot.file(name, cache), path)
            urls.append(root.as_uri())
        yield urls


def launch(engine, snapshots, cache, *, offline=False, root=None, command=None, packages=(), profile=None, profiles=None, headless=False):
    host_os = {"Linux": "linux", "Windows": "windows", "Darwin": "macos"}.get(platform.system())
    host_arch = "arm64" if platform.machine().lower() in ("arm64", "aarch64") else "x64"
    if any(s.manifest["target"] != f"{host_os}-{host_arch}" for s in snapshots):
        raise ValueError("Package target does not match this host")
    if profile and command not in (None, "install"):
        raise ValueError("Profiles apply to installation; specify component IDs for maintenance")
    selected = []
    if profile:
        if profile not in (profiles or {}):
            raise ValueError(f"Profile is not configured in this registry: {profile}")
        selected = profiles[profile]
        offered = package_map(snapshots)
        if any(component not in offered for component in selected):
            raise ValueError(f"Profile requires components absent from the selected repositories: {profile}")
    if command == "update":
        installation = Path(root) if root else Path(engine).resolve().parent
        installed = inventory(installation)
        offered = package_map(snapshots)
        requested = list(packages) or list(installed)
        if any(name not in installed for name in requested):
            raise ValueError("Update requested for a component that is not installed")
        packages = [name for name in requested if name in offered and version(offered[name]["version"]) > version(installed[name])]
        if not packages:
            return "No newer packages available; installed versions were preserved."
    context = local_repositories if offline else gateway
    with context(snapshots, cache) as urls:
        qt_cache = Path(cache).resolve() / "qt-metadata"
        qt_cache.mkdir(parents=True, exist_ok=True)
        args = [executable(engine), "--set-temp-repository", ",".join(urls),
                "--cache-path", qt_cache, "--type", "package"]
        if headless:
            args += ["--accept-licenses", "--accept-messages", "--confirm-command"]
        if root:
            args += ["--root", str(Path(root).resolve())]
        if command:
            args += [command, *packages]
            if profile:
                args += selected
        elif profile:
            args += ["ProfileComponents=" + json.dumps(selected, separators=(",", ":"))]
        if command:
            return run(args, timeout=300)
        # GUI must use the native platform plugin; run() is for headless builds/tests.
        import subprocess
        result = subprocess.run(list(map(str, args)), check=False)
        if result.returncode:
            raise RuntimeError(f"Installer exited with status {result.returncode}")
        return ""
