from __future__ import annotations

from decimal import Decimal
from typing import Any, Iterable

from ..canonical.models import (
    CanonicalDocument,
    CanonicalFact,
    QualityIssue,
    QualityReport,
)
from ..canonical.normalize import normalize_unit, sorted_attributes


def evaluate_document(
    document: CanonicalDocument,
    *,
    fixture: dict[str, Any] | None = None,
) -> QualityReport:
    issues: list[QualityIssue] = []
    score = 0

    if document.title:
        score += 10
    else:
        issues.append(
            QualityIssue(
                code="metadata_title_missing",
                severity="warning",
                message="document title is missing",
            )
        )
    if document.language:
        score += 5
    if document.document_number:
        score += 5
    if document.blocks:
        score += 10
    else:
        issues.append(
            QualityIssue(
                code="document_empty",
                severity="fatal",
                message="parser produced no canonical blocks",
            )
        )
    score += min(20, len(document.tables) * 5)
    score += min(20, len(document.facts))
    score += min(10, len(document.records) * 2)

    replacement_count = sum(
        block.text.count("\ufffd") for block in document.blocks
    )
    if replacement_count:
        issues.append(
            QualityIssue(
                code="unicode_replacement_character",
                severity="quarantine",
                message="canonical text contains Unicode replacement characters",
            )
        )

    for table in document.tables:
        if table.row_count < 2 or table.column_count < 2:
            issues.append(
                QualityIssue(
                    code="degenerate_table",
                    severity="quarantine",
                    message="table grid is not at least 2x2",
                    locator=table.provenance.locator,
                )
            )
        if not any(cell.role == "header" for cell in table.cells):
            issues.append(
                QualityIssue(
                    code="table_header_missing",
                    severity="quarantine",
                    message="table has no canonical header cells",
                    locator=table.provenance.locator,
                )
            )
        numeric_cells = [
            cell for cell in table.cells if cell.numeric_value is not None
        ]
        if numeric_cells:
            score += 3
        if any(cell.header_path for cell in numeric_cells):
            score += 3
        if any(cell.unit_raw for cell in numeric_cells):
            score += 3

    if fixture is not None:
        fixture_score, fixture_issues = _evaluate_fixture(document, fixture)
        score += fixture_score
        issues.extend(fixture_issues)

    severity_rank = {"warning": 1, "quarantine": 2, "fatal": 3}
    maximum = max(
        (severity_rank[issue.severity] for issue in issues),
        default=0,
    )
    status = (
        "fatal"
        if maximum >= 3
        else "quarantine"
        if maximum == 2
        else "warning"
        if maximum == 1
        else "pass"
    )
    return QualityReport(
        status=status,  # type: ignore[arg-type]
        semantic_score=score,
        metrics=sorted_attributes(
            {
                "block_count": len(document.blocks),
                "fact_count": len(document.facts),
                "record_count": len(document.records),
                "table_count": len(document.tables),
                "typed_cell_count": sum(
                    cell.numeric_value is not None
                    for table in document.tables
                    for cell in table.cells
                ),
            }
        ),
        issues=tuple(issues),
    )


def _evaluate_fixture(
    document: CanonicalDocument,
    fixture: dict[str, Any],
) -> tuple[int, list[QualityIssue]]:
    score = 0
    issues: list[QualityIssue] = []

    expected_metadata = fixture.get("expected_metadata", {})
    for field in (
        "title",
        "language",
        "document_number",
        "subject",
        "source_filename",
    ):
        expected = expected_metadata.get(field)
        if expected is None:
            continue
        actual = getattr(document, field)
        if actual == expected:
            score += 20
        else:
            issues.append(
                _fixture_issue(
                    f"fixture_metadata_{field}_mismatch",
                    f"expected {field} {expected!r}, found {actual!r}",
                )
            )

    for expected_record in fixture.get("expected_records", []):
        matching = [
            record
            for record in document.records
            if record.record_type == expected_record["record_type"]
            and record.field("file_number") == expected_record.get("file_number")
        ]
        if not matching:
            issues.append(
                _fixture_issue(
                    "fixture_record_missing",
                    f"record {expected_record.get('file_number')!r} is missing",
                )
            )
            continue
        record = matching[0]
        for field in (
            "title",
            "organization",
            "address",
            "language",
            "file_number",
            "file_size",
            "download_path",
        ):
            expected = expected_record.get(field)
            if expected is None:
                continue
            actual = record.field(field)
            if actual == expected:
                score += 8
            else:
                issues.append(
                    _fixture_issue(
                        "fixture_record_field_mismatch",
                        (
                            f"record {expected_record.get('file_number')} field "
                            f"{field} expected {expected!r}, found {actual!r}"
                        ),
                        record.provenance.locator,
                    )
                )

    for expected_table in fixture.get("expected_tables", []):
        matching_tables = [
            table
            for table in document.tables
            if (
                expected_table.get("caption") is None
                or table.caption == expected_table["caption"]
            )
        ]
        if expected_table.get("group"):
            matching_tables = [
                table
                for table in matching_tables
                if any(
                    cell.role == "group_header"
                    and cell.text == expected_table["group"]
                    for cell in table.cells
                )
            ]
        if not matching_tables:
            issues.append(
                _fixture_issue(
                    "fixture_table_missing",
                    f"table {expected_table.get('caption')!r} is missing",
                )
            )
            continue
        table = matching_tables[0]
        score += 30
        if tuple(expected_table.get("section_path", ())) == table.section_path:
            score += 10
        elif expected_table.get("section_path"):
            issues.append(
                _fixture_issue(
                    "fixture_table_section_mismatch",
                    (
                        f"expected section {expected_table['section_path']!r}, "
                        f"found {table.section_path!r}"
                    ),
                    table.provenance.locator,
                )
            )

        header_labels = {
            label
            for cell in table.cells
            for label in cell.header_path
        }
        raw_units = {
            cell.unit_raw for cell in table.cells if cell.unit_raw is not None
        }
        for header in expected_table.get("headers", []):
            if header["label"] in header_labels:
                score += 4
            else:
                issues.append(
                    _fixture_issue(
                        "fixture_header_missing",
                        f"header {header['label']!r} is missing",
                        table.provenance.locator,
                    )
                )
            for unit in header.get("units", []):
                normalized_raw, _ = normalize_unit(unit)
                if normalized_raw in raw_units:
                    score += 2
                else:
                    issues.append(
                        _fixture_issue(
                            "fixture_unit_missing",
                            f"unit {unit!r} is missing",
                            table.provenance.locator,
                        )
                    )
        expected_notes = {
            (item["marker"], item["text"])
            for item in expected_table.get("footnotes", [])
        }
        actual_notes = {
            (item.marker, item.text) for item in table.footnotes
        }
        missing_notes = expected_notes - actual_notes
        if missing_notes:
            issues.append(
                _fixture_issue(
                    "fixture_footnote_missing",
                    f"footnotes are missing: {sorted(missing_notes)!r}",
                    table.provenance.locator,
                )
            )
        else:
            score += len(expected_notes) * 8

        for row in expected_table.get("rows", []):
            row_issues, row_score = _evaluate_expected_row(
                document.facts,
                row,
            )
            issues.extend(row_issues)
            score += row_score

    for region in fixture.get("expected_regions", []):
        physical_page = region["physical_page"]
        page_texts = {
            block.text
            for block in document.blocks
            if block.provenance.physical_page == physical_page
        }
        for line in region.get("text_lines", []):
            if line in page_texts:
                score += 10
            else:
                issues.append(
                    _fixture_issue(
                        "fixture_region_text_missing",
                        f"page {physical_page} is missing line {line!r}",
                    )
                )
        for expected_fact in region.get("family_level_facts", []):
            matches = [
                fact
                for fact in document.facts
                if fact.predicate == expected_fact["predicate"]
                and fact.subject == region["region"]
                and fact.minimum_value
                == Decimal(str(expected_fact["minimum"]))
                and fact.maximum_value
                == Decimal(str(expected_fact["maximum"]))
                and fact.unit_raw == expected_fact["unit"]
            ]
            if matches:
                score += 20
            else:
                issues.append(
                    _fixture_issue(
                        "fixture_family_fact_missing",
                        (
                            f"page {physical_page} is missing family fact "
                            f"{expected_fact!r}"
                        ),
                    )
                )
    return score, issues


def _evaluate_expected_row(
    facts: Iterable[CanonicalFact],
    row: dict[str, Any],
) -> tuple[list[QualityIssue], int]:
    issues: list[QualityIssue] = []
    score = 0
    group = row.get("group")
    for predicate, unit, expected_value in _flatten_expected_values(row["values"]):
        matches = []
        for fact in facts:
            qualifiers = dict(fact.qualifiers)
            if (
                fact.subject == row["model"]
                and fact.predicate == predicate
                and (group is None or qualifiers.get("group") == group)
                and (
                    unit is None
                    or fact.unit_raw == unit
                )
                and fact.numeric_value == Decimal(str(expected_value))
            ):
                matches.append(fact)
        if matches:
            score += 10
        else:
            issues.append(
                _fixture_issue(
                    "fixture_row_value_missing",
                    (
                        f"row {row['model']!r} group {group!r} is missing "
                        f"{predicate}={expected_value!r} {unit or ''}".strip()
                    ),
                )
            )
    return issues, score


def _flatten_expected_values(
    values: dict[str, Any],
) -> Iterable[tuple[str, str | None, int | float]]:
    for predicate, value in values.items():
        if isinstance(value, dict):
            for unit, number in value.items():
                yield predicate, unit, number
        else:
            yield predicate, None, value


def _fixture_issue(
    code: str,
    message: str,
    locator: str | None = None,
) -> QualityIssue:
    return QualityIssue(
        code=code,
        severity="quarantine",
        message=message,
        locator=locator,
    )
