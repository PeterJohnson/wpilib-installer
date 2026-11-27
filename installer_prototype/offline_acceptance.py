"""Native, network-blocked acceptance of the exact offline publication candidate."""
import argparse
from contextlib import contextmanager
import json
import os
from pathlib import Path
import platform
import shutil
import socket
import subprocess
import sys
import uuid
import zipfile

from .bundle import load_bundle
from .common import atomic_json, digest, run, safe_path, version
from .manager import build_installer, executable, inventory, launch
from .registry_updates import resolve
from .repository import package_map

SUITE = 1
CHECKS = ["fresh-install", "profiles", "baseline-install", "offline-upgrade", "older-bundle-no-downgrade"]
RUNNERS = {"linux-x64": "ubuntu-24.04", "windows-x64": "windows-2022",
           "macos-x64": "macos-15-intel", "macos-arm64": "macos-15"}


def host_target():
    system = {"Linux": "linux", "Windows": "windows", "Darwin": "macos"}[platform.system()]
    machine = platform.machine().lower()
    if machine not in ("arm64", "aarch64", "x86_64", "amd64"):
        raise ValueError("Unsupported native host architecture")
    return system + ("-arm64" if machine in ("arm64", "aarch64") else "-x64")


def runner_for(target):
    if target not in RUNNERS:
        raise ValueError(f"No pinned native Qt acceptance runner for {target}; publication is blocked")
    return RUNNERS[target]


def report_identity(directory, plan):
    current = plan["current"]["publication"]["signed"]
    previous = plan["previous"]
    return {"kind": "offline-acceptance", "schema": 1, "suite": SUITE,
            "candidate_sha256": digest(Path(directory) / "candidate.json"),
            "sha256": current["sha256"], "target": current["target"],
            "baseline_sha256": previous["publication"]["signed"]["sha256"] if previous else current["sha256"],
            "upgrade_mode": "previous-bundle" if previous else "initial-reinstall"}


def verify_acceptance(report, directory, plan):
    expected = report_identity(directory, plan)
    isolation = {"linux": "network-namespace", "windows": "program-firewall", "macos": "sandbox-exec"}
    if (any(report.get(k) != v for k, v in expected.items()) or report.get("status") != "passed"
            or report.get("host_target") != expected["target"] or report.get("checks") != CHECKS
            or report.get("network_isolation") != isolation[expected["target"].split("-")[0]]):
        raise ValueError("Missing, failed, or mismatched native offline acceptance report")


def profile_closure(packages, selected):
    result, pending = set(), list(selected)
    while pending:
        name = pending.pop()
        if name in result:
            continue
        if name not in packages:
            raise ValueError(f"Profile requires absent package {name}")
        result.add(name)
        pending.extend(packages[name].get("dependencies", {}))
    return result


def installed_files(root, packages):
    result = {}
    for package in packages.values():
        for name in package["owned_files"]:
            path = Path(root) / str(safe_path(name))
            if path.is_symlink() or not path.is_file() or not path.resolve().is_relative_to(Path(root).resolve()):
                raise ValueError(f"Missing or escaping installed payload: {name}")
            result[name] = digest(path)
    return result


def exercise(directory, source, engine, work, report_path, isolation):
    """Runs only inside the OS network restriction, with independent empty caches."""
    from .bundle_publication import load_candidate
    plan = load_candidate(directory, source)
    target = plan["current"]["publication"]["signed"]["target"]
    if target != host_target():
        raise ValueError("Native acceptance target does not match this host")
    # A firewall/sandbox setup failure must never silently become an online test.
    try:
        with socket.create_connection(("1.1.1.1", 443), timeout=3):
            pass
    except OSError:
        pass
    else:
        raise ValueError("Offline network restriction did not block outbound TCP")
    work = Path(work)
    media = work / "media"
    for role in ("current", "previous"):
        if plan[role] is not None:
            identity = plan[role]["publication"]["signed"]["id"]
            with zipfile.ZipFile(Path(directory) / role / (identity + ".zip")) as archive:
                # load_candidate authenticated every member/path before extraction.
                archive.extractall(media / role)
    current = media / "current" / plan["current"]["publication"]["signed"]["id"]
    baseline = (media / "previous" / plan["previous"]["publication"]["signed"]["id"]
                if plan["previous"] else current)
    suffix = ".exe" if os.name == "nt" else ".app" if sys.platform == "darwin" else ""
    counter = 0

    def operation(bundle, root, command, *, engine_path=engine, packages=(), profile=None):
        nonlocal counter
        counter += 1
        cache = work / f"empty-cache-{counter}"
        if cache.exists():
            raise ValueError("Acceptance cache must start empty")
        view = resolve(source, cache, bundle=bundle)
        snapshots, _ = load_bundle(bundle, view.registry, cache)
        log = work / f"operation-{counter}-{command}.log"
        try:
            output = launch(engine_path, snapshots, cache, offline=True, root=root, command=command,
                            packages=packages, profile=profile, profiles=view.registry.get("profiles"), headless=True)
            log.write_text(output, encoding="utf-8")
        except Exception as exc:
            log.write_text(str(exc), encoding="utf-8")
            raise
        finally:
            shutil.rmtree(cache, ignore_errors=True)
        return package_map(snapshots), view.registry

    offered = {p["id"]: p["version"] for p in plan["current"]["publication"]["signed"]["components"]}
    fresh = work / "fresh"
    packages, registry = operation(current, fresh, "install", packages=sorted(offered))
    if inventory(fresh) != offered:
        raise ValueError("Fresh offline inventory differs from the candidate")
    fresh_files = installed_files(fresh, packages)
    for index, (profile, selected) in enumerate(sorted(registry.get("profiles", {}).items())):
        root = work / f"profile-{index}"
        operation(current, root, "install", profile=profile)
        expected = {name: offered[name] for name in profile_closure(packages, selected)}
        if inventory(root) != expected:
            raise ValueError(f"Incorrect offline profile inventory: {profile}")
        installed_files(root, {name: packages[name] for name in expected})
        shutil.rmtree(root)
    old_record = plan["previous"] or plan["current"]
    old_versions = {p["id"]: p["version"] for p in old_record["publication"]["signed"]["components"]}
    upgraded = work / "upgraded"
    old_packages, _ = operation(baseline, upgraded, "install", packages=sorted(old_versions))
    if inventory(upgraded) != old_versions:
        raise ValueError("Baseline offline install differs from the previous bundle")
    old_files = installed_files(upgraded, old_packages)
    if any(version(offered[name]) < version(old_versions[name]) for name in offered.keys() & old_versions.keys()):
        raise ValueError("Candidate would downgrade a previously published component")
    maintenance = upgraded / ("maintenancetool" + suffix)
    operation(current, upgraded, "update", engine_path=maintenance)
    added = sorted(offered.keys() - old_versions.keys())
    if added:
        operation(current, upgraded, "install", engine_path=maintenance, packages=added)
    expected = {**old_versions, **offered}
    if inventory(upgraded) != expected or installed_files(upgraded, packages) != fresh_files:
        raise ValueError("Offline upgrade inventory or payload differs from fresh installation")
    retained = {name: p for name, p in old_packages.items() if name not in packages}
    if any(old_files[name] != value for name, value in installed_files(upgraded, retained).items()):
        raise ValueError("Upgrade changed an unrelated retained package")
    operation(baseline, upgraded, "update", engine_path=maintenance)
    if inventory(upgraded) != expected or installed_files(upgraded, packages) != fresh_files:
        raise ValueError("Older offline media downgraded the installation")
    atomic_json(report_path, {**report_identity(directory, plan), "status": "passed", "host_target": host_target(),
                "checks": CHECKS, "network_isolation": isolation, "inventory": expected})


@contextmanager
def windows_firewall(programs):
    """Scope temporary rules to Python and the exact Qt executables, not the Actions agent."""
    group = "WPILib-offline-" + uuid.uuid4().hex
    env = {**os.environ, "OFFLINE_PROGRAMS": json.dumps(list(map(str, programs))), "OFFLINE_RULE_GROUP": group}
    add = """$ErrorActionPreference = 'Stop'
if (Get-NetFirewallProfile | Where-Object { -not $_.Enabled }) { throw 'Windows Firewall must be enabled' }
$i = 0
foreach ($program in ($env:OFFLINE_PROGRAMS | ConvertFrom-Json)) {
  New-NetFirewallRule -DisplayName ($env:OFFLINE_RULE_GROUP + '-' + $i) -Group $env:OFFLINE_RULE_GROUP -Direction Outbound -Action Block -Profile Any -Program $program | Out-Null
  $i += 1
}
"""
    remove = "Get-NetFirewallRule -Group $env:OFFLINE_RULE_GROUP -ErrorAction SilentlyContinue | Remove-NetFirewallRule -ErrorAction Stop"
    try:
        subprocess.run(["pwsh", "-NoProfile", "-Command", add], env=env, check=True)
        yield
    finally:
        subprocess.run(["pwsh", "-NoProfile", "-Command", remove], env=env, check=True)


def check_candidate(directory, source_path, ifw, work, report_path):
    from .bundle_publication import load_candidate
    directory, source_path, work, report_path = (Path(p).resolve() for p in (directory, source_path, work, report_path))
    report_path.unlink(missing_ok=True)
    if work.exists():
        raise ValueError("Native acceptance workspace must be new")
    source = json.loads(source_path.read_text())
    plan = load_candidate(directory, source)
    if plan["current"]["publication"]["signed"]["target"] != host_target():
        raise ValueError("Native acceptance target does not match this host")
    work.mkdir(parents=True)
    suffix = ".exe" if os.name == "nt" else ".app" if sys.platform == "darwin" else ""
    engine = build_installer(ifw, work / ("installer" + suffix))
    args = [sys.executable, "-m", "installer_prototype.offline_acceptance", "worker", "--candidate", directory,
            "--source", source_path, "--engine", engine, "--work", work, "--report", report_path]
    if sys.platform == "linux":
        # GitHub's Ubuntu AppArmor profile can prohibit unprivileged namespaces.
        prefix = (["sudo", "-n", "unshare", "-n", "--", "setpriv", "--reuid", str(os.getuid()),
                   "--regid", str(os.getgid()), "--init-groups", "env",
                   # setup-python may need its tool-cache shared library directory.
                   *(f"{name}={os.environ[name]}" for name in ("LD_LIBRARY_PATH", "PYTHONPATH") if name in os.environ)]
                  if os.environ.get("GITHUB_ACTIONS") == "true"
                  else ["unshare", "-Urn", "--"])
        run([*prefix, *args, "--isolation", "network-namespace"], timeout=3600)
    elif sys.platform == "darwin":
        run(["sandbox-exec", "-p", "(version 1) (allow default) (deny network*)", *args,
             "--isolation", "sandbox-exec"], timeout=3600)
    elif os.name == "nt":
        profiles = plan["current"]["registry_record"]["signed"]["registry"].get("profiles", {})
        programs = [Path(sys.executable), executable(engine),
                    *(work / root / "maintenancetool.exe" for root in ["fresh", "upgraded", *(f"profile-{i}" for i in range(len(profiles)))])]
        with windows_firewall(programs):
            run([*args, "--isolation", "program-firewall"], timeout=3600)
    else:
        raise ValueError("No native offline isolation for this host")
    result = json.loads(report_path.read_text())
    verify_acceptance(result, directory, plan)
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("check", "worker"))
    for name in ("candidate", "source", "work", "report"):
        parser.add_argument("--" + name, type=Path, required=True)
    parser.add_argument("--ifw", type=Path)
    parser.add_argument("--engine", type=Path)
    parser.add_argument("--isolation")
    args = parser.parse_args()
    if args.command == "check":
        if args.ifw is None:
            parser.error("check requires --ifw")
        check_candidate(args.candidate, args.source, args.ifw, args.work, args.report)
    else:
        if args.engine is None or not args.isolation:
            parser.error("worker requires --engine and --isolation")
        exercise(args.candidate, json.loads(args.source.read_text()), args.engine, args.work, args.report, args.isolation)


if __name__ == "__main__":
    main()
