"""Small shared primitives. All subprocesses use argument arrays."""
from __future__ import annotations

import base64
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import subprocess
import tempfile
from urllib.parse import urlparse, quote
from urllib.request import HTTPRedirectHandler, build_opener

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey, Ed25519PublicKey

TARGETS = tuple(f"{os}-{arch}" for os in ("windows", "macos", "linux") for arch in ("x64", "arm64"))


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode()


def atomic_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(dir=path.parent, prefix=".pending-")
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(canonical(value) + b"\n")
        os.replace(name, path)
    finally:
        Path(name).unlink(missing_ok=True)


def digest(path):
    with Path(path).open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def safe_path(value):
    # Avoid Windows drive/ADS names and ambiguous URL escaping on every host.
    if not isinstance(value, str) or not value or any(c in value for c in "\\:%?#\x00"):
        raise ValueError(f"Unsafe relative path: {value!r}")
    p = PurePosixPath(value)
    if p.is_absolute() or any(x in ("", ".", "..") for x in value.split("/")):
        raise ValueError(f"Unsafe relative path: {value!r}")
    return p


def version(value):
    if not isinstance(value, str) or not re.fullmatch(r"\d+(?:\.\d+){0,3}", value):
        raise ValueError(f"Expected numeric IFW version, got {value!r}")
    parts = tuple(map(int, value.split(".")))
    return parts + (0,) * (4 - len(parts))


def keygen(path):
    path = Path(path)
    if path.exists():
        raise ValueError(f"Refusing to overwrite key {path}")
    key = Ed25519PrivateKey.generate()
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("xb") as stream:
        stream.write(key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                      serialization.NoEncryption()))
    path.chmod(0o600)
    return base64.b64encode(key.public_key().public_bytes_raw()).decode()


def sign(value, key_path):
    key = serialization.load_pem_private_key(Path(key_path).read_bytes(), password=None)
    return {"signed": value, "signature": base64.b64encode(key.sign(canonical(value))).decode()}


def public_key_from_private(key_path):
    key = serialization.load_pem_private_key(Path(key_path).read_bytes(), password=None)
    return base64.b64encode(key.public_key().public_bytes_raw()).decode()


def verify(envelope, public_key):
    try:
        key = Ed25519PublicKey.from_public_bytes(base64.b64decode(public_key, validate=True))
        key.verify(base64.b64decode(envelope["signature"], validate=True), canonical(envelope["signed"]))
    except Exception as exc:
        raise ValueError("Invalid manifest signature") from exc
    return envelope["signed"]


def check_url(url):
    parsed = urlparse(url)
    if parsed.scheme == "https" and parsed.hostname and not parsed.username:
        return
    if parsed.scheme == "http" and parsed.hostname in ("127.0.0.1", "localhost", "::1"):
        return
    if parsed.scheme == "file" and not parsed.netloc:
        return
    raise ValueError(f"Only HTTPS, local file URLs, and loopback HTTP are allowed: {url}")


def resource_url(base, path):
    safe_path(path)
    url = base.rstrip("/") + "/" + quote(path, safe="/")
    check_url(url)
    return url


class CheckedRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        check_url(newurl)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def urlopen(url, timeout):
    return build_opener(CheckedRedirect()).open(url, timeout=timeout)


def read_json_url(url):
    check_url(url)
    with urlopen(url, timeout=30) as response:
        check_url(response.geturl())
        data = response.read(8 * 1024 * 1024 + 1)
    if len(data) > 8 * 1024 * 1024:
        raise ValueError("Metadata exceeds 8 MiB limit")
    return json.loads(data)


def cached_file(base, relative, record, cache):
    expected, size = record["sha256"], record["size"]
    if not re.fullmatch(r"[0-9a-f]{64}", expected) or not isinstance(size, int) or size < 0:
        raise ValueError("Invalid file digest or size")
    cache = Path(cache)
    cache.mkdir(parents=True, exist_ok=True)
    dest = cache / expected
    if dest.exists():
        if dest.stat().st_size == size and digest(dest) == expected:
            return dest
        dest.unlink()
    fd, tmp = tempfile.mkstemp(dir=cache, prefix=".fetch-")
    try:
        url = resource_url(base, relative)
        with os.fdopen(fd, "wb") as out, urlopen(url, timeout=60) as response:
            check_url(response.geturl())
            hasher, length = hashlib.sha256(), 0
            while chunk := response.read(min(1024 * 1024, size - length + 1)):
                length += len(chunk)
                if length > size:
                    raise ValueError(f"Length mismatch: {relative}")
                hasher.update(chunk)
                out.write(chunk)
        if length != size or hasher.hexdigest() != expected:
            raise ValueError(f"Hash/length mismatch: {relative}")
        os.replace(tmp, dest)
    finally:
        Path(tmp).unlink(missing_ok=True)
    return dest


def run(args, **kwargs):
    env = dict(os.environ)
    env.setdefault("QT_QPA_PLATFORM", "offscreen")
    result = subprocess.run(list(map(str, args)), env=env, text=True, capture_output=True, **kwargs)
    if result.returncode:
        raise RuntimeError(f"Command failed ({result.returncode}): {args}\n{result.stdout}\n{result.stderr}")
    return result.stdout
