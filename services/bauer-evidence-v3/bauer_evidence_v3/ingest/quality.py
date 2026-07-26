from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from typing import Iterable

from .canonical import (
    Attributes,
    Block,
    Cell,
    Document,
    Table,
    attribute_items,
    stable_id,
)


_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_SEVERITY_RANK = {"warning": 1, "quarantine": 2, "fatal": 3}


@dataclass(frozen=True, slots=True)
class QualityPolicy:
    max_source_bytes: int = 64 * 1024 * 1024
    max_pages: int = 500
    max_blocks_per_page: int = 10_000
    max_tables_per_page: int = 200
    max_rows_per_table: int = 1_000
    max_columns_per_table: int = 128
    min_printable_ratio: float = 0.90
    max_replacement_ratio: float = 0.0
    max_control_ratio: float = 0.01


@dataclass(frozen=True, slots=True)
class QualityIssue:
    code: str
    severity: str
    message: str
    location: str


@dataclass(frozen=True, slots=True)
class QualityReport:
    document_id: str
    status: str
    issues: tuple[QualityIssue, ...]
    metrics: Attributes = ()

    @property
    def publishable(self) -> bool:
        return self.status in {"pass", "warning"}


class QualityGateError(ValueError):
    def __init__(self, report: QualityReport) -> None:
        super().__init__(
            f"document {report.document_id} failed quality gate: {report.status}"
        )
        self.report = report


def _issue(
    issues: list[QualityIssue],
    code: str,
    severity: str,
    message: str,
    location: str,
) -> None:
    if severity not in _SEVERITY_RANK:
        raise ValueError(f"unknown quality severity: {severity}")
    issues.append(
        QualityIssue(
            code=code,
            severity=severity,
            message=message,
            location=location,
        )
    )


def _validate_bbox(
    *,
    bbox: tuple[float, float, float, float] | None,
    location: str,
    issues: list[QualityIssue],
) -> None:
    if bbox is None:
        return
    x0, y0, x1, y1 = bbox
    if not (0 <= x0 <= x1 <= 1 and 0 <= y0 <= y1 <= 1):
        _issue(
            issues,
            "bbox_out_of_bounds",
            "fatal",
            "canonical bounding boxes must be normalized and ordered",
            location,
        )


def _validate_confidence(
    *,
    confidence: float | None,
    location: str,
    issues: list[QualityIssue],
) -> None:
    if confidence is not None and not 0 <= confidence <= 1:
        _issue(
            issues,
            "confidence_out_of_bounds",
            "fatal",
            "confidence must be between zero and one",
            location,
        )


def _validate_provenance(
    *,
    identifier: str,
    expected_prefix: str,
    source_locator: str,
    parser_id: str,
    location: str,
    seen_ids: set[str],
    issues: list[QualityIssue],
) -> None:
    if not identifier.startswith(f"{expected_prefix}_"):
        _issue(
            issues,
            "invalid_identifier",
            "fatal",
            f"identifier must have the {expected_prefix}_ prefix",
            location,
        )
    if identifier in seen_ids:
        _issue(
            issues,
            "duplicate_identifier",
            "fatal",
            "canonical identifiers must be unique",
            location,
        )
    seen_ids.add(identifier)
    if not source_locator:
        _issue(
            issues,
            "missing_source_locator",
            "fatal",
            "evidence is missing a source locator",
            location,
        )
    if not parser_id:
        _issue(
            issues,
            "missing_parser_id",
            "fatal",
            "evidence is missing parser provenance",
            location,
        )


def _validate_table(
    *,
    document: Document,
    table: Table,
    page_index: int,
    policy: QualityPolicy,
    issues: list[QualityIssue],
    seen_ids: set[str],
) -> int:
    location = table.source_locator or f"page:{page_index}/table:{table.order}"
    _validate_provenance(
        identifier=table.table_id,
        expected_prefix="table",
        source_locator=table.source_locator,
        parser_id=table.parser_id,
        location=location,
        seen_ids=seen_ids,
        issues=issues,
    )
    _validate_bbox(bbox=table.bbox, location=location, issues=issues)
    _validate_confidence(
        confidence=table.confidence, location=location, issues=issues
    )
    if table.page_index != page_index:
        _issue(
            issues,
            "table_page_mismatch",
            "fatal",
            "table page_index does not match its containing page",
            location,
        )
    if table.row_count <= 0 or table.column_count <= 0:
        _issue(
            issues,
            "invalid_table_shape",
            "fatal",
            "table dimensions must be positive",
            location,
        )
        return 0
    if table.row_count > policy.max_rows_per_table:
        _issue(
            issues,
            "table_row_limit",
            "fatal",
            "table exceeds the configured row limit",
            location,
        )
    if table.column_count > policy.max_columns_per_table:
        _issue(
            issues,
            "table_column_limit",
            "fatal",
            "table exceeds the configured column limit",
            location,
        )
    if table.row_count < 2 or table.column_count < 2:
        _issue(
            issues,
            "degenerate_table",
            "quarantine",
            "a table must contain at least two rows and two columns",
            location,
        )
    if not table.cells:
        _issue(
            issues,
            "empty_table",
            "fatal",
            "table contains no source cells",
            location,
        )
        return 0

    occupancy: dict[tuple[int, int], str] = {}
    active_group: str | None = None
    cell_characters = 0
    for cell in sorted(table.cells, key=lambda item: (item.row, item.column)):
        cell_location = cell.source_locator or (
            f"{location}/row:{cell.row}/column:{cell.column}"
        )
        _validate_provenance(
            identifier=cell.cell_id,
            expected_prefix="cell",
            source_locator=cell.source_locator,
            parser_id=cell.parser_id,
            location=cell_location,
            seen_ids=seen_ids,
            issues=issues,
        )
        _validate_bbox(bbox=cell.bbox, location=cell_location, issues=issues)
        _validate_confidence(
            confidence=cell.confidence,
            location=cell_location,
            issues=issues,
        )
        try:
            expected_id = stable_id(
                "cell",
                document.source_sha256,
                cell.source_locator,
                cell.text,
            )
        except ValueError:
            expected_id = None
        if expected_id is not None and cell.cell_id != expected_id:
            _issue(
                issues,
                "unstable_cell_id",
                "fatal",
                "cell ID does not match its source/content coordinates",
                cell_location,
            )
        if (
            cell.row < 0
            or cell.column < 0
            or cell.row_span < 1
            or cell.column_span < 1
            or cell.row + cell.row_span > table.row_count
            or cell.column + cell.column_span > table.column_count
        ):
            _issue(
                issues,
                "cell_span_out_of_bounds",
                "fatal",
                "cell origin or span exceeds the declared table shape",
                cell_location,
            )
            continue
        for row in range(cell.row, cell.row + cell.row_span):
            for column in range(
                cell.column, cell.column + cell.column_span
            ):
                coordinate = (row, column)
                if coordinate in occupancy:
                    _issue(
                        issues,
                        "overlapping_cell_spans",
                        "fatal",
                        "two source cells occupy the same canonical coordinate",
                        cell_location,
                    )
                else:
                    occupancy[coordinate] = cell.cell_id

        if cell.role == "group_header":
            if not (
                cell.column == 0
                and cell.column_span == table.column_count
            ):
                _issue(
                    issues,
                    "invalid_group_header",
                    "fatal",
                    "group headers must span the complete table width",
                    cell_location,
                )
            active_group = cell.text or None
            if cell.group is not None:
                _issue(
                    issues,
                    "group_header_self_reference",
                    "fatal",
                    "a group header cannot inherit a group",
                    cell_location,
                )
        elif active_group is not None and cell.group != active_group:
            _issue(
                issues,
                "missing_group_inheritance",
                "fatal",
                "cell did not inherit the active full-width group",
                cell_location,
            )
        elif active_group is None and cell.group is not None:
            _issue(
                issues,
                "unknown_group_reference",
                "fatal",
                "cell references a group before its source header",
                cell_location,
            )
        cell_characters += len(cell.text)

    expected_coordinates = table.row_count * table.column_count
    if len(occupancy) < expected_coordinates:
        _issue(
            issues,
            "table_shape_holes",
            "quarantine",
            "declared table shape contains coordinates with no source cell",
            location,
        )
    return cell_characters


def _all_evidence_text(document: Document) -> Iterable[tuple[str, str]]:
    for page in document.pages:
        for block in page.blocks:
            yield block.source_locator, block.text
        for table in page.tables:
            for cell in table.cells:
                yield cell.source_locator, cell.text


def evaluate_document(
    document: Document,
    *,
    source_size: int,
    policy: QualityPolicy | None = None,
) -> QualityReport:
    policy = policy or QualityPolicy()
    issues: list[QualityIssue] = []
    seen_ids: set[str] = set()
    character_count = 0
    block_count = 0
    table_count = 0
    cell_count = 0

    if source_size < 0:
        raise ValueError("source_size cannot be negative")
    if source_size > policy.max_source_bytes:
        _issue(
            issues,
            "source_size_limit",
            "fatal",
            "source exceeds the configured byte limit",
            f"source:{document.source_name}",
        )
    if not _SHA256_RE.fullmatch(document.source_sha256):
        _issue(
            issues,
            "invalid_source_hash",
            "fatal",
            "source_sha256 is not a lowercase SHA-256 digest",
            f"source:{document.source_name}",
        )
    _validate_provenance(
        identifier=document.document_id,
        expected_prefix="document",
        source_locator=f"source:{document.source_name}",
        parser_id=document.parser_id,
        location=f"source:{document.source_name}",
        seen_ids=seen_ids,
        issues=issues,
    )
    try:
        expected_document_id = stable_id(
            "document",
            document.source_sha256,
            f"source:{document.source_name}",
            document.media_type,
        )
    except ValueError:
        expected_document_id = None
    if (
        expected_document_id is not None
        and document.document_id != expected_document_id
    ):
        _issue(
            issues,
            "unstable_document_id",
            "fatal",
            "document ID does not match its source coordinates",
            f"source:{document.source_name}",
        )
    if not document.pages:
        _issue(
            issues,
            "no_pages",
            "fatal",
            "parser produced no canonical pages",
            f"source:{document.source_name}",
        )
    if len(document.pages) > policy.max_pages:
        _issue(
            issues,
            "page_limit",
            "fatal",
            "document exceeds the configured page limit",
            f"source:{document.source_name}",
        )

    document_has_canonical_evidence = any(
        page.blocks or page.tables for page in document.pages
    )
    for expected_index, page in enumerate(document.pages):
        page_location = page.source_locator or f"page:{expected_index}"
        _validate_provenance(
            identifier=page.page_id,
            expected_prefix="page",
            source_locator=page.source_locator,
            parser_id=page.parser_id,
            location=page_location,
            seen_ids=seen_ids,
            issues=issues,
        )
        if page.index != expected_index:
            _issue(
                issues,
                "nonsequential_pages",
                "fatal",
                "page indexes must be zero-based and sequential",
                page_location,
            )
        if (page.width is None) != (page.height is None) or (
            page.width is not None
            and (page.width <= 0 or (page.height or 0) <= 0)
        ):
            _issue(
                issues,
                "invalid_page_dimensions",
                "fatal",
                "page width and height must both be positive or both absent",
                page_location,
            )
        if page.ocr_needed:
            page_signals = dict(page.signals)
            ocr_attempted_but_rejected = (
                page_signals.get("ocr_accepted") == "false"
            )
            _issue(
                issues,
                "ocr_needed",
                (
                    "warning"
                    if ocr_attempted_but_rejected
                    and document_has_canonical_evidence
                    else "quarantine"
                ),
                (
                    "OCR was attempted but the page remained below the "
                    "acceptance threshold"
                    if ocr_attempted_but_rejected
                    else "native PDF evidence is insufficient; OCR is required"
                ),
                page_location,
            )
        if len(page.blocks) > policy.max_blocks_per_page:
            _issue(
                issues,
                "block_limit",
                "fatal",
                "page exceeds the configured block limit",
                page_location,
            )
        if len(page.tables) > policy.max_tables_per_page:
            _issue(
                issues,
                "table_limit",
                "fatal",
                "page exceeds the configured table limit",
                page_location,
            )
        if not page.blocks and not page.tables and not page.ocr_needed:
            _issue(
                issues,
                "empty_page",
                "warning",
                "page contains no canonical evidence",
                page_location,
            )

        for block in page.blocks:
            block_location = block.source_locator or (
                f"{page_location}/block:{block.order}"
            )
            block_count += 1
            character_count += len(block.text)
            _validate_provenance(
                identifier=block.block_id,
                expected_prefix="block",
                source_locator=block.source_locator,
                parser_id=block.parser_id,
                location=block_location,
                seen_ids=seen_ids,
                issues=issues,
            )
            _validate_bbox(
                bbox=block.bbox, location=block_location, issues=issues
            )
            _validate_confidence(
                confidence=block.confidence,
                location=block_location,
                issues=issues,
            )
            if block.page_index != page.index:
                _issue(
                    issues,
                    "block_page_mismatch",
                    "fatal",
                    "block page_index does not match its containing page",
                    block_location,
                )
            try:
                expected_block_id = stable_id(
                    "block",
                    document.source_sha256,
                    block.source_locator,
                    block.text,
                )
            except ValueError:
                expected_block_id = None
            if (
                expected_block_id is not None
                and block.block_id != expected_block_id
            ):
                _issue(
                    issues,
                    "unstable_block_id",
                    "fatal",
                    "block ID does not match its source/content coordinates",
                    block_location,
                )

        for table in page.tables:
            table_count += 1
            cell_count += len(table.cells)
            character_count += _validate_table(
                document=document,
                table=table,
                page_index=page.index,
                policy=policy,
                issues=issues,
                seen_ids=seen_ids,
            )

    evidence_text = list(_all_evidence_text(document))
    joined = "".join(text for _, text in evidence_text)
    if not joined.strip() and not any(page.ocr_needed for page in document.pages):
        _issue(
            issues,
            "no_evidence_text",
            "fatal",
            "document contains no textual evidence",
            f"source:{document.source_name}",
        )
    if joined:
        printable_count = sum(
            character.isprintable() or character in "\n\t"
            for character in joined
        )
        replacement_count = joined.count("\ufffd")
        control_count = sum(
            unicodedata.category(character) == "Cc"
            and character not in "\n\t"
            for character in joined
        )
        printable_ratio = printable_count / len(joined)
        replacement_ratio = replacement_count / len(joined)
        control_ratio = control_count / len(joined)
        if replacement_ratio > policy.max_replacement_ratio:
            _issue(
                issues,
                "replacement_characters",
                "quarantine",
                "decoded evidence contains Unicode replacement characters",
                f"source:{document.source_name}",
            )
        if printable_ratio < policy.min_printable_ratio:
            _issue(
                issues,
                "low_printable_ratio",
                "quarantine",
                "evidence has an unusually low printable-character ratio",
                f"source:{document.source_name}",
            )
        if control_ratio > policy.max_control_ratio:
            _issue(
                issues,
                "control_characters",
                "quarantine",
                "evidence contains too many control characters",
                f"source:{document.source_name}",
            )
    else:
        printable_ratio = 1.0
        replacement_ratio = 0.0
        control_ratio = 0.0

    issues.sort(
        key=lambda issue: (
            -_SEVERITY_RANK[issue.severity],
            issue.location,
            issue.code,
            issue.message,
        )
    )
    highest = max(
        (_SEVERITY_RANK[issue.severity] for issue in issues),
        default=0,
    )
    status = {
        0: "pass",
        1: "warning",
        2: "quarantine",
        3: "fatal",
    }[highest]
    return QualityReport(
        document_id=document.document_id,
        status=status,
        issues=tuple(issues),
        metrics=attribute_items(
            {
                "block_count": block_count,
                "cell_count": cell_count,
                "character_count": character_count,
                "control_ratio": control_ratio,
                "page_count": len(document.pages),
                "printable_ratio": printable_ratio,
                "replacement_ratio": replacement_ratio,
                "source_size": source_size,
                "table_count": table_count,
            }
        ),
    )


def require_publishable(report: QualityReport) -> None:
    if not report.publishable:
        raise QualityGateError(report)
