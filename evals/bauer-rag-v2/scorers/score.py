from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from collections import defaultdict
from pathlib import Path
from typing import Any


RUNNERS = Path(__file__).resolve().parents[1] / "runners"
sys.path.insert(0, str(RUNNERS))
from common import EVAL_ROOT, load_json, percentile, utc_now, write_json_exclusive  # noqa: E402

SERVICE_ROOT = EVAL_ROOT.parents[1] / "services" / "rag-api-custom"
sys.path.insert(0, str(SERVICE_ROOT))
from bauer_rag_v2.validator import validate_answer  # noqa: E402


IDENTIFIER_RE = re.compile(r"\b(?:SYN-[A-Z0-9-]+|DOC-[A-Z0-9-]+|N\d{4,7})\b", re.I)
NUMBER_UNIT_RE = re.compile(
    r"\b\d+(?:[.,]\d+)?\s*(?:bar|barg|psi|mpa|kpa|l/min|m[³3]/h|cfm|kw|kg|rpm|ppm|%)\b",
    re.I,
)
CITATION_RE = re.compile(
    r"\bV2-\d+\b|\bcitation_[0-9a-f]{32}\b|"
    r"\\ue202turn\d+file\d+|\ue202turn\d+file\d+",
    re.I,
)
REFUSAL_RE = re.compile(
    r"\b(?:not established|not found|no compatible|no matching|cannot determine|cannot confirm|"
    r"does not exist|not available|keine[nrs]?|nicht belegt|nicht festgestellt)\b",
    re.I,
)
STOPWORDS = {
    "this",
    "that",
    "with",
    "from",
    "have",
    "will",
    "into",
    "according",
    "source",
    "document",
    "evidence",
}


def normalize(value: Any) -> str:
    return re.sub(r"\s+", " ", str(value or "").casefold()).strip()


def load_gold() -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
    answers = {
        case["id"]: case
        for case in load_json(EVAL_ROOT / "gold" / "answers.yaml")["cases"]
    }
    evidence = {
        case["id"]: case
        for case in load_json(EVAL_ROOT / "gold" / "evidence.yaml")["cases"]
    }
    gates = load_json(EVAL_ROOT / "gold" / "promotion-gates.json")
    return answers, evidence, gates


def load_cases() -> dict[str, Any]:
    result = {}
    for split in ("development", "holdout"):
        for case in load_json(EVAL_ROOT / "cases" / f"{split}.yaml")["cases"]:
            result[case["id"]] = {**case, "split": split}
    return result


def location_match(result: dict[str, Any], required: dict[str, Any]) -> bool:
    if str(result.get("file_id")) != str(required.get("file_id")):
        return False
    content = normalize(result.get("content"))
    if required.get("page") is not None and result.get("page") is not None:
        if str(result["page"]) != str(required["page"]):
            return False
    for key in ("section", "table", "row"):
        wanted = normalize(required.get(key))
        if not wanted:
            continue
        result_key = {"table": "table_title", "row": "row_label"}.get(key, key)
        actual = result.get(result_key)
        if isinstance(actual, list):
            actual = " / ".join(str(item) for item in actual)
        if wanted not in normalize(actual) and wanted not in content:
            return False
    return True


def retrieval_metrics(
    observations: list[dict[str, Any]],
    evidence_gold: dict[str, Any],
    allowed_file_ids: set[str],
) -> dict[str, Any]:
    scored: list[dict[str, Any]] = []
    latencies: list[float] = []
    authorization_violations = 0
    errors = 0
    for observation in observations:
        required = evidence_gold.get(observation["case_id"], {}).get("required_evidence", [])
        if not required:
            continue
        results = observation.get("results") or []
        latencies.extend(
            [observation["elapsed_ms"]]
            if isinstance(observation.get("elapsed_ms"), (int, float))
            else []
        )
        if observation.get("error") or int(observation.get("http_status", 200)) >= 400:
            errors += 1
        authorization_violations += int(observation.get("unauthorized_result_count") or 0)
        authorization_violations += sum(
            1 for item in results if str(item.get("file_id")) not in allowed_file_ids
        )
        ranks = []
        for gold_item in required:
            rank = next(
                (
                    index
                    for index, result in enumerate(results, start=1)
                    if location_match(result, gold_item)
                ),
                None,
            )
            ranks.append(rank)
        duplicate_keys = [
            (
                str(item.get("file_id")),
                str(item.get("page") or ""),
                normalize(item.get("section")),
                normalize(item.get("table_title")),
                normalize(item.get("row_label")),
                (
                    ""
                    if any(
                        (
                            item.get("page"),
                            item.get("section"),
                            item.get("table_title"),
                            item.get("row_label"),
                        )
                    )
                    else hashlib.sha256(
                        str(item.get("content") or "").encode("utf-8")
                    ).hexdigest()
                ),
            )
            for item in results
        ]
        table_items = [
            item
            for item in required
            if item.get("table") or item.get("requires_structured_row")
        ]
        table_integrity = []
        for item in table_items:
            match = next((result for result in results if location_match(result, item)), None)
            requires_units = item.get(
                "requires_units",
                bool(item.get("table")),
            )
            table_integrity.append(
                bool(
                    match
                    and match.get("headers")
                    and match.get("row_values")
                    and (not requires_units or match.get("units"))
                    and (
                        not item.get("requires_footnotes")
                        or match.get("footnotes")
                    )
                )
            )
        scored.append(
            {
                "case_id": observation["case_id"],
                "category": observation.get("category"),
                "system": observation["system"],
                "repetition": observation.get("repetition"),
                "ranks": ranks,
                "recall_at": {
                    str(k): sum(rank is not None and rank <= k for rank in ranks) / len(ranks)
                    for k in (1, 3, 5, 10)
                },
                "reciprocal_rank": 1 / min(rank for rank in ranks if rank is not None)
                if any(rank is not None for rank in ranks)
                else 0,
                "exact_top1": bool(ranks) and all(rank == 1 for rank in ranks),
                "table_integrity": (
                    sum(table_integrity) / len(table_integrity) if table_integrity else None
                ),
                "duplicate_rate": (
                    (len(duplicate_keys) - len(set(duplicate_keys))) / len(duplicate_keys)
                    if duplicate_keys
                    else 0
                ),
            }
        )

    by_system: dict[str, Any] = {}
    for system in sorted({item["system"] for item in scored}):
        items = [item for item in scored if item["system"] == system]
        exact_categories = {
            "exact_table",
            "exact_table_ambiguity",
            "exact_certificate",
            "exact_document_number",
            "exact_part_number",
            "bilingual_exact",
        }
        exact_items = [
            item for item in items if item.get("category") in exact_categories
        ]
        table_values = [
            item["table_integrity"]
            for item in items
            if item["table_integrity"] is not None
        ]
        system_latencies = [
            observation["elapsed_ms"]
            for observation in observations
            if observation.get("system") == system
            and isinstance(observation.get("elapsed_ms"), (int, float))
        ]
        by_system[system] = {
            "observations": len(items),
            **{
                f"recall_at_{k}": round(
                    sum(item["recall_at"][str(k)] for item in items) / len(items), 4
                )
                if items
                else None
                for k in (1, 3, 5, 10)
            },
            "mean_reciprocal_rank": round(
                sum(item["reciprocal_rank"] for item in items) / len(items), 4
            )
            if items
            else None,
            "exact_metadata_success": round(
                sum(item["exact_top1"] for item in exact_items) / len(exact_items), 4
            )
            if exact_items
            else None,
            "table_integrity": round(sum(table_values) / len(table_values), 4)
            if table_values
            else None,
            "duplicate_rate": round(
                sum(item["duplicate_rate"] for item in items) / len(items), 4
            )
            if items
            else None,
            "latency_p50_ms": percentile(system_latencies, 0.5),
            "latency_p95_ms": percentile(system_latencies, 0.95),
        }
    category_recall_at_5: dict[str, dict[str, float]] = {}
    for category in sorted(
        {str(item["category"]) for item in scored if item.get("category")}
    ):
        category_recall_at_5[category] = {}
        for system in sorted({item["system"] for item in scored}):
            values = [
                item["recall_at"]["5"]
                for item in scored
                if item.get("category") == category and item["system"] == system
            ]
            if values:
                category_recall_at_5[category][system] = round(
                    sum(values) / len(values), 4
                )
    return {
        "by_system": by_system,
        "category_recall_at_5": category_recall_at_5,
        "authorization_violations": authorization_violations,
        "errors": errors,
        "observations": scored,
    }


def claim_passes(answer: str, claim: dict[str, Any]) -> bool:
    text = normalize(answer)
    all_terms = [normalize(term) for term in claim.get("all_terms", [])]
    any_terms = [normalize(term) for term in claim.get("any_terms", [])]
    return all(term in text for term in all_terms) and (
        not any_terms or any(term in text for term in any_terms)
    )


def v2_citation_supports(
    answer: str,
    match: re.Match[str],
    evidence_by_id: dict[str, dict[str, Any]],
) -> bool:
    citation = normalize(match.group(0))
    evidence = evidence_by_id.get(citation)
    if not evidence:
        return False
    start = max(
        answer.rfind(".", 0, match.start()),
        answer.rfind("\n", 0, match.start()),
        answer.rfind(";", 0, match.start()),
    )
    claim = answer[start + 1 : match.start()]
    evidence_text = normalize(evidence.get("content"))
    important = [
        *IDENTIFIER_RE.findall(claim),
        *NUMBER_UNIT_RE.findall(claim),
    ]
    if important:
        return all(normalize(value) in evidence_text for value in important)
    tokens = {
        token
        for token in normalize(claim).split()
        if len(token) >= 4 and token not in STOPWORDS
    }
    return bool(tokens) and any(token in evidence_text for token in tokens)


def answer_metrics(
    observations: list[dict[str, Any]],
    answer_gold: dict[str, Any],
    cases: dict[str, Any],
    allowed_file_ids: set[str],
) -> dict[str, Any]:
    scored = []
    for observation in observations:
        gold = answer_gold.get(observation["case_id"])
        if not gold:
            continue
        answer = str(observation.get("answer") or "")
        answer_normalized = normalize(answer)
        evidence = observation.get("evidence") or observation.get("results") or []
        evidence_blob = normalize(
            "\n".join(
                f"{item.get('citation_id', '')} {item.get('file_id', '')} "
                f"{item.get('filename', '')} {item.get('content', '')}"
                for item in evidence
                if isinstance(item, dict)
            )
        )
        support_blob = evidence_blob
        claim_results = [
            {
                "id": claim["id"],
                "passed": claim_passes(answer, claim),
                "grounded": all(
                    normalize(term) in support_blob for term in claim.get("all_terms", [])
                ),
            }
            for claim in gold.get("required_claims", [])
        ]
        forbidden = [
            phrase
            for phrase in gold.get("forbidden_claims", [])
            if normalize(phrase) in answer_normalized
        ]
        incompatible_engineering = [
            phrase
            for phrase in gold.get("incompatible_engineering_claims", [])
            if normalize(phrase) in answer_normalized
        ]
        refusal_observed = bool(REFUSAL_RE.search(answer))
        safe_refusal_passed = (
            refusal_observed
            if gold.get("safe_refusal_expected")
            else True
        )
        citation_matches = list(CITATION_RE.finditer(answer))
        citations = [match.group(0) for match in citation_matches]
        known_evidence = {
            normalize(item.get("citation_id")): item
            for item in evidence
            if isinstance(item, dict) and item.get("citation_id")
        }
        citation_correct = all(
            not match.group(0).upper().startswith("V2-")
            or v2_citation_supports(answer, match, known_evidence)
            for match in citation_matches
        )
        public_claim_expected = any(
            item.get("source_type", "public_document") == "public_document"
            for item in evidence
            if isinstance(item, dict)
        )
        citation_complete = not public_claim_expected or bool(citations)
        synthetic_ids = [
            identifier
            for identifier in IDENTIFIER_RE.findall(answer)
            if identifier.upper().startswith("SYN-")
        ]
        source_type_passed = not synthetic_ids or "synthetic" in answer_normalized
        validation = validate_answer(
            answer=answer,
            evidence=evidence,
            mandatory_constraints=gold.get("mandatory_constraints", {}),
            safe_refusal_expected=bool(gold.get("safe_refusal_expected")),
        )
        unsupported_identifiers = [
            item["value"]
            for item in validation["violations"]
            if item["code"] == "unsupported_identifier" and item.get("value")
        ]
        unsupported_numbers = [
            item["value"]
            for item in validation["violations"]
            if item["code"] == "unsupported_number" and item.get("value")
        ]
        constraint_passed = not any(
            item["code"] == "mandatory_constraint_missing"
            for item in validation["violations"]
        )
        cross_kb = [
            item.get("file_id")
            for item in evidence
            if isinstance(item, dict)
            and item.get("file_id")
            and str(item["file_id"]) not in allowed_file_ids
        ]
        hard_failures = []
        if unsupported_identifiers:
            hard_failures.append("fabricated_identifier")
        if unsupported_numbers:
            hard_failures.append("unsupported_high_severity_number")
        if incompatible_engineering:
            hard_failures.append("incompatible_engineering_result")
        if synthetic_ids and not source_type_passed:
            hard_failures.append("synthetic_data_presented_as_confirmed")
        if cross_kb:
            hard_failures.append("cross_knowledge_base_evidence")
        if not safe_refusal_passed:
            hard_failures.append("incorrect_safe_refusal")
        if not constraint_passed:
            hard_failures.append("weakened_mandatory_constraint")
        if any(
            item["code"] == "synthetic_source_unlabelled"
            for item in validation["violations"]
        ) and "synthetic_data_presented_as_confirmed" not in hard_failures:
            hard_failures.append("synthetic_data_presented_as_confirmed")
        exact_passed = (
            all(item["passed"] for item in claim_results)
            and not forbidden
            and citation_correct
            and citation_complete
            and safe_refusal_passed
            and source_type_passed
            and constraint_passed
            and not unsupported_identifiers
            and not unsupported_numbers
            and not cross_kb
            and not any(
                item["severity"] == "high" for item in validation["violations"]
            )
        )
        scored.append(
            {
                "case_id": observation["case_id"],
                "system": observation["system"],
                "repetition": observation.get("repetition"),
                "exact_answer_passed": exact_passed,
                "claim_results": claim_results,
                "forbidden_claims_found": forbidden,
                "incompatible_engineering_claims_found": incompatible_engineering,
                "citation_correct": citation_correct,
                "citation_complete": citation_complete,
                "source_type_passed": source_type_passed,
                "constraint_passed": constraint_passed,
                "safe_refusal_passed": safe_refusal_passed,
                "unsupported_identifiers": unsupported_identifiers,
                "unsupported_numbers": unsupported_numbers,
                "cross_knowledge_base_file_ids": cross_kb,
                "validator": validation,
                "hard_failures": hard_failures,
            }
        )

    by_system = {}
    for system in sorted({item["system"] for item in scored}):
        items = [item for item in scored if item["system"] == system]
        claims = [claim for item in items for claim in item["claim_results"]]
        system_observations = [
            item for item in observations if item.get("system") == system
        ]
        latencies = [
            item["elapsed_ms"]
            for item in system_observations
            if isinstance(item.get("elapsed_ms"), (int, float))
        ]
        refusal_items = [
            item
            for item in items
            if answer_gold[item["case_id"]].get("safe_refusal_expected")
        ]
        by_system[system] = {
            "observations": len(items),
            "exact_answer_accuracy": round(
                sum(item["exact_answer_passed"] for item in items) / len(items), 4
            )
            if items
            else None,
            "claim_level_correctness": round(
                sum(claim["passed"] for claim in claims) / len(claims), 4
            )
            if claims
            else None,
            "grounded_claim_rate": round(
                sum(claim["grounded"] for claim in claims) / len(claims), 4
            )
            if claims
            else None,
            "citation_correctness": round(
                sum(
                    item["citation_correct"] and item["citation_complete"]
                    for item in items
                )
                / len(items),
                4,
            )
            if items
            else None,
            "citation_completeness": round(
                sum(item["citation_complete"] for item in items) / len(items), 4
            )
            if items
            else None,
            "source_type_discipline": round(
                sum(item["source_type_passed"] for item in items) / len(items), 4
            )
            if items
            else None,
            "constraint_compliance": round(
                sum(item["constraint_passed"] for item in items) / len(items), 4
            )
            if items
            else None,
            "safe_refusal_accuracy": round(
                sum(item["safe_refusal_passed"] for item in refusal_items)
                / len(refusal_items),
                4,
            )
            if refusal_items
            else None,
            "fabrication_count": sum(
                len(item["unsupported_identifiers"]) + len(item["unsupported_numbers"])
                for item in items
            ),
            "hard_failure_count": sum(len(item["hard_failures"]) for item in items),
            "latency_p50_ms": percentile(latencies, 0.5),
            "latency_p95_ms": percentile(latencies, 0.95),
        }
    category_exact_accuracy: dict[str, dict[str, float]] = {}
    for category in sorted(
        {cases[item["case_id"]]["category"] for item in scored}
    ):
        category_exact_accuracy[category] = {}
        for system in sorted({item["system"] for item in scored}):
            values = [
                item["exact_answer_passed"]
                for item in scored
                if cases[item["case_id"]]["category"] == category
                and item["system"] == system
            ]
            if values:
                category_exact_accuracy[category][system] = round(
                    sum(values) / len(values), 4
                )
    return {
        "by_system": by_system,
        "category_exact_accuracy": category_exact_accuracy,
        "observations": scored,
    }


def compare_v2(answer_report: dict[str, Any]) -> float | None:
    values: dict[tuple[str, Any], dict[str, bool]] = defaultdict(dict)
    for item in answer_report["observations"]:
        values[(item["case_id"], item["repetition"])][item["system"]] = item[
            "exact_answer_passed"
        ]
    comparable = [item for item in values.values() if {"v1", "v2"} <= item.keys()]
    if not comparable:
        return None
    return round(
        sum(item["v2"] or not item["v1"] for item in comparable) / len(comparable),
        4,
    )


def promotion(
    run_kind: str,
    retrieval: dict[str, Any] | None,
    answers: dict[str, Any] | None,
    gates: dict[str, Any],
) -> dict[str, Any]:
    thresholds = gates["thresholds"]
    checks: dict[str, Any] = {}
    if retrieval and "v2" in retrieval["by_system"]:
        v2 = retrieval["by_system"]["v2"]
        v1 = retrieval["by_system"].get("v1")
        recall_delta = (
            round(v2["recall_at_5"] - v1["recall_at_5"], 4)
            if v1
            and v1["recall_at_5"] is not None
            and v2["recall_at_5"] is not None
            else None
        )
        category_deltas = [
            values["v2"] - values["v1"]
            for values in retrieval["category_recall_at_5"].values()
            if {"v1", "v2"} <= values.keys()
        ]
        worst_category_delta = (
            round(min(category_deltas), 4) if category_deltas else None
        )
        checks.update(
            {
                "exact_identifier_document_lookup": {
                    "value": v2["exact_metadata_success"],
                    "target": thresholds["exact_identifier_document_lookup"],
                    "passed": v2["exact_metadata_success"] is not None
                    and v2["exact_metadata_success"]
                    >= thresholds["exact_identifier_document_lookup"],
                },
                "retrieval_p95_seconds": {
                    "value": (v2["latency_p95_ms"] or 0) / 1000,
                    "target": thresholds["retrieval_p95_seconds"],
                    "passed": v2["latency_p95_ms"] is not None
                    and v2["latency_p95_ms"] / 1000
                    < thresholds["retrieval_p95_seconds"],
                },
                "authorization_violations": {
                    "value": retrieval["authorization_violations"],
                    "target": 0,
                    "passed": retrieval["authorization_violations"] == 0,
                },
                "retrieval_recall_at_5_delta": {
                    "value": recall_delta,
                    "target": thresholds["retrieval_recall_at_5_minimum_delta"],
                    "passed": recall_delta is not None
                    and recall_delta
                    >= thresholds["retrieval_recall_at_5_minimum_delta"],
                },
                "worst_category_recall_at_5_delta": {
                    "value": worst_category_delta,
                    "target": -thresholds["maximum_category_regression"],
                    "passed": worst_category_delta is not None
                    and worst_category_delta
                    >= -thresholds["maximum_category_regression"],
                },
            }
        )
    if answers and "v2" in answers["by_system"]:
        v2 = answers["by_system"]["v2"]
        win_rate = compare_v2(answers)
        category_deltas = [
            values["v2"] - values["v1"]
            for values in answers["category_exact_accuracy"].values()
            if {"v1", "v2"} <= values.keys()
        ]
        worst_answer_category_delta = (
            round(min(category_deltas), 4) if category_deltas else None
        )
        checks.update(
            {
                "exact_answer_accuracy": {
                    "value": v2["exact_answer_accuracy"],
                    "target": thresholds["exact_answer_accuracy"],
                    "passed": v2["exact_answer_accuracy"]
                    >= thresholds["exact_answer_accuracy"],
                },
                "citation_correctness": {
                    "value": v2["citation_correctness"],
                    "target": thresholds["citation_correctness"],
                    "passed": v2["citation_correctness"]
                    >= thresholds["citation_correctness"],
                },
                "safe_refusal_repeated_runs": {
                    "value": v2["safe_refusal_accuracy"],
                    "target": thresholds["safe_refusal_repeated_runs"],
                    "passed": v2["safe_refusal_accuracy"] is not None
                    and v2["safe_refusal_accuracy"]
                    >= thresholds["safe_refusal_repeated_runs"],
                },
                "v2_wins_or_ties_v1": {
                    "value": win_rate,
                    "target": thresholds["v2_wins_or_ties_v1"],
                    "passed": win_rate is not None
                    and win_rate >= thresholds["v2_wins_or_ties_v1"],
                },
                "end_to_end_p95_seconds": {
                    "value": (v2["latency_p95_ms"] or 0) / 1000,
                    "target": thresholds["end_to_end_p95_seconds"],
                    "passed": v2["latency_p95_ms"] is not None
                    and v2["latency_p95_ms"] / 1000
                    < thresholds["end_to_end_p95_seconds"],
                },
                "hard_failures": {
                    "value": v2["hard_failure_count"],
                    "target": 0,
                    "passed": v2["hard_failure_count"] == 0,
                },
                "worst_answer_category_accuracy_delta": {
                    "value": worst_answer_category_delta,
                    "target": -thresholds["maximum_category_regression"],
                    "passed": worst_answer_category_delta is not None
                    and worst_answer_category_delta
                    >= -thresholds["maximum_category_regression"],
                },
            }
        )
    complete_gate_set = (
        run_kind == "combined"
        and len(checks) >= 12
        and all(check["passed"] for check in checks.values())
    )
    gold_status = gates["gold_status"]
    gold_verified = gold_status == "verified"
    if gold_verified:
        gold_blocking_reasons = []
    elif str(gold_status).startswith("interim_"):
        gold_blocking_reasons = [
            "Gold has interim review only and requires independent Bauer sign-off."
        ]
    else:
        gold_blocking_reasons = [
            "Gold is provisional and requires Bauer adjudication."
        ]
    return {
        "checks": checks,
        "all_available_checks_pass": bool(checks)
        and all(check["passed"] for check in checks.values()),
        "gold_verified": gold_verified,
        "promotion_allowed": complete_gate_set and gold_verified,
        "blocking_reasons": [
            *gold_blocking_reasons,
            *(
                []
                if run_kind == "combined"
                else ["A combined retrieval and end-to-end report is required for promotion."]
            ),
            *(
                []
                if all(check["passed"] for check in checks.values())
                else ["One or more measured promotion gates failed."]
            ),
        ],
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Score an immutable Bauer RAG V2 raw run.")
    parser.add_argument("--run", type=Path)
    parser.add_argument("--retrieval-run", type=Path)
    parser.add_argument("--answer-run", type=Path)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()

    if args.run and (args.retrieval_run or args.answer_run):
        parser.error("--run cannot be combined with --retrieval-run or --answer-run")
    if not args.run and not (args.retrieval_run and args.answer_run):
        parser.error("supply --run, or both --retrieval-run and --answer-run")
    retrieval_run = load_json(args.retrieval_run) if args.retrieval_run else None
    answer_run = load_json(args.answer_run) if args.answer_run else None
    run = load_json(args.run) if args.run else {
        "kind": "combined",
        "run_id": f"{retrieval_run['run_id']}--{answer_run['run_id']}",
    }
    answers_gold, evidence_gold, gates = load_gold()
    cases = load_cases()
    manifest = load_json(EVAL_ROOT / "baselines" / "corpus-manifest.json")
    allowed = {str(item["file_id"]) for item in manifest["records"]}
    observations = run.get("observations", [])
    retrieval_observations = (
        retrieval_run["observations"] if retrieval_run else observations
    )
    answer_observations = answer_run["observations"] if answer_run else observations
    retrieval = (
        retrieval_metrics(retrieval_observations, evidence_gold, allowed)
        if run.get("kind") in {"retrieval", "combined"}
        else None
    )
    answers = (
        answer_metrics(answer_observations, answers_gold, cases, allowed)
        if run.get("kind") in {"end_to_end", "frozen_evidence", "combined"}
        else None
    )
    sources = [
        str(path.resolve())
        for path in (args.run, args.retrieval_run, args.answer_run)
        if path
    ]
    report = {
        "schema_version": 1,
        "generated_at": utc_now(),
        "source_runs": sources,
        "source_run_id": run.get("run_id"),
        "source_run_kind": run.get("kind"),
        "gold_status": gates["gold_status"],
        "retrieval": retrieval,
        "answers": answers,
        "promotion": promotion(run.get("kind", ""), retrieval, answers, gates),
    }
    output = args.output or EVAL_ROOT / "reports" / f"{run.get('run_id', 'run')}-score.json"
    write_json_exclusive(output, report)
    print(json.dumps(report["promotion"], indent=2, ensure_ascii=False))
    print(output)


if __name__ == "__main__":
    main()
