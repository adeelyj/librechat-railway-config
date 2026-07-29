from __future__ import annotations

import dataclasses
import json
import math
from dataclasses import dataclass
from decimal import Decimal
from typing import Any, Literal, Mapping


JsonScalar = str | int | float | bool | None
JsonValue = JsonScalar | list["JsonValue"] | dict[str, "JsonValue"]
BoundingBox = tuple[float, float, float, float]


@dataclass(frozen=True, slots=True)
class Provenance:
    source_sha256: str
    source_path: str
    parser_id: str
    parser_version: str
    locator: str
    physical_page: int | None = None
    printed_page: str | None = None
    bounding_box: BoundingBox | None = None


@dataclass(frozen=True, slots=True)
class Footnote:
    marker: str
    text: str
    provenance: Provenance


@dataclass(frozen=True, slots=True)
class CanonicalBlock:
    block_id: str
    kind: Literal["title", "heading", "paragraph", "list_item", "note"]
    text: str
    section_path: tuple[str, ...]
    provenance: Provenance


@dataclass(frozen=True, slots=True)
class CanonicalCell:
    cell_id: str
    row: int
    column: int
    row_span: int
    column_span: int
    role: Literal[
        "header",
        "group_header",
        "row_header",
        "body",
        "note",
    ]
    text: str
    value_kind: Literal["empty", "text", "integer", "decimal"]
    numeric_value: Decimal | None
    header_path: tuple[str, ...]
    unit_raw: str | None
    unit_ucum: str | None
    qualifier_markers: tuple[str, ...]
    group: str | None
    provenance: Provenance


@dataclass(frozen=True, slots=True)
class CanonicalTable:
    table_id: str
    caption: str | None
    section_path: tuple[str, ...]
    row_count: int
    column_count: int
    cells: tuple[CanonicalCell, ...]
    footnotes: tuple[Footnote, ...]
    provenance: Provenance


@dataclass(frozen=True, slots=True)
class CanonicalRecord:
    record_id: str
    record_type: str
    fields: tuple[tuple[str, str], ...]
    section_path: tuple[str, ...]
    provenance: Provenance

    def field(self, name: str) -> str | None:
        return next((value for key, value in self.fields if key == name), None)


@dataclass(frozen=True, slots=True)
class CanonicalFact:
    fact_id: str
    subject: str
    predicate: str
    value_kind: Literal["integer", "decimal", "text", "range"]
    raw_value: str
    numeric_value: Decimal | None
    minimum_value: Decimal | None
    maximum_value: Decimal | None
    unit_raw: str | None
    unit_ucum: str | None
    qualifiers: tuple[tuple[str, str], ...]
    provenance_ids: tuple[str, ...]
    confidence: float
    review_status: Literal["candidate", "verified", "conflicted"]


@dataclass(frozen=True, slots=True)
class CanonicalDocument:
    document_id: str
    source_sha256: str
    source_path: str
    media_type: str
    parser_id: str
    parser_version: str
    title: str | None
    language: str | None
    document_number: str | None
    subject: str | None
    source_filename: str
    page_count: int
    blocks: tuple[CanonicalBlock, ...]
    tables: tuple[CanonicalTable, ...]
    records: tuple[CanonicalRecord, ...]
    facts: tuple[CanonicalFact, ...]
    attributes: tuple[tuple[str, str], ...] = ()
    schema_version: int = 1

    def to_data(self) -> dict[str, Any]:
        return canonical_data(self)

    def to_json(self) -> str:
        return json.dumps(
            self.to_data(),
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        )


@dataclass(frozen=True, slots=True)
class QualityIssue:
    code: str
    severity: Literal["warning", "quarantine", "fatal"]
    message: str
    locator: str | None = None


@dataclass(frozen=True, slots=True)
class QualityReport:
    status: Literal["pass", "warning", "quarantine", "fatal"]
    semantic_score: int
    metrics: tuple[tuple[str, str], ...]
    issues: tuple[QualityIssue, ...]


@dataclass(frozen=True, slots=True)
class CompilationCandidate:
    parser_id: str
    document: CanonicalDocument | None
    quality: QualityReport
    error: str | None = None


@dataclass(frozen=True, slots=True)
class CompilationResult:
    status: Literal["published", "quarantined"]
    document: CanonicalDocument | None
    selected_parser_id: str | None
    candidates: tuple[CompilationCandidate, ...]
    source_sha256: str
    source_path: str


def canonical_data(value: Any) -> Any:
    if dataclasses.is_dataclass(value):
        return {
            field.name: canonical_data(getattr(value, field.name))
            for field in dataclasses.fields(value)
        }
    if isinstance(value, Decimal):
        return format(value, "f")
    if isinstance(value, tuple):
        return [canonical_data(item) for item in value]
    if isinstance(value, list):
        return [canonical_data(item) for item in value]
    if isinstance(value, Mapping):
        return {
            str(key): canonical_data(item)
            for key, item in sorted(value.items(), key=lambda pair: str(pair[0]))
        }
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ValueError("canonical JSON does not permit non-finite floats")
        return value
    if value is None or isinstance(value, (str, int, bool)):
        return value
    raise TypeError(f"unsupported canonical value: {type(value).__name__}")
