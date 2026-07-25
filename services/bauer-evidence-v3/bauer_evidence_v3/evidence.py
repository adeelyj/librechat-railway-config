from __future__ import annotations

from dataclasses import dataclass

from .ids import sha256_json
from .retrieval import RetrievalResult


@dataclass(frozen=True, slots=True)
class EvidenceCitation:
    citation_id: str
    evidence_id: str
    source_document_id: str
    external_file_id: str | None
    source_version_id: str
    source_sha256: str
    title: str
    source_type: str
    page_number: int
    printed_page_label: str | None
    content: str
    table_headers: tuple[str, ...]
    table_values: tuple[str, ...]
    unit: str | None
    footnotes: tuple[str, ...]
    channels: tuple[str, ...]
    score: float

    @property
    def support_text(self) -> str:
        return "\n".join(
            value
            for value in (
                self.title,
                self.content,
                " | ".join(self.table_headers),
                " | ".join(self.table_values),
                self.unit or "",
                " ".join(self.footnotes),
            )
            if value
        )


@dataclass(frozen=True, slots=True)
class EvidencePackage:
    release_id: str
    tenant_id: str
    knowledge_base_id: str
    question: str
    citations: tuple[EvidenceCitation, ...]
    truncated: bool

    def to_prompt(self) -> str:
        if not self.citations:
            return "No authorized evidence was retrieved."
        sections = [
            "Use only the evidence below. Cite important claims with the stable citation ID in "
            "square brackets. Generated navigation summaries are not evidence."
        ]
        for item in self.citations:
            page = item.printed_page_label or str(item.page_number)
            table = ""
            if item.table_headers or item.table_values:
                table = (
                    f"\nTable headers: {' | '.join(item.table_headers)}"
                    f"\nTable values: {' | '.join(item.table_values)}"
                )
            sections.append(
                f"[{item.citation_id}] {item.title}; page {page}; "
                f"source version {item.source_version_id}\n{item.content}{table}"
            )
        return "\n\n".join(sections)


def build_evidence_package(
    *,
    question: str,
    release_id: str,
    tenant_id: str,
    knowledge_base_id: str,
    results: tuple[RetrievalResult, ...],
    authorized_source_ids: set[str] | frozenset[str],
    character_budget: int = 12_000,
    per_item_budget: int = 2_400,
) -> EvidencePackage:
    if character_budget < 1_000 or per_item_budget < 200:
        raise ValueError("evidence budgets are below safe operational minima")
    citations: list[EvidenceCitation] = []
    used = 0
    truncated = False
    seen: set[str] = set()
    for result in results:
        item = result.evidence
        external_file_id = str(item.metadata.get("external_file_id", ""))
        if (
            item.source_document_id not in authorized_source_ids
            and external_file_id not in authorized_source_ids
        ):
            continue
        if item.release_id != release_id:
            raise ValueError("retrieval result crosses the pinned release")
        if item.tenant_id != tenant_id or item.knowledge_base_id != knowledge_base_id:
            raise ValueError("retrieval result crosses the authorization context")
        if not item.is_citable or item.generated_summary:
            continue
        if item.evidence_id in seen:
            continue
        seen.add(item.evidence_id)
        content = item.content.strip()
        if len(content) > per_item_budget:
            content = content[: per_item_budget - 1].rstrip() + "…"
            truncated = True
        if used + len(content) > character_budget:
            remaining = character_budget - used
            if remaining < 200:
                truncated = True
                break
            content = content[: remaining - 1].rstrip() + "…"
            truncated = True
        citation_id = f"E-{sha256_json(item.evidence_id)[:12].upper()}"
        citations.append(
            EvidenceCitation(
                citation_id=citation_id,
                evidence_id=item.evidence_id,
                source_document_id=item.source_document_id,
                external_file_id=external_file_id or None,
                source_version_id=item.source_version_id,
                source_sha256=item.source_sha256,
                title=item.title,
                source_type=item.source_type,
                page_number=item.coordinate.page_number,
                printed_page_label=item.coordinate.printed_page_label,
                content=content,
                table_headers=item.table_headers,
                table_values=item.table_values,
                unit=item.unit,
                footnotes=item.footnotes,
                channels=result.channels,
                score=result.score,
            )
        )
        used += len(content)
        if used >= character_budget:
            break
    return EvidencePackage(
        release_id=release_id,
        tenant_id=tenant_id,
        knowledge_base_id=knowledge_base_id,
        question=question,
        citations=tuple(citations),
        truncated=truncated,
    )
