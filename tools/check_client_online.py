"""Native HTTP/TUF and offline-to-online lifecycle acceptance. Python is test tooling only."""
import argparse
from datetime import datetime, timedelta, timezone
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
import json
import math
import os
from pathlib import Path
import shutil
import subprocess
import sys
import threading
import time

from check_client_offline import app_path, unpack_manifest
from installer_tooling.common import atomic_json, digest
from installer_tooling.qt_engine import inventory
from installer_tooling.payload import verify_installed
from installer_tooling.tuf_repository import DirectoryStore, publish_catalog


def check(a):
    fixture = a.fixture.resolve()
    config = json.loads((fixture / 'online-fixture.json').read_text())
    home = Path(config['home'])
    source = json.loads((fixture / 'online-source.json').read_text())
    registry = json.loads((fixture / 'online-registry.json').read_text())
    origin = home / 'store' / source['target']
    def bundle_for(name):
        directory = fixture / name
        if config['real']:
            directory /= 'current'
        bundles = list(directory.glob('*.zip'))
        assert len(bundles) == 1, (name, bundles)
        return bundles[0]

    baseline, candidate = bundle_for('baseline'), bundle_for('candidate')
    initial, current = unpack_manifest(baseline), unpack_manifest(candidate)
    primary = next(k for k in initial if k.endswith('.elastic'))
    secondary = next(k for k in initial if k.endswith('.advantagescope'))
    work = a.work.resolve()
    work.mkdir(parents=True)
    root = work / 'installation'
    download = work / 'download'
    if sys.platform == 'darwin':
        shutil.copytree(a.app.resolve().parents[2], download / 'WPILib Manager.app')
    else:
        download.mkdir()
        shutil.copy2(a.app, app_path(download))
    application = app_path(download)
    env = {k: v for k, v in os.environ.items() if not k.startswith(('PYTHON', 'CARGO', 'RUSTUP', 'VIRTUAL_ENV', 'WPILIB_ASSET'))}
    env.pop('WAYLAND_DISPLAY', None)
    env.update(PATH=str(work / 'no-runtimes'), QT_QPA_PLATFORM='offscreen')
    checks, requests = [], []
    mode = {'status': None, 'path': None}

    class Handler(SimpleHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_GET(self):
            requests.append(self.path)
            if mode['status'] and (mode['path'] is None or mode['path'] in self.path):
                self.send_error(mode['status'])
            else:
                super().do_GET()

    server = ThreadingHTTPServer(('127.0.0.1', config['port']), partial(Handler, directory=str(home / 'store')))
    thread = threading.Thread(target=server.serve_forever, daemon=True)

    def report():
        atomic_json(a.report, {'schema': 1, 'target': source['target'], 'real_packages': config['real'],
                              'checks': checks, 'requests': requests, 'external_runtime_path': env['PATH'],
                              'application_sha256': digest(application), 'root_history': config['roots']})

    def record(name, response):
        checks.append({'name': name, 'passed': True, 'response': response})
        report()
        print('PASS', name, flush=True)
        return response

    last_cli_seconds = 0.0

    def cli(name, command='verify', *extra, ok=True, install_root=None, implicit=False):
        nonlocal last_cli_seconds
        started = time.monotonic()
        args = [str(application), '--command', command]
        if not implicit:
            args += ['--root', str(install_root or root)]
        p = subprocess.run([*args, *map(str, extra)], env=env, cwd=work, capture_output=True, text=True, timeout=900)
        last_cli_seconds = time.monotonic() - started
        (work / (name + '.log')).write_text(p.stdout + '\n' + p.stderr, encoding='utf-8')
        try:
            value = json.loads(p.stdout)
        except ValueError as e:
            raise AssertionError(f'{name}: {p.returncode}: {p.stdout}\n{p.stderr}') from e
        assert (p.returncode == 0) == ok and (value['status'] == 'ok') == ok, (name, value, p.stderr)
        record(name, value)
        checks[-1]['wall_seconds'] = round(last_cli_seconds, 3)
        report()
        return value

    def gui(name, command, *extra):
        path = work / (name + '.json')
        p = subprocess.run([str(application), '--root', str(root), '--ui-test-report', str(path),
                            '--ui-test-command', command, *map(str, extra)], env=env, cwd=work,
                           capture_output=True, text=True, timeout=900)
        (work / (name + '.log')).write_text(p.stdout + '\n' + p.stderr, encoding='utf-8')
        assert p.returncode == 0, (name, p.stderr)
        startup = json.loads(path.with_suffix('.startup.json').read_text())
        final = json.loads(path.read_text())
        assert startup['native_window'] and startup['elapsed_ms'] < 10000, startup
        assert Path(startup['screenshot']).stat().st_size > 5000
        assert final['error'] is None and final['result']['status'] == 'ok', final
        return record(name, {'startup_frame_ms': startup['elapsed_ms'], 'result': final['result']})

    registry_dir = origin / 'registry'
    keys = {role: list(map(Path, paths)) for role, paths in config['keys'].items()}
    good_record = json.loads((registry_dir / 'registry.json').read_text())

    def renew(record_value=None, **kw):
        return publish_catalog(DirectoryStore(registry_dir), source['tuf'], keys,
                               good_record if record_value is None else record_value, **kw)

    def changed(path, mutation, name, *, cached=False):
        old = path.read_bytes()
        try:
            path.write_bytes(mutation(old))
            value = cli(name, 'verify', '--cached' if cached else '--online', ok=False)
            assert 'root-update-pending' not in value['message'], value
        finally:
            path.write_bytes(old)

    try:
        gui('offline-install-no-server', 'install', '--bundle', baseline,
            '--package', primary, '--package', secondary, '--accept-licenses')
        assert not requests
        for component in inventory(root):
            verify_installed(root, initial[component])
        archived_state = json.loads((root / '.wpilib-manager.json').read_text())
        shutil.rmtree(download)
        application = app_path(root)
        thread.start()
        # Root catch-up is bounded per operation, resumable across processes.
        pending = cli('long-root-history-pending', 'check-updates', '--online', ok=False)
        assert 'root-update-pending' in pending['message'], pending
        found = cli('resume-expired-root-through-1030', 'check-updates', '--online', implicit=True)
        assert found['registry']['registry_revision'] == 2
        assert found['registry']['repositories'][0]['root_version'] == config['roots']
        assert {v['id'] for v in found['updates']} == {p for p in inventory(root) if initial[p]['version'] != current[p]['version']}
        assert all(p.endswith('Updates.xml') for p in requests if '/releases/' in p), requests
        if not config['real']:
            assert found['profiles']['community'] == ['org.community.example']
            assert 'org.community.example' in found['packages']
        gui('online-gui-discovery', 'verify', '--online')
        # Same process-independent high water survives key rotation and restarts.
        timestamp = registry_dir / 'tuf/metadata/timestamp.json'
        old_timestamp = timestamp.read_bytes()
        renew()
        cli('renewed-metadata-accepted', 'verify', '--online')
        changed(timestamp, lambda _: old_timestamp, 'timestamp-rollback-after-key-rotation')
        changed(timestamp, lambda b: b.replace(b'"version":3', b'"version":99999'), 'invalid-signature-no-fallback')
        changed(origin / 'elastic/tuf/metadata/timestamp.json', lambda _: timestamp.read_bytes(),
                'independent-publisher-signature-rejected')
        # A valid new timestamp referencing an unavailable snapshot establishes a
        # durable floor even though the refresh cannot complete.
        prior = timestamp.read_bytes()
        meta = renew()
        snapshot = registry_dir / f'tuf/metadata/{meta["tuf_revision"]}.snapshot.json'
        missing = snapshot.with_suffix('.saved')
        snapshot.rename(missing)
        try:
            cli('partial-refresh-missing-snapshot', 'verify', '--online', ok=False)
            changed(timestamp, lambda _: prior, 'partial-refresh-floor-retained')
        finally:
            missing.rename(snapshot)
        cli('partial-refresh-recovered', 'verify', '--online')
        # A newer TUF envelope may not lower the application registry revision.
        lower = json.loads(json.dumps(good_record))
        lower['signed']['revision'] = 1
        renew(lower)
        cli('authenticated-registry-rollback', 'verify', '--online', ok=False)
        mutated = json.loads(json.dumps(good_record))
        mutated['signed']['registry']['profiles']['unexpected'] = []
        renew(mutated)
        cli('same-registry-revision-mutation', 'verify', '--online', ok=False)
        renew()
        cli('registry-recovered', 'verify', '--online')
        # Payload integrity is checked before Qt can change the installation.
        repo = origin / 'elastic'
        release = json.loads((repo / 'releases/2/release.json').read_text())
        payload = next(name for name in release['signed']['files'] if name.startswith(primary + '/') and name.endswith(('.7z', '.tar.gz')) and not name.endswith('meta.7z'))
        file = repo / 'releases/2' / payload
        before = inventory(root)
        original = file.read_bytes()
        try:
            file.write_bytes(b'X' + original[1:])
            cli('bad-download-before-mutation', 'update', '--online', '--package', primary, '--accept-licenses', ok=False)
            assert inventory(root) == before
        finally:
            file.write_bytes(original)
        requests.clear()
        gui('offline-to-online-upgrade', 'update', '--online', '--accept-licenses')
        for component in inventory(root):
            verify_installed(root, current[component])
        assert not any('/robotpy/' in p and p.endswith('.7z') and not p.endswith('meta.7z') for p in requests)
        state = json.loads((root / '.wpilib-manager.json').read_text())
        assert state['bundle_sha256'] == archived_state['bundle_sha256']
        assert state['offline_registry'] == archived_state['registry']
        assert state['registry']['registry_revision'] == 2 and state['last_source'] == 'online'
        cli('installed-default-online-source', 'check-updates', implicit=True)
        # Only availability failures permit current-clock verified fallback.
        mode['status'] = 503
        fallback = cli('http-503-verified-cache', 'verify', '--online')
        assert fallback['registry']['status'] == 'verified-cache'
        mode.update(status=404, path='timestamp.json')
        cli('http-404-not-cache-fallback', 'verify', '--online', ok=False)
        mode.update(status=None, path=None)
        # Actual expired cached timestamp, followed by renewal from its historical floor.
        # A fresh timestamp must survive the measured root replay on slower native
        # runners. Still use the real clock and wait past its signed expiry.
        expires = datetime.now(timezone.utc) + timedelta(seconds=max(18, math.ceil(last_cli_seconds * 3 + 5)))
        renew(now=expires - timedelta(days=1), timestamp_days=1)
        cli('short-lived-cache-populated', 'verify', '--online')
        time.sleep(max(0, (expires - datetime.now(timezone.utc)).total_seconds()) + 1)
        cli('expired-cache-rejected', 'verify', '--cached', ok=False)
        renew()
        cli('expired-history-renewed', 'verify', '--online')
        cache = work / '.installation.online'
        tuf_dir = next(p.parent for p in (cache / 'tuf').glob('*/checkpoint.json')
                       if json.loads(p.read_text())['root.json']['signed']['version'] == config['roots'])
        changed(tuf_dir / 'roots/257.root.json', lambda b: b.replace(b'"version":257', b'"version":999'), 'poisoned-root-cache', cached=True)
        changed(tuf_dir / 'checkpoint.json', lambda b: b.replace(b'"version":1030', b'"version":1031'), 'poisoned-checkpoint', cached=True)
        head = json.loads((tuf_dir / 'current.json').read_text())
        proof = tuf_dir / 'generations' / head['generation'] / head['proof']
        changed(proof / 'metadata/timestamp.json', lambda b: b + b' ', 'poisoned-metadata-cache', cached=True)
        payload_hash = release['signed']['files'][payload]['sha256']
        payload_cache = cache / 'objects' / payload_hash
        # Metadata-only verification intentionally does not read all payloads.
        saved = payload_cache.read_bytes()
        try:
            payload_cache.write_bytes(b'X' + saved[1:])
            cli('corrupt-payload-cache-rejected', 'install', '--cached', '--package', primary, '--accept-licenses', ok=False)
        finally:
            payload_cache.write_bytes(saved)
        if not config['real']:
            cli('uncached-payload-fails-before-mutation', 'install', '--cached', '--profile', 'community', '--accept-licenses', ok=False)
            assert 'org.community.example' not in inventory(root)
            cli('dynamic-publisher-profile', 'install', '--online', '--profile', 'community', '--accept-licenses')
            assert (root / 'community.txt').read_text().startswith('Dynamically')
        # A fresh online installation also works from the same single executable.
        fresh = work / 'fresh-online'
        r = cli('fresh-online-root-catchup', 'install', '--online', '--package', primary, '--accept-licenses', install_root=fresh, ok=False)
        assert 'root-update-pending' in r['message']
        cli('fresh-online-installation', 'install', '--online', '--package', primary, '--accept-licenses', install_root=fresh)
        verify_installed(fresh, current[primary])
        server.shutdown()
        thread.join()
        server.server_close()
        refused = cli('connection-refused-verified-cache', 'verify', '--online')
        assert refused['registry']['status'] == 'verified-cache'
        gui('verified-cache-remove', 'remove', '--cached', '--package', primary, '--accept-licenses')
        cli('verified-cache-reinstall', 'install', '--cached', '--package', primary, '--accept-licenses')
        verify_installed(root, current[primary])
        # Archived media remains independently usable and cannot reset online floors.
        cli('archival-verification-after-online', 'verify', '--bundle', baseline)
        cli('online-floor-after-archival-use', 'verify', '--cached')
        before = inventory(root)
        sentinel = root / 'user-project.txt'
        sentinel.write_text('Preserved after uninstall')
        gui('online-installation-uninstall-offline', 'uninstall', '--accept-licenses')
        assert not inventory(root) and sentinel.read_text() == 'Preserved after uninstall'
        for component in before:
            package = current.get(component)
            if package:
                for name in package['owned_files']: assert not (root / name).exists(), name
        cli('reinstall-online-cache-after-uninstall', 'install', '--cached', '--package', primary, '--accept-licenses')
        verify_installed(root, current[primary])
    finally:
        if thread.is_alive():
            server.shutdown()
            thread.join()
        server.server_close()
        report()
    print(f'Passed {len(checks)} native online checks', flush=True)


if __name__ == '__main__':
    p = argparse.ArgumentParser(description=__doc__)
    for name in ['app', 'fixture', 'work', 'report']:
        p.add_argument('--' + name, type=Path, required=True)
    check(p.parse_args())
