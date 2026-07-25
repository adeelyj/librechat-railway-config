from __future__ import annotations

import dataclasses
import hashlib
import json
import math
import re
import unicodedata
from dataclasses import dataclass
from typing import Any, Iterable, Mapping, TypeAlias


BoundingBox: TypeAlias = tuple[float, float, float, float]
Attributes: TypeAlias = tuple[tuple[str, str], ...]

_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_KIND_RE = re.compile(r"^[a-z][a-z0-9_]{0,31}$")


def attribute_items(
    values: Mapping[str, Any] | Iterable[tuple[str, Any]] | None = None,
) -> Attributes:
    """Return a deterministic, immutable string attribute collection."""

    if values is None:
        return ()
    items = values.items() if isinstance(values, Mapping) else values
    normalized = {
        str(key): _attribute_value(value)
        for key, value in items
    }
    return tuple(sorted(normalized.items()))


def _attribute_value(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ValueError("attribute floats must be finite")
        return format(value, ".12g")
    if isinstance(value, (str, int)):
        return str(value)
    return json.dumps(
        value,
        ensure_ascii=False,
        allow_nan=False,
        separators=(",", ":"),
        sort_keys=True,
    )


def clean_text(value: str) -> str:
    """Normalize Unicode and line endings without collapsing document layout."""

    return unicodedata.normalize("NFC", value).replace("\r\n", "\n").replace("\r", "\n")


def rounded_bbox(value: Iterable[float] | None) -> BoundingBox | None:
    if value is None:
        return None
    coordinates = tuple(round(float(item), 6) for item in value)
    if len(coordinates) != 4:
        raise ValueError("a bounding box must contain four coordinates")
    if not all(math.isfinite(item) for item in coordinates):
        raise ValueError("bounding box coordinates must be finite")
    return coordinates  # type: ignore[return-value]


def stable_id(
    kind: str,
    source_sha256: str,
    location: str,
    content: str = "",
) -> str:
    """Build a stable identifier from immutable source and evidence coordinates."""

    normalized_kind = kind.strip().lower()
    normalized_sha = source_sha256.strip().lower()
    if not _KIND_RE.fullmatch(normalized_kind):
        raise ValueError(f"invalid stable-id kind: {kind!r}")
    if not _SHA256_RE.fullmatch(normalized_sha):
        raise ValueError("source_sha256 must be a lowercase SHA-256 hex digest")
    material = "\0".join(
        (
            "bauer-evidence-v3",
            normalized_kind,
            normalized_sha,
            clean_text(location),
            clean_text(content),
        )
    )
    digest = hashlib.sha256(material.encode("utf-8")).hexdigest()
    return f"{normalized_kind}_{digest[:32]}"


@dataclass(frozen=True, slots=True)
class Cell:
    cell_id: str
    row: int
    column: int
    text: str
    source_locator: str
    parser_id: str
    row_span: int = 1
    column_span: int = 1
    role: str = "body"
    group: str | None = None
    bbox: BoundingBox | None = None
    confidence: float | None = None
    attributes: Attributes = ()


@dataclass(frozen=True, slots=True)
class Table:
    table_id: str
    page_index: int
    order: int
    row_count: int
    column_count: int
    cells: tuple[Cell, ...]
    source_locator: str
    parser_id: str
    caption: str | None = None
    section_path: tuple[str, ...] = ()
    bbox: BoundingBox | None = None
    confidence: float | None = None
    attributes: Attributes = ()


@dataclass(frozen=True, slots=True)
class Block:
    block_id: str
    page_index: int
    order: int
    kind: str
    text: str
    source_locator: str
    parser_id: str
    section_path: tuple[str, ...] = ()
    bbox: BoundingBox | None = None
    confidence: float | None = None
    attributes: Attributes = ()


@dataclass(frozen=True, slots=True)
class Page:
    page_id: str
    index: int
    blocks: tuple[Block, ...]
    tables: tuple[Table, ...]
    source_locator: str
    parser_id: str
    printed_label: str | None = None
    width: float | None = None
    height: float | None = None
    rotation: int = 0
    ocr_needed: bool = False
    signals: Attributes = ()


@dataclass(frozen=True, slots=True)
class Document:
    document_id: str
    source_sha256: str
    source_name: str
    media_type: str
    parser_id: str
    parser_version: str
    pages: tuple[Page, ...]
    title: str | None = None
    language: str | None = None
    attributes: Attributes = ()
    schema_version: int = 1

    def to_data(self) -> dict[str, Any]:
        return canonical_data(self)

    def to_json(self) -> str:
        return canonical_json(self)


def canonical_data(value: Any) -> Any:
    """Convert canonical IR into JSON-compatible data without mutable aliases."""

    if dataclasses.is_dataclass(value):
        return {
            field.name: canonical_data(getattr(value, field.name))
            for field in dataclasses.fields(value)
        }
    if isinstance(value, tuple):
        return [canonical_data(item) for item in value]
    if isinstance(value, list):
        return [canonical_data(item) for item in value]
    if isinstance(value, Mapping):
        return {
            str(key): canonical_data(item)
            for key, item in sorted(value.items(), key=lambda pair: str(pair[0]))
        }
    if isinstance(value, float) and not math.isfinite(value):
        raise ValueError("canonical JSON does not permit non-finite floats")
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    raise TypeError(f"unsupported canonical value: {type(value).__name__}")


def canonical_json(value: Any) -> str:
    return json.dumps(
        canonical_data(value),
        ensure_ascii=False,
        allow_nan=False,
        separators=(",", ":"),
        sort_keys=True,
    )
