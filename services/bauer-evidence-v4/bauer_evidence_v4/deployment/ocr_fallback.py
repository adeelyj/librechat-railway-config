from __future__ import annotations

import re
from pathlib import PurePosixPath
from typing import Any

from ..canonical.models import (
    CanonicalBlock,
    CanonicalCell,
    CanonicalDocument,
    CanonicalFact,
    CanonicalTable,
    Provenance,
)
from ..canonical.normalize import (
    document_number_from_text,
    inherited_header_paths,
    normalize_unit,
    numeric_value,
    predicate_slug,
    sorted_attributes,
    stable_id,
)


_BLOCK_KINDS = {"title", "heading", "paragraph", "list_item", "note"}
_CELL_ROLES = {"header", "group_header", "row_header", "body", "note"}


def compile_ocr_fallback(
    compiler: Any,
    payload: bytes,
    *,
    source_path: str,
    declared_media_type: str,
) -> CanonicalDocument:
    """Translate the proven V3 OCR result into the V4 canonical contract.

    The adapter runs only after both native V4 PDF candidates report an empty
    document. It preserves V3 OCR page coordinates while assigning V4 IDs and
    typed table facts. Native V4 output always remains preferred.
    """

    result = compiler.compile(
        payload,
        source_name=source_path,
        declared_media_type=declared_media_type,
        enforce_gate=False,
    )
    source = result.document
    filename = PurePosixPath(source_path).name
    parser_version = str(
        getattr(getattr(compiler, "ocr_engine", None), "engine_version", "1")
    )
    document_id = stable_id(
        "document",
        source.source_sha256,
        source_path,
        "v3_ocr_adapter",
        parser_version,
    )
    blocks: list[CanonicalBlock] = []
    tables: list[CanonicalTable] = []
    facts: list[CanonicalFact] = []

    for page in source.pages:
        printed_page = page.printed_label or str(page.index + 1)
        for block in page.blocks:
            text = block.text.strip()
            if not text:
                continue
            kind = block.kind if block.kind in _BLOCK_KINDS else "paragraph"
            locator = block.source_locator
            blocks.append(
                CanonicalBlock(
                    block_id=stable_id(
                        "block",
                        document_id,
                        locator,
                        text,
                    ),
                    kind=kind,
                    text=text,
                    section_path=tuple(block.section_path),
                    provenance=_provenance(
                        source_sha256=source.source_sha256,
                        source_path=source_path,
                        parser_version=parser_version,
                        locator=locator,
                        physical_page=page.index + 1,
                        printed_page=printed_page,
                        bounding_box=block.bbox,
                    ),
                )
            )
        for table in page.tables:
            canonical_table, table_facts = _table(
                source_sha256=source.source_sha256,
                source_path=source_path,
                document_id=document_id,
                parser_version=parser_version,
                physical_page=page.index + 1,
                printed_page=printed_page,
                table=table,
            )
            tables.append(canonical_table)
            facts.extend(table_facts)

    title = source.title or filename
    return CanonicalDocument(
        document_id=document_id,
        source_sha256=source.source_sha256,
        source_path=source_path,
        media_type=declared_media_type,
        parser_id="v3_ocr_adapter",
        parser_version=parser_version,
        title=title,
        language=source.language,
        document_number=document_number_from_text(filename, title),
        subject=None,
        source_filename=filename,
        page_count=max(1, len(source.pages)),
        blocks=tuple(blocks),
        tables=tuple(tables),
        records=(),
        facts=tuple(facts),
        attributes=sorted_attributes(
            {
                "fallback": "v3_ocr",
                "native_parser": source.parser_id,
                "native_quality_status": result.quality.status,
                "ocr_page_count": sum(
                    1
                    for page in source.pages
                    if dict(page.signals).get("ocr_accepted") == "true"
                ),
            }
        ),
    )


def _table(
    *,
    source_sha256: str,
    source_path: str,
    document_id: str,
    parser_version: str,
    physical_page: int,
    printed_page: str,
    table: Any,
) -> tuple[CanonicalTable, tuple[CanonicalFact, ...]]:
    table_id = stable_id(
        "table",
        document_id,
        table.source_locator,
        table.row_count,
        table.column_count,
    )
    header_paths = inherited_header_paths(
        column_count=table.column_count,
        header_cells=(
            (
                cell.row,
                cell.column,
                cell.column_span,
                cell.text,
            )
            for cell in table.cells
            if cell.role in {"header", "group_header"}
            or cell.row == 0
        ),
    )
    subject_by_row = {
        cell.row: cell.text
        for cell in table.cells
        if cell.column == 0 and cell.text
    }
    cells: list[CanonicalCell] = []
    facts: list[CanonicalFact] = []
    for cell in table.cells:
        role = cell.role if cell.role in _CELL_ROLES else "body"
        path = (
            header_paths[cell.column]
            if 0 <= cell.column < len(header_paths)
            else ()
        )
        unit_raw, unit_ucum = _unit_from_header_path(path)
        value_kind, number = (
            numeric_value(cell.text)
            if role == "body"
            else ("text", None)
        )
        if not cell.text:
            value_kind = "empty"
        cell_id = stable_id(
            "cell",
            table_id,
            cell.source_locator,
            cell.row,
            cell.column,
            cell.text,
        )
        canonical_cell = CanonicalCell(
            cell_id=cell_id,
            row=cell.row,
            column=cell.column,
            row_span=cell.row_span,
            column_span=cell.column_span,
            role=role,
            text=cell.text,
            value_kind=value_kind,  # type: ignore[arg-type]
            numeric_value=number,
            header_path=tuple(
                label
                for label in path
                if normalize_unit(label)[1] is None
            ),
            unit_raw=unit_raw,
            unit_ucum=unit_ucum,
            qualifier_markers=(),
            group=cell.group,
            provenance=_provenance(
                source_sha256=source_sha256,
                source_path=source_path,
                parser_version=parser_version,
                locator=cell.source_locator,
                physical_page=physical_page,
                printed_page=printed_page,
                bounding_box=cell.bbox,
            ),
        )
        cells.append(canonical_cell)
        if role != "body" or number is None:
            continue
        subject = subject_by_row.get(cell.row) or cell.group or "table row"
        predicate = predicate_slug(
            canonical_cell.header_path[0]
            if canonical_cell.header_path
            else f"column_{cell.column + 1}"
        )
        facts.append(
            CanonicalFact(
                fact_id=stable_id(
                    "fact",
                    table_id,
                    cell_id,
                    subject,
                    predicate,
                    cell.text,
                ),
                subject=subject,
                predicate=predicate,
                value_kind=value_kind,  # type: ignore[arg-type]
                raw_value=cell.text,
                numeric_value=number,
                minimum_value=None,
                maximum_value=None,
                unit_raw=unit_raw,
                unit_ucum=unit_ucum,
                qualifiers=(),
                provenance_ids=(cell_id,),
                confidence=0.8,
                review_status="candidate",
            )
        )
    canonical_table = CanonicalTable(
        table_id=table_id,
        caption=table.caption,
        section_path=tuple(table.section_path),
        row_count=table.row_count,
        column_count=table.column_count,
        cells=tuple(cells),
        footnotes=(),
        provenance=_provenance(
            source_sha256=source_sha256,
            source_path=source_path,
            parser_version=parser_version,
            locator=table.source_locator,
            physical_page=physical_page,
            printed_page=printed_page,
            bounding_box=table.bbox,
        ),
    )
    return canonical_table, tuple(facts)


def _unit_from_header_path(
    path: tuple[str, ...],
) -> tuple[str | None, str | None]:
    for label in reversed(path):
        normalized = normalize_unit(label)
        if normalized[1] is not None:
            return normalized
        tokens = re.findall(
            r"[A-Za-zµμ°³0-9]+(?:/[A-Za-zµμ°³0-9]+)?",
            label,
        )
        for token in reversed(tokens):
            normalized = normalize_unit(token)
            if normalized[1] is not None:
                return normalized
    return None, None


def _provenance(
    *,
    source_sha256: str,
    source_path: str,
    parser_version: str,
    locator: str,
    physical_page: int,
    printed_page: str,
    bounding_box: Any,
) -> Provenance:
    return Provenance(
        source_sha256=source_sha256,
        source_path=source_path,
        parser_id="v3_ocr_adapter",
        parser_version=parser_version,
        locator=locator,
        physical_page=physical_page,
        printed_page=printed_page,
        bounding_box=(
            tuple(float(value) for value in bounding_box)
            if bounding_box is not None
            else None
        ),
    )
