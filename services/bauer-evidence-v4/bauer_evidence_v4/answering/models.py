from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from ..contracts.models import CitationContract, EvidenceUnitContract
from ..reranking.models import RankedCandidate
from ..retrieval.models import Subquestion


@dataclass(frozen=True, slots=True)
class RequiredField:
    field: str
    label: str
    required: bool = True
    anchor_terms: tuple[str, ...] = ()
    match_terms: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class TaskPlan:
    original_question: str
    locale: str
    intent: Literal[
        "table_row",
        "certificate",
        "document_metadata",
        "family_range",
        "general",
    ]
    fields: tuple[RequiredField, ...]
    subquestions: tuple[Subquestion, ...]
    exclusions: tuple[str, ...]
    exact_identifiers: tuple[str, ...]
    required_qualifiers: tuple[tuple[str, str], ...]


@dataclass(frozen=True, slots=True)
class EvidenceContext:
    ranked: RankedCandidate
    unit: EvidenceUnitContract
    citation: CitationContract
    values: tuple[tuple[str, str, str | None], ...]


@dataclass(frozen=True, slots=True)
class FieldCoverage:
    field: RequiredField
    state: Literal[
        "supported",
        "ambiguous",
        "contradicted",
        "absent",
        "refused",
    ]
    values: tuple[tuple[str, str | None], ...]
    evidence_ids: tuple[str, ...]
    detail: str | None = None


@dataclass(frozen=True, slots=True)
class Claim:
    field: str
    text: str
    values: tuple[str, ...]
    evidence_ids: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class AnswerDraft:
    status: Literal["complete", "partial", "not_found", "refused"]
    answer: str
    coverage: tuple[FieldCoverage, ...]
    claims: tuple[Claim, ...]
    citations: tuple[CitationContract, ...]
    repair_count: int = 0


@dataclass(frozen=True, slots=True)
class ValidationDefect:
    code: str
    field: str | None
    detail: str


@dataclass(frozen=True, slots=True)
class ValidationReport:
    passed: bool
    defects: tuple[ValidationDefect, ...]
    repair_attempted: bool
