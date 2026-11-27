"""Collect the Linux runtime on the oldest supported build OS (Ubuntu 22.04).
Build-time tool only: the Qt dependency closure, fonts and notices are embedded.
The GUI uses host graphics; glibc and the OS loader stay host supplied.
"""
import argparse
import hashlib
import json
from pathlib import Path
import re
import shutil
import subprocess
import tempfile
import zipfile

BASE = {'libc.so.6', 'libm.so.6', 'libpthread.so.0', 'libdl.so.2', 'librt.so.1',
        'libutil.so.1', 'libresolv.so.2', 'ld-linux-x86-64.so.2'}


def run(*args):
    return subprocess.check_output(list(map(str, args)), text=True)


def digest(path):
    h = hashlib.sha256()
    with path.open('rb') as stream:
        for part in iter(lambda: stream.read(1024 * 1024), b''):
            h.update(part)
    return h.hexdigest()


def dependencies(path):
    output = run('ldd', path)
    if 'not found' in output:
        raise ValueError(f'Unresolved build dependency: {path}\n{output}')
    result = {}
    for line in output.splitlines():
        m = re.match(r'\s*(\S+) => (/\S+)', line)
        if m:
            result[m[1]] = Path(m[2])
    return result


def package(engine, output):
    if 'VERSION_ID="22.04"' not in Path('/etc/os-release').read_text():
        raise ValueError('Build the Linux runtime in the pinned Ubuntu 22.04 builder')
    libraries = {}
    pending = [Path(engine)]
    visited = set()
    while pending:
        p = pending.pop()
        if p.resolve() in visited:
            continue
        visited.add(p.resolve())
        for name, source in dependencies(p).items():
            if name not in BASE:
                libraries[name] = source.resolve()
                pending.append(source)
    forbidden = ('libLLVM', 'libGL.', 'libGLX', 'libEGL', 'libglapi', 'libgbm', 'libdrm', 'libicu', 'libxml2')
    if any(name.startswith(forbidden) for name in libraries):
        raise ValueError('Qt worker unexpectedly depends on a graphics driver stack')
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        (root / 'lib').mkdir()
        for name, source in sorted(libraries.items()):
            shutil.copy2(source, root / 'lib' / name)
        shutil.copytree('/usr/share/fonts/truetype/dejavu', root / 'share/fonts')
        (root / 'share/fonts.conf').write_text('''<?xml version="1.0"?>
<!DOCTYPE fontconfig SYSTEM "urn:fontconfig:fonts.dtd">
<fontconfig><dir prefix="relative">fonts</dir><cachedir prefix="xdg">fontconfig</cachedir></fontconfig>
''')
        # Preserve notices for all packages in the build environment, a superset
        # of the shipped runtime. The exact runtime files are hashed below.
        notices = root / 'licenses'
        notices.mkdir()
        for copyright_file in sorted(Path('/usr/share/doc').glob('*/copyright')):
            if copyright_file.is_file():
                shutil.copyfile(copyright_file, notices / (copyright_file.parent.name + '.copyright'))
        packages = run('dpkg-query', '-W', '-f=${binary:Package}\t${Version}\t${source:Package}\t${source:Version}\n')
        (notices / 'build-packages.tsv').write_text(packages)
        report = {'schema': 1, 'baseline': 'Ubuntu 22.04 / glibc 2.35, x86-64',
                  'base_libraries': sorted(BASE), 'scope': 'private headless Qt worker only',
                  'graphics': 'host OpenGL/EGL and X11/Wayland for GUI; no graphics required for CLI',
                  'files': {p.relative_to(root).as_posix(): {'sha256': digest(p), 'size': p.stat().st_size}
                            for p in sorted(root.rglob('*')) if p.is_file()}}
        (root / 'runtime-manifest.json').write_text(json.dumps(report, indent=2))
        output = Path(output)
        with zipfile.ZipFile(output, 'w', zipfile.ZIP_DEFLATED, compresslevel=6) as archive:
            for p in sorted(root.rglob('*')):
                if p.is_file():
                    archive.write(p, p.relative_to(root))
        output.with_suffix('.json').write_text(json.dumps(report, indent=2))
        print(f'Bundled {len(libraries)} private Qt worker libraries: {output.stat().st_size} bytes', flush=True)


if __name__ == '__main__':
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--engine', type=Path, required=True)
    p.add_argument('--output', type=Path, required=True)
    a = p.parse_args()
    package(a.engine, a.output)
