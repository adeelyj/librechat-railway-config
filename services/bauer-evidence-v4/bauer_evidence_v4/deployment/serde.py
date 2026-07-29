from __future__ import annotations

from decimal import Decimal
from typing import Any

from ..canonical.models import (
    CanonicalBlock,
    CanonicalCell,
    CanonicalDocument,
    CanonicalFact,
    CanonicalRecord,
    CanonicalTable,
    Footnote,
    Provenance,
)
from ..indexing.models import SearchProjection


def _decimal(value: Any) -> Decimal | None:
    return Decimal(str(value)) if value is not None else None


def _tuple_pairs(value: Any) -> tuple[tuple[str, str], ...]:
    return tuple((str(left), str(right)) for left, right in (value or []))


def provenance_from_data(value: dict[str, Any]) -> Provenance:
    bbox = value.get("bounding_box")
    return Provenance(
        source_sha256=str(value["source_sha256"]),
        source_path=str(value["source_path"]),
        parser_id=str(value["parser_id"]),
        parser_version=str(value["parser_version"]),
        locator=str(value["locator"]),
        physical_page=value.get("physical_page"),
        printed_page=value.get("printed_page"),
        bounding_box=(
            tuple(float(item) for item in bbox) if bbox is not None else None
        ),
    )


def document_from_data(value: dict[str, Any]) -> CanonicalDocument:
    blocks = tuple(
        CanonicalBlock(
            block_id=str(item["block_id"]),
            kind=str(item["kind"]),
            text=str(item["text"]),
            section_path=tuple(item.get("section_path") or ()),
            provenance=provenance_from_data(item["provenance"]),
        )
        for item in value.get("blocks", [])
    )
    tables = []
    for item in value.get("tables", []):
        cells = tuple(
            CanonicalCell(
                cell_id=str(cell["cell_id"]),
                row=int(cell["row"]),
                column=int(cell["column"]),
                row_span=int(cell["row_span"]),
                column_span=int(cell["column_span"]),
                role=str(cell["role"]),
                text=str(cell["text"]),
                value_kind=str(cell["value_kind"]),
                numeric_value=_decimal(cell.get("numeric_value")),
                header_path=tuple(cell.get("header_path") or ()),
                unit_raw=cell.get("unit_raw"),
                unit_ucum=cell.get("unit_ucum"),
                qualifier_markers=tuple(
                    cell.get("qualifier_markers") or ()
                ),
                group=cell.get("group"),
                provenance=provenance_from_data(cell["provenance"]),
            )
            for cell in item.get("cells", [])
        )
        footnotes = tuple(
            Footnote(
                marker=str(note["marker"]),
                text=str(note["text"]),
                provenance=provenance_from_data(note["provenance"]),
            )
            for note in item.get("footnotes", [])
        )
        tables.append(
            CanonicalTable(
                table_id=str(item["table_id"]),
                caption=item.get("caption"),
                section_path=tuple(item.get("section_path") or ()),
                row_count=int(item["row_count"]),
                column_count=int(item["column_count"]),
                cells=cells,
                footnotes=footnotes,
                provenance=provenance_from_data(item["provenance"]),
            )
        )
    records = tuple(
        CanonicalRecord(
            record_id=str(item["record_id"]),
            record_type=str(item["record_type"]),
            fields=_tuple_pairs(item.get("fields")),
            section_path=tuple(item.get("section_path") or ()),
            provenance=provenance_from_data(item["provenance"]),
        )
        for item in value.get("records", [])
    )
    facts = tuple(
        CanonicalFact(
            fact_id=str(item["fact_id"]),
            subject=str(item["subject"]),
            predicate=str(item["predicate"]),
            value_kind=str(item["value_kind"]),
            raw_value=str(item["raw_value"]),
            numeric_value=_decimal(item.get("numeric_value")),
            minimum_value=_decimal(item.get("minimum_value")),
            maximum_value=_decimal(item.get("maximum_value")),
            unit_raw=item.get("unit_raw"),
            unit_ucum=item.get("unit_ucum"),
            qualifiers=_tuple_pairs(item.get("qualifiers")),
            provenance_ids=tuple(item.get("provenance_ids") or ()),
            confidence=float(item["confidence"]),
            review_status=str(item["review_status"]),
        )
        for item in value.get("facts", [])
    )
    return CanonicalDocument(
        document_id=str(value["document_id"]),
        source_sha256=str(value["source_sha256"]),
        source_path=str(value["source_path"]),
        media_type=str(value["media_type"]),
        parser_id=str(value["parser_id"]),
        parser_version=str(value["parser_version"]),
        title=value.get("title"),
        language=value.get("language"),
        document_number=value.get("document_number"),
        subject=value.get("subject"),
        source_filename=str(value["source_filename"]),
        page_count=int(value["page_count"]),
        blocks=blocks,
        tables=tuple(tables),
        records=records,
        facts=facts,
        attributes=_tuple_pairs(value.get("attributes")),
        schema_version=int(value.get("schema_version", 1)),
    )


def projection_from_data(value: dict[str, Any]) -> SearchProjection:
    return SearchProjection(
        projection_id=str(value["projection_id"]),
        projection_type=str(value["projection_type"]),
        projection_schema=str(value["projection_schema"]),
        source_sha256=str(value["source_sha256"]),
        source_path=str(value["source_path"]),
        title=str(value["title"]),
        source_filename=str(value["source_filename"]),
        document_number=value.get("document_number"),
        language=value.get("language"),
        search_text=str(value["search_text"]),
        search_text_sha256=str(value["search_text_sha256"]),
        exact_terms=tuple(value.get("exact_terms") or ()),
        canonical_evidence_ids=tuple(
            value.get("canonical_evidence_ids") or ()
        ),
        physical_page=value.get("physical_page"),
        printed_page=value.get("printed_page"),
        section_path=tuple(value.get("section_path") or ()),
        table_id=value.get("table_id"),
        row_index=value.get("row_index"),
        subject=value.get("subject"),
        predicate=value.get("predicate"),
        numeric_value=_decimal(value.get("numeric_value")),
        minimum_value=_decimal(value.get("minimum_value")),
        maximum_value=_decimal(value.get("maximum_value")),
        unit_raw=value.get("unit_raw"),
        unit_ucum=value.get("unit_ucum"),
        qualifiers=_tuple_pairs(value.get("qualifiers")),
    )
