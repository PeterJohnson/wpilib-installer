"""Run the delivered ELF in unmodified minimal Linux images with no client tooling.
Only the host drives tests. Display servers live in separate infrastructure containers.
"""
import argparse
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import time

from check_rust_manager import unpack_manifest
from installer_prototype.common import digest
from installer_prototype.manager import inventory
from installer_prototype.payload import verify_installed
from audit_rust_dependencies import LINUX_BASE


def run(*args, **kw):
    return subprocess.run(list(map(str, args)), check=True, text=True, **kw)


def check(a):
    work = a.work.resolve()
    work.mkdir(parents=True)
    repo = Path(__file__).resolve().parents[1]
    display_image = 'wpilib-installer-test-displays'
    run('docker', 'build', '-f', repo / '.github/containers/linux-display.Dockerfile', '-t', display_image, repo)
    uid, gid = os.getuid(), os.getgid()
    sockets = work / 'sockets'
    x11, wayland = sockets / 'x11', sockets / 'wayland'
    x11.mkdir(parents=True); x11.chmod(0o1777)
    wayland.mkdir(); wayland.chmod(0o700)
    prefix = ['docker', 'run', '-d', '--rm', '--network', 'none', '--user', f'{uid}:{gid}']
    servers = []
    evidence = {'schema': 1, 'application_sha256': digest(a.app), 'images': [],
                'isolation': 'unmodified OS images; no network; read-only OS; no FUSE; empty client PATH',
                'display_servers': 'external Xvfb and headless Weston; client has no added packages'}
    try:
        servers.append(run(*prefix, '-v', f'{x11}:/tmp/.X11-unix', display_image,
                           'Xvfb', ':99', '-ac', '-screen', '0', '1024x768x24', '-nolisten', 'tcp', capture_output=True).stdout.strip())
        servers.append(run(*prefix, '-v', f'{wayland}:/wayland', '-e', 'XDG_RUNTIME_DIR=/wayland', display_image,
                           'weston', '--backend=headless-backend.so', '--use-pixman', '--socket=wpilib-wayland', '--idle-time=0', capture_output=True).stdout.strip())
        for _ in range(100):
            if (x11 / 'X99').exists() and (wayland / 'wpilib-wayland').exists(): break
            time.sleep(.1)
        else:
            for server in servers: run('docker', 'logs', server)
            raise AssertionError('Display servers failed to start')
        initial, current = unpack_manifest(a.baseline), unpack_manifest(a.candidate)
        primary = next(k for k in initial if k.endswith('.elastic'))
        secondary = next(k for k in initial if k.endswith('.advantagescope'))
        for image in a.images:
            run('docker', 'pull', image)
            case = work / image.replace(':', '-')
            case.mkdir()
            home = case / 'home'; home.mkdir()
            download = case / 'download'; download.mkdir()
            usb = case / 'usb'; usb.mkdir()
            shutil.copy2(a.app, download / 'wpilib-manager')
            shutil.copy2(a.baseline, usb / 'baseline.zip')
            shutil.copy2(a.candidate, usb / 'candidate.zip')
            app = '/work/download/wpilib-manager'
            root = case / 'installation'
            checks, loaded = [], set()
            base = ['docker', 'run', '--rm', '--network', 'none', '--read-only', '--cap-drop', 'ALL',
                    '--user', f'{uid}:{gid}',
                    '--tmpfs', f'/tmp:exec,uid={uid},gid={gid},size=1g',
                    '-v', f'{case}:/work', '-v', f'{x11}:/tmp/.X11-unix:ro', '-v', f'{wayland}:/wayland',
                    '-v', f'{a.engine.resolve()}:/control/engine:ro',
                    '-e', 'HOME=/work/home', '-e', 'PATH=/no-external-runtimes', '-e', 'QT_QPA_PLATFORM=offscreen',
                    '-e', 'XDG_RUNTIME_DIR=/wayland']
            identity = json.loads(run('docker', 'image', 'inspect', image, capture_output=True).stdout)[0]
            os_packages = run(*base, image, '/usr/bin/dpkg-query', '-W', capture_output=True).stdout
            (case / 'base-packages.txt').write_text(os_packages)
            # A bare private engine must fail here, proving the base image isn't
            # accidentally supplying the runtime we claim to embed.
            negative = subprocess.run([*base, image, '/control/engine', '--help'], capture_output=True, text=True)
            assert negative.returncode and 'error while loading shared libraries' in negative.stderr, negative
            checks.append({'name': 'unbundled-worker-negative-control', 'passed': True, 'error': negative.stderr})
            run(*base, image, '/bin/sh', '-c', 'test ! -e /dev/fuse && test ! -e /usr/bin/python3 && test ! -e /usr/bin/cargo')

            def invoke(name, command=None, *extra, gui=False, backend='x11', implicit=False):
                args = [app]
                if not implicit: args += ['--root', '/work/installation']
                if gui:
                    args += ['--ui-test-report', f'/work/{name}.json']
                    if command: args += ['--ui-test-command', command]
                else: args += ['--command', command]
                display = ['-e', 'DISPLAY=:99'] if backend == 'x11' else ['-e', 'WAYLAND_DISPLAY=wpilib-wayland', '-e', 'XDG_SESSION_TYPE=wayland']
                started = time.monotonic()
                result = subprocess.run([*base, *display, '-e', 'LD_DEBUG=libs', image, *args, *extra],
                                        capture_output=True, text=True, timeout=300)
                (case / (name + '.log')).write_text(result.stdout + '\n' + result.stderr)
                assert result.returncode == 0, (name, result.stderr[-6000:], result.stdout)
                for path in re.findall(r'calling init: (\S+)', result.stderr):
                    if Path(path).name not in LINUX_BASE:
                        assert path.startswith('/tmp/wpilib-runtime-') and ('/lib/' in path or '/dri/' in path), path
                    loaded.add(Path(path).name)
                value = json.loads((case / (name + '.json')).read_text()) if gui else json.loads(result.stdout)
                if gui:
                    startup = json.loads((case / (name + '.startup.json')).read_text())
                    assert startup['native_window'] and startup['elapsed_ms'] < 10000, startup
                    assert (case / Path(startup['screenshot']).relative_to('/work')).stat().st_size > 5000
                    assert value['error'] is None, value
                    if command: assert value['result']['status'] == 'ok', value
                else: assert value['status'] == 'ok', value
                checks.append({'name': name, 'passed': True, 'wall_seconds': round(time.monotonic()-started, 3)})
                print('PASS', image, name, flush=True)
                return value

            invoke('x11-startup', gui=True)
            invoke('wayland-startup', gui=True, backend='wayland')
            invoke('offline-install', 'install', '--bundle', '/work/usb/baseline.zip', '--package', primary,
                   '--package', secondary, '--accept-licenses', gui=True)
            for component in inventory(root): verify_installed(root, initial[component])
            shutil.rmtree(download); (usb / 'baseline.zip').unlink()
            app = '/work/installation/wpilib-manager'
            invoke('installed-cached-verify', 'verify', implicit=True)
            updates = invoke('discover-offline-updates', 'check-updates', '--bundle', '/work/usb/candidate.zip')
            assert {u['id'] for u in updates['updates']} == {k for k in inventory(root) if initial[k]['version'] != current[k]['version']}
            invoke('offline-upgrade', 'update', '--bundle', '/work/usb/candidate.zip', '--accept-licenses', gui=True, backend='wayland')
            for component in inventory(root): verify_installed(root, current[component])
            (usb / 'candidate.zip').unlink()
            invoke('cached-remove', 'remove', '--package', secondary, '--accept-licenses', gui=True)
            assert secondary not in inventory(root)
            invoke('cached-reinstall', 'install', '--package', secondary, '--accept-licenses')
            verify_installed(root, current[secondary])
            invoke('final-cached-verify', 'verify', implicit=True)
            assert any(n.startswith('libX11') for n in loaded) and 'libwayland-client.so.0' in loaded and 'swrast_dri.so' in loaded, loaded
            assert not download.exists() and not list(usb.iterdir())
            evidence['images'].append({'image': image, 'image_id': identity['Id'], 'digests': identity['RepoDigests'],
                                       'checks': checks, 'loaded_libraries': sorted(loaded), 'inventory': inventory(root)})
            a.report.parent.mkdir(parents=True, exist_ok=True)
            a.report.write_text(json.dumps(evidence, indent=2)+'\n')
    finally:
        for server in servers:
            subprocess.run(['docker', 'stop', server], capture_output=True)


if __name__ == '__main__':
    p = argparse.ArgumentParser(description=__doc__)
    for name in ['app', 'engine', 'baseline', 'candidate', 'work', 'report']:
        p.add_argument('--'+name, type=Path, required=True)
    p.add_argument('--images', nargs='+', default=['ubuntu:22.04', 'ubuntu:24.04', 'ubuntu:26.04', 'debian:12'])
    check(p.parse_args())
