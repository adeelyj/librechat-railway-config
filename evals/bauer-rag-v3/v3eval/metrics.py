from __future__ import annotations

import math
import re
from collections import defaultdict
from typing import Any, Iterable, Mapping, Sequence


def _ratio(numerator: int | float, denominator: int | float) -> float | None:
    if not denominator:
        return None
    return round(float(numerator) / float(denominator), 6)


def _normalized(value: Any) -> str:
    return re.sub(r"\s+", " ", str(value or "").casefold()).strip()


def _identifier(value: Any) -> str:
    return str(value or "").strip()


def percentile(values: Iterable[float], fraction: float) -> float | None:
    ordered = sorted(float(value) for value in values)
    if not ordered:
        return None
    position = (len(ordered) - 1) * fraction
    lower = math.floor(position)
    upper = min(lower + 1, len(ordered) - 1)
    weight = position - lower
    return round(ordered[lower] + (ordered[upper] - ordered[lower]) * weight, 3)


def score_extraction(
    targets: Sequence[Mapping[str, Any]],
    observations: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    by_source: dict[str, Mapping[str, Any]] = {}
    duplicate_source_ids: list[str] = []
    for observation in observations:
        source_id = _identifier(observation.get("source_id"))
        if source_id in by_source:
            duplicate_source_ids.append(source_id)
        elif source_id:
            by_source[source_id] = observation

    missing_source_ids: list[str] = []
    expected_pages = extracted_pages = 0
    published = quarantined = failed = 0
    evidence_total = evidence_resolvable = 0
    reading_checks = reading_passes = 0
    stable_id_checks = stable_id_matches = 0
    fact_checks = fact_value_matches = 0
    fact_unit_checks = fact_unit_matches = 0
    fact_coordinate_matches = missing_fact_count = 0
    page_count_mismatches: list[str] = []

    for target in targets:
        source_id = _identifier(target.get("source_id"))
        observation = by_source.get(source_id)
        target_pages = max(0, int(target.get("expected_page_count") or 0))
        expected_pages += target_pages
        if observation is None:
            missing_source_ids.append(source_id)
            continue
        status = _identifier(observation.get("status")).casefold()
        published += status == "published"
        quarantined += status == "quarantined"
        failed += status == "failed"
        actual_pages = max(0, int(observation.get("page_count") or 0))
        extracted_pages += min(actual_pages, target_pages)
        if actual_pages != target_pages:
            page_count_mismatches.append(source_id)
        evidence_total += max(0, int(observation.get("evidence_count") or 0))
        evidence_resolvable += max(
            0, int(observation.get("resolvable_evidence_count") or 0)
        )
        reading_checks += max(0, int(observation.get("reading_order_checks") or 0))
        reading_passes += max(0, int(observation.get("reading_order_passes") or 0))
        stable_id_checks += max(0, int(observation.get("stable_id_checks") or 0))
        stable_id_matches += max(0, int(observation.get("stable_id_matches") or 0))
        expected_facts = {
            _identifier(fact.get("fact_id")): fact
            for fact in (target.get("facts") or [])
            if isinstance(fact, Mapping) and _identifier(fact.get("fact_id"))
        }
        actual_facts = {
            _identifier(fact.get("fact_id")): fact
            for fact in (observation.get("facts") or [])
            if isinstance(fact, Mapping) and _identifier(fact.get("fact_id"))
        }
        for fact_id, expected_fact in expected_facts.items():
            fact_checks += 1
            actual_fact = actual_facts.get(fact_id)
            if actual_fact is None:
                missing_fact_count += 1
                if expected_fact.get("unit") is not None:
                    fact_unit_checks += 1
                continue
            fact_value_matches += _normalized(
                expected_fact.get("value")
            ) == _normalized(actual_fact.get("value"))
            expected_unit = expected_fact.get("unit")
            if expected_unit is not None:
                fact_unit_checks += 1
                fact_unit_matches += _normalized(expected_unit) == _normalized(
                    actual_fact.get("unit")
                )
            fact_coordinate_matches += bool(
                actual_fact.get("source_coordinate_resolvable")
            )

    return {
        "expected_source_count": len(targets),
        "observed_source_count": len(targets) - len(missing_source_ids),
        "source_accounting_rate": _ratio(
            len(targets) - len(missing_source_ids), len(targets)
        ),
        "published_source_rate": _ratio(published, len(targets)),
        "published_count": published,
        "quarantined_count": quarantined,
        "failed_count": failed,
        "missing_source_ids": missing_source_ids,
        "duplicate_source_ids": sorted(set(duplicate_source_ids)),
        "expected_page_count": expected_pages,
        "resolved_page_count": extracted_pages,
        "page_completeness": _ratio(extracted_pages, expected_pages),
        "page_count_mismatch_source_ids": page_count_mismatches,
        "citation_resolvability": _ratio(evidence_resolvable, evidence_total),
        "reading_order_accuracy": _ratio(reading_passes, reading_checks),
        "deterministic_id_rate": _ratio(stable_id_matches, stable_id_checks),
        "fact_value_accuracy": _ratio(fact_value_matches, fact_checks),
        "fact_unit_accuracy": _ratio(fact_unit_matches, fact_unit_checks),
        "fact_coordinate_resolvability": _ratio(
            fact_coordinate_matches, fact_checks
        ),
        "missing_fact_count": missing_fact_count,
    }


def _result_evidence_id(result: Mapping[str, Any]) -> str:
    return _identifier(result.get("evidence_id") or result.get("search_unit_id"))


def score_retrieval(
    queries: Sequence[Mapping[str, Any]],
    observations: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    targets = {
        _identifier(query.get("case_id")): query
        for query in queries
        if query.get("retrieval") is not None
    }
    by_case: dict[str, list[Mapping[str, Any]]] = defaultdict(list)
    for observation in observations:
        by_case[_identifier(observation.get("case_id"))].append(observation)

    recalls: dict[int, list[float]] = {1: [], 3: [], 5: []}
    reciprocal_ranks: list[float] = []
    exact_passes: list[bool] = []
    duplicate_count = result_count = 0
    missing_case_ids: list[str] = []
    observation_count = 0
    per_case: dict[str, Any] = {}

    for case_id, query in targets.items():
        case_observations = by_case.get(case_id, [])
        if not case_observations:
            missing_case_ids.append(case_id)
            continue
        required = {
            _identifier(item)
            for item in (query.get("retrieval") or {}).get(
                "required_evidence_ids", []
            )
            if _identifier(item)
        }
        case_recall_at_5: list[float] = []
        case_rr: list[float] = []
        for observation in case_observations:
            observation_count += 1
            results = [
                item
                for item in (observation.get("results") or [])
                if isinstance(item, Mapping)
            ]
            ids = [_result_evidence_id(item) for item in results]
            duplicate_count += len(ids) - len(set(ids))
            result_count += len(ids)
            if required:
                for cutoff in recalls:
                    value = len(required.intersection(ids[:cutoff])) / len(required)
                    recalls[cutoff].append(value)
                    if cutoff == 5:
                        case_recall_at_5.append(value)
                rank = next(
                    (
                        index
                        for index, evidence_id in enumerate(ids, start=1)
                        if evidence_id in required
                    ),
                    None,
                )
                rr = 1.0 / rank if rank else 0.0
                reciprocal_ranks.append(rr)
                case_rr.append(rr)
                if bool((query.get("retrieval") or {}).get("exact_lookup")):
                    exact_passes.append(bool(ids and ids[0] in required))
        per_case[case_id] = {
            "observation_count": len(case_observations),
            "recall_at_5": (
                round(sum(case_recall_at_5) / len(case_recall_at_5), 6)
                if case_recall_at_5
                else None
            ),
            "reciprocal_rank": (
                round(sum(case_rr) / len(case_rr), 6) if case_rr else None
            ),
        }

    return {
        "target_case_count": len(targets),
        "observation_count": observation_count,
        "missing_case_ids": missing_case_ids,
        "recall_at_1": (
            round(sum(recalls[1]) / len(recalls[1]), 6) if recalls[1] else None
        ),
        "recall_at_3": (
            round(sum(recalls[3]) / len(recalls[3]), 6) if recalls[3] else None
        ),
        "recall_at_5": (
            round(sum(recalls[5]) / len(recalls[5]), 6) if recalls[5] else None
        ),
        "mean_reciprocal_rank": (
            round(sum(reciprocal_ranks) / len(reciprocal_ranks), 6)
            if reciprocal_ranks
            else None
        ),
        "exact_lookup_accuracy": (
            _ratio(sum(exact_passes), len(exact_passes)) if exact_passes else None
        ),
        "duplicate_rate": _ratio(duplicate_count, result_count),
        "per_case": per_case,
    }


def score_tables(
    targets: Sequence[Mapping[str, Any]],
    observations: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    by_table = {
        _identifier(observation.get("table_id")): observation
        for observation in observations
        if _identifier(observation.get("table_id"))
    }
    missing_table_ids: list[str] = []
    grid_checks = grid_matches = 0
    cell_checks = value_matches = unit_checks = unit_matches = 0
    span_matches = coordinate_matches = integrity_matches = 0
    missing_cell_count = unexpected_cell_count = 0

    for target in targets:
        table_id = _identifier(target.get("table_id"))
        actual = by_table.get(table_id)
        if actual is None:
            missing_table_ids.append(table_id)
            continue
        expected_cells = {
            _identifier(cell.get("cell_id")): cell
            for cell in (target.get("cells") or [])
            if isinstance(cell, Mapping) and _identifier(cell.get("cell_id"))
        }
        actual_cells = {
            _identifier(cell.get("cell_id")): cell
            for cell in (actual.get("cells") or [])
            if isinstance(cell, Mapping) and _identifier(cell.get("cell_id"))
        }
        grid_checks += 1
        expected_shape = (
            int(target.get("row_count") or 0),
            int(target.get("column_count") or 0),
        )
        actual_shape = (
            int(actual.get("row_count") or 0),
            int(actual.get("column_count") or 0),
        )
        grid_match = (
            set(expected_cells) == set(actual_cells)
            and expected_shape == actual_shape
        )
        grid_matches += grid_match
        missing_cell_count += len(set(expected_cells) - set(actual_cells))
        unexpected_cell_count += len(set(actual_cells) - set(expected_cells))

        for cell_id, expected in expected_cells.items():
            cell_checks += 1
            observed = actual_cells.get(cell_id)
            if observed is None:
                if expected.get("unit") is not None:
                    unit_checks += 1
                continue
            value_match = _normalized(expected.get("value")) == _normalized(
                observed.get("value")
            )
            value_matches += value_match
            expected_unit = expected.get("unit")
            unit_match = True
            if expected_unit is not None:
                unit_checks += 1
                unit_match = _normalized(expected_unit) == _normalized(
                    observed.get("unit")
                )
                unit_matches += unit_match
            span_match = (
                int(expected.get("row_span") or 1)
                == int(observed.get("row_span") or 1)
                and int(expected.get("column_span") or 1)
                == int(observed.get("column_span") or 1)
            )
            span_matches += span_match
            coordinate_match = bool(observed.get("source_coordinate_resolvable"))
            coordinate_matches += coordinate_match
            integrity_matches += (
                value_match and unit_match and span_match and coordinate_match
            )

    return {
        "expected_table_count": len(targets),
        "observed_table_count": len(targets) - len(missing_table_ids),
        "missing_table_ids": missing_table_ids,
        "grid_exact_rate": _ratio(grid_matches, grid_checks),
        "cell_value_accuracy": _ratio(value_matches, cell_checks),
        "unit_accuracy": _ratio(unit_matches, unit_checks),
        "span_accuracy": _ratio(span_matches, cell_checks),
        "cell_coordinate_resolvability": _ratio(coordinate_matches, cell_checks),
        "overall_cell_integrity": _ratio(integrity_matches, cell_checks),
        "missing_cell_count": missing_cell_count,
        "unexpected_cell_count": unexpected_cell_count,
    }


def score_answers(
    queries: Sequence[Mapping[str, Any]],
    observations: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    targets = {
        _identifier(query.get("case_id")): query
        for query in queries
        if query.get("answer") is not None
    }
    by_case: dict[str, list[Mapping[str, Any]]] = defaultdict(list)
    for observation in observations:
        by_case[_identifier(observation.get("case_id"))].append(observation)

    important_claims = supported_claims = citation_valid_claims = 0
    constraint_checks = constraint_passes = 0
    refusal_checks = refusal_passes = 0
    unsupported_high_risk_accepted = 0
    validator_decisions: dict[str, int] = defaultdict(int)
    missing_case_ids: list[str] = []
    observation_count = 0

    for case_id, query in targets.items():
        case_observations = by_case.get(case_id, [])
        if not case_observations:
            missing_case_ids.append(case_id)
            continue
        target = query.get("answer") or {}
        expected_constraints = {
            _identifier(value)
            for value in target.get("required_constraints", [])
            if _identifier(value)
        }
        refusal_expected = bool(target.get("refusal_expected"))
        for observation in case_observations:
            observation_count += 1
            decision = _identifier(observation.get("validator_decision")).casefold()
            validator_decisions[decision or "missing"] += 1
            accepted = decision in {"accepted", "repaired"}
            for claim in observation.get("claims") or []:
                if not isinstance(claim, Mapping) or not bool(claim.get("important")):
                    continue
                important_claims += 1
                supported = bool(claim.get("supported"))
                supported_claims += supported
                citation_valid_claims += bool(claim.get("citation_valid"))
                if bool(claim.get("high_risk")) and not supported and accepted:
                    unsupported_high_risk_accepted += 1
            actual_constraints = {
                _identifier(item.get("id")): bool(item.get("preserved"))
                for item in (observation.get("constraints") or [])
                if isinstance(item, Mapping) and _identifier(item.get("id"))
            }
            for constraint_id in expected_constraints:
                constraint_checks += 1
                constraint_passes += actual_constraints.get(constraint_id, False)
            if refusal_expected:
                refusal_checks += 1
                refusal_passes += bool(observation.get("refused"))

    return {
        "target_case_count": len(targets),
        "observation_count": observation_count,
        "missing_case_ids": missing_case_ids,
        "important_claim_support_rate": _ratio(
            supported_claims, important_claims
        ),
        "important_claim_citation_correctness": _ratio(
            citation_valid_claims, important_claims
        ),
        "constraint_preservation_rate": _ratio(
            constraint_passes, constraint_checks
        ),
        "safe_abstention_accuracy": _ratio(refusal_passes, refusal_checks),
        "unsupported_high_risk_claims_accepted": unsupported_high_risk_accepted,
        "validator_decisions": dict(sorted(validator_decisions.items())),
    }


def score_latency(observations: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    groups: dict[tuple[str, str], list[Mapping[str, Any]]] = defaultdict(list)
    for observation in observations:
        stage = _identifier(observation.get("stage")) or "unknown"
        route = _identifier(observation.get("route")) or "unknown"
        groups[(stage, route)].append(observation)

    by_route: dict[str, Any] = {}
    for (stage, route), records in sorted(groups.items()):
        elapsed = [
            float(record["elapsed_ms"])
            for record in records
            if isinstance(record.get("elapsed_ms"), (int, float))
            and float(record["elapsed_ms"]) >= 0
        ]
        errors = sum(
            1
            for record in records
            if record.get("success") is False or bool(record.get("error"))
        )
        by_route[f"{stage}:{route}"] = {
            "stage": stage,
            "route": route,
            "sample_count": len(records),
            "timed_sample_count": len(elapsed),
            "error_count": errors,
            "p50_ms": percentile(elapsed, 0.50),
            "p95_ms": percentile(elapsed, 0.95),
            "p99_ms": percentile(elapsed, 0.99),
        }
    return {"by_route": by_route}


def score_authorization(
    scope: Mapping[str, Any],
    *,
    retrieval_observations: Sequence[Mapping[str, Any]],
    answer_observations: Sequence[Mapping[str, Any]],
    authorization_observations: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    expected_tenant = _identifier(scope.get("tenant_id"))
    expected_kb = _identifier(scope.get("knowledge_base_id"))
    allowed_releases = {
        _identifier(value)
        for value in scope.get("allowed_release_ids", [])
        if _identifier(value)
    }
    allowed_sources = {
        _identifier(value)
        for value in scope.get("allowed_source_ids", [])
        if _identifier(value)
    }
    scope_configured = bool(
        expected_tenant and expected_kb and allowed_releases and allowed_sources
    )
    violations: list[dict[str, Any]] = []

    def inspect_item(item: Mapping[str, Any], location: str) -> None:
        coordinates = {
            "tenant_id": _identifier(item.get("tenant_id")),
            "knowledge_base_id": _identifier(item.get("knowledge_base_id")),
            "release_id": _identifier(item.get("release_id")),
            "source_id": _identifier(
                item.get("source_id") or item.get("source_document_id")
            ),
        }
        expected_checks = (
            ("tenant_id", expected_tenant),
            ("knowledge_base_id", expected_kb),
        )
        for field, expected in expected_checks:
            if expected and coordinates[field] != expected:
                violations.append(
                    {
                        "kind": f"{field}_mismatch",
                        "location": location,
                        "actual": coordinates[field],
                        "expected": expected,
                    }
                )
        if allowed_releases and coordinates["release_id"] not in allowed_releases:
            violations.append(
                {
                    "kind": "release_scope_mismatch",
                    "location": location,
                    "actual": coordinates["release_id"],
                }
            )
        if allowed_sources and coordinates["source_id"] not in allowed_sources:
            violations.append(
                {
                    "kind": "source_scope_mismatch",
                    "location": location,
                    "actual": coordinates["source_id"],
                }
            )

    for observation_index, observation in enumerate(retrieval_observations):
        for result_index, result in enumerate(observation.get("results") or []):
            if isinstance(result, Mapping):
                inspect_item(
                    result,
                    f"retrieval[{observation_index}].results[{result_index}]",
                )
    for observation_index, observation in enumerate(answer_observations):
        for evidence_index, evidence in enumerate(observation.get("evidence") or []):
            if isinstance(evidence, Mapping):
                inspect_item(
                    evidence,
                    f"answers[{observation_index}].evidence[{evidence_index}]",
                )
    explicit_counter_total = 0
    for observation_index, observation in enumerate(authorization_observations):
        counter = max(0, int(observation.get("unauthorized_result_count") or 0))
        explicit_counter_total += counter
        if counter:
            violations.append(
                {
                    "kind": "explicit_unauthorized_result_count",
                    "location": f"authorization[{observation_index}]",
                    "count": counter,
                }
            )
        if observation.get("authorized") is False and observation.get("delivered") is True:
            violations.append(
                {
                    "kind": "explicit_denied_item_delivered",
                    "location": f"authorization[{observation_index}]",
                }
            )

    if not scope_configured:
        violations.append(
            {
                "kind": "authorization_scope_not_configured",
                "location": "suite.authorization_scope",
            }
        )
    return {
        "scope_configured": scope_configured,
        "explicit_unauthorized_result_count": explicit_counter_total,
        "violation_count": len(violations),
        "passed": scope_configured and not violations,
        "violations": violations,
    }
