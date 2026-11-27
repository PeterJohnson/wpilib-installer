"""Extract verified release archives without traversing links or escaping paths."""
import os
from pathlib import Path, PurePosixPath
import shutil
import stat
import tarfile
import zipfile

from .common import safe_path
from .payload import inventory, link_destination
from .macos_metadata import archive_attributes


def extract(archive, destination):
    destination = Path(destination)
    if destination.exists():
        raise ValueError('Release extraction needs a fresh directory')
    try:
        return _extract(archive, destination)
    except Exception:
        shutil.rmtree(destination, ignore_errors=True)
        raise


def _extract(archive, destination):
    destination = Path(destination)
    if destination.exists():
        raise ValueError('Release extraction needs a fresh directory')
    is_zip = zipfile.is_zipfile(archive)
    with (zipfile.ZipFile(archive) if is_zip else tarfile.open(archive)) as stream:
        entries = []
        attributes = {}
        for member in (stream.infolist() if is_zip else stream.getmembers()):
            name = (member.filename if is_zip else member.name).rstrip('/')
            safe_path(name)
            if is_zip:
                mode = member.external_attr >> 16
                kind = 'dir' if member.is_dir() else 'link' if stat.S_ISLNK(mode) else 'file'
                if stat.S_IFMT(mode) not in (0, stat.S_IFREG, stat.S_IFDIR, stat.S_IFLNK):
                    raise ValueError('Unsupported release archive member')
                target = stream.read(member).decode('utf-8') if kind == 'link' else None
                opener = stream.open
            else:
                mode = member.mode
                kind = 'dir' if member.isdir() else 'link' if member.issym() else 'file' if member.isfile() else None
                if kind is None:
                    raise ValueError('Release archives cannot contain hardlinks or special files')
                target = member.linkname if kind == 'link' else None
                attrs = archive_attributes(member.pax_headers)
                if attrs:
                    if kind != 'file':
                        raise ValueError('Extended attributes require a regular payload file')
                    attributes[name] = attrs
                opener = stream.extractfile
            if kind == 'link':
                link_destination(name, target)
            entries.append((name, kind, mode, target, member))
        filtered = []
        for item in entries:
            name, kind, _, _, member = item
            path = PurePosixPath(name)
            counterpart = (path.parent / path.name[2:]).as_posix() if path.name.startswith('._') else None
            if counterpart in attributes:
                if kind != 'file':
                    raise ValueError('Invalid AppleDouble metadata entry')
                with opener(member) as metadata:
                    if metadata.read(4) != b'\x00\x05\x16\x07':
                        raise ValueError('Invalid AppleDouble metadata header')
                continue
            filtered.append(item)
        entries = filtered
        names = {}
        for name, kind, *_ in entries:
            folded = name.casefold()
            if folded in names:
                raise ValueError('Duplicate release archive member')
            names[folded] = kind
        for name, *_ in entries:
            for parent in PurePosixPath(name).parents:
                if str(parent) != '.' and names.get(parent.as_posix().casefold(), 'dir') != 'dir':
                    raise ValueError('Archive member is nested beneath a file or symlink')
        namespace = {name for name, *_ in entries}
        namespace.update(parent.as_posix() for name, *_ in entries for parent in PurePosixPath(name).parents)
        links = {name: target for name, kind, _, target, _ in entries if kind == 'link'}
        def resolve(name, seen=frozenset()):
            parts = name.split('/')
            for i in range(1, len(parts) + 1):
                prefix = '/'.join(parts[:i])
                if prefix in links:
                    if prefix in seen:
                        raise ValueError('Cyclic archive symlink')
                    rewritten = '/'.join(filter(None, [link_destination(prefix, links[prefix]), *parts[i:]]))
                    return resolve(rewritten, seen | {prefix})
            if name not in namespace and name != '':
                raise ValueError(f'Dangling archive symlink: {name}')
        for name in links:
            resolve(name)
        destination.mkdir(parents=True)
        # Make links last; no archive data is ever written through a symlink.
        for name, kind, mode, target, member in entries:
            path = destination / name
            if kind == 'dir':
                path.mkdir(parents=True, exist_ok=True)
            elif kind == 'file':
                path.parent.mkdir(parents=True, exist_ok=True)
                with opener(member) as source, path.open('xb') as out:
                    shutil.copyfileobj(source, out)
                path.chmod((mode & 0o777) or 0o644)
        for name, kind, mode, target, _ in entries:
            if kind == 'link':
                path = destination / name
                path.parent.mkdir(parents=True, exist_ok=True)
                os.symlink(target, path)
        inventory(destination)  # Reject dangling/cyclic/indirectly escaping links.
        return attributes
