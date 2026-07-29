from __future__ import annotations

from dataclasses import dataclass

from ..contracts.models import CitationContract
from .models import AnswerDraft, Claim, FieldCoverage, TaskPlan


@dataclass(frozen=True, slots=True)
class GroundedAnswerBuilder:
    def build(
        self,
        plan: TaskPlan,
        coverage: tuple[FieldCoverage, ...],
        citations_by_evidence: dict[str, CitationContract],
        *,
        repair_count: int = 0,
    ) -> AnswerDraft:
        supported = [
            item for item in coverage if item.state == "supported"
        ]
        missing = [
            item
            for item in coverage
            if item.field.required and item.state != "supported"
        ]
        if not supported:
            status = "not_found"
        elif missing:
            status = "partial"
        else:
            status = "complete"

        claims: list[Claim] = []
        lines = [
            {
                "table_row": "Requested technical data:",
                "certificate": "Matching English EN ISO 3834-2 certificate:",
                "document_metadata": "Document metadata:",
                "family_range": "Documented family-level range:",
                "general": "Supported result:",
            }[plan.intent]
        ]
        if plan.intent == "general" and not supported:
            lines[0] = "No supported result from authorized evidence:"
        used_citation_ids: list[str] = []
        for item in supported:
            rendered_values = []
            for value, qualifier in item.values:
                rendered_values.append(
                    f"{qualifier}: {value}" if qualifier else value
                )
            evidence_ids = tuple(
                evidence_id
                for evidence_id in item.evidence_ids
                if evidence_id in citations_by_evidence
            )
            citation_ids = tuple(
                citations_by_evidence[evidence_id].citation_id
                for evidence_id in evidence_ids
            )
            used_citation_ids.extend(citation_ids)
            citation_text = " ".join(f"[{value}]" for value in citation_ids)
            claim_text = (
                f"{item.field.label}: {'; '.join(rendered_values)}"
            )
            lines.append(f"- {claim_text} {citation_text}".rstrip())
            claims.append(
                Claim(
                    field=item.field.field,
                    text=claim_text,
                    values=tuple(value for value, _ in item.values),
                    evidence_ids=evidence_ids,
                )
            )
        if missing:
            if not supported:
                lines.append(
                    "Not established in the authorized Bauer evidence: "
                    + ", ".join(item.field.label for item in missing)
                    + "."
                )
            lines.append(
                "Missing or unresolved fields: "
                + ", ".join(item.field.label for item in missing)
                + "."
            )
        citations_by_id = {
            citations_by_evidence[evidence_id].citation_id: (
                citations_by_evidence[evidence_id]
            )
            for item in supported
            for evidence_id in item.evidence_ids
            if evidence_id in citations_by_evidence
        }
        citations = tuple(
            citations_by_id[key] for key in sorted(citations_by_id)
        )
        return AnswerDraft(
            status=status,  # type: ignore[arg-type]
            answer="\n".join(lines),
            coverage=coverage,
            claims=tuple(claims),
            citations=citations,
            repair_count=repair_count,
        )
