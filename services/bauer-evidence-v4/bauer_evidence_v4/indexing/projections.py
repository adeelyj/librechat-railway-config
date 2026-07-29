from __future__ import annotations

import hashlib
import re
from collections import defaultdict
from dataclasses import dataclass
from pathlib import PurePosixPath
from typing import Iterable

from ..canonical.models import (
    CanonicalCell,
    CanonicalDocument,
    CanonicalFact,
    CanonicalTable,
)
from ..canonical.normalize import clean_text, stable_id
from .models import SearchProjection


_PROJECTION_SCHEMA = "bauer-search-projection-v4.1"
_EXACT_IDENTIFIER_RE = re.compile(
    r"\b(?:"
    r"N\d{4,}|"
    r"[A-Z]{1,6}(?:[\s.-]?\d)+(?:[./-][A-Z0-9.]+)*|"
    r"(?:ISO|EN)\s+\d{3,5}(?:-\d+)?"
    r")\b",
    re.IGNORECASE,
)


def _text_hash(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _exact_terms(*values: str | None) -> tuple[str, ...]:
    terms: dict[str, str] = {}
    for value in values:
        if not value:
            continue
        compact = clean_text(value)
        if compact:
            terms.setdefault(compact.casefold(), compact)
        for match in _EXACT_IDENTIFIER_RE.finditer(value):
            term = clean_text(match.group(0))
            terms.setdefault(term.casefold(), term)
    return tuple(terms[key] for key in sorted(terms))


def _title(document: CanonicalDocument) -> str:
    return document.title or document.source_filename


def _context_prefix(
    document: CanonicalDocument,
    *,
    section_path: tuple[str, ...] = (),
) -> str:
    parts = [
        f"Document title: {_title(document)}.",
        f"Source filename: {document.source_filename}.",
    ]
    if document.document_number:
        parts.append(f"Document number: {document.document_number}.")
    if document.language:
        parts.append(f"Language: {document.language}.")
    if document.subject:
        parts.append(f"Subject: {document.subject}.")
    if section_path:
        parts.append(f"Section: {' / '.join(section_path)}.")
    return " ".join(parts)


def _projection(
    document: CanonicalDocument,
    *,
    projection_type: str,
    identity_parts: Iterable[object],
    search_text: str,
    canonical_evidence_ids: tuple[str, ...],
    exact_terms: tuple[str, ...],
    physical_page: int | None = None,
    printed_page: str | None = None,
    section_path: tuple[str, ...] = (),
    table_id: str | None = None,
    row_index: int | None = None,
    subject: str | None = None,
    predicate: str | None = None,
    numeric_value=None,
    minimum_value=None,
    maximum_value=None,
    unit_raw: str | None = None,
    unit_ucum: str | None = None,
    qualifiers: tuple[tuple[str, str], ...] = (),
) -> SearchProjection:
    normalized_text = clean_text(search_text)
    text_sha = _text_hash(normalized_text)
    projection_id = stable_id(
        "projection",
        _PROJECTION_SCHEMA,
        document.source_sha256,
        projection_type,
        *identity_parts,
        text_sha,
    )
    return SearchProjection(
        projection_id=projection_id,
        projection_type=projection_type,  # type: ignore[arg-type]
        projection_schema=_PROJECTION_SCHEMA,
        source_sha256=document.source_sha256,
        source_path=document.source_path,
        title=_title(document),
        source_filename=document.source_filename,
        document_number=document.document_number,
        language=document.language,
        search_text=normalized_text,
        search_text_sha256=text_sha,
        exact_terms=exact_terms,
        canonical_evidence_ids=canonical_evidence_ids,
        physical_page=physical_page,
        printed_page=printed_page,
        section_path=section_path,
        table_id=table_id,
        row_index=row_index,
        subject=subject,
        predicate=predicate,
        numeric_value=numeric_value,
        minimum_value=minimum_value,
        maximum_value=maximum_value,
        unit_raw=unit_raw,
        unit_ucum=unit_ucum,
        qualifiers=qualifiers,
    )


@dataclass(frozen=True, slots=True)
class ProjectionBuilder:
    max_passage_characters: int = 1800

    def build(self, document: CanonicalDocument) -> tuple[SearchProjection, ...]:
        projections: list[SearchProjection] = []
        projections.extend(self._metadata(document))
        projections.extend(self._passages(document))
        projections.extend(self._table_rows(document))
        projections.extend(self._facts(document))
        self._require_traceability(document, projections)
        return tuple(sorted(projections, key=lambda item: item.projection_id))

    def _metadata(
        self,
        document: CanonicalDocument,
    ) -> list[SearchProjection]:
        evidence = (
            (document.blocks[0].block_id,)
            if document.blocks
            else tuple(record.record_id for record in document.records[:1])
        )
        text = _context_prefix(document)
        projections = [
            _projection(
                document,
                projection_type="metadata",
                identity_parts=("document",),
                search_text=text,
                canonical_evidence_ids=evidence,
                exact_terms=_exact_terms(
                    document.title,
                    document.source_filename,
                    document.document_number,
                    document.subject,
                ),
            )
        ]
        for record in document.records:
            fields = dict(record.fields)
            field_text = " ".join(
                f"{key.replace('_', ' ').title()}: {value}."
                for key, value in record.fields
            )
            search_text = (
                f"{_context_prefix(document, section_path=record.section_path)} "
                f"Record type: {record.record_type}. {field_text}"
            )
            projections.append(
                _projection(
                    document,
                    projection_type="metadata",
                    identity_parts=("record", record.record_id),
                    search_text=search_text,
                    canonical_evidence_ids=(record.record_id,),
                    exact_terms=_exact_terms(
                        document.source_filename,
                        document.document_number,
                        *fields.values(),
                    ),
                    physical_page=record.provenance.physical_page,
                    printed_page=record.provenance.printed_page,
                    section_path=record.section_path,
                    subject=fields.get("organization") or fields.get("title"),
                    qualifiers=tuple(record.fields),
                )
            )
        return projections

    def _passages(
        self,
        document: CanonicalDocument,
    ) -> list[SearchProjection]:
        projections: list[SearchProjection] = []
        buffer = []
        buffer_length = 0
        buffer_key: tuple[int | None, tuple[str, ...]] | None = None

        def flush() -> None:
            nonlocal buffer, buffer_length, buffer_key
            if not buffer or buffer_key is None:
                return
            physical_page, section_path = buffer_key
            content = " ".join(block.text for block in buffer)
            prefix = _context_prefix(document, section_path=section_path)
            search_text = f"{prefix} Content: {content}"
            projections.append(
                _projection(
                    document,
                    projection_type="passage",
                    identity_parts=tuple(block.block_id for block in buffer),
                    search_text=search_text,
                    canonical_evidence_ids=tuple(
                        block.block_id for block in buffer
                    ),
                    exact_terms=_exact_terms(
                        document.source_filename,
                        document.document_number,
                        content,
                    ),
                    physical_page=physical_page,
                    printed_page=buffer[0].provenance.printed_page,
                    section_path=section_path,
                )
            )
            buffer = []
            buffer_length = 0
            buffer_key = None

        for block in document.blocks:
            key = (block.provenance.physical_page, block.section_path)
            if buffer_key is not None and (
                key != buffer_key
                or buffer_length + len(block.text) > self.max_passage_characters
            ):
                flush()
            if buffer_key is None:
                buffer_key = key
            buffer.append(block)
            buffer_length += len(block.text) + 1
        flush()
        return projections

    def _table_rows(
        self,
        document: CanonicalDocument,
    ) -> list[SearchProjection]:
        projections: list[SearchProjection] = []
        for table in document.tables:
            by_row: dict[int, list[CanonicalCell]] = defaultdict(list)
            for cell in table.cells:
                by_row[cell.row].append(cell)
            for row_index, cells in sorted(by_row.items()):
                cells.sort(key=lambda item: item.column)
                if not any(
                    cell.role in {"row_header", "body"} and cell.text
                    for cell in cells
                ):
                    continue
                group = next(
                    (cell.group for cell in cells if cell.group),
                    None,
                )
                subject = next(
                    (
                        cell.text
                        for cell in cells
                        if cell.role == "row_header" and cell.text
                    ),
                    group,
                )
                rendered: list[str] = []
                for cell in cells:
                    if not cell.text:
                        continue
                    label = (
                        cell.header_path[0]
                        if cell.header_path
                        else "Model designation"
                        if cell.role == "row_header"
                        else f"Column {cell.column + 1}"
                    )
                    unit = f" [{cell.unit_raw}]" if cell.unit_raw else ""
                    rendered.append(f"{label}{unit}: {cell.text}.")
                footnotes = " ".join(
                    f"Footnote {note.marker}: {note.text}"
                    for note in table.footnotes
                )
                search_text = " ".join(
                    part
                    for part in (
                        _context_prefix(
                            document,
                            section_path=table.section_path,
                        ),
                        f"Table: {table.caption}." if table.caption else "",
                        f"Group: {group}." if group else "",
                        " ".join(rendered),
                        footnotes,
                    )
                    if part
                )
                projections.append(
                    _projection(
                        document,
                        projection_type="table_row",
                        identity_parts=(table.table_id, row_index),
                        search_text=search_text,
                        canonical_evidence_ids=tuple(
                            cell.cell_id for cell in cells
                        ),
                        exact_terms=_exact_terms(
                            document.source_filename,
                            document.document_number,
                            table.caption,
                            group,
                            subject,
                            *(cell.text for cell in cells),
                        ),
                        physical_page=table.provenance.physical_page,
                        printed_page=table.provenance.printed_page,
                        section_path=table.section_path,
                        table_id=table.table_id,
                        row_index=row_index,
                        subject=subject,
                        qualifiers=(("group", group),) if group else (),
                    )
                )
        return projections

    def _facts(
        self,
        document: CanonicalDocument,
    ) -> list[SearchProjection]:
        return [
            self._fact(document, fact)
            for fact in document.facts
        ]

    @staticmethod
    def _fact(
        document: CanonicalDocument,
        fact: CanonicalFact,
    ) -> SearchProjection:
        qualifiers = " ".join(
            f"{key.replace('_', ' ')}: {value}."
            for key, value in fact.qualifiers
        )
        if fact.value_kind == "range":
            value_text = (
                f"{fact.minimum_value} to {fact.maximum_value} "
                f"{fact.unit_raw or ''}"
            ).strip()
        else:
            value_text = (
                f"{fact.raw_value} {fact.unit_raw or ''}"
            ).strip()
        search_text = (
            f"{_context_prefix(document)} "
            f"Subject: {fact.subject}. "
            f"Predicate: {fact.predicate.replace('_', ' ')}. "
            f"Value: {value_text}. {qualifiers}"
        )
        physical_page = next(
            (
                block.provenance.physical_page
                for block in document.blocks
                if block.block_id in fact.provenance_ids
            ),
            next(
                (
                    cell.provenance.physical_page
                    for table in document.tables
                    for cell in table.cells
                    if cell.cell_id in fact.provenance_ids
                ),
                None,
            ),
        )
        return _projection(
            document,
            projection_type="fact",
            identity_parts=(fact.fact_id,),
            search_text=search_text,
            canonical_evidence_ids=fact.provenance_ids,
            exact_terms=_exact_terms(
                document.source_filename,
                document.document_number,
                fact.subject,
                fact.predicate,
                fact.raw_value,
                *(value for _, value in fact.qualifiers),
            ),
            physical_page=physical_page,
            subject=fact.subject,
            predicate=fact.predicate,
            numeric_value=fact.numeric_value,
            minimum_value=fact.minimum_value,
            maximum_value=fact.maximum_value,
            unit_raw=fact.unit_raw,
            unit_ucum=fact.unit_ucum,
            qualifiers=fact.qualifiers,
        )

    @staticmethod
    def _require_traceability(
        document: CanonicalDocument,
        projections: list[SearchProjection],
    ) -> None:
        canonical_ids = {
            block.block_id for block in document.blocks
        } | {
            record.record_id for record in document.records
        } | {
            cell.cell_id
            for table in document.tables
            for cell in table.cells
        }
        for projection in projections:
            if not projection.canonical_evidence_ids:
                raise ValueError(
                    f"projection {projection.projection_id} has no provenance"
                )
            if not set(projection.canonical_evidence_ids) <= canonical_ids:
                raise ValueError(
                    f"projection {projection.projection_id} references "
                    "non-canonical evidence"
                )
