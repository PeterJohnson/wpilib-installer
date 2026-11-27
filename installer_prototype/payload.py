"""Portable payload inventories and contained relative symlinks for native apps."""
import os
from pathlib import Path, PurePosixPath
import re
import stat

from .common import digest, safe_path


def link_destination(name, target):
    if (not isinstance(target, str) or not target or target.startswith('/')
            or any(c in target for c in '\\:%?#\x00')):
        raise ValueError(f'Unsafe symlink: {name}')
    parts = list(PurePosixPath(name).parent.parts)
    for part in target.split('/'):
        if part == '..':
            if not parts:
                raise ValueError(f'Escaping symlink: {name}')
            parts.pop()
        elif part not in ('', '.'):
            parts.append(part)
    return '/'.join(parts)


def validate_ownership(names):
    folded = set()
    for name in names:
        safe_path(name)
        if name.casefold() in folded:
            raise ValueError(f'Conflicting ownership: {name}')
        folded.add(name.casefold())
    for name in folded:
        if any(parent.as_posix() in folded for parent in PurePosixPath(name).parents if str(parent) != '.'):
            raise ValueError(f'Payload ownership is nested beneath a file or symlink: {name}')


def validate_records(records):
    for name, record in records.items():
        safe_path(name)
        if record.get('type') == 'symlink':
            link_destination(name, record['target'])
        elif record.get('type') == 'file':
            if (not re.fullmatch(r'[0-9a-f]{64}', record.get('sha256', ''))
                    or type(record.get('size')) is not int or record['size'] < 0
                    or type(record.get('executable')) is not bool):
                raise ValueError(f'Invalid payload record: {name}')
        else:
            raise ValueError(f'Unsupported payload entry: {name}')
        if any(parent.as_posix() in records for parent in PurePosixPath(name).parents if str(parent) != '.'):
            raise ValueError(f'Payload entry is nested beneath a file or symlink: {name}')


def entry(root, name):
    root = Path(root).resolve()
    path = root / str(safe_path(name))
    if not path.parent.resolve().is_relative_to(root):
        raise ValueError(f'Escaping payload parent: {name}')
    mode = path.lstat().st_mode
    if stat.S_ISLNK(mode):
        target = os.readlink(path)
        link_destination(name, target)
        try:
            resolved = path.resolve(strict=True)
        except (OSError, RuntimeError) as exc:
            raise ValueError(f'Dangling or cyclic payload symlink: {name}') from exc
        if not resolved.is_relative_to(root):
            raise ValueError(f'Escaping payload symlink: {name}')
        return {'type': 'symlink', 'target': target}
    if not stat.S_ISREG(mode):
        raise ValueError(f'Payload is not a regular file: {name}')
    return {'type': 'file', 'sha256': digest(path), 'size': path.stat().st_size,
            'executable': bool(mode & 0o111) if os.name != 'nt' else False}


def inventory(root):
    records = {}
    for directory, dirs, files in os.walk(root, followlinks=False):
        for name in [*files, *(d for d in dirs if (Path(directory) / d).is_symlink())]:
            relative = (Path(directory) / name).relative_to(root).as_posix()
            records[relative] = entry(root, relative)
    validate_records(records)
    return dict(sorted(records.items()))


def verify_installed(root, package):
    records = {}
    for name in package['owned_files']:
        actual = entry(root, name)
        expected = package.get('file_records', {}).get(name)
        if expected is not None and actual != expected:
            raise ValueError(f'Installed payload differs from signed manifest: {name}')
        if expected is None and actual['type'] != 'file':
            raise ValueError(f'Unexpected installed symlink: {name}')
        records[name] = actual
    return records
