from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal

from ..canonical.models import CanonicalDocument
from ..canonical.normalize import predicate_slug, stable_id
from ..contracts.models import (
    CitationContract,
    DocumentMetadataContract,
    EvidenceUnitContract,
    SourceCoordinate,
)
from ..reranking.models import RerankResult
from .models import EvidenceContext


@dataclass(frozen=True, slots=True)
class SourceRegistry:
    documents_by_sha256: dict[str, CanonicalDocument]
    external_source_ids: dict[str, str]
    linked_filenames: dict[tuple[str, str], str] = field(default_factory=dict)

    def document(self, source_sha256: str) -> CanonicalDocument:
        try:
            return self.documents_by_sha256[source_sha256]
        except KeyError as error:
            raise ValueError(
                f"canonical source {source_sha256} is not registered"
            ) from error

    def external_source_id(self, source_sha256: str) -> str:
        try:
            return self.external_source_ids[source_sha256]
        except KeyError as error:
            raise ValueError(
                f"authorization source {source_sha256} is not registered"
            ) from error


@dataclass(frozen=True, slots=True)
class EvidenceMaterializer:
    registry: SourceRegistry

    def materialize(
        self,
        reranked: RerankResult,
    ) -> tuple[EvidenceContext, ...]:
        return tuple(self._one(item) for item in reranked.ranked)

    def _one(self, ranked) -> EvidenceContext:
        projection = ranked.candidate.item.projection
        document = self.registry.document(projection.source_sha256)
        evidence_text, canonical_evidence_ids = self._expanded_evidence(
            projection,
            document,
        )
        external_source_id = self.registry.external_source_id(
            projection.source_sha256
        )
        source_version_id = stable_id(
            "source_version",
            projection.source_sha256,
            projection.projection_schema,
        )
        coordinate = SourceCoordinate(
            source_id=external_source_id,
            source_version_id=source_version_id,
            page=projection.physical_page,
            printed_page=projection.printed_page,
            section_path=list(projection.section_path),
            table_id=projection.table_id,
            row_index=projection.row_index,
            source_span=",".join(canonical_evidence_ids)[:512],
        )
        table = next(
            (
                item
                for item in document.tables
                if item.table_id == projection.table_id
            ),
            None,
        )
        row_cells = (
            tuple(
                cell
                for cell in table.cells
                if cell.row == projection.row_index
            )
            if table is not None and projection.row_index is not None
            else ()
        )
        header_path = tuple(
            dict.fromkeys(
                label for cell in row_cells for label in cell.header_path
            )
        )
        raw_values = " | ".join(cell.text for cell in row_cells if cell.text)
        footnotes = (
            [f"{note.marker}: {note.text}" for note in table.footnotes]
            if table is not None
            else []
        )
        normalized_value = self._contract_number(
            projection.numeric_value
        )
        citation_id = stable_id(
            "citation",
            projection.projection_id,
            source_version_id,
        )
        citation = CitationContract(
            citation_id=citation_id,
            evidence_id=projection.projection_id,
            source_title=projection.title,
            original_filename=projection.source_filename,
            document_number=projection.document_number,
            coordinate=coordinate,
            table_title=table.caption if table is not None else None,
            header_path=list(header_path),
            raw_value=raw_values[:2048] or None,
            normalized_value=normalized_value,
            raw_unit=projection.unit_raw,
            normalized_unit=projection.unit_ucum,
            qualifier=dict(projection.qualifiers).get("group"),
            footnotes=footnotes,
            excerpt=evidence_text[:8000],
        )
        metadata = DocumentMetadataContract(
            original_filename=document.source_filename,
            external_file_id=external_source_id,
            document_number=document.document_number,
            title=document.title or document.source_filename,
            language=document.language,
            subject=document.subject,
            source_type=(
                "pdf"
                if document.media_type == "application/pdf"
                else "html"
                if document.media_type == "text/html"
                else "other"
            ),
            page_count=document.page_count,
            source_sha256=document.source_sha256,
            authority=(
                "derived_verified"
                if "bauer-synthetic-demo" in document.source_filename.casefold()
                else "original"
            ),
        )
        unit = EvidenceUnitContract(
            evidence_id=projection.projection_id,
            evidence_type={
                "metadata": "document_metadata",
                "passage": "passage",
                "table_row": "table_row",
                "fact": "typed_fact",
            }[projection.projection_type],
            release_public_id=ranked.candidate.item.release_id,
            coordinate=coordinate,
            search_text=evidence_text,
            metadata=metadata,
            citation=citation,
            canonical_record_ids=list(canonical_evidence_ids),
            authorization_source_id=external_source_id,
        )
        return EvidenceContext(
            ranked=ranked,
            unit=unit,
            citation=citation,
            values=self._values(projection, document, table, row_cells),
        )

    @staticmethod
    def _expanded_evidence(
        projection,
        document: CanonicalDocument,
    ) -> tuple[str, tuple[str, ...]]:
        """Join a split passage to an immediately following source list.

        PDF reading order can place a list in the next bounded projection even
        when the preceding canonical block ends with its list-introducing
        colon. This reconstruction is coordinate-bound, deterministic, and
        uses only adjacent canonical blocks from the same page and section.
        """

        evidence_ids = tuple(projection.canonical_evidence_ids)
        if (
            projection.projection_type != "passage"
            or not evidence_ids
            or not projection.search_text.rstrip().endswith(":")
        ):
            return projection.search_text, evidence_ids
        index_by_id = {
            block.block_id: index
            for index, block in enumerate(document.blocks)
        }
        final_index = index_by_id.get(evidence_ids[-1])
        if final_index is None:
            return projection.search_text, evidence_ids
        additions = []
        addition_ids = []
        for block in document.blocks[final_index + 1:]:
            if (
                block.provenance.physical_page != projection.physical_page
                or block.section_path != projection.section_path
            ):
                break
            if not block.text.lstrip().startswith(("›", "•")):
                break
            additions.append(block.text)
            addition_ids.append(block.block_id)
            if len(additions) >= 6:
                break
        if not additions:
            return projection.search_text, evidence_ids
        return (
            f"{projection.search_text} {' '.join(additions)}",
            evidence_ids + tuple(addition_ids),
        )

    def _values(
        self,
        projection,
        document: CanonicalDocument,
        table,
        row_cells,
    ) -> tuple[tuple[str, str, str | None], ...]:
        values: list[tuple[str, str, str | None]] = []
        qualifier = dict(projection.qualifiers).get("group")
        if projection.projection_type == "table_row":
            for cell in row_cells:
                if cell.role != "body" or not cell.text:
                    continue
                predicate = predicate_slug(
                    cell.header_path[0]
                    if cell.header_path
                    else f"column_{cell.column + 1}"
                )
                rendered = (
                    f"{cell.text} {cell.unit_raw}"
                    if cell.unit_raw
                    else cell.text
                )
                values.append((predicate, rendered, qualifier))
            if table is not None:
                values.extend(
                    ("footnotes", f"{note.marker}: {note.text}", qualifier)
                    for note in table.footnotes
                )
            values.append(("source", document.source_filename, qualifier))
        elif projection.projection_type == "fact":
            if projection.minimum_value is not None:
                rendered = (
                    f"{projection.minimum_value}–{projection.maximum_value} "
                    f"{projection.unit_raw or ''}"
                ).strip()
            else:
                rendered = (
                    f"{projection.numeric_value} {projection.unit_raw or ''}"
                ).strip()
            if projection.predicate and rendered:
                values.append(
                    (
                        projection.predicate,
                        rendered,
                        projection.subject,
                    )
                )
            values.append(("source", document.source_filename, None))
        elif projection.projection_type == "metadata":
            record = next(
                (
                    item
                    for item in document.records
                    if item.record_id in projection.canonical_evidence_ids
                ),
                None,
            )
            if record is not None:
                fields = dict(record.fields)
                for field in (
                    "organization",
                    "address",
                    "language",
                ):
                    if fields.get(field):
                        values.append((field, fields[field], None))
                if fields.get("title"):
                    values.append(
                        ("certificate_title", fields["title"], None)
                    )
                file_number = fields.get("file_number")
                linked = (
                    self.registry.linked_filenames.get(
                        (document.source_sha256, file_number),
                    )
                    if file_number
                    else None
                )
                values.append(
                    (
                        "source_document",
                        linked
                        or (
                            f"download file {file_number}"
                            if file_number
                            else document.source_filename
                        ),
                        None,
                    )
                )
            values.extend(
                (
                    ("title", document.title or document.source_filename, None),
                    ("language", document.language or "unknown", None),
                    ("source_filename", document.source_filename, None),
                    ("source", document.source_filename, None),
                    ("source_document", document.source_filename, None),
                )
            )
            if document.subject:
                values.append(("subject", document.subject, None))
        else:
            values.extend(
                (
                    ("source", document.source_filename, None),
                    ("source_document", document.source_filename, None),
                )
            )
            if "EN ISO 3834-2" in projection.search_text:
                values.append(("certificate_standard", "EN ISO 3834-2", None))
        by_value = {
            (field, value, item_qualifier): (
                field,
                value,
                item_qualifier,
            )
            for field, value, item_qualifier in values
            if value
        }
        return tuple(by_value[key] for key in sorted(by_value))

    @staticmethod
    def _contract_number(value: Decimal | None) -> int | float | None:
        if value is None:
            return None
        if value == value.to_integral_value():
            return int(value)
        return float(value)
