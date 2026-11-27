"""Build one native application with an embedded public bootstrap and private Qt engine.
Python, Cargo and IFW are build dependencies only. Never embeds signing keys.
"""
import argparse
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import zipfile
from installer_prototype.manager import build_installer
from installer_prototype.offline_acceptance import host_target
from installer_prototype.registry_updates import validate_source


def build(source, ifw, output, assets, cargo='cargo'):
    source, output, assets = (Path(p).resolve() for p in (source, output, assets))
    pin = json.loads(source.read_text())
    validate_source(pin)
    if pin['target'] != host_target():
        raise ValueError('Bootstrap must match the build host')
    assets.mkdir(parents=True, exist_ok=True)
    (assets / 'bootstrap.json').write_bytes(source.read_bytes())
    suffix = '.exe' if os.name == 'nt' else '.app' if sys.platform == 'darwin' else ''
    engine = build_installer(ifw, assets / ('engine' + suffix), maintenance_name='.wpilib-engine')
    with zipfile.ZipFile(assets / 'engine.zip', 'w', zipfile.ZIP_DEFLATED, compresslevel=1) as archive:
        for file in sorted(engine.rglob('*')) if engine.is_dir() else [engine]:
            if file.is_file():
                archive.write(file, file.relative_to(assets))
    env = {**os.environ, 'WPILIB_ASSET_DIR': str(assets)}
    subprocess.run([cargo, 'build', '--locked', '--release', '--manifest-path', 'manager/Cargo.toml'], env=env, check=True)
    binary = Path('manager/target/release/wpilib-manager' + ('.exe' if os.name == 'nt' else ''))
    if sys.platform == 'darwin':
        app = output / 'WPILib Manager.app'
        exe = app / 'Contents/MacOS/wpilib-manager'
        exe.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2('manager/Info.plist', app / 'Contents/Info.plist')
    else:
        exe = output / ('WPILibManager.exe' if os.name == 'nt' else 'wpilib-manager')
        exe.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(binary, exe)
    print(exe, flush=True)
    return exe


if __name__ == '__main__':
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--source', type=Path, required=True)
    p.add_argument('--ifw', type=Path, required=True)
    p.add_argument('--output', type=Path, default=Path('build/rust-manager/download'))
    p.add_argument('--assets', type=Path, default=Path('build/rust-manager/assets'))
    p.add_argument('--cargo', default='cargo')
    a = p.parse_args()
    build(a.source, a.ifw, a.output, a.assets, a.cargo)
