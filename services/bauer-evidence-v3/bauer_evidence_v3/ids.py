from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping, Sequence
from typing import Any


_KIND_RE = re.compile(r"^[a-z][a-z0-9_]{1,31}$")
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")


def canonical_json(value: Any) -> bytes:
    """Return the single canonical JSON representation used for hashes and signatures."""

    return json.dumps(
        value,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def sha256_bytes(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def sha256_json(value: Any) -> str:
    return sha256_bytes(canonical_json(value))


def stable_id(kind: str, *parts: object) -> str:
    """Create a readable, deterministic ID without leaking source names or text."""

    if not _KIND_RE.fullmatch(kind):
        raise ValueError("kind must be lowercase snake_case and between 2 and 32 characters")
    if not parts or any(part is None or str(part) == "" for part in parts):
        raise ValueError("stable IDs require one or more non-empty parts")
    digest = sha256_json([str(part) for part in parts])
    return f"{kind}_{digest[:32]}"


def content_object_key(content_sha256: str, *, suffix: str = "") -> str:
    """Map a verified SHA-256 digest to an immutable, content-addressed object key."""

    digest = content_sha256.casefold()
    if not _SHA256_RE.fullmatch(digest):
        raise ValueError("content_sha256 must contain exactly 64 lowercase hexadecimal characters")
    if suffix:
        if not re.fullmatch(r"\.[a-z0-9][a-z0-9._-]{0,31}", suffix.casefold()):
            raise ValueError("suffix must be a short file extension")
        suffix = suffix.casefold()
    return f"sha256/{digest[:2]}/{digest[2:4]}/{digest}{suffix}"


def canonicalize_mapping(value: Mapping[str, Any]) -> dict[str, Any]:
    """Copy nested JSON-compatible values into a deterministic key order."""

    def normalize(item: Any) -> Any:
        if isinstance(item, Mapping):
            return {str(key): normalize(item[key]) for key in sorted(item, key=str)}
        if isinstance(item, Sequence) and not isinstance(item, (str, bytes, bytearray)):
            return [normalize(child) for child in item]
        return item

    return normalize(value)
