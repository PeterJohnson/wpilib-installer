"""Preserve upstream signature xattrs across Qt's data-only extraction operations."""
import base64
import ctypes
import hashlib
import os
from pathlib import Path
import shlex
import sys

from .common import safe_path


def archive_attributes(headers):
    result = {}
    for key, value in headers.items():
        if key.startswith('LIBARCHIVE.xattr.'):
            name = key.removeprefix('LIBARCHIVE.xattr.')
            if (not name.startswith('com.apple.cs.') and name not in ('com.apple.FinderInfo', 'com.apple.ResourceFork')):
                raise ValueError(f'Unsupported upstream macOS extended attribute: {name}')
            data = base64.b64decode(value + '=' * (-len(value) % 4), validate=True)
            if len(data) > 1024 * 1024:
                raise ValueError('Upstream extended attribute is too large')
            result[name] = data.hex()
    return result


def records(attributes):
    result = {}
    for path, values in attributes.items():
        safe_path(path)
        result[path] = {}
        for name, encoded in values.items():
            if not name.startswith('com.apple.cs.') and name not in ('com.apple.FinderInfo', 'com.apple.ResourceFork'):
                raise ValueError(f'Unsupported macOS extended attribute: {name}')
            data = bytes.fromhex(encoded)
            if len(data) > 1024 * 1024:
                raise ValueError('Extended attribute is too large')
            result[path][name] = {'sha256': hashlib.sha256(data).hexdigest(), 'size': len(data)}
    return result


def restore_script(attributes):
    records(attributes)  # Validate names, paths, and values before generating commands.
    lines = ['#!/bin/sh', 'set -eu', 'root=$1']
    for path, values in sorted(attributes.items()):
        for name, value in sorted(values.items()):
            lines.append('/usr/bin/xattr -wx ' + shlex.quote(name) + ' ' + shlex.quote(value)
                         + ' "$root"/' + shlex.quote(path))
    return '\n'.join(lines) + '\n'


def verify(path, expected):
    if sys.platform != 'darwin':
        raise ValueError('macOS signature metadata must be checked on macOS')
    # Darwin's getxattr ABI includes a position and options argument. Reading only
    # the declared names avoids treating OS-added quarantine/provenance as payload.
    libc = ctypes.CDLL(None, use_errno=True)
    get = libc.getxattr
    get.argtypes = [ctypes.c_char_p, ctypes.c_char_p, ctypes.c_void_p, ctypes.c_size_t, ctypes.c_uint32, ctypes.c_int]
    get.restype = ctypes.c_ssize_t
    for name, record in expected.items():
        size = get(os.fsencode(path), name.encode(), None, 0, 0, 1)  # XATTR_NOFOLLOW
        if size != record['size']:
            raise ValueError(f'Missing/changed macOS signature attribute: {path}: {name}')
        buffer = ctypes.create_string_buffer(size)
        actual = get(os.fsencode(path), name.encode(), buffer, size, 0, 1)
        if actual != size or hashlib.sha256(buffer.raw).hexdigest() != record['sha256']:
            raise ValueError(f'Changed macOS signature attribute: {path}: {name}')
