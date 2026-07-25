"""Deterministic manifests for immutable, original Bauer source files.

The manifest is deliberately a boundary between source acquisition and the V3
compiler.  It describes only original source bytes.  Parsed text, Markdown
exports, summaries, OCR output, and other derivatives belong in compiler
artifacts and must never enter this manifest.

All paths are canonical, relative POSIX paths under an explicitly supplied
source root.  Manifest creation and verification resolve those paths and reject
escapes, missing files, and byte drift.  Object-store staging re-verifies each
file before writing it to a content-addressed store.
"""

from __future__ import annotations

import hmac
import json
import re
import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path, PurePosixPath
from types import MappingProxyType
from typing import Any

from .ids import canonical_json, sha256_bytes, sha256_json
from .object_store import ContentAddressedObjectStore, ObjectIntegrityError
from .postgres_admin import COMPILABLE_SOURCE_TYPES, CompileJobSpec


# The registry schema reserves additional source types for future compilers.
# A release manifest may only claim formats this compiler can currently turn
# into source-native, provenance-bearing canonical evidence.
COMPILER_SOURCE_TYPES = COMPILABLE_SOURCE_TYPES


SCHEMA_VERSION = 1
MAX_SOURCE_COUNT = 100_000
MAX_MANIFEST_BYTES = 64 * 1024 * 1024
MAX_LOGICAL_PATH_LENGTH = 2_048
MAX_METADATA_BYTES = 64 * 1024

DATABASE_ID_NAMESPACE = uuid.UUID("b8c80114-5b53-56cf-8a22-405432e05930")
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_MEDIA_TYPE_RE = re.compile(
    r"^[a-z0-9][a-z0-9!#$&^_.+-]*/[a-z0-9][a-z0-9!#$&^_.+-]*$"
)
_UTC_TIMESTAMP_RE = re.compile(
    r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{1,6})?Z$"
)

_EXTENSION_MEDIA_TYPES = {
    ".csv": "text/csv",
    ".doc": "application/msword",
    ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    ".gif": "image/gif",
    ".htm": "text/html",
    ".html": "text/html",
    ".jpeg": "image/jpeg",
    ".jpg": "image/jpeg",
    ".json": "application/json",
    ".md": "text/markdown",
    ".pdf": "application/pdf",
    ".png": "image/png",
    ".ppt": "application/vnd.ms-powerpoint",
    ".pptx": "application/vnd.openxmlformats-officedocument.presentationml.presentation",
    ".rtf": "application/rtf",
    ".tif": "image/tiff",
    ".tiff": "image/tiff",
    ".txt": "text/plain",
    ".xls": "application/vnd.ms-excel",
    ".xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    ".xml": "application/xml",
}


class SourceManifestError(RuntimeError):
    """Base class for source-manifest failures."""


class SourceManifestValidationError(SourceManifestError, ValueError):
    """The manifest or source specification violates the immutable schema."""


class SourceManifestIntegrityError(SourceManifestError):
    """Source or staged bytes no longer match their recorded identity."""


@dataclass(frozen=True, slots=True)
class SourceSpec:
    """Input specification used to create one original-source manifest entry."""

    external_file_id: str
    logical_path: str
    source_type: str
    declared_media_type: str | None = None
    visibility: str = "inherited"
    category_path: tuple[str, ...] = ()
    metadata: Mapping[str, Any] = field(default_factory=dict)
    is_original: bool = True
    is_derivative: bool = False

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "external_file_id",
            _required_text(self.external_file_id, "external_file_id"),
        )
        object.__setattr__(self, "logical_path", _logical_path(self.logical_path))
        object.__setattr__(self, "source_type", _source_type(self.source_type))
        object.__setattr__(
            self,
            "declared_media_type",
            _optional_media_type(self.declared_media_type, "declared_media_type"),
        )
        object.__setattr__(self, "visibility", _visibility(self.visibility))
        object.__setattr__(
            self,
            "category_path",
            _category_path(self.category_path),
        )
        object.__setattr__(self, "metadata", _frozen_json_object(self.metadata))
        _require_original(self.is_original, self.is_derivative)


@dataclass(frozen=True, slots=True)
class SourceManifestEntry:
    """One immutable original source version recorded by a manifest."""

    external_file_id: str
    logical_path: str
    sha256: str
    byte_size: int
    detected_media_type: str
    declared_media_type: str | None
    source_type: str
    visibility: str
    category_path: tuple[str, ...]
    metadata: Mapping[str, Any] = field(default_factory=dict)
    is_original: bool = True
    is_derivative: bool = False

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "external_file_id",
            _required_text(self.external_file_id, "external_file_id"),
        )
        object.__setattr__(self, "logical_path", _logical_path(self.logical_path))
        digest = str(self.sha256).casefold()
        if not _SHA256_RE.fullmatch(digest):
            raise SourceManifestValidationError(
                "sha256 must contain exactly 64 lowercase hexadecimal characters"
            )
        object.__setattr__(self, "sha256", digest)
        if (
            isinstance(self.byte_size, bool)
            or not isinstance(self.byte_size, int)
            or self.byte_size < 0
        ):
            raise SourceManifestValidationError(
                "byte_size must be a non-negative integer"
            )
        object.__setattr__(
            self,
            "detected_media_type",
            _media_type(self.detected_media_type, "detected_media_type"),
        )
        object.__setattr__(
            self,
            "declared_media_type",
            _optional_media_type(self.declared_media_type, "declared_media_type"),
        )
        object.__setattr__(self, "source_type", _source_type(self.source_type))
        object.__setattr__(self, "visibility", _visibility(self.visibility))
        object.__setattr__(
            self,
            "category_path",
            _category_path(self.category_path),
        )
        object.__setattr__(self, "metadata", _frozen_json_object(self.metadata))
        _require_original(self.is_original, self.is_derivative)

    def to_dict(self) -> dict[str, Any]:
        return {
            "external_file_id": self.external_file_id,
            "logical_path": self.logical_path,
            "sha256": self.sha256,
            "byte_size": self.byte_size,
            "detected_media_type": self.detected_media_type,
            "declared_media_type": self.declared_media_type,
            "source_type": self.source_type,
            "visibility": self.visibility,
            "category_path": list(self.category_path),
            "metadata": _thaw_json(self.metadata),
            "is_original": self.is_original,
            "is_derivative": self.is_derivative,
        }


@dataclass(frozen=True, slots=True)
class SourceManifest:
    """An immutable, self-checksummed release source manifest."""

    tenant_id: str
    knowledge_base_id: str
    release_id: str
    source_count: int
    sources: tuple[SourceManifestEntry, ...]
    created_at: str | None = None
    schema_version: int = SCHEMA_VERSION

    def __post_init__(self) -> None:
        if self.schema_version != SCHEMA_VERSION:
            raise SourceManifestValidationError(
                f"unsupported manifest schema_version: {self.schema_version}"
            )
        object.__setattr__(self, "tenant_id", _uuid(self.tenant_id, "tenant_id"))
        object.__setattr__(
            self,
            "knowledge_base_id",
            _uuid(self.knowledge_base_id, "knowledge_base_id"),
        )
        object.__setattr__(self, "release_id", _uuid(self.release_id, "release_id"))
        if (
            isinstance(self.source_count, bool)
            or not isinstance(self.source_count, int)
            or not 1 <= self.source_count <= MAX_SOURCE_COUNT
        ):
            raise SourceManifestValidationError(
                f"source_count must be between 1 and {MAX_SOURCE_COUNT}"
            )
        sources = tuple(self.sources)
        if len(sources) != self.source_count:
            raise SourceManifestValidationError(
                "source_count must exactly equal the number of manifest entries"
            )
        if any(not isinstance(item, SourceManifestEntry) for item in sources):
            raise SourceManifestValidationError(
                "sources must contain SourceManifestEntry values"
            )
        _reject_duplicate_entries(sources)
        canonical_order = tuple(sorted(sources, key=_entry_sort_key))
        if sources != canonical_order:
            raise SourceManifestValidationError(
                "manifest entries must use canonical logical-path order"
            )
        object.__setattr__(self, "sources", sources)
        object.__setattr__(
            self,
            "created_at",
            _explicit_timestamp(self.created_at),
        )
        if len(canonical_json(self._digest_payload())) > MAX_MANIFEST_BYTES:
            raise SourceManifestValidationError(
                f"manifest exceeds {MAX_MANIFEST_BYTES} canonical bytes"
            )

    @property
    def manifest_sha256(self) -> str:
        return sha256_json(self._digest_payload())

    def _digest_payload(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "schema_version": self.schema_version,
            "tenant_id": self.tenant_id,
            "knowledge_base_id": self.knowledge_base_id,
            "release_id": self.release_id,
            "source_count": self.source_count,
            "sources": [item.to_dict() for item in self.sources],
        }
        if self.created_at is not None:
            payload["created_at"] = self.created_at
        return payload

    def to_dict(self) -> dict[str, Any]:
        payload = self._digest_payload()
        payload["manifest_sha256"] = self.manifest_sha256
        return payload

    def to_bytes(self) -> bytes:
        """Return the one canonical JSON encoding used for persistence."""

        return canonical_json(self.to_dict())

    def to_json(self) -> str:
        return self.to_bytes().decode("utf-8")


def create_source_manifest(
    source_root: str | Path,
    *,
    tenant_id: str,
    knowledge_base_id: str,
    release_id: str,
    sources: Sequence[SourceSpec],
    created_at: str | None = None,
) -> SourceManifest:
    """Inspect original files and create a deterministic immutable manifest."""

    root = _source_root(source_root)
    specs = tuple(sources)
    if not 1 <= len(specs) <= MAX_SOURCE_COUNT:
        raise SourceManifestValidationError(
            f"sources must contain between 1 and {MAX_SOURCE_COUNT} entries"
        )
    if any(not isinstance(spec, SourceSpec) for spec in specs):
        raise SourceManifestValidationError("sources must contain SourceSpec values")
    _reject_duplicate_specs(specs)

    entries: list[SourceManifestEntry] = []
    for spec in specs:
        content = _read_source(root, spec.logical_path)
        source_sha256 = sha256_bytes(content)
        _verify_bound_source_bytes(
            spec,
            content,
            source_sha256=source_sha256,
        )
        entries.append(
            SourceManifestEntry(
                external_file_id=spec.external_file_id,
                logical_path=spec.logical_path,
                sha256=source_sha256,
                byte_size=len(content),
                detected_media_type=detect_media_type(
                    content,
                    logical_path=spec.logical_path,
                ),
                declared_media_type=spec.declared_media_type,
                source_type=spec.source_type,
                visibility=spec.visibility,
                category_path=spec.category_path,
                metadata=spec.metadata,
                is_original=spec.is_original,
                is_derivative=spec.is_derivative,
            )
        )
    ordered = tuple(sorted(entries, key=_entry_sort_key))
    return SourceManifest(
        tenant_id=tenant_id,
        knowledge_base_id=knowledge_base_id,
        release_id=release_id,
        source_count=len(ordered),
        sources=ordered,
        created_at=created_at,
    )


def _verify_bound_source_bytes(
    spec: SourceSpec,
    content: bytes,
    *,
    source_sha256: str,
) -> None:
    """Fail closed when a frozen source contract binds the expected bytes."""

    has_sha256 = "expected_source_sha256" in spec.metadata
    has_byte_size = "expected_source_byte_size" in spec.metadata
    if not has_sha256 and not has_byte_size:
        return
    if has_sha256 != has_byte_size:
        raise SourceManifestValidationError(
            "bound source metadata must supply expected_source_sha256 and "
            "expected_source_byte_size together"
        )

    expected_sha256 = spec.metadata["expected_source_sha256"]
    if (
        not isinstance(expected_sha256, str)
        or not _SHA256_RE.fullmatch(expected_sha256)
    ):
        raise SourceManifestValidationError(
            "expected_source_sha256 must be a lowercase SHA-256 digest"
        )
    expected_byte_size = spec.metadata["expected_source_byte_size"]
    if (
        isinstance(expected_byte_size, bool)
        or not isinstance(expected_byte_size, int)
        or expected_byte_size < 0
    ):
        raise SourceManifestValidationError(
            "expected_source_byte_size must be a non-negative integer"
        )

    actual_byte_size = len(content)
    if actual_byte_size != expected_byte_size:
        raise SourceManifestIntegrityError(
            f"source size drift for {spec.logical_path}: "
            f"expected {expected_byte_size}, found {actual_byte_size}"
        )
    if not hmac.compare_digest(source_sha256, expected_sha256):
        raise SourceManifestIntegrityError(
            f"source hash drift for {spec.logical_path}"
        )


def load_source_manifest(content: bytes | str | Mapping[str, Any]) -> SourceManifest:
    """Load strict canonical manifest data and verify its embedded checksum."""

    if isinstance(content, bytes):
        if len(content) > MAX_MANIFEST_BYTES:
            raise SourceManifestValidationError(
                f"manifest exceeds {MAX_MANIFEST_BYTES} bytes"
            )
        try:
            text = content.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise SourceManifestValidationError("manifest must be UTF-8 JSON") from exc
        raw = _load_json(text)
    elif isinstance(content, str):
        if len(content.encode("utf-8")) > MAX_MANIFEST_BYTES:
            raise SourceManifestValidationError(
                f"manifest exceeds {MAX_MANIFEST_BYTES} bytes"
            )
        raw = _load_json(content)
    elif isinstance(content, Mapping):
        raw = dict(content)
    else:
        raise TypeError("manifest content must be bytes, text, or a mapping")

    expected_top = {
        "schema_version",
        "tenant_id",
        "knowledge_base_id",
        "release_id",
        "source_count",
        "sources",
        "manifest_sha256",
    }
    optional_top = {"created_at"}
    _require_schema_keys(raw, expected_top, optional_top, "manifest")
    raw_sources = raw["sources"]
    if not isinstance(raw_sources, list):
        raise SourceManifestValidationError("manifest sources must be a JSON array")
    if len(raw_sources) > MAX_SOURCE_COUNT:
        raise SourceManifestValidationError(
            f"manifest contains more than {MAX_SOURCE_COUNT} entries"
        )
    entries = tuple(_entry_from_mapping(item) for item in raw_sources)
    manifest = SourceManifest(
        tenant_id=raw["tenant_id"],
        knowledge_base_id=raw["knowledge_base_id"],
        release_id=raw["release_id"],
        source_count=raw["source_count"],
        sources=entries,
        created_at=raw.get("created_at"),
        schema_version=raw["schema_version"],
    )
    supplied_digest = str(raw["manifest_sha256"]).casefold()
    if not _SHA256_RE.fullmatch(supplied_digest):
        raise SourceManifestValidationError(
            "manifest_sha256 must be a lowercase SHA-256 digest"
        )
    if not hmac.compare_digest(supplied_digest, manifest.manifest_sha256):
        raise SourceManifestIntegrityError("manifest checksum does not match its contents")
    return manifest


def verify_source_manifest(
    manifest: SourceManifest,
    source_root: str | Path,
) -> SourceManifest:
    """Verify that every original file still exactly matches the manifest."""

    if not isinstance(manifest, SourceManifest):
        raise TypeError("manifest must be a SourceManifest")
    root = _source_root(source_root)
    for entry in manifest.sources:
        content = _read_source(root, entry.logical_path)
        _verify_entry_bytes(entry, content)
    return manifest


def stage_manifest_sources(
    manifest: SourceManifest,
    source_root: str | Path,
    object_store: ContentAddressedObjectStore,
    *,
    priority: int = 0,
    max_attempts: int = 5,
) -> tuple[CompileJobSpec, ...]:
    """Verify and stage the manifest plus originals, returning compile specs.

    Content-addressed writes are immutable and therefore safe to retry.  A
    failure may leave already verified objects in the store, but it returns no
    partial job list.
    """

    if not isinstance(manifest, SourceManifest):
        raise TypeError("manifest must be a SourceManifest")
    root = _source_root(source_root)
    # Complete a read-only verification pass before writing any object.  Each
    # file is verified again immediately before its own write to close the
    # normal drift window as far as a filesystem boundary permits.
    verify_source_manifest(manifest, root)
    manifest_bytes = manifest.to_bytes()
    manifest_object = object_store.put(
        manifest_bytes,
        media_type="application/json",
        suffix=".manifest.json",
    )
    if (
        manifest_object.sha256 != sha256_bytes(manifest_bytes)
        or manifest_object.byte_size != len(manifest_bytes)
    ):
        raise SourceManifestIntegrityError(
            "object store returned a different release-manifest identity"
        )
    staged_manifest = object_store.get(manifest_object.object_key)
    if staged_manifest != manifest_bytes:
        raise ObjectIntegrityError(
            "staged release manifest failed read-after-write verification"
        )

    jobs: list[CompileJobSpec] = []
    for ordinal, entry in enumerate(manifest.sources):
        content = _read_source(root, entry.logical_path)
        _verify_entry_bytes(entry, content)
        stored = object_store.put(
            content,
            media_type=entry.declared_media_type or entry.detected_media_type,
            suffix=_safe_suffix(entry.logical_path),
        )
        if stored.sha256 != entry.sha256 or stored.byte_size != entry.byte_size:
            raise SourceManifestIntegrityError(
                f"object store returned a different identity for {entry.logical_path}"
            )
        staged_content = object_store.get(stored.object_key)
        if (
            len(staged_content) != entry.byte_size
            or sha256_bytes(staged_content) != entry.sha256
        ):
            raise ObjectIntegrityError(
                f"staged object failed read-after-write verification: {stored.object_key}"
            )

        source_id = deterministic_source_id(
            manifest.tenant_id,
            manifest.knowledge_base_id,
            entry.external_file_id,
        )
        source_version_id = deterministic_source_version_id(
            source_id,
            entry.sha256,
        )
        jobs.append(
            CompileJobSpec(
                source_id=source_id,
                source_version_id=source_version_id,
                external_file_id=entry.external_file_id,
                source_name=entry.logical_path,
                source_object_key=stored.object_key,
                source_type=entry.source_type,
                declared_media_type=entry.declared_media_type,
                category_path=entry.category_path,
                priority=priority,
                max_attempts=max_attempts,
                payload={
                    "manifest_sha256": manifest.manifest_sha256,
                    "manifest_object_key": manifest_object.object_key,
                    "manifest_object_sha256": manifest_object.sha256,
                    "ordinal": ordinal,
                    "logical_path": entry.logical_path,
                    "source_sha256": entry.sha256,
                    "source_byte_size": entry.byte_size,
                    "detected_media_type": entry.detected_media_type,
                    "visibility": entry.visibility,
                    "source_metadata": _thaw_json(entry.metadata),
                    "is_original": True,
                    "is_derivative": False,
                },
            )
        )
    return tuple(jobs)


def deterministic_source_id(
    tenant_id: str,
    knowledge_base_id: str,
    external_file_id: str,
) -> str:
    tenant = _uuid(tenant_id, "tenant_id")
    knowledge_base = _uuid(knowledge_base_id, "knowledge_base_id")
    external = _required_text(external_file_id, "external_file_id")
    return str(
        uuid.uuid5(
            DATABASE_ID_NAMESPACE,
            f"source\0{tenant}\0{knowledge_base}\0{external}",
        )
    )


def deterministic_source_version_id(source_id: str, source_sha256: str) -> str:
    source = _uuid(source_id, "source_id")
    digest = str(source_sha256).casefold()
    if not _SHA256_RE.fullmatch(digest):
        raise SourceManifestValidationError(
            "source_sha256 must be a lowercase SHA-256 digest"
        )
    return str(
        uuid.uuid5(
            DATABASE_ID_NAMESPACE,
            f"source-version\0{source}\0{digest}",
        )
    )


def detect_media_type(content: bytes, *, logical_path: str) -> str:
    """Detect a stable media type without platform-specific MIME databases."""

    path = _logical_path(logical_path)
    if content.startswith(b"%PDF-"):
        return "application/pdf"
    if content.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if content.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if content.startswith((b"GIF87a", b"GIF89a")):
        return "image/gif"
    if content.startswith((b"II*\x00", b"MM\x00*")):
        return "image/tiff"

    extension_type = _EXTENSION_MEDIA_TYPES.get(PurePosixPath(path).suffix.casefold())
    prefix = content[:4096].lstrip().lower()
    if prefix.startswith((b"<!doctype html", b"<html", b"<?xml")):
        if prefix.startswith(b"<?xml") and extension_type != "text/html":
            return "application/xml"
        return "text/html"
    if extension_type is not None:
        return extension_type
    if b"\x00" not in content[:4096]:
        try:
            content[:4096].decode("utf-8")
        except UnicodeDecodeError:
            pass
        else:
            return "text/plain"
    return "application/octet-stream"


def _entry_from_mapping(value: Any) -> SourceManifestEntry:
    if not isinstance(value, Mapping):
        raise SourceManifestValidationError("each manifest source must be an object")
    expected = {
        "external_file_id",
        "logical_path",
        "sha256",
        "byte_size",
        "detected_media_type",
        "declared_media_type",
        "source_type",
        "visibility",
        "category_path",
        "metadata",
        "is_original",
        "is_derivative",
    }
    _require_schema_keys(value, expected, set(), "manifest source")
    if not isinstance(value["category_path"], list):
        raise SourceManifestValidationError("category_path must be a JSON array")
    return SourceManifestEntry(
        external_file_id=value["external_file_id"],
        logical_path=value["logical_path"],
        sha256=value["sha256"],
        byte_size=value["byte_size"],
        detected_media_type=value["detected_media_type"],
        declared_media_type=value["declared_media_type"],
        source_type=value["source_type"],
        visibility=value["visibility"],
        category_path=tuple(value["category_path"]),
        metadata=value["metadata"],
        is_original=value["is_original"],
        is_derivative=value["is_derivative"],
    )


def _verify_entry_bytes(entry: SourceManifestEntry, content: bytes) -> None:
    if len(content) != entry.byte_size:
        raise SourceManifestIntegrityError(
            f"source size drift for {entry.logical_path}: "
            f"expected {entry.byte_size}, found {len(content)}"
        )
    digest = sha256_bytes(content)
    if digest != entry.sha256:
        raise SourceManifestIntegrityError(
            f"source hash drift for {entry.logical_path}: "
            f"expected {entry.sha256}, found {digest}"
        )
    detected = detect_media_type(content, logical_path=entry.logical_path)
    if detected != entry.detected_media_type:
        raise SourceManifestIntegrityError(
            f"detected media-type drift for {entry.logical_path}: "
            f"expected {entry.detected_media_type}, found {detected}"
        )


def _source_root(source_root: str | Path) -> Path:
    try:
        root = Path(source_root).resolve(strict=True)
    except (FileNotFoundError, OSError) as exc:
        raise SourceManifestValidationError(
            f"source root does not exist: {source_root}"
        ) from exc
    if not root.is_dir():
        raise SourceManifestValidationError(
            f"source root is not a directory: {source_root}"
        )
    return root


def _read_source(root: Path, logical_path: str) -> bytes:
    relative = PurePosixPath(_logical_path(logical_path))
    candidate = root.joinpath(*relative.parts)
    try:
        resolved = candidate.resolve(strict=True)
    except (FileNotFoundError, OSError) as exc:
        raise SourceManifestValidationError(
            f"source file is missing: {logical_path}"
        ) from exc
    try:
        resolved.relative_to(root)
    except ValueError as exc:
        raise SourceManifestValidationError(
            f"source path escapes source root: {logical_path}"
        ) from exc
    if not resolved.is_file():
        raise SourceManifestValidationError(
            f"source path is not a file: {logical_path}"
        )
    try:
        return resolved.read_bytes()
    except OSError as exc:
        raise SourceManifestValidationError(
            f"source file cannot be read: {logical_path}"
        ) from exc


def _logical_path(value: Any) -> str:
    path = _required_text(value, "logical_path")
    if len(path) > MAX_LOGICAL_PATH_LENGTH:
        raise SourceManifestValidationError(
            f"logical_path exceeds {MAX_LOGICAL_PATH_LENGTH} characters"
        )
    if "\\" in path or "\x00" in path:
        raise SourceManifestValidationError(
            "logical_path must be a relative POSIX path"
        )
    parsed = PurePosixPath(path)
    if (
        parsed.is_absolute()
        or path != parsed.as_posix()
        or not parsed.parts
        or any(part in {"", ".", ".."} for part in parsed.parts)
        or ":" in parsed.parts[0]
    ):
        raise SourceManifestValidationError(
            "logical_path must be canonical, relative, and contain no traversal"
        )
    return path


def _required_text(value: Any, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise SourceManifestValidationError(f"{name} is required")
    if value != value.strip() or "\x00" in value:
        raise SourceManifestValidationError(
            f"{name} must not contain surrounding whitespace or NUL"
        )
    return value


def _uuid(value: Any, name: str) -> str:
    try:
        parsed = uuid.UUID(str(value))
    except (TypeError, ValueError, AttributeError) as exc:
        raise SourceManifestValidationError(
            f"{name} must be a UUID-compatible identifier"
        ) from exc
    return str(parsed)


def _source_type(value: Any) -> str:
    source_type = _required_text(value, "source_type").casefold()
    if source_type not in COMPILER_SOURCE_TYPES:
        raise SourceManifestValidationError(
            "unsupported source_type; expected one of: "
            + ", ".join(sorted(COMPILER_SOURCE_TYPES))
        )
    return source_type


def _visibility(value: Any) -> str:
    visibility = _required_text(value, "visibility").casefold()
    if visibility not in {"inherited", "restricted"}:
        raise SourceManifestValidationError(
            "visibility must be inherited or restricted"
        )
    return visibility


def _category_path(value: Any) -> tuple[str, ...]:
    if isinstance(value, (str, bytes, bytearray)) or not isinstance(value, Sequence):
        raise SourceManifestValidationError("category_path must be a sequence")
    return tuple(_required_text(item, "category_path item") for item in value)


def _media_type(value: Any, name: str) -> str:
    media_type = _required_text(value, name).casefold()
    if not _MEDIA_TYPE_RE.fullmatch(media_type):
        raise SourceManifestValidationError(
            f"{name} must be a bare RFC media type without parameters"
        )
    return media_type


def _optional_media_type(value: Any, name: str) -> str | None:
    if value is None:
        return None
    return _media_type(value, name)


def _require_original(is_original: Any, is_derivative: Any) -> None:
    if is_original is not True or is_derivative is not False:
        raise SourceManifestValidationError(
            "source manifests may contain original, non-derivative files only"
        )


def _explicit_timestamp(value: Any) -> str | None:
    if value is None:
        return None
    timestamp = _required_text(value, "created_at")
    if not _UTC_TIMESTAMP_RE.fullmatch(timestamp):
        raise SourceManifestValidationError(
            "created_at must be an explicitly supplied UTC RFC 3339 timestamp"
        )
    try:
        datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
    except ValueError as exc:
        raise SourceManifestValidationError("created_at is not a valid timestamp") from exc
    return timestamp


def _reject_duplicate_specs(specs: Sequence[SourceSpec]) -> None:
    _reject_duplicate_identity(
        ((item.external_file_id, item.logical_path) for item in specs)
    )


def _reject_duplicate_entries(entries: Sequence[SourceManifestEntry]) -> None:
    _reject_duplicate_identity(
        ((item.external_file_id, item.logical_path) for item in entries)
    )


def _reject_duplicate_identity(values) -> None:
    external_ids: set[str] = set()
    logical_paths: set[str] = set()
    for external_file_id, logical_path in values:
        external_key = external_file_id.casefold()
        path_key = logical_path.casefold()
        if external_key in external_ids:
            raise SourceManifestValidationError(
                f"duplicate external_file_id: {external_file_id}"
            )
        if path_key in logical_paths:
            raise SourceManifestValidationError(
                f"duplicate logical_path: {logical_path}"
            )
        external_ids.add(external_key)
        logical_paths.add(path_key)


def _entry_sort_key(entry: SourceManifestEntry) -> tuple[str, str, str]:
    return (
        entry.logical_path.casefold(),
        entry.logical_path,
        entry.external_file_id,
    )


def _frozen_json_object(value: Any) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise SourceManifestValidationError("metadata must be a JSON object")
    try:
        normalized = json.loads(canonical_json(_thaw_json(value)))
    except (TypeError, ValueError) as exc:
        raise SourceManifestValidationError(
            "metadata must contain finite JSON-compatible values"
        ) from exc
    if len(canonical_json(normalized)) > MAX_METADATA_BYTES:
        raise SourceManifestValidationError(
            f"metadata exceeds {MAX_METADATA_BYTES} canonical bytes"
        )
    return _freeze_json(normalized)


def _freeze_json(value: Any) -> Any:
    if isinstance(value, dict):
        return MappingProxyType(
            {key: _freeze_json(value[key]) for key in sorted(value)}
        )
    if isinstance(value, list):
        return tuple(_freeze_json(item) for item in value)
    return value


def _thaw_json(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {str(key): _thaw_json(child) for key, child in value.items()}
    if isinstance(value, tuple):
        return [_thaw_json(child) for child in value]
    return value


def _safe_suffix(logical_path: str) -> str:
    suffix = PurePosixPath(logical_path).suffix.casefold()
    if not suffix or not re.fullmatch(r"\.[a-z0-9][a-z0-9._-]{0,31}", suffix):
        return ""
    return suffix


def _load_json(content: str) -> Mapping[str, Any]:
    def strict_object(pairs):
        result: dict[str, Any] = {}
        for key, value in pairs:
            if key in result:
                raise SourceManifestValidationError(
                    f"duplicate JSON object key: {key}"
                )
            result[key] = value
        return result

    try:
        value = json.loads(
            content,
            object_pairs_hook=strict_object,
            parse_constant=lambda constant: (_raise_invalid_constant(constant)),
        )
    except SourceManifestValidationError:
        raise
    except (json.JSONDecodeError, TypeError) as exc:
        raise SourceManifestValidationError("manifest must be valid JSON") from exc
    if not isinstance(value, Mapping):
        raise SourceManifestValidationError("manifest root must be a JSON object")
    return value


def _raise_invalid_constant(constant: str) -> None:
    raise SourceManifestValidationError(
        f"manifest contains non-JSON numeric constant: {constant}"
    )


def _require_schema_keys(
    value: Mapping[str, Any],
    required: set[str],
    optional: set[str],
    label: str,
) -> None:
    keys = set(value)
    missing = required.difference(keys)
    unknown = keys.difference(required | optional)
    if missing:
        raise SourceManifestValidationError(
            f"{label} is missing fields: {', '.join(sorted(missing))}"
        )
    if unknown:
        raise SourceManifestValidationError(
            f"{label} contains unknown fields: {', '.join(sorted(unknown))}"
        )
