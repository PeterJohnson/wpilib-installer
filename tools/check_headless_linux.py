"""Headless lifecycle in unmodified minimal Linux images, without graphics or tools.
Only the host drives tests; the client contains no Python, Qt installation or FUSE.
"""
import argparse
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import time

from check_client_offline import unpack_manifest
from installer_tooling.common import digest
from installer_tooling.qt_engine import inventory
from installer_tooling.payload import verify_installed
from audit_client_dependencies import LINUX_BASE


def run(*args, **kw):
    return subprocess.run(list(map(str, args)), check=True, text=True, **kw)


def check(a):
    work = a.work.resolve()
    work.mkdir(parents=True)
    uid, gid = os.getuid(), os.getgid()
    evidence = {'schema': 2, 'application_sha256': digest(a.app), 'images': [],
                'isolation': 'unmodified OS images; no network/display/graphics/FUSE; read-only OS; empty client PATH',
                'scope': 'headless CLI; GUI acceptance runs in installed Ubuntu Desktop VMs'}
    initial, current = unpack_manifest(a.baseline), unpack_manifest(a.candidate)
    primary = next(k for k in initial if k.endswith('.elastic'))
    secondary = next(k for k in initial if k.endswith('.advantagescope'))
    for image in a.images:
        run('docker', 'pull', image)
        case = work / image.replace(':', '-')
        case.mkdir()
        (case / 'home').mkdir()
        download = case / 'download'; download.mkdir()
        usb = case / 'usb'; usb.mkdir()
        shutil.copy2(a.app, download / 'wpilib-manager')
        shutil.copy2(a.baseline, usb / 'baseline.zip')
        shutil.copy2(a.candidate, usb / 'candidate.zip')
        app = '/work/download/wpilib-manager'
        root = case / 'installation'
        checks, loaded = [], set()
        base = ['docker', 'run', '--rm', '--network', 'none', '--read-only', '--cap-drop', 'ALL',
                '--user', f'{uid}:{gid}', '--tmpfs', f'/tmp:exec,uid={uid},gid={gid},size=512m',
                '-v', f'{case}:/work', '-v', f'{a.engine.resolve()}:/control/engine:ro',
                '-e', 'HOME=/work/home', '-e', 'PATH=/no-external-runtimes']
        identity = json.loads(run('docker', 'image', 'inspect', image, capture_output=True).stdout)[0]
        os_packages = run(*base, image, '/usr/bin/dpkg-query', '-W', capture_output=True).stdout
        (case / 'base-packages.txt').write_text(os_packages)
        assert not any(n in os_packages for n in ['libegl1\t', 'libgl1\t', 'libgl1-mesa-dri\t']), os_packages
        negative = subprocess.run([*base, image, '/control/engine', '--help'], capture_output=True, text=True)
        assert negative.returncode and 'error while loading shared libraries' in negative.stderr, negative
        checks.append({'name': 'unbundled-worker-negative-control', 'passed': True, 'error': negative.stderr})
        run(*base, image, '/bin/sh', '-c', 'test ! -e /dev/fuse && test ! -e /usr/bin/python3 && test ! -e /usr/bin/cargo && test -z "$DISPLAY$WAYLAND_DISPLAY"')

        def invoke(name, command, *extra, implicit=False, worker=False):
            args = [app, '--command', command]
            if not implicit: args += ['--root', '/work/installation']
            started = time.monotonic()
            result = subprocess.run([*base, '-e', 'LD_DEBUG=libs', image, *args, *extra],
                                    capture_output=True, text=True, timeout=300)
            (case / (name + '.log')).write_text(result.stdout + '\n' + result.stderr)
            assert result.returncode == 0, (name, result.stderr[-6000:], result.stdout)
            private = []
            for path in re.findall(r'calling init: (\S+)', result.stderr):
                assert not Path(path).name.startswith(('libLLVM', 'libGL.', 'libGLX', 'libEGL', 'libgbm')), path
                if Path(path).name not in LINUX_BASE:
                    assert ('/.installation.qt-worker/runtime/lib/' in path or path.startswith('/tmp/wpilib-csharp-app-')), path
                    if '/.installation.qt-worker/runtime/lib/' in path: private.append(path)
                loaded.add(Path(path).name)
            assert bool(private) == worker, (name, private)
            value = json.loads(result.stdout)
            assert value['status'] == 'ok', value
            checks.append({'name': name, 'passed': True, 'wall_seconds': round(time.monotonic()-started, 3)})
            print('PASS', image, name, flush=True)
            return value

        invoke('verify-without-worker-runtime', 'verify', '--bundle', '/work/usb/baseline.zip')
        invoke('offline-install', 'install', '--bundle', '/work/usb/baseline.zip', '--package', primary,
               '--package', secondary, '--accept-licenses', worker=True)
        for component in inventory(root): verify_installed(root, initial[component])
        shutil.rmtree(download); (usb / 'baseline.zip').unlink()
        app = '/work/installation/wpilib-manager'
        invoke('installed-cached-verify', 'verify', implicit=True)
        updates = invoke('discover-offline-updates', 'check-updates', '--bundle', '/work/usb/candidate.zip')
        assert {u['id'] for u in updates['updates']} == {k for k in inventory(root) if initial[k]['version'] != current[k]['version']}
        invoke('offline-upgrade', 'update', '--bundle', '/work/usb/candidate.zip', '--accept-licenses', worker=True)
        for component in inventory(root): verify_installed(root, current[component])
        (usb / 'candidate.zip').unlink()
        invoke('cached-remove', 'remove', '--package', secondary, '--accept-licenses', worker=True)
        assert secondary not in inventory(root)
        invoke('cached-reinstall', 'install', '--package', secondary, '--accept-licenses', worker=True)
        verify_installed(root, current[secondary])
        invoke('uninstall-all', 'uninstall', '--accept-licenses', implicit=True, worker=True)
        assert not inventory(root)
        invoke('reinstall-after-uninstall', 'install', '--package', primary, '--accept-licenses', implicit=True, worker=True)
        invoke('final-cached-verify', 'verify', implicit=True)
        assert 'libxkbcommon-x11.so.0' in loaded, loaded
        assert not download.exists() and not list(usb.iterdir())
        evidence['images'].append({'image': image, 'image_id': identity['Id'], 'digests': identity['RepoDigests'],
                                   'checks': checks, 'loaded_libraries': sorted(loaded), 'inventory': inventory(root)})
        a.report.parent.mkdir(parents=True, exist_ok=True)
        a.report.write_text(json.dumps(evidence, indent=2)+'\n')


if __name__ == '__main__':
    p = argparse.ArgumentParser(description=__doc__)
    for name in ['app', 'engine', 'baseline', 'candidate', 'work', 'report']:
        p.add_argument('--'+name, type=Path, required=True)
    p.add_argument('--images', nargs='+', default=['ubuntu:22.04', 'ubuntu:24.04', 'ubuntu:26.04', 'debian:12'])
    check(p.parse_args())
