"""Qt build/launch adapter and stable JSON update discovery."""
from __future__ import annotations

from contextlib import contextmanager
import json
from pathlib import Path
import platform
import shutil
import tempfile
import xml.etree.ElementTree as ET

from .common import run, version
from .gateway import gateway
from .repository import package_map, tool, validate_index, xml_file


def build_installer(ifw, output):
    output = Path(output).resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="ifw-installer-") as tmp:
        root = Path(tmp)
        config = ET.Element("Installer")
        for tag, value in {"Name": "WPILib Qt Prototype", "Version": "0.1.0",
                           "Title": "WPILib package installer prototype",
                           "Publisher": "WPILib", "TargetDir": "@HomeDir@/wpilib-qt-prototype",
                           "MaintenanceToolName": "maintenancetool", "RemoveTargetDir": "false",
                           "RepositorySettingsPageVisible": "false", "SaveDefaultRepositories": "false",
                           "ControlScript": "controller.js"}.items():
            ET.SubElement(config, tag).text = value
        xml_file(config, root / "config.xml")
        shutil.copyfile(Path(__file__).parent / "assets" / "controller.js", root / "controller.js")
        # binarycreator requires an input package directory, even for online-only.
        meta = root / "packages" / "org.wpilib.bootstrap" / "meta"
        meta.mkdir(parents=True)
        (meta / "package.xml").write_text("<Package><DisplayName>Bootstrap</DisplayName>"
            "<Description>Prototype bootstrap</Description><Version>1.0.0</Version>"
            "<ReleaseDate>2026-11-26</ReleaseDate><Default>false</Default></Package>")
        run([tool(ifw, "binarycreator"), "--online-only", "-c", root / "config.xml",
             "-p", root / "packages", output], timeout=180)
    return output


def executable(path):
    path = Path(path).resolve()
    if path.suffix == ".app":
        candidates = list((path / "Contents" / "MacOS").iterdir())
        if len(candidates) != 1:
            raise ValueError(f"Cannot identify application executable: {path}")
        path = candidates[0]
    return path


def inventory(root):
    path = Path(root) / "components.xml"
    if not path.is_file():
        raise ValueError(f"No Qt installation inventory at {path}")
    xml = ET.parse(path).getroot()
    result = {}
    for package in xml.findall("Package"):
        result[package.findtext("Name")] = package.findtext("Version")
    return result


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
