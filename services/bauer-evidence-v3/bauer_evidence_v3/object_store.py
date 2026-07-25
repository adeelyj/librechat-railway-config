from __future__ import annotations

import base64
import os
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from .ids import content_object_key, sha256_bytes


class ObjectIntegrityError(RuntimeError):
    pass


class ObjectStoreUnavailableError(RuntimeError):
    """A remote object store could not complete an availability operation."""


@dataclass(frozen=True, slots=True)
class StoredObject:
    sha256: str
    object_key: str
    byte_size: int
    media_type: str


class ContentAddressedObjectStore(Protocol):
    def put(self, content: bytes, *, media_type: str, suffix: str = "") -> StoredObject: ...

    def get(self, object_key: str) -> bytes: ...

    def exists(self, object_key: str) -> bool: ...


class MemoryObjectStore:
    def __init__(self) -> None:
        self._objects: dict[str, bytes] = {}

    def put(self, content: bytes, *, media_type: str, suffix: str = "") -> StoredObject:
        digest = sha256_bytes(content)
        key = content_object_key(digest, suffix=suffix)
        existing = self._objects.get(key)
        if existing is not None and existing != content:
            raise ObjectIntegrityError(f"content-address collision at {key}")
        self._objects[key] = bytes(content)
        return StoredObject(digest, key, len(content), media_type)

    def get(self, object_key: str) -> bytes:
        try:
            return self._objects[object_key]
        except KeyError as exc:
            raise FileNotFoundError(object_key) from exc

    def exists(self, object_key: str) -> bool:
        return object_key in self._objects


class LocalObjectStore:
    """Immutable local adapter for development and deterministic integration tests."""

    def __init__(self, root: str | Path) -> None:
        self.root = Path(root).resolve()
        self.root.mkdir(parents=True, exist_ok=True)

    def put(self, content: bytes, *, media_type: str, suffix: str = "") -> StoredObject:
        digest = sha256_bytes(content)
        key = content_object_key(digest, suffix=suffix)
        target = self._resolve_key(key)
        target.parent.mkdir(parents=True, exist_ok=True)
        if target.exists():
            existing = target.read_bytes()
            if sha256_bytes(existing) != digest or existing != content:
                raise ObjectIntegrityError(f"existing object failed integrity check: {key}")
        else:
            file_descriptor, temporary_name = tempfile.mkstemp(
                prefix=f".{digest[:12]}-",
                dir=target.parent,
            )
            temporary = Path(temporary_name)
            try:
                with os.fdopen(file_descriptor, "wb") as handle:
                    handle.write(content)
                    handle.flush()
                    os.fsync(handle.fileno())
                os.replace(temporary, target)
            finally:
                temporary.unlink(missing_ok=True)
        return StoredObject(digest, key, len(content), media_type)

    def get(self, object_key: str) -> bytes:
        target = self._resolve_key(object_key)
        content = target.read_bytes()
        expected = target.name.split(".", 1)[0]
        actual = sha256_bytes(content)
        if actual != expected:
            raise ObjectIntegrityError(f"object checksum mismatch: {object_key}")
        return content

    def exists(self, object_key: str) -> bool:
        return self._resolve_key(object_key).is_file()

    def _resolve_key(self, object_key: str) -> Path:
        if "\\" in object_key or object_key.startswith("/") or ".." in object_key.split("/"):
            raise ValueError("invalid object key")
        candidate = (self.root / Path(*object_key.split("/"))).resolve()
        try:
            candidate.relative_to(self.root)
        except ValueError as exc:
            raise ValueError("object key escapes object-store root") from exc
        return candidate


class S3ObjectStore:
    """S3-compatible immutable adapter with end-to-end SHA-256 verification."""

    def __init__(
        self,
        *,
        bucket: str,
        client=None,
        prefix: str = "",
        endpoint_url: str | None = None,
        region_name: str | None = None,
    ) -> None:
        if not bucket.strip():
            raise ValueError("bucket is required")
        normalized_prefix = prefix.strip("/")
        if normalized_prefix and (
            "\\" in normalized_prefix or ".." in normalized_prefix.split("/")
        ):
            raise ValueError("invalid S3 object prefix")
        if client is None:
            try:
                import boto3
            except ModuleNotFoundError as exc:  # pragma: no cover - deployment dependency
                raise RuntimeError("S3 object storage requires boto3") from exc
            client = boto3.client(
                "s3",
                endpoint_url=endpoint_url or None,
                region_name=region_name or None,
            )
        self.bucket = bucket
        self.client = client
        self.prefix = normalized_prefix

    def put(self, content: bytes, *, media_type: str, suffix: str = "") -> StoredObject:
        digest = sha256_bytes(content)
        key = self._key(content_object_key(digest, suffix=suffix))
        existing = self._head_or_none(key)
        if existing is None:
            try:
                self.client.put_object(
                    Bucket=self.bucket,
                    Key=key,
                    Body=content,
                    ContentType=media_type,
                    Metadata={"sha256": digest},
                    ChecksumSHA256=base64.b64encode(bytes.fromhex(digest)).decode("ascii"),
                )
                existing = self.client.head_object(Bucket=self.bucket, Key=key)
            except Exception as exc:
                raise ObjectStoreUnavailableError(
                    "S3 object write verification failed"
                ) from exc
        self._verify_head(key, digest, len(content), existing)
        return StoredObject(digest, key, len(content), media_type)

    def get(self, object_key: str) -> bytes:
        key = self._validate_key(object_key)
        try:
            response = self.client.get_object(Bucket=self.bucket, Key=key)
            body = response["Body"]
            content = body.read() if hasattr(body, "read") else bytes(body)
        except Exception as exc:
            if _is_not_found(exc):
                raise FileNotFoundError(key) from exc
            raise ObjectStoreUnavailableError("S3 object read failed") from exc
        expected = Path(key).name.split(".", 1)[0]
        if sha256_bytes(content) != expected:
            raise ObjectIntegrityError(f"object checksum mismatch: {key}")
        return content

    def exists(self, object_key: str) -> bool:
        return self._head_or_none(self._validate_key(object_key)) is not None

    def _key(self, content_key: str) -> str:
        return f"{self.prefix}/{content_key}" if self.prefix else content_key

    def _validate_key(self, key: str) -> str:
        if not key or "\\" in key or key.startswith("/") or ".." in key.split("/"):
            raise ValueError("invalid object key")
        if self.prefix and not key.startswith(f"{self.prefix}/"):
            raise ValueError("object key is outside the configured prefix")
        return key

    def _head_or_none(self, key: str):
        try:
            return self.client.head_object(Bucket=self.bucket, Key=key)
        except Exception as exc:
            if _is_not_found(exc):
                return None
            raise ObjectStoreUnavailableError("S3 object metadata read failed") from exc

    @staticmethod
    def _verify_head(key: str, digest: str, size: int, head) -> None:
        metadata = {
            str(name).casefold(): str(value).casefold()
            for name, value in (head.get("Metadata") or {}).items()
        }
        if metadata.get("sha256") != digest:
            raise ObjectIntegrityError(f"S3 object has missing or incorrect SHA-256 metadata: {key}")
        if int(head.get("ContentLength", -1)) != size:
            raise ObjectIntegrityError(f"S3 object size does not match immutable source: {key}")


class MirroredObjectStore:
    """Write to two independent stores before exposing an authoritative object reference."""

    def __init__(
        self,
        *,
        primary: ContentAddressedObjectStore,
        mirror: ContentAddressedObjectStore,
    ) -> None:
        self.primary = primary
        self.mirror = mirror

    def put(self, content: bytes, *, media_type: str, suffix: str = "") -> StoredObject:
        primary = self.primary.put(content, media_type=media_type, suffix=suffix)
        mirrored = self.mirror.put(content, media_type=media_type, suffix=suffix)
        if (
            primary.sha256 != mirrored.sha256
            or primary.byte_size != mirrored.byte_size
            or primary.object_key != mirrored.object_key
        ):
            raise ObjectIntegrityError("primary and mirror object identities diverged")
        return primary

    def get(self, object_key: str) -> bytes:
        try:
            return self.primary.get(object_key)
        except (
            FileNotFoundError,
            ObjectIntegrityError,
            ObjectStoreUnavailableError,
        ):
            return self.mirror.get(object_key)

    def exists(self, object_key: str) -> bool:
        return self.primary.exists(object_key) and self.mirror.exists(object_key)


def _is_not_found(exc: BaseException) -> bool:
    if isinstance(exc, FileNotFoundError):
        return True
    response = getattr(exc, "response", {})
    error = response.get("Error", {}) if isinstance(response, dict) else {}
    return str(error.get("Code", "")).casefold() in {
        "404",
        "nosuchkey",
        "notfound",
        "nosuchobject",
    }
