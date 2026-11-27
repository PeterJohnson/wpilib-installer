"""Stdlib-only driver autostarted in a newly installed Ubuntu Desktop VM.
Python is preinstalled OS test tooling; the delivered manager runs with an empty PATH.
"""
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import time
import traceback
import xml.etree.ElementTree as ET
import zipfile

BASE = {'libc.so.6', 'libm.so.6', 'libpthread.so.0', 'libdl.so.2', 'librt.so.1',
        'libutil.so.1', 'libresolv.so.2', 'ld-linux-x86-64.so.2', 'libgcc_s.so.1'}


def sha(p): return hashlib.sha256(Path(p).read_bytes()).hexdigest()


def manifest(p):
    with zipfile.ZipFile(p) as z:
        return {p['id']: p for n in z.namelist() if n.endswith('/release.json')
                for p in json.loads(z.read(n))['signed']['packages']}


def inventory(root):
    return {p.findtext('Name'): p.findtext('Version') for p in ET.parse(root/'components.xml').getroot().findall('Package')}


def verify(root, packages):
    for component, version in inventory(root).items():
        p = packages[component]
        assert version == p['version'], (component, version, p['version'])
        for name in p['owned_files']:
            f = root/name; r = p['file_records'][name]
            if r['type'] == 'symlink': assert f.is_symlink() and os.readlink(f) == r['target'], name
            else:
                assert not f.is_symlink() and f.stat().st_size == r['size'] and sha(f) == r['sha256'], name
                assert bool(f.stat().st_mode & 0o111) == r['executable'], name


def main(shared):
    result = {'schema': 1, 'checks': [], 'session': {k: os.environ.get(k) for k in ['XDG_SESSION_TYPE','DISPLAY','WAYLAND_DISPLAY']},
              'application_sha256': sha(shared/'wpilib-manager'), 'os_release': Path('/etc/os-release').read_text(),
              'network_interfaces': sorted(p.name for p in Path('/sys/class/net').iterdir()),
              'test_tooling': 'OS Python stdlib only; no guest packages installed for tests; client PATH empty'}
    work = Path.home()/'wpilib-acceptance'; work.mkdir()
    (shared/'base-packages.txt').write_text(subprocess.check_output(['/usr/bin/dpkg-query','-W'],text=True))
    assert result['network_interfaces'] == ['lo'], result
    assert os.environ.get('WAYLAND_DISPLAY') and os.environ.get('DISPLAY'), result
    initial, current = manifest(shared/'baseline.zip'), manifest(shared/'candidate.zip')
    primary = next(n for n in initial if n.endswith('.elastic'))
    secondary = next(n for n in initial if n.endswith('.advantagescope'))
    env = {k:v for k,v in os.environ.items() if not k.startswith(('PYTHON','CARGO','RUSTUP','VIRTUAL_ENV','WPILIB_'))}
    for k in ['LD_LIBRARY_PATH','LIBGL_DRIVERS_PATH','LIBGL_ALWAYS_SOFTWARE','GALLIUM_DRIVER','__GLX_VENDOR_LIBRARY_NAME','__EGL_VENDOR_LIBRARY_FILENAMES','FONTCONFIG_FILE','FONTCONFIG_PATH']:
        env.pop(k, None)
    env['PATH'] = '/no-external-runtimes'
    result['client_environment'] = {k:v for k,v in env.items() if k in ['PATH','DISPLAY','WAYLAND_DISPLAY'] or k.startswith(('LD_','LIBGL','GALLIUM','__EGL','FONTCONFIG'))}
    for backend in ['wayland','x11']:
        case = work/backend; case.mkdir()
        download = case/'download'; download.mkdir()
        usb = case/'usb'; usb.mkdir()
        shutil.copy2(shared/'wpilib-manager',download/'wpilib-manager')
        shutil.copy2(shared/'baseline.zip',usb/'baseline.zip'); shutil.copy2(shared/'candidate.zip',usb/'candidate.zip')
        app = download/'wpilib-manager'; root = case/'installation'
        child_env = env.copy()
        child_env.pop('DISPLAY' if backend == 'wayland' else 'WAYLAND_DISPLAY',None)
        child_env['LD_DEBUG'] = 'libs'

        def invoke(name, command=None, *extra, gui=True, worker=False):
            label = backend+'-'+name
            path = shared/(label+'.json')
            trace = case/(name+'-loader')
            args = [str(app),'--root',str(root)]
            if gui:
                args += ['--ui-test-report',str(path)]
                if command:args += ['--ui-test-command',command]
            else:args += ['--command',command]
            start = time.monotonic()
            p = subprocess.run([*args,*map(str,extra)],env={**child_env,'LD_DEBUG_OUTPUT':str(trace)},
                               cwd=case,capture_output=True,text=True,timeout=300)
            (shared/(label+'.log')).write_text(p.stdout+'\n'+p.stderr)
            assert p.returncode == 0, (label,p.stdout,p.stderr)
            value = json.loads(path.read_text()) if gui else json.loads(p.stdout)
            if gui:
                first = json.loads(path.with_suffix('.startup.json').read_text())
                assert first['native_window'] and first['elapsed_ms'] < 30000, first
                assert Path(first['screenshot']).stat().st_size > 5000
                assert value['error'] is None, value
                if command:assert value['result']['status'] == 'ok',value
            else: assert value['status'] == 'ok',value
            manager_libs, worker_libs = set(), set()
            for log in case.glob(name+'-loader.*'):
                text = log.read_text()
                is_worker = bool(re.search(r'initialize program: .*?(?:/engine|/\.wpilib-engine)\s',text))
                paths = re.findall(r'calling init: (\S+)',text)
                for library in paths:
                    if is_worker:
                        if Path(library).name not in BASE:
                            assert '/wpilib-qt-runtime-' in library and '/lib/' in library, (label,library)
                        worker_libs.add(library)
                    else:
                        assert '/wpilib-qt-runtime-' not in library, (label,library)
                        assert library.startswith(('/lib/','/lib64/','/usr/lib/')), (label,library)
                        manager_libs.add(library)
                shutil.copy2(log,shared/(backend+'-'+log.name+'.log'))
            assert bool(worker_libs) == worker,(label,worker_libs)
            if gui:
                assert any(Path(p).name in ['libGL.so.1','libEGL.so.1'] for p in manager_libs),manager_libs
            result['checks'].append({'name':label,'passed':True,'wall_seconds':round(time.monotonic()-start,3),
                                     'manager_libraries':sorted(manager_libs),'worker_libraries':sorted(worker_libs)})
            (shared/'progress.json').write_text(json.dumps(result,indent=2))
            return value

        invoke('startup')
        invoke('offline-install','install','--bundle',usb/'baseline.zip','--package',primary,'--package',secondary,'--accept-licenses',worker=True)
        verify(root, initial)
        shutil.rmtree(download);(usb/'baseline.zip').unlink();app=root/'wpilib-manager'
        invoke('installed-startup')
        invoke('cached-verification','verify',gui=False)
        found=invoke('discover-updates','check-updates','--bundle',usb/'candidate.zip',gui=False)
        assert {p['id'] for p in found['updates']} == {n for n in inventory(root) if initial[n]['version'] != current[n]['version']}
        invoke('offline-upgrade','update','--bundle',usb/'candidate.zip','--accept-licenses',worker=True)
        verify(root,current);(usb/'candidate.zip').unlink()
        invoke('cached-remove','remove','--package',secondary,'--accept-licenses',worker=True)
        assert secondary not in inventory(root)
        invoke('cached-reinstall','install','--package',secondary,'--accept-licenses',gui=False,worker=True)
        verify(root,current)
        invoke('final-cached-verification','verify',gui=False)
        assert not download.exists() and not list(usb.iterdir())
    result['status']='passed'
    return result


if __name__ == '__main__':
    shared=Path('/mnt/wpilib-test')
    # An autostart entry can be invoked again after a session restart.
    if not (shared/'report.json').exists():
        try: result=main(shared)
        except BaseException:
            result={'status':'failed','error':traceback.format_exc()}
        tmp=shared/'report.json.tmp';tmp.write_text(json.dumps(result,indent=2)+'\n');tmp.replace(shared/'report.json')
