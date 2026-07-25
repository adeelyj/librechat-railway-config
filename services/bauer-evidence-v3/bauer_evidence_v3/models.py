from __future__ import annotations

import math
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any


class ReleaseStatus(StrEnum):
    DRAFT = "draft"
    BUILDING = "building"
    VALIDATING = "validating"
    READY = "ready"
    FAILED = "failed"
    RETIRED = "retired"


class ArtifactStatus(StrEnum):
    BUILDING = "building"
    VALID = "valid"
    QUARANTINED = "quarantined"
    INVALID = "invalid"


class JobState(StrEnum):
    QUEUED = "queued"
    RUNNING = "running"
    RETRY_WAIT = "retry_wait"
    SUCCEEDED = "succeeded"
    DEAD = "dead"
    CANCELLED = "cancelled"


@dataclass(frozen=True, slots=True)
class BoundingBox:
    x0: float
    y0: float
    x1: float
    y1: float

    def __post_init__(self) -> None:
        values = (self.x0, self.y0, self.x1, self.y1)
        if not all(math.isfinite(value) for value in values):
            raise ValueError("bounding-box coordinates must be finite")
        if self.x0 > self.x1 or self.y0 > self.y1:
            raise ValueError("bounding box must form a non-negative rectangle")


@dataclass(frozen=True, slots=True)
class SourceCoordinate:
    page_number: int
    printed_page_label: str | None = None
    bounding_box: BoundingBox | None = None
    section_id: str | None = None
    block_id: str | None = None
    table_id: str | None = None
    row_index: int | None = None
    cell_id: str | None = None
    character_start: int | None = None
    character_end: int | None = None

    def __post_init__(self) -> None:
        if self.page_number < 1:
            raise ValueError("physical page_number is one-based")
        if (self.character_start is None) != (self.character_end is None):
            raise ValueError("character range must contain both start and end")
        if self.character_start is not None:
            if self.character_start < 0 or self.character_end is None:
                raise ValueError("character range must be non-negative")
            if self.character_end <= self.character_start:
                raise ValueError("character_end must be greater than character_start")
        if self.row_index is not None and self.row_index < 0:
            raise ValueError("row_index is zero-based and cannot be negative")
        if self.cell_id and not self.table_id:
            raise ValueError("cell coordinates require table_id")
        if self.row_index is not None and not self.table_id:
            raise ValueError("row coordinates require table_id")


@dataclass(frozen=True, slots=True)
class EvidenceItem:
    evidence_id: str
    release_id: str
    tenant_id: str
    knowledge_base_id: str
    source_document_id: str
    source_version_id: str
    source_sha256: str
    source_type: str
    title: str
    content: str
    coordinate: SourceCoordinate
    language: str | None = None
    table_headers: tuple[str, ...] = ()
    table_values: tuple[str, ...] = ()
    unit: str | None = None
    footnotes: tuple[str, ...] = ()
    retrieval_channels: tuple[str, ...] = ()
    score: float = 0.0
    is_citable: bool = True
    generated_summary: bool = False
    metadata: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        required = (
            self.evidence_id,
            self.release_id,
            self.tenant_id,
            self.knowledge_base_id,
            self.source_document_id,
            self.source_version_id,
            self.source_sha256,
            self.source_type,
            self.title,
            self.content,
        )
        if any(not value for value in required):
            raise ValueError("evidence identity, provenance, title, and content are required")
        if len(self.source_sha256) != 64:
            raise ValueError("source_sha256 must be a full SHA-256 digest")
        if not math.isfinite(self.score):
            raise ValueError("evidence score must be finite")
        if self.generated_summary and self.is_citable:
            raise ValueError("generated navigation summaries cannot be claim-supporting evidence")


@dataclass(frozen=True, slots=True)
class TypedFact:
    fact_id: str
    artifact_set_id: str
    subject: str
    predicate: str
    value_kind: str
    raw_value: str
    provenance_evidence_ids: tuple[str, ...]
    text_value: str | None = None
    numeric_value: str | None = None
    boolean_value: bool | None = None
    date_value: str | None = None
    entity_value: str | None = None
    raw_unit: str | None = None
    normalized_unit: str | None = None
    qualifiers: dict[str, Any] = field(default_factory=dict)
    confidence: float = 1.0
    review_status: str = "unreviewed"

    def __post_init__(self) -> None:
        supported = {
            "text": self.text_value,
            "numeric": self.numeric_value,
            "boolean": self.boolean_value,
            "date": self.date_value,
            "entity": self.entity_value,
        }
        if self.value_kind not in supported:
            raise ValueError(f"unsupported value_kind: {self.value_kind}")
        present = [key for key, value in supported.items() if value is not None]
        if present != [self.value_kind]:
            raise ValueError("exactly the typed value matching value_kind must be present")
        if not self.provenance_evidence_ids:
            raise ValueError("facts require at least one provenance evidence item")
        if not 0.0 <= self.confidence <= 1.0:
            raise ValueError("fact confidence must be between zero and one")
