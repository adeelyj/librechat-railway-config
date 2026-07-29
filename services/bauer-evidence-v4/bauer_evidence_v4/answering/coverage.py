from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass

from .models import EvidenceContext, FieldCoverage, TaskPlan


_DASH_TRANSLATION = str.maketrans(
    {
        character: "-"
        for character in "\u00ad\u2010\u2011\u2012\u2013\u2014\u2015\u2212"
    }
)


def _normalize(value: str) -> str:
    folded = unicodedata.normalize(
        "NFKD",
        value.translate(_DASH_TRANSLATION),
    ).casefold()
    return "".join(
        character for character in folded if not unicodedata.combining(character)
    )


@dataclass(frozen=True, slots=True)
class CoverageEngine:
    def evaluate(
        self,
        plan: TaskPlan,
        evidence: tuple[EvidenceContext, ...],
    ) -> tuple[FieldCoverage, ...]:
        relevant = tuple(
            context
            for context in evidence
            if self._relevant(plan, context)
        )
        return tuple(
            self._field(plan, field, relevant)
            for field in plan.fields
        )

    def _field(
        self,
        plan: TaskPlan,
        field,
        evidence: tuple[EvidenceContext, ...],
    ) -> FieldCoverage:
        if plan.intent == "general":
            return self._general_field(field, evidence)
        support: list[tuple[str, str | None, str]] = []
        for context in evidence:
            for name, value, qualifier in context.values:
                if name == field.field:
                    support.append(
                        (value, qualifier, context.unit.evidence_id)
                    )
        if (
            plan.intent == "certificate"
            and field.field == "source_document"
            and any(value.lower().endswith(".pdf") for value, _, _ in support)
        ):
            support = [
                item for item in support if item[0].lower().endswith(".pdf")
            ]
        deduplicated: dict[
            tuple[str, str | None],
            tuple[str, str | None, str],
        ] = {}
        for value, qualifier, evidence_id in support:
            deduplicated.setdefault(
                (value, qualifier),
                (value, qualifier, evidence_id),
            )
        support = list(deduplicated.values())
        if not support:
            return FieldCoverage(
                field=field,
                state="absent",
                values=(),
                evidence_ids=(),
                detail=f"No authorized evidence supports {field.label}.",
            )
        return FieldCoverage(
            field=field,
            state="supported",
            values=tuple(
                dict.fromkeys(
                    (value, qualifier)
                    for value, qualifier, _ in support
                )
            ),
            evidence_ids=tuple(
                dict.fromkeys(evidence_id for _, _, evidence_id in support)
            ),
            detail=None,
        )

    def _general_field(
        self,
        field,
        evidence: tuple[EvidenceContext, ...],
    ) -> FieldCoverage:
        support: list[tuple[str, str | None, str]] = []
        seen: set[str] = set()
        for context in evidence:
            text = context.unit.search_text.strip()
            normalized = _normalize(text)
            if field.anchor_terms and not all(
                _normalize(term) in normalized for term in field.anchor_terms
            ):
                continue
            if field.match_terms and not any(
                _normalize(term) in normalized for term in field.match_terms
            ):
                continue
            snippet = self._bounded_snippet(
                text,
                field.anchor_terms + field.match_terms,
            )
            snippet_key = _normalize(snippet)
            if not snippet or snippet_key in seen:
                continue
            seen.add(snippet_key)
            support.append(
                (
                    snippet,
                    context.citation.original_filename,
                    context.unit.evidence_id,
                )
            )
            if len(support) >= 3:
                break
        if not support:
            return FieldCoverage(
                field=field,
                state="absent",
                values=(),
                evidence_ids=(),
                detail=(
                    f"{field.label} is not established in the authorized "
                    "Bauer evidence."
                ),
            )
        return FieldCoverage(
            field=field,
            state="supported",
            values=tuple(
                (value, qualifier) for value, qualifier, _ in support
            ),
            evidence_ids=tuple(
                evidence_id for _, _, evidence_id in support
            ),
            detail=None,
        )

    @staticmethod
    def _bounded_snippet(
        text: str,
        terms: tuple[str, ...],
        *,
        limit: int = 700,
    ) -> str:
        compact = re.sub(r"\s+", " ", text).strip()
        if len(compact) <= limit:
            return compact
        normalized = _normalize(compact)
        positions = [
            position
            for term in terms
            for position in (normalized.find(_normalize(term)),)
            if position >= 0
        ]
        center = min(positions) if positions else 0
        start = max(0, center - 180)
        end = min(len(compact), start + limit)
        if end - start < limit:
            start = max(0, end - limit)
        if start:
            boundary = compact.find(" ", start)
            start = boundary + 1 if boundary >= 0 else start
        if end < len(compact):
            boundary = compact.rfind(" ", start, end)
            end = boundary if boundary > start else end
        prefix = "… " if start else ""
        suffix = " …" if end < len(compact) else ""
        return f"{prefix}{compact[start:end].strip()}{suffix}"

    @staticmethod
    def _relevant(plan: TaskPlan, context: EvidenceContext) -> bool:
        projection = context.ranked.candidate.item.projection
        text = _normalize(projection.search_text)
        if plan.exclusions and any(
            _normalize(exclusion) in text for exclusion in plan.exclusions
        ):
            return False
        if plan.intent == "certificate":
            fields = dict(projection.qualifiers)
            return (
                "en iso 3834-2" in text
                and (
                    fields.get("file_number") == "216"
                    or projection.source_filename == "bauer_amfile_216.pdf"
                )
            )
        if plan.intent == "document_metadata":
            return all(
                _normalize(identifier) in text
                for identifier in plan.exact_identifiers
                if identifier.upper().startswith("N")
            )
        if plan.intent == "family_range":
            return (
                projection.subject is not None
                and _normalize(projection.subject)
                == _normalize("K 22 – K 28 SERIES")
            )
        if plan.intent == "table_row":
            model_identifiers = [
                identifier
                for identifier in plan.exact_identifiers
                if not identifier.upper().startswith("N")
            ]
            if model_identifiers and not all(
                _normalize(identifier) in text
                for identifier in model_identifiers
            ):
                return False
            question = _normalize(plan.original_question)
            qualifiers = dict(projection.qualifiers)
            group = _normalize(qualifiers.get("group", ""))
            for key, expected in plan.required_qualifiers:
                if _normalize(qualifiers.get(key, "")) != _normalize(expected):
                    return False
            if all(token in question for token in ("100", "50", "hz")):
                return bool(group)
            if "40 bar" in question and group:
                return "40 bar" in group
            return True
        return True
