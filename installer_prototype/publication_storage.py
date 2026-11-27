"""Origin storage backends used by the release workflows."""
from dataclasses import dataclass
import base64
import hashlib
import json
import os
from pathlib import Path
import re
from urllib.error import HTTPError
from urllib.parse import quote, urlencode, urlsplit, urlunsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener

from .common import canonical, digest, safe_path


class PublicationConflict(ValueError):
    """Another publication won, or an immutable name has different contents."""


@dataclass
class Object:
    data: bytes
    etag: str


def content_type(name):
    if name.endswith(".html"):
        return "text/html; charset=utf-8"
    if name.endswith(".json"):
        return "application/json"
    if name.endswith(".zip"):
        return "application/zip"
    return "application/octet-stream"


def content_info(value):
    return {"sha256": digest(value) if isinstance(value, Path) else hashlib.sha256(value).hexdigest(),
            "size": value.stat().st_size if isinstance(value, Path) else len(value)}


class S3Store:
    multipart_threshold = 64 * 1024**2
    multipart_part_size = 64 * 1024**2

    def __init__(self, config):
        import boto3
        from botocore.config import Config

        self.bucket = config["bucket"]
        self.prefix = str(safe_path(config["prefix"]))
        endpoint = config.get("endpoint_url")
        if endpoint and not endpoint.startswith("https://"):
            raise ValueError("Storage API endpoint must use HTTPS")
        self.client = boto3.client(
            "s3", endpoint_url=endpoint, region_name=config.get("region", "us-east-1"),
            config=Config(retries={"mode": "standard", "max_attempts": 3},
                          request_checksum_calculation="when_required",
                          response_checksum_validation="when_required"))

    def key(self, name):
        return self.prefix + "/" + str(safe_path(name))

    def reserved(self, name):
        return self.get(name)

    def publish_immutable(self, files):
        for name, value in files.items():
            self.put(name, value, immutable=True)

    def verify_immutable(self, files):
        for name, value in files.items():
            expected = content_info(value)
            info = self.client.head_object(Bucket=self.bucket, Key=self.key(name))
            if (info["ContentLength"] != expected["size"]
                    or info.get("Metadata", {}).get("sha256") != expected["sha256"]):
                raise ValueError(f"S3 upload verification failed: {name}")

    def get(self, name):
        from botocore.exceptions import ClientError

        try:
            response = self.client.get_object(Bucket=self.bucket, Key=self.key(name))
        except ClientError as exc:
            # AccessDenied, transport errors, and missing buckets are NOT empty feeds.
            if exc.response["Error"]["Code"] == "NoSuchKey":
                return None
            raise
        with response["Body"] as stream:
            data = stream.read(8 * 1024 * 1024 + 1)
        if len(data) > 8 * 1024 * 1024:
            raise ValueError("Origin metadata exceeds 8 MiB")
        return Object(data, response["ETag"])

    def put(self, name, value, *, previous=None, immutable=False):
        """Stream files; small mutable metadata is bytes. None means create-only."""
        from botocore.exceptions import ClientError

        is_file = isinstance(value, Path)
        size = value.stat().st_size if is_file else len(value)
        if size > 5 * 1024**4:
            raise ValueError("S3 upload exceeds the supported 5 TiB object limit")
        expected = digest(value) if is_file else hashlib.sha256(value).hexdigest()
        condition = {"IfMatch": previous} if previous is not None else {"IfNoneMatch": "*"}
        body = value.open("rb") if is_file else value
        try:
            metadata = {"ContentType": content_type(name), "Metadata": {"sha256": expected},
                        "CacheControl": "public,max-age=31536000,immutable" if immutable else "no-cache,max-age=0"}
            if is_file and size >= self.multipart_threshold:
                self.multipart(name, body, size, metadata, condition)
            else:
                md5 = hashlib.file_digest(body, "md5").digest() if is_file else hashlib.md5(value).digest()
                if is_file:
                    body.seek(0)
                self.client.put_object(
                    Bucket=self.bucket, Key=self.key(name), Body=body, ContentLength=size,
                    ContentMD5=base64.b64encode(md5).decode(), **metadata, **condition)
        except ClientError as exc:
            if exc.response["Error"]["Code"] not in ("PreconditionFailed", "ConditionalRequestConflict", "412", "409"):
                raise
            if not immutable:
                raise PublicationConflict("Origin head changed; rerun against the new head") from exc
            # An interrupted immutable upload may be retried only with identical bytes.
            response = self.client.get_object(Bucket=self.bucket, Key=self.key(name))
            hasher, length = hashlib.sha256(), 0
            with response["Body"] as stream:
                while chunk := stream.read(1024 * 1024):
                    length += len(chunk)
                    if length > size:
                        break
                    hasher.update(chunk)
            if length != size or hasher.hexdigest() != expected:
                raise PublicationConflict(f"Immutable object differs: {name}") from exc
        finally:
            if is_file:
                body.close()

    def multipart(self, name, stream, size, metadata, condition):
        """Bounded-memory uploads; the final completion retains the write condition."""
        args = {"Bucket": self.bucket, "Key": self.key(name)}
        upload = self.client.create_multipart_upload(**args, **metadata)["UploadId"]
        parts = []
        # S3/R2 allow at most 10,000 parts; round the size upward to whole MiB.
        part_size = max(self.multipart_part_size, ((size + 9999) // 10000 + 1024**2 - 1) // 1024**2 * 1024**2)
        try:
            while data := stream.read(part_size):
                number = len(parts) + 1
                result = self.client.upload_part(**args, UploadId=upload, PartNumber=number,
                    Body=data, ContentLength=len(data), ContentMD5=base64.b64encode(hashlib.md5(data).digest()).decode())
                parts.append({"PartNumber": number, "ETag": result["ETag"]})
            self.client.complete_multipart_upload(**args, UploadId=upload,
                                                  MultipartUpload={"Parts": parts}, **condition)
        except BaseException:
            try:
                self.client.abort_multipart_upload(**args, UploadId=upload)
            except Exception:
                # Preserve the original failure, including an ambiguous completion.
                pass
            raise


def https_origin(url):
    """Normalize one canonical API origin; reject ambiguous credential/path forms."""
    parsed = urlsplit(url)
    if (parsed.scheme != "https" or not parsed.hostname or parsed.username is not None
            or parsed.password is not None or parsed.query or parsed.fragment):
        raise ValueError("Storage API must be an HTTPS URL without credentials, query, or fragment")
    path = parsed.path.rstrip("/")
    if path:
        safe_path(path.lstrip("/"))
    host = parsed.hostname.lower()
    if ":" in host:
        host = f"[{host}]"
    if parsed.port not in (None, 443):
        host += f":{parsed.port}"
    return urlunsplit(("https", host, path, "", ""))


def publication_key(config):
    """Same destination => same Actions lock, regardless of config file or branch."""
    kind = config.get("type", "s3")
    prefix = str(safe_path(config["prefix"]))
    if kind == "artifactory":
        identity = [kind, https_origin(config["api_url"]), config["release_repository"], prefix]
    elif kind == "s3":
        identity = [kind, https_origin(config.get("endpoint_url") or "https://s3.amazonaws.com"),
                    config["bucket"], prefix]
    else:
        raise ValueError(f"Unknown publication storage type: {kind}")
    return hashlib.sha256(canonical(identity)).hexdigest()


def require_serialized_writer(config):
    """Guard the supported Actions entry point; this is NOT a distributed lock.

    Artifactory credentials must be restricted to the configured writer repository.
    Its trusted workflow holds the destination-based Actions concurrency group.
    """
    writer = config.get("writer_repository", "")
    if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", writer):
        raise ValueError("Artifactory requires an authoritative writer_repository")
    if (os.environ.get("GITHUB_ACTIONS") != "true"
            or os.environ.get("GITHUB_REPOSITORY", "").lower() != writer.lower()
            or os.environ.get("PUBLICATION_SERIALIZATION_KEY") != publication_key(config)):
        raise ValueError("Artifactory publication requires its configured serialized Actions writer")


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        # Never forward the bearer token to a login UI, CDN, or alternate host.
        return None


class ArtifactoryStore:
    """Generic Local staging -> release promotion under an external writer lock.

    Copy Item promotes the exact signed file set. No build-info search or whole
    repository copy is used. A read-before-write check detects changed heads, but
    does not replace the required Actions serialization / single-writer policy.
    """
    def __init__(self, config):
        require_serialized_writer(config)
        self.config = config
        self.api = https_origin(config["api_url"])
        self.prefix = str(safe_path(config["prefix"]))
        self.staging = config["staging_repository"]
        self.release = config["release_repository"]
        for repository in (self.staging, self.release):
            if not re.fullmatch(r"[A-Za-z][A-Za-z0-9._-]*", repository):
                raise ValueError("Invalid Artifactory repository key")
        if self.staging == self.release:
            raise ValueError("Staging and release repositories must be distinct")
        self.token = os.environ.pop("ARTIFACTORY_ACCESS_TOKEN", "")
        if not self.token.strip() or any(c in self.token for c in "\r\n"):
            raise ValueError("Missing or invalid ARTIFACTORY_ACCESS_TOKEN")
        self.opener = build_opener(NoRedirect())
        self.promoted = False
        # Check repository existence/access before interpreting an artifact 404.
        for repository in (self.staging, self.release):
            info = self.request("GET", f"api/storage/{repository}")
            if json.loads(info).get("repo") != repository:
                raise ValueError("Artifactory repository identity mismatch")

    def request(self, method, path, value=None, *, missing=False, query=None, headers=None):
        url = self.api + "/" + path
        if query:
            url += "?" + urlencode(query)
        request_headers = {"Authorization": "Bearer " + self.token, "Accept": "application/json",
                           "Cache-Control": "no-cache"}
        request_headers.update(headers or {})
        body = value.open("rb") if isinstance(value, Path) else value
        try:
            request = Request(url, data=body, method=method, headers=request_headers)
            try:
                with self.opener.open(request, timeout=120) as response:
                    data = response.read(8 * 1024 * 1024 + 1)
                    if len(data) > 8 * 1024 * 1024:
                        raise ValueError("Artifactory metadata exceeds 8 MiB")
                    return data
            except HTTPError as exc:
                code = exc.code
                exc.close()
                if missing and code == 404:
                    return None
                # Server response text may contain credentials; do not log it.
                raise ValueError(f"Artifactory {method} failed: HTTP {code}") from None
        finally:
            if isinstance(value, Path):
                body.close()

    def path(self, repository, name):
        return repository + "/" + quote(self.prefix + "/" + str(safe_path(name)), safe="/")

    def read(self, repository, name):
        data = self.request("GET", self.path(repository, name), missing=True)
        # The token is a content fingerprint, not a claim of conditional PUT support.
        return None if data is None else Object(data, hashlib.sha256(data).hexdigest())

    def get(self, name):
        return self.read(self.release, name)

    def reserved(self, name):
        return self.get(name) or self.read(self.staging, name)

    def info(self, repository, name):
        data = self.request("GET", "api/storage/" + self.path(repository, name), missing=True)
        if data is None:
            return None
        info = json.loads(data)
        checksum = info.get("checksums", {}).get("sha256")
        size = info.get("size")
        if (not isinstance(checksum, str) or not re.fullmatch(r"[0-9a-f]{64}", checksum)
                or isinstance(size, bool) or not str(size).isdigit()):
            raise ValueError("Artifactory file metadata lacks a SHA-256 or valid size")
        return {"sha256": checksum, "size": int(size)}

    content_info = staticmethod(content_info)

    def verify_immutable(self, files):
        for name, value in files.items():
            if self.info(self.release, name) != content_info(value):
                raise ValueError("Artifactory promoted revision is incomplete or corrupt")

    def upload(self, repository, name, value, expected):
        self.request("PUT", self.path(repository, name), value, headers={
            "Content-Length": str(expected["size"]), "X-Checksum-Sha256": expected["sha256"],
            "Content-Type": content_type(name)})
        if self.info(repository, name) != expected:
            raise ValueError("Artifactory upload verification failed")

    def publish_immutable(self, files):
        require_serialized_writer(self.config)
        self.promoted = False
        if not files:
            raise ValueError("No revision files to promote")
        expected = {}
        for name, value in files.items():
            safe_path(name)
            if not re.fullmatch(r"(?:releases/[1-9][0-9]*/.+|revisions/[1-9][0-9]*\.json|bundles/[0-9a-f]{64}/.+|tuf/metadata/[1-9][0-9]*\.(?:root|targets|snapshot|timestamp)\.json|tuf/targets/[0-9a-f]{64}\.catalog\.json)", name):
                raise ValueError("Only immutable revision files may be staged/promoted")
            expected[name] = self.content_info(value)
        # Complete staging validation before touching the public repository.
        for name, value in files.items():
            existing = self.info(self.staging, name)
            if existing is not None and existing != expected[name]:
                raise PublicationConflict(f"Immutable staged object differs: {name}")
            if existing is None:
                self.upload(self.staging, name, value, expected[name])
        # Reject a collision anywhere before starting the copy operations.
        for name in files:
            existing = self.info(self.release, name)
            if existing is not None and existing != expected[name]:
                raise PublicationConflict(f"Immutable release object differs: {name}")
        for name in files:
            if self.info(self.release, name) == expected[name]:
                continue
            result = json.loads(self.request("POST", "api/copy/" + self.path(self.staging, name),
                query={"to": "/" + self.release + "/" + self.prefix + "/" + name,
                       "suppressLayouts": "1", "failFast": "1"}))
            if result.get("errors") or any(m.get("level", "").upper() == "ERROR" for m in result.get("messages", [])):
                raise ValueError("Artifactory promotion reported an error")
        self.verify_immutable(files)
        self.promoted = True

    def put(self, name, value, *, previous=None, immutable=False):
        require_serialized_writer(self.config)
        if immutable or name not in ("index.json", "registry.json", "index.html", "tuf/metadata/timestamp.json") or not self.promoted:
            raise ValueError("Publish and verify an immutable revision before advancing its head")
        current = self.get(name)
        if (current.etag if current else None) != previous:
            raise PublicationConflict("Artifactory head changed; inspect competing writers before retrying")
        self.upload(self.release, name, value, self.content_info(value))
        self.promoted = False


def storage_from_config(config):
    kind = config.get("type", "s3")
    if kind == "s3":
        return S3Store(config)
    if kind == "artifactory":
        return ArtifactoryStore(config)
    raise ValueError(f"Unknown publication storage type: {kind}")

