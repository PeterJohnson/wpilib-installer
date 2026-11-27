"""Native GUI startup and Rust/private-Qt offline lifecycle acceptance.
The Python driver is test tooling; every client operation launches the packaged
application in a separate directory with no Python/Cargo/IFW on its PATH.
"""
import argparse
from contextlib import nullcontext
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import zipfile

from installer_prototype.common import digest
from installer_prototype.manager import executable, inventory
from installer_prototype.offline_acceptance import windows_firewall
from installer_prototype.payload import verify_installed


def app_path(root):
    return root / ('WPILibManager.exe' if os.name == 'nt' else 'WPILib Manager.app/Contents/MacOS/wpilib-manager' if sys.platform == 'darwin' else 'wpilib-manager')


def unpack_manifest(bundle):
    with zipfile.ZipFile(bundle) as archive:
        return {p['id']: p for name in archive.namelist() if name.endswith('/release.json')
                for p in json.loads(archive.read(name))['signed']['packages']}


def worker(a):
    work = a.work.resolve()
    work.mkdir(parents=True)
    root = work / 'installation'
    initial, current = unpack_manifest(a.baseline), unpack_manifest(a.candidate)
    real = any(p.get('upstream') for p in current.values())
    primary = next(k for k in initial if k.endswith('.elastic'))
    secondary = next(k for k in initial if k.endswith('.advantagescope'))
    # Copy only the delivered app and browser ZIPs into an isolated user workspace.
    download = work / 'download'
    if sys.platform == 'darwin':
        shutil.copytree(a.app.resolve().parents[2], download / 'WPILib Manager.app')
    else:
        download.mkdir()
        shutil.copy2(a.app, app_path(download))
    media = work / 'usb'
    media.mkdir()
    baseline = media / 'baseline.zip'
    candidate = media / 'candidate.zip'
    shutil.copyfile(a.baseline, baseline)
    shutil.copyfile(a.candidate, candidate)
    application = app_path(download)
    # Reject accidental calls to python, cargo, binarycreator, repogen, etc.
    env = {k: v for k, v in os.environ.items() if not k.startswith(('PYTHON', 'CARGO', 'RUSTUP', 'VIRTUAL_ENV', 'WPILIB_ASSET'))}
    if sys.platform == 'linux':
        env.pop('WAYLAND_DISPLAY', None)  # native X11/Xvfb acceptance surface
    env['PATH'] = str(work / 'no-external-runtimes')
    env['QT_QPA_PLATFORM'] = 'offscreen'  # affects private Qt only; egui uses native windowing
    checks = []

    def cli(name, command, *extra, ok=True):
        result = subprocess.run([str(application), '--command', command, '--root', str(root), *map(str, extra)],
                                env=env, cwd=work, capture_output=True, text=True, timeout=900)
        (work / (name + '.log')).write_text(result.stderr, encoding='utf-8')
        try:
            value = json.loads(result.stdout)
        except ValueError:
            raise AssertionError(f'{name}: no JSON response ({result.returncode}): {result.stdout}\n{result.stderr}')
        if (result.returncode == 0) != ok or (value['status'] == 'ok') != ok:
            raise AssertionError(f'{name}: {value}\n{result.stderr}')
        checks.append({'name': name, 'passed': True, 'response': value})
        return value

    def gui(name, command=None, *extra, ok=True):
        report = work / (name + '.json')
        args = [str(application), '--root', str(root), '--ui-test-report', str(report)]
        if command:
            args += ['--ui-test-command', command]
        result = subprocess.run([*args, *map(str, extra)], env=env, cwd=work, capture_output=True, text=True, timeout=900)
        (work / (name + '.log')).write_text(result.stdout + result.stderr, encoding='utf-8')
        if result.returncode:
            raise AssertionError(f'{name}: GUI exited {result.returncode}: {result.stderr}')
        first = json.loads(report.with_suffix('.startup.json').read_text())
        final = json.loads(report.read_text())
        assert first['native_window'] and first['elapsed_ms'] < 10000, first
        assert first['size'][0] >= 640 and Path(first['screenshot']).stat().st_size > 5000
        assert final['native_window'] and (final['error'] is None) == ok, final
        if command and ok:
            assert final['result']['status'] == 'ok', final
        checks.append({'name': name, 'passed': True, 'startup_ms': first['elapsed_ms'], 'final': final})

    gui('startup')
    gui('invalid-media', 'verify', '--bundle', media / 'missing.zip', ok=False)
    # GUI event-loop automation invokes the same background handler as the buttons.
    gui('offline-install', 'install', '--bundle', baseline, '--package', primary, '--accept-licenses')
    assert root.joinpath('.wpilib-manager.json').exists()
    for p in inventory(root):
        verify_installed(root, initial[p])
    assert primary in inventory(root) and secondary not in inventory(root)
    # Delete both original application and baseline media. Maintenance must be self-contained.
    shutil.rmtree(download)
    baseline.unlink()
    application = app_path(root)
    gui('installed-startup')
    cli('cached-verification', 'verify')
    result = cli('discover-updates', 'check-updates', '--bundle', candidate)
    assert primary in {u['id'] for u in result['updates']}, result
    gui('offline-upgrade', 'update', '--bundle', candidate, '--accept-licenses')
    for p in inventory(root):
        verify_installed(root, current[p])
    upgraded = inventory(root)
    assert upgraded[primary] == current[primary]['version'] != initial[primary]['version']
    candidate.unlink()
    cli('cached-add-package', 'install', '--package', secondary, '--accept-licenses')
    verify_installed(root, current[secondary])
    # Remove a leaf and put it back using retained media, no original application or ZIPs.
    gui('cached-remove', 'remove', '--package', secondary, '--accept-licenses')
    assert secondary not in inventory(root)
    for name in current[secondary]['owned_files']:
        assert not (root / name).exists(), name
    cli('cached-reinstall', 'install', '--package', secondary, '--accept-licenses')
    verify_installed(root, current[secondary])
    if not real:
        cli('dependency-removal-rejected', 'remove', '--package', 'org.wpilib.runtime', '--accept-licenses', ok=False)
        cli('robotpy-profile', 'install', '--profile', 'python', '--accept-licenses')
        for name in ['org.robotpy.python', 'org.robotpy.sdk', 'org.robotpy.deploy']:
            verify_installed(root, current[name])
    saved_state = (root / '.wpilib-manager.json').read_bytes()
    cli('old-media-no-downgrade', 'update', '--bundle', a.baseline, '--accept-licenses')
    assert inventory(root)[primary] == current[primary]['version']
    assert (root / '.wpilib-manager.json').read_bytes() == saved_state
    cli('old-media-install-rejected', 'install', '--bundle', a.baseline, '--package', primary, '--accept-licenses', ok=False)
    # Cache is verified every time. An edited cache must not change Qt inventory or state.
    state = json.loads(saved_state)
    cache = root / '.wpilib-manager' / (state['bundle_sha256'] + '.zip')
    before = inventory(root)
    with cache.open('r+b') as f:
        original = f.read(1); f.seek(0); f.write(b'X')
    cli('corrupt-cache-rejected', 'verify', ok=False)
    with cache.open('r+b') as f:
        f.write(original)
    assert inventory(root) == before
    # A malicious outer archive must never escape its extraction directory.
    bad = work / 'bad.zip'
    with zipfile.ZipFile(bad, 'w') as z:
        z.writestr('../escaped', 'rejected')
    cli('zip-traversal-rejected', 'verify', '--bundle', bad, ok=False)
    # Signature rejection using a structurally intact bundle with one edited envelope.
    with zipfile.ZipFile(a.candidate) as src, zipfile.ZipFile(bad, 'w', zipfile.ZIP_STORED) as dst:
        for name in src.namelist():
            data = src.read(name)
            if name.endswith('/registry.json'):
                envelope = json.loads(data)
                envelope['signed']['revision'] += 1
                data = json.dumps(envelope).encode()
            dst.writestr(name, data)
    cli('invalid-registry-signature', 'verify', '--bundle', bad, ok=False)
    bad.unlink()
    assert inventory(root) == before and (root / '.wpilib-manager.json').read_bytes() == saved_state
    cli('final-cached-verification', 'verify')
    result = {'schema': 1, 'target': sys.platform, 'isolation': a.isolation, 'payload_kind': 'upstream-releases' if real else 'prototype-fixtures',
              'application_sha256': digest(application), 'checks': checks, 'inventory': inventory(root),
              'original_application_deleted': not download.exists(), 'original_media_deleted': not any(media.iterdir()),
              'external_runtime_path': env['PATH']}
    a.report.parent.mkdir(parents=True, exist_ok=True)
    a.report.write_text(json.dumps(result, indent=2), encoding='utf-8')
    print(f"Passed {len(checks)} native Rust manager checks ({a.isolation})", flush=True)


def main():
    p = argparse.ArgumentParser(description=__doc__)
    for name in ['app', 'baseline', 'candidate', 'work', 'report']:
        p.add_argument('--' + name, type=Path, required=True)
    p.add_argument('--worker', action='store_true')
    p.add_argument('--isolation', default='none-local-development')
    p.add_argument('--no-isolation', action='store_true')
    a = p.parse_args()
    if a.worker or a.no_isolation:
        worker(a)
        return
    args = [sys.executable, str(Path(__file__).resolve()), '--worker']
    for name in ['app', 'baseline', 'candidate', 'work', 'report']:
        args += ['--' + name, str(getattr(a, name).resolve())]
    context = nullcontext()
    if sys.platform == 'linux':
        prefix = ['sudo', '-n', 'unshare', '-n', '--', 'setpriv', '--reuid', str(os.getuid()), '--regid', str(os.getgid()), '--init-groups', 'env',
                  *(f'{k}={os.environ[k]}' for k in ['DISPLAY', 'XAUTHORITY', 'LD_LIBRARY_PATH', 'PYTHONPATH'] if k in os.environ)]
        args = [*prefix, *args, '--isolation', 'network-namespace']
    elif sys.platform == 'darwin':
        args = ['sandbox-exec', '-p', '(version 1) (allow default) (deny network*)', *args, '--isolation', 'sandbox-exec']
    elif os.name == 'nt':
        w = a.work.resolve()
        programs = [app_path(w / 'download'), app_path(w / 'installation'),
                    w / '.installation.qt-worker/engine.exe', w / 'installation/.wpilib-engine.exe']
        context = windows_firewall(programs)
        args += ['--isolation', 'program-firewall']
    else:
        raise ValueError('Unsupported platform')
    with context:
        subprocess.run(args, check=True, timeout=3600)


if __name__ == '__main__':
    main()
