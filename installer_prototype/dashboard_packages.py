"""Pinned WPILib upstream dashboard releases packaged as independent Qt components."""
import argparse
import json
from pathlib import Path
import shutil
import struct
import sys
import tarfile

from .common import atomic_json, cached_file, run, safe_path, version
from .release_archive import extract


RECIPES = {
    'elastic': {'name': 'Elastic', 'executables': {
        'windows': 'elastic_dashboard.exe', 'linux': 'elastic_dashboard',
        'macos': 'elastic_dashboard.app/Contents/MacOS/elastic_dashboard'}},
    'advantagescope': {'name': 'AdvantageScope (WPILib)', 'executables': {
        'windows': 'AdvantageScope (WPILib).exe', 'linux': 'advantagescope-wpilib',
        'macos': 'AdvantageScope (WPILib).app/Contents/MacOS/AdvantageScope'}},
}


def download(asset, cache):
    # A checked-in size and digest pin are required even for already cached files.
    base, name = asset['url'].rsplit('/', 1)
    return cached_file(base, name, asset, cache)


def binary_architectures(path):
    with Path(path).open('rb') as stream:
        header = stream.read(64)
        if header[:4] == b'\x7fELF':
            endian = '<' if header[5] == 1 else '>'
            machine = struct.unpack_from(endian + 'H', header, 18)[0]
            return 'linux', {62: {'x64'}, 183: {'arm64'}}.get(machine, set())
        if header[:2] == b'MZ':
            stream.seek(struct.unpack_from('<I', header, 60)[0])
            pe = stream.read(6)
            if pe[:4] != b'PE\0\0':
                raise ValueError('Invalid PE executable')
            return 'windows', {0x8664: {'x64'}, 0xaa64: {'arm64'}}.get(struct.unpack_from('<H', pe, 4)[0], set())
        magic = header[:4]
        cpus = set()
        if magic in (b'\xca\xfe\xba\xbe', b'\xca\xfe\xba\xbf'):
            count = struct.unpack_from('>I', header, 4)[0]
            if not 1 <= count <= 16:
                raise ValueError('Invalid universal Mach-O architecture count')
            size = 32 if magic[-1] == 0xbf else 20
            stream.seek(8)
            cpus = {struct.unpack_from('>I', stream.read(size))[0] for _ in range(count)}
        elif magic == b'\xcf\xfa\xed\xfe':
            cpus = {struct.unpack_from('<I', header, 4)[0]}
        if cpus:
            return 'macos', {arch for cpu, arch in ((0x1000007, 'x64'), (0x100000c, 'arm64')) if cpu in cpus}
    raise ValueError(f'Unsupported native executable: {path}')


def verify_app(root, package, target):
    definition = package['upstream']
    exe = Path(root) / str(safe_path(definition['executable']))
    if not exe.resolve().is_relative_to(Path(root).resolve()):
        raise ValueError('Upstream executable escapes its installation')
    system, arch = target.split('-')
    actual_os, architectures = binary_architectures(exe)
    if actual_os != system or arch not in architectures:
        raise ValueError(f'Upstream executable does not support {target}: {exe}')
    if system == 'macos' and sys.platform == 'darwin':
        # Verify after repackaging and installation, without contacting Apple.
        app = next(p for p in exe.parents if p.suffix == '.app')
        run(['codesign', '--verify', '--deep', '--strict', app], timeout=120)
    return {'executable': definition['executable'], 'architectures': sorted(architectures),
            'codesign_verified': system == 'macos' and sys.platform == 'darwin'}


def package_release(lock_path, publisher, target, release, output, cache, *, season='2026', channel='real-packages', revision=None):
    lock = json.loads(Path(lock_path).read_text())
    if lock['schema'] != 1 or publisher not in RECIPES:
        raise ValueError('Unsupported dashboard recipe or lock schema')
    config, recipe = lock['publishers'][publisher], RECIPES[publisher]
    selected = next((r for r in config['releases'] if r['tag'] == release), None)
    if selected is None or target not in selected['assets']:
        raise ValueError('Release/target is not pinned in the upstream lock')
    version(selected['version'])
    revision = config['releases'].index(selected) + 1 if revision is None else revision
    if type(revision) is not int or revision < 1:
        raise ValueError('Publisher revision must be a positive integer')
    output = Path(output).resolve()
    if output.exists():
        raise ValueError('Package workspace must be new')
    asset = selected['assets'][target]
    archive = download(asset, cache)
    license_file = download(selected['license'], cache)
    prefix = f'tools/{publisher}'
    payload = output / 'payload'
    extract(archive, payload / prefix)
    # Retain all upstream runtime resources and notices; add the project license
    # and provenance outside the signed .app bundle.
    notice = payload / 'licenses' / publisher / 'LICENSE'
    notice.parent.mkdir(parents=True)
    shutil.copyfile(license_file, notice)
    executable = prefix + '/' + recipe['executables'][target.split('-')[0]]
    upstream = {'repository': config['repository'], 'tag': selected['tag'], 'asset': asset,
                'license': selected['license'], 'executable': executable}
    record = payload / 'licenses' / publisher / 'upstream.json'
    atomic_json(record, upstream)
    package = {'id': config['component'], 'name': recipe['name'], 'version': selected['version'],
               'description': f"Official WPILib distribution from {config['repository']} {selected['tag']}",
               'payload': 'payload', 'upstream': upstream, 'release_date': selected.get('release_date', '2026-11-26')}
    verify_app(payload, package, target)
    spec = {'publisher': publisher, 'target': target, 'season': season, 'channel': channel,
            'revision': revision, 'archive_format': 'tar.gz', 'packages': [package]}
    atomic_json(output / 'spec.json', spec)
    return output / 'spec.json'


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--lock', type=Path, default=Path('packaging/upstream-lock.json'))
    parser.add_argument('--publisher', choices=tuple(RECIPES), required=True)
    parser.add_argument('--target', required=True)
    parser.add_argument('--release', required=True, help='Exact upstream tag from the lock')
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--cache', type=Path, default=Path('build/upstream-cache'))
    parser.add_argument('--revision', type=int, help='Publisher repository revision; defaults to position in lock')
    parser.add_argument('--season', default='2026')
    parser.add_argument('--channel', default='real-packages')
    parser.add_argument('--release-tar', type=Path, help='Optional publisher workflow input artifact')
    args = parser.parse_args()
    spec = package_release(args.lock, args.publisher, args.target, args.release, args.output, args.cache,
                           season=args.season, channel=args.channel, revision=args.revision)
    if args.release_tar:
        if args.release_tar.resolve().is_relative_to(spec.parent):
            raise ValueError('Release tar must be outside its package directory')
        args.release_tar.parent.mkdir(parents=True, exist_ok=True)
        with tarfile.open(args.release_tar, 'w', dereference=False) as archive:
            for path in sorted(spec.parent.iterdir()):
                archive.add(path, arcname=path.name)
    print(spec)


if __name__ == '__main__':
    main()
