from __future__ import annotations

from dataclasses import dataclass
import re

from .models import (
    AnswerDraft,
    EvidenceContext,
    TaskPlan,
    ValidationDefect,
    ValidationReport,
)
from .render import GroundedAnswerBuilder


@dataclass(frozen=True, slots=True)
class AnswerValidator:
    def validate(
        self,
        plan: TaskPlan,
        draft: AnswerDraft,
        evidence: tuple[EvidenceContext, ...],
        *,
        repair_attempted: bool = False,
    ) -> ValidationReport:
        defects: list[ValidationDefect] = []
        coverage = {item.field.field: item for item in draft.coverage}
        evidence_by_id = {
            context.unit.evidence_id: context for context in evidence
        }
        citations = {item.citation_id: item for item in draft.citations}
        for claim in draft.claims:
            item = coverage.get(claim.field)
            if item is None or item.state != "supported":
                defects.append(
                    ValidationDefect(
                        code="unsupported_claim",
                        field=claim.field,
                        detail="claim has no supported coverage item",
                    )
                )
                continue
            supported_values = {value for value, _ in item.values}
            if not set(claim.values) <= supported_values:
                defects.append(
                    ValidationDefect(
                        code="claim_value_not_supported",
                        field=claim.field,
                        detail="claim contains a value outside canonical support",
                    )
                )
            supported_fact_ids = {fact.fact_id for fact in item.facts}
            if supported_fact_ids and not claim.fact_ids:
                defects.append(
                    ValidationDefect(
                        code="claim_missing_fact_identity",
                        field=claim.field,
                        detail="structured coverage claim omits fact identities",
                    )
                )
            if not set(claim.fact_ids) <= supported_fact_ids:
                defects.append(
                    ValidationDefect(
                        code="claim_fact_not_supported",
                        field=claim.field,
                        detail="claim references a fact outside canonical support",
                    )
                )
            for value in claim.values:
                if value not in draft.answer:
                    defects.append(
                        ValidationDefect(
                            code="claim_value_not_rendered",
                            field=claim.field,
                            detail=f"supported value was omitted from the answer: {value[:120]}",
                        )
                    )
            if not claim.evidence_ids:
                defects.append(
                    ValidationDefect(
                        code="claim_missing_citation",
                        field=claim.field,
                        detail="supported claim has no evidence ID",
                    )
                )
            if any(
                evidence_id not in evidence_by_id
                for evidence_id in claim.evidence_ids
            ):
                defects.append(
                    ValidationDefect(
                        code="claim_unknown_evidence",
                        field=claim.field,
                        detail="claim references unknown evidence",
                    )
                )
            expected_citations = {
                evidence_by_id[evidence_id].citation.citation_id
                for evidence_id in claim.evidence_ids
                if evidence_id in evidence_by_id
            }
            if any(
                f"[{citation_id}]" not in draft.answer
                for citation_id in expected_citations
            ):
                defects.append(
                    ValidationDefect(
                        code="claim_citation_not_rendered",
                        field=claim.field,
                        detail="one or more claim citations are absent from visible output",
                    )
                )
        required_missing = [
            field.field
            for field in plan.fields
            if field.required
            and (
                field.field not in coverage
                or coverage[field.field].state != "supported"
            )
        ]
        if draft.status == "complete" and required_missing:
            defects.append(
                ValidationDefect(
                    code="completion_false_positive",
                    field=None,
                    detail=f"required fields are missing: {required_missing}",
                )
            )
        if draft.status == "partial" and not required_missing:
            defects.append(
                ValidationDefect(
                    code="completion_false_negative",
                    field=None,
                    detail="partial response has no missing required field",
                )
            )
        used_evidence_ids = {
            evidence_id
            for claim in draft.claims
            for evidence_id in claim.evidence_ids
        }
        for exclusion in plan.exclusions:
            for evidence_id in used_evidence_ids:
                context = evidence_by_id[evidence_id]
                if exclusion.casefold() in context.unit.search_text.casefold():
                    defects.append(
                        ValidationDefect(
                            code="constraint_violation",
                            field=None,
                            detail=f"selected evidence contains excluded {exclusion}",
                        )
                    )
        for citation in draft.citations:
            if (
                not citation.original_filename
                or not citation.source_title
                or not citation.excerpt
                or not citation.coordinate.source_id
                or not citation.coordinate.source_version_id
            ):
                defects.append(
                    ValidationDefect(
                        code="citation_envelope_incomplete",
                        field=None,
                        detail=citation.citation_id,
                    )
                )
        referenced_citation_ids = {
            citation_id
            for item in draft.coverage
            if item.state == "supported"
            for evidence_id in item.evidence_ids
            if evidence_id in evidence_by_id
            for citation_id in (
                evidence_by_id[evidence_id].citation.citation_id,
            )
        }
        if not referenced_citation_ids <= citations.keys():
            defects.append(
                ValidationDefect(
                    code="coverage_citation_missing",
                    field=None,
                    detail="coverage references a citation not returned",
                )
                )
        internal_patterns = (
            r"\bSupported result:\s*",
            r"\bRequested-topic evidence\b",
            r"\bMissing or unresolved fields:\s*",
            r"\bNo supported result from authorized evidence:\s*",
            r"\bUsed \d+ tools?\b",
        )
        if any(re.search(pattern, draft.answer, flags=re.IGNORECASE) for pattern in internal_patterns):
            defects.append(
                ValidationDefect(
                    code="internal_planner_language",
                    field=None,
                    detail="visible answer exposes internal coverage or tool-process language",
                )
            )
        if (
            draft.status in {"complete", "partial"}
            and plan.output_structure == "table"
            and "| ---" not in draft.answer
        ):
            defects.append(
                ValidationDefect(
                    code="requested_structure_missing",
                    field=None,
                    detail="exact-row answer is not rendered as a table",
                )
            )
        if (
            draft.status in {"complete", "partial"}
            and plan.output_structure == "bullets"
            and "\n- " not in f"\n{draft.answer}"
        ):
            defects.append(
                ValidationDefect(
                    code="requested_structure_missing",
                    field=None,
                    detail="requested list is not rendered as bullets",
                )
            )
        if plan.authority_boundary == "synthetic_demo" and draft.status in {
            "complete",
            "partial",
        }:
            normalized_answer = draft.answer.casefold()
            if (
                "synthetic" not in normalized_answer
                or "not confirmed bauer" not in normalized_answer
                or any(
                    fact.authority != "synthetic_demo"
                    for item in draft.coverage
                    for fact in item.facts
                )
            ):
                defects.append(
                    ValidationDefect(
                        code="synthetic_authority_boundary_missing",
                        field=None,
                        detail="synthetic answer lacks explicit authority boundary",
                    )
                )
        if any(
            draft.answer.strip() == context.unit.search_text.strip()
            for context in evidence
        ) or len(draft.answer) > 8000 or "Content:" in draft.answer:
            defects.append(
                ValidationDefect(
                    code="evidence_dump_detected",
                    field=None,
                    detail="answer resembles an unfiltered evidence dump",
                )
            )
        return ValidationReport(
            passed=not defects,
            defects=tuple(defects),
            repair_attempted=repair_attempted,
        )


@dataclass(frozen=True, slots=True)
class TargetedRepair:
    builder: GroundedAnswerBuilder

    def repair(
        self,
        plan: TaskPlan,
        draft: AnswerDraft,
        citations_by_evidence,
    ) -> AnswerDraft:
        if draft.repair_count >= 1:
            raise ValueError("only one targeted repair is permitted")
        # Re-render exclusively from the already-computed coverage matrix.
        # This removes unsupported claims and restores missing citation links
        # without broadening retrieval or dumping evidence.
        return self.builder.build(
            plan,
            draft.coverage,
            citations_by_evidence,
            repair_count=draft.repair_count + 1,
        )
