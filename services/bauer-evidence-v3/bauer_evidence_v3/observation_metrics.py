"""Deterministic scoring for canonical extraction and table observations.

The standalone evaluation harness has equivalent metric families for captured
JSON runs.  The executable evaluator cannot depend on that repository-side
package in production, so this module implements the strict release gate used
while the observations are captured from PostgreSQL.
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from typing import Any


def score_canonical_observations(
    *,
    extraction_targets: Sequence[Mapping[str, Any]],
    extraction_observations: Sequence[Mapping[str, Any]],
    table_targets: Sequence[Mapping[str, Any]],
    table_observations: Sequence[Mapping[str, Any]],
) -> tuple[dict[str, Any], tuple[str, ...]]:
    extraction = _score_extraction(
        extraction_targets,
        extraction_observations,
    )
    tables = _score_tables(table_targets, table_observations)
    hard_failures: list[str] = []
    if extraction_targets and not extraction["passed"]:
        hard_failures.append("extraction_target_failed")
    if table_targets and not tables["passed"]:
        hard_failures.append("table_target_failed")
    return (
        {
            "extraction_qa": extraction,
            "table_integrity": tables,
        },
        tuple(hard_failures),
    )


def _score_extraction(
    targets: Sequence[Mapping[str, Any]],
    observations: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    by_source: dict[str, Mapping[str, Any]] = {}
    duplicate_source_ids: list[str] = []
    for observation in observations:
        source_id = _identifier(observation.get("source_id"))
        if not source_id:
            continue
        if source_id in by_source:
            duplicate_source_ids.append(source_id)
        else:
            by_source[source_id] = observation

    missing_source_ids: list[str] = []
    failed_source_ids: list[str] = []
    page_count_mismatch_source_ids: list[str] = []
    citation_failure_source_ids: list[str] = []
    reading_order_failure_source_ids: list[str] = []
    stable_id_failure_source_ids: list[str] = []
    fact_failures: list[dict[str, str]] = []

    for target in targets:
        source_id = _identifier(target.get("source_id"))
        observation = by_source.get(source_id)
        if observation is None:
            missing_source_ids.append(source_id)
            continue
        if _identifier(observation.get("status")).casefold() != "published":
            failed_source_ids.append(source_id)
        if _integer(observation.get("page_count")) != _integer(
            target.get("expected_page_count")
        ):
            page_count_mismatch_source_ids.append(source_id)

        evidence_count = _integer(observation.get("evidence_count"))
        resolvable_count = _integer(
            observation.get("resolvable_evidence_count")
        )
        if evidence_count <= 0 or resolvable_count != evidence_count:
            citation_failure_source_ids.append(source_id)
        reading_checks = _integer(observation.get("reading_order_checks"))
        reading_passes = _integer(observation.get("reading_order_passes"))
        if reading_checks != reading_passes:
            reading_order_failure_source_ids.append(source_id)
        stable_checks = _integer(observation.get("stable_id_checks"))
        stable_matches = _integer(observation.get("stable_id_matches"))
        if stable_checks <= 0 or stable_checks != stable_matches:
            stable_id_failure_source_ids.append(source_id)

        actual_facts = {
            _identifier(fact.get("fact_id")): fact
            for fact in observation.get("facts") or ()
            if isinstance(fact, Mapping)
            and _identifier(fact.get("fact_id"))
        }
        for expected in target.get("facts") or ():
            if not isinstance(expected, Mapping):
                continue
            fact_id = _identifier(expected.get("fact_id"))
            actual = actual_facts.get(fact_id)
            if actual is None:
                fact_failures.append(
                    {
                        "source_id": source_id,
                        "fact_id": fact_id,
                        "kind": "missing",
                    }
                )
                continue
            if _normalized(actual.get("value")) != _normalized(
                expected.get("value")
            ):
                fact_failures.append(
                    {
                        "source_id": source_id,
                        "fact_id": fact_id,
                        "kind": "value_mismatch",
                    }
                )
            if expected.get("unit") is not None and _normalized(
                actual.get("unit")
            ) != _normalized(expected.get("unit")):
                fact_failures.append(
                    {
                        "source_id": source_id,
                        "fact_id": fact_id,
                        "kind": "unit_mismatch",
                    }
                )
            if not bool(actual.get("source_coordinate_resolvable")):
                fact_failures.append(
                    {
                        "source_id": source_id,
                        "fact_id": fact_id,
                        "kind": "coordinate_unresolvable",
                    }
                )

    unexpected_source_ids = sorted(set(by_source) - {
        _identifier(target.get("source_id")) for target in targets
    })
    passed = not any(
        (
            missing_source_ids,
            duplicate_source_ids,
            unexpected_source_ids,
            failed_source_ids,
            page_count_mismatch_source_ids,
            citation_failure_source_ids,
            reading_order_failure_source_ids,
            stable_id_failure_source_ids,
            fact_failures,
        )
    )
    return {
        "passed": passed,
        "target_count": len(targets),
        "observation_count": len(observations),
        "missing_source_ids": missing_source_ids,
        "duplicate_source_ids": sorted(set(duplicate_source_ids)),
        "unexpected_source_ids": unexpected_source_ids,
        "failed_source_ids": failed_source_ids,
        "page_count_mismatch_source_ids": page_count_mismatch_source_ids,
        "citation_failure_source_ids": citation_failure_source_ids,
        "reading_order_failure_source_ids": (
            reading_order_failure_source_ids
        ),
        "stable_id_failure_source_ids": stable_id_failure_source_ids,
        "fact_failures": fact_failures,
    }


def _score_tables(
    targets: Sequence[Mapping[str, Any]],
    observations: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    by_table: dict[str, Mapping[str, Any]] = {}
    duplicate_table_ids: list[str] = []
    for observation in observations:
        table_id = _identifier(observation.get("table_id"))
        if not table_id:
            continue
        if table_id in by_table:
            duplicate_table_ids.append(table_id)
        else:
            by_table[table_id] = observation

    missing_table_ids: list[str] = []
    grid_failure_table_ids: list[str] = []
    cell_failures: list[dict[str, str]] = []
    for target in targets:
        table_id = _identifier(target.get("table_id"))
        observation = by_table.get(table_id)
        if observation is None:
            missing_table_ids.append(table_id)
            continue
        expected_cells = {
            _identifier(cell.get("cell_id")): cell
            for cell in target.get("cells") or ()
            if isinstance(cell, Mapping) and _identifier(cell.get("cell_id"))
        }
        actual_cells = {
            _identifier(cell.get("cell_id")): cell
            for cell in observation.get("cells") or ()
            if isinstance(cell, Mapping) and _identifier(cell.get("cell_id"))
        }
        if (
            _integer(target.get("row_count"))
            != _integer(observation.get("row_count"))
            or _integer(target.get("column_count"))
            != _integer(observation.get("column_count"))
            or set(expected_cells) != set(actual_cells)
        ):
            grid_failure_table_ids.append(table_id)
        for cell_id, expected in expected_cells.items():
            actual = actual_cells.get(cell_id)
            if actual is None:
                cell_failures.append(
                    {
                        "table_id": table_id,
                        "cell_id": cell_id,
                        "kind": "missing",
                    }
                )
                continue
            comparisons = (
                (
                    "value_mismatch",
                    _normalized(actual.get("value"))
                    == _normalized(expected.get("value")),
                ),
                (
                    "row_span_mismatch",
                    _integer(actual.get("row_span"), default=1)
                    == _integer(expected.get("row_span"), default=1),
                ),
                (
                    "column_span_mismatch",
                    _integer(actual.get("column_span"), default=1)
                    == _integer(expected.get("column_span"), default=1),
                ),
            )
            for kind, matches in comparisons:
                if not matches:
                    cell_failures.append(
                        {
                            "table_id": table_id,
                            "cell_id": cell_id,
                            "kind": kind,
                        }
                    )
            if expected.get("unit") is not None and _normalized(
                actual.get("unit")
            ) != _normalized(expected.get("unit")):
                cell_failures.append(
                    {
                        "table_id": table_id,
                        "cell_id": cell_id,
                        "kind": "unit_mismatch",
                    }
                )
            if not bool(actual.get("source_coordinate_resolvable")):
                cell_failures.append(
                    {
                        "table_id": table_id,
                        "cell_id": cell_id,
                        "kind": "coordinate_unresolvable",
                    }
                )

    unexpected_table_ids = sorted(set(by_table) - {
        _identifier(target.get("table_id")) for target in targets
    })
    passed = not any(
        (
            missing_table_ids,
            duplicate_table_ids,
            unexpected_table_ids,
            grid_failure_table_ids,
            cell_failures,
        )
    )
    return {
        "passed": passed,
        "target_count": len(targets),
        "observation_count": len(observations),
        "missing_table_ids": missing_table_ids,
        "duplicate_table_ids": sorted(set(duplicate_table_ids)),
        "unexpected_table_ids": unexpected_table_ids,
        "grid_failure_table_ids": grid_failure_table_ids,
        "cell_failures": cell_failures,
    }


def _identifier(value: Any) -> str:
    return str(value or "").strip()


def _normalized(value: Any) -> str:
    return re.sub(r"\s+", " ", str(value or "").casefold()).strip()


def _integer(value: Any, *, default: int = 0) -> int:
    if value is None:
        return default
    return int(value)
