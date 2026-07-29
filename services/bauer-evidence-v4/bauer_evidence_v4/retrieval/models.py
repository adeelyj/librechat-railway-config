from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from ..indexing.models import SearchProjection


@dataclass(frozen=True, slots=True)
class AuthorizedScope:
    principal_id: str
    tenant_id: str
    knowledge_base_id: str
    release_id: str
    authorized_external_source_ids: frozenset[str]

    def __post_init__(self) -> None:
        for name in (
            "principal_id",
            "tenant_id",
            "knowledge_base_id",
            "release_id",
        ):
            if not getattr(self, name).strip():
                raise ValueError(f"{name} must be non-empty")
        if not self.authorized_external_source_ids:
            raise ValueError("authorized source set must not be empty")


@dataclass(frozen=True, slots=True)
class IndexedProjection:
    projection: SearchProjection
    tenant_id: str
    knowledge_base_id: str
    release_id: str
    authorization_source_id: str

    @property
    def canonical_key(self) -> tuple[str, ...]:
        if (
            self.projection.projection_type == "table_row"
            and self.projection.table_id is not None
            and self.projection.row_index is not None
        ):
            return (
                "table_row",
                self.projection.table_id,
                str(self.projection.row_index),
            )
        return (
            self.projection.projection_type,
            *sorted(self.projection.canonical_evidence_ids),
        )


@dataclass(frozen=True, slots=True)
class StructuredConstraint:
    field: Literal[
        "document_number",
        "source_filename",
        "subject",
        "predicate",
        "unit",
    ]
    operator: Literal["eq", "in"]
    value: str | tuple[str, ...]


@dataclass(frozen=True, slots=True)
class Subquestion:
    subquestion_id: str
    text: str

    def __post_init__(self) -> None:
        if not self.subquestion_id.strip() or not self.text.strip():
            raise ValueError("subquestion ID and text must be non-empty")


@dataclass(frozen=True, slots=True)
class RetrievalRequest:
    question: str
    search_hint: str | None
    subquestions: tuple[Subquestion, ...]
    constraints: tuple[StructuredConstraint, ...]
    scope: AuthorizedScope

    def __post_init__(self) -> None:
        if not self.question.strip():
            raise ValueError("original question must be non-empty")
        if self.search_hint is not None and not self.search_hint.strip():
            raise ValueError("search hint must be non-empty when supplied")


@dataclass(frozen=True, slots=True)
class ChannelHit:
    item: IndexedProjection
    channel: Literal["exact", "lexical", "dense"]
    score: float
    rank: int
    subquestion_id: str


@dataclass(frozen=True, slots=True)
class Candidate:
    item: IndexedProjection
    candidate_score: float
    channels: tuple[str, ...]
    subquestion_ids: tuple[str, ...]
    channel_scores: tuple[tuple[str, float], ...]
    preserved_channel_head: bool = False


@dataclass(frozen=True, slots=True)
class CandidateSet:
    original_question: str
    search_hint: str | None
    candidates: tuple[Candidate, ...]
    authorized_projection_count: int
    channel_hit_counts: tuple[tuple[str, int], ...]

    def __post_init__(self) -> None:
        if not self.original_question.strip():
            raise ValueError("candidate set must retain the original question")
