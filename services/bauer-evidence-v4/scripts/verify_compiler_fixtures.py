from __future__ import annotations

import argparse
import hashlib
import json
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

from bauer_evidence_v4.canonical.normalize import predicate_slug
from bauer_evidence_v4.compilation import CanonicalCompiler


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Compile and verify the unlocked Bauer V4 difficult documents."
    )
    parser.add_argument("--fixture", required=True, type=Path)
    parser.add_argument("--source-root", required=True, type=Path)
    parser.add_argument("--gates", type=Path)
    parser.add_argument("--output", type=Path)
    return parser


def _ratio(passed: int, total: int) -> float:
    return passed / max(total, 1)


def _scalar_metadata(
    fixture: dict[str, Any],
    document: Any,
) -> tuple[int, int]:
    expected = fixture.get("expected_metadata", {})
    passed = 0
    total = 0
    for field, value in expected.items():
        if isinstance(value, (dict, list)):
            continue
        total += 1
        actual = getattr(document, field, None)
        passed += actual == value
    return passed, total


def _record_metadata(
    fixture: dict[str, Any],
    document: Any,
) -> tuple[int, int]:
    passed = 0
    total = 0
    for expected in fixture.get("expected_records", []):
        scalar = {
            key: value
            for key, value in expected.items()
            if not isinstance(value, (dict, list))
        }
        anchor = expected.get("file_number")
        record = next(
            (
                item
                for item in document.records
                if anchor is None or item.field("file_number") == anchor
            ),
            None,
        )
        fields = dict(record.fields) if record is not None else {}
        for key, value in scalar.items():
            total += 1
            actual = (
                record.record_type
                if record is not None and key == "record_type"
                else fields.get(key)
            )
            passed += actual == value
    return passed, total


def _table_metrics(
    fixture: dict[str, Any],
    document: Any,
) -> dict[str, int]:
    values = {
        "header_passed": 0,
        "header_total": 0,
        "association_passed": 0,
        "association_total": 0,
        "typed_passed": 0,
        "typed_total": 0,
        "integrity_passed": 0,
        "integrity_total": 0,
        "identifier_passed": 0,
        "identifier_total": 0,
    }
    for expected_table in fixture.get("expected_tables", []):
        candidates = [
            table
            for table in document.tables
            if table.caption == expected_table.get("caption")
        ]
        values["association_total"] += 1
        values["association_passed"] += bool(candidates)
        values["association_total"] += 1
        values["association_passed"] += any(
            table.section_path
            == tuple(expected_table.get("section_path", []))
            for table in candidates
        )
        for expected_note in expected_table.get("footnotes", []):
            values["association_total"] += 1
            values["association_passed"] += any(
                note.marker == str(expected_note["marker"])
                and note.text == expected_note["text"]
                for table in candidates
                for note in table.footnotes
            )
        for expected_header in expected_table.get("headers", []):
            label = expected_header["label"]
            units = expected_header.get("units", []) or [None]
            for unit in units:
                values["header_total"] += 1
                values["header_passed"] += any(
                    label in cell.header_path
                    and (unit is None or cell.unit_raw == unit)
                    for table in candidates
                    for cell in table.cells
                )
        for expected_row in expected_table.get("rows", []):
            model = expected_row["model"]
            group = expected_row.get("group", expected_table.get("group"))
            values["identifier_total"] += 1
            row_matches: list[tuple[Any, int]] = []
            for table in candidates:
                for cell in table.cells:
                    if (
                        cell.role == "row_header"
                        and cell.text == model
                        and (group is None or cell.group == group)
                    ):
                        row_matches.append((table, cell.row))
            values["identifier_passed"] += bool(row_matches)
            for predicate, expected_value in expected_row["values"].items():
                unit_values = (
                    expected_value.items()
                    if isinstance(expected_value, dict)
                    else ((None, expected_value),)
                )
                for unit, numeric in unit_values:
                    values["typed_total"] += 1
                    values["integrity_total"] += 1
                    matching_cells = [
                        cell
                        for table, row_index in row_matches
                        for cell in table.cells
                        if cell.row == row_index
                        and cell.role == "body"
                        and cell.header_path
                        and predicate_slug(cell.header_path[0]) == predicate
                        and (unit is None or cell.unit_raw == unit)
                    ]
                    typed = [
                        cell
                        for cell in matching_cells
                        if cell.numeric_value is not None
                    ]
                    values["typed_passed"] += bool(typed)
                    expected_decimal = Decimal(str(numeric))
                    values["integrity_passed"] += any(
                        cell.numeric_value == expected_decimal
                        for cell in typed
                    )
    return values


def main() -> int:
    args = _parser().parse_args()
    bundle = json.loads(args.fixture.read_text(encoding="utf-8"))
    compiler = CanonicalCompiler()
    results: list[dict[str, Any]] = []
    passed = True
    counters = {
        "metadata_passed": 0,
        "metadata_total": 0,
        "record_passed": 0,
        "record_total": 0,
        "identifier_passed": 0,
        "identifier_total": 0,
        "header_passed": 0,
        "header_total": 0,
        "association_passed": 0,
        "association_total": 0,
        "typed_passed": 0,
        "typed_total": 0,
        "integrity_passed": 0,
        "integrity_total": 0,
    }
    for fixture in bundle["documents"]:
        source = fixture["source"]
        path = args.source_root.joinpath(*source["logical_path"].split("/"))
        payload = path.read_bytes()
        result = compiler.compile(
            payload,
            source_path=source["logical_path"],
            declared_media_type=source["media_type"],
            fixture=fixture,
            enforce_gate=False,
        )
        passed = passed and result.status == "published"
        if result.document is not None:
            metadata_passed, metadata_total = _scalar_metadata(
                fixture, result.document
            )
            record_passed, record_total = _record_metadata(
                fixture, result.document
            )
            table = _table_metrics(fixture, result.document)
            counters["metadata_passed"] += metadata_passed
            counters["metadata_total"] += metadata_total
            counters["record_passed"] += record_passed
            counters["record_total"] += record_total
            for key, value in table.items():
                counters[key] += value
            if fixture.get("expected_metadata", {}).get("document_number"):
                counters["identifier_total"] += 1
                counters["identifier_passed"] += (
                    result.document.document_number
                    == fixture["expected_metadata"]["document_number"]
                )
            for expected_record in fixture.get("expected_records", []):
                if expected_record.get("file_number"):
                    counters["identifier_total"] += 1
                    counters["identifier_passed"] += any(
                        record.field("file_number")
                        == expected_record["file_number"]
                        for record in result.document.records
                    )
        results.append(
            {
                "fixture_id": fixture["fixture_id"],
                "case_ids": fixture["case_ids"],
                "source_path": source["logical_path"],
                "source_sha256": hashlib.sha256(payload).hexdigest(),
                "status": result.status,
                "selected_parser_id": result.selected_parser_id,
                "canonical_sha256": (
                    hashlib.sha256(result.document.to_json().encode("utf-8")).hexdigest()
                    if result.document is not None
                    else None
                ),
                "table_count": (
                    len(result.document.tables)
                    if result.document is not None
                    else 0
                ),
                "typed_cell_count": (
                    sum(
                        cell.numeric_value is not None
                        for table in result.document.tables
                        for cell in table.cells
                    )
                    if result.document is not None
                    else 0
                ),
                "fact_count": (
                    len(result.document.facts)
                    if result.document is not None
                    else 0
                ),
                "candidates": [
                    {
                        "parser_id": candidate.parser_id,
                        "status": candidate.quality.status,
                        "semantic_score": candidate.quality.semantic_score,
                        "issues": [
                            {
                                "code": issue.code,
                                "severity": issue.severity,
                            }
                            for issue in candidate.quality.issues
                        ],
                        "error_type": (
                            candidate.error.split(":", 1)[0]
                            if candidate.error
                            else None
                        ),
                    }
                    for candidate in result.candidates
                ],
            }
        )
    combined_metadata_passed = (
        counters["metadata_passed"] + counters["record_passed"]
    )
    combined_metadata_total = (
        counters["metadata_total"] + counters["record_total"]
    )
    metrics = {
        "golden_document_metadata_fields": _ratio(
            counters["metadata_passed"], counters["metadata_total"]
        ),
        "golden_exact_identifiers_retained": _ratio(
            counters["identifier_passed"], counters["identifier_total"]
        ),
        "golden_table_header_unit_association": _ratio(
            counters["header_passed"], counters["header_total"]
        ),
        "golden_caption_section_footnote_association": _ratio(
            counters["association_passed"],
            counters["association_total"],
        ),
        "numeric_looking_golden_cells_typed": _ratio(
            counters["typed_passed"], counters["typed_total"]
        ),
        "exact_metadata_retrieval": _ratio(
            combined_metadata_passed, combined_metadata_total
        ),
        "table_integrity": _ratio(
            counters["integrity_passed"],
            counters["integrity_total"],
        ),
    }
    gate_sha256 = None
    gate_minimums = {
        "golden_document_metadata_fields": 1.0,
        "golden_exact_identifiers_retained": 1.0,
        "golden_table_header_unit_association": 0.95,
        "golden_caption_section_footnote_association": 0.95,
        "numeric_looking_golden_cells_typed": 0.98,
        "exact_metadata_retrieval": 1.0,
        "table_integrity": 0.9,
    }
    if args.gates:
        gate_payload = json.loads(args.gates.read_text(encoding="utf-8"))
        gate_sha256 = hashlib.sha256(args.gates.read_bytes()).hexdigest()
        gate_minimums = {
            key: gate_payload["minimums"][key]
            for key in gate_minimums
        }
    metric_gates_passed = all(
        metrics[key] >= minimum
        for key, minimum in gate_minimums.items()
    )
    passed = passed and metric_gates_passed
    report = {
        "schema_version": 1,
        "kind": "bauer-rag-v4-compiler-fixture-verification",
        "generated_at_utc": datetime.now(UTC).isoformat(),
        "passed": passed,
        "locked_holdout_opened": False,
        "source_contract_sha256": bundle["source_contract_sha256"],
        "fixture_sha256": hashlib.sha256(args.fixture.read_bytes()).hexdigest(),
        "gate_manifest_sha256": gate_sha256,
        "metrics": metrics,
        "metric_counts": counters,
        "metric_gates_passed": metric_gates_passed,
        "results": results,
    }
    encoded = json.dumps(
        report,
        ensure_ascii=False,
        allow_nan=False,
        indent=2,
        sort_keys=True,
    ) + "\n"
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(encoded, encoding="utf-8")
    else:
        print(encoded, end="")
    return 0 if passed else 2


if __name__ == "__main__":
    raise SystemExit(main())
