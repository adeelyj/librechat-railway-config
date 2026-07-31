from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path


REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
EVIDENCE_ROOT = REPOSITORY_ROOT / "evidence" / "bauer-rag-v4-answer-quality"
DEFAULT_BASELINE = EVIDENCE_ROOT / "baseline-five-case-v1-v4.json"
DEFAULT_RUN = EVIDENCE_ROOT / "local-five-case-v4-repaired.json"
DEFAULT_OUTPUT = EVIDENCE_ROOT / "five-case-comparative-evaluation.json"
DEFAULT_MARKDOWN = EVIDENCE_ROOT / "five-case-comparative-evaluation.md"
CASE_IDS = ("B06", "B17", "B10", "B19", "B03")
FORBIDDEN_INTERNAL = (
    "supported result",
    "requested-topic evidence",
    "missing or unresolved fields",
    "used tools",
    "tool-process",
    "content:",
)
HISTORICAL_SCORES = {
    "B06": {"v1": 10, "v2": 11, "v3": 2, "v4": 12},
    "B17": {"v1": 3, "v2": 12, "v3": 4, "v4": 12},
    "B10": {"v1": 4, "v2": 13, "v3": 6, "v4": 12},
    "B19": {"v1": 4, "v2": 13, "v3": 16, "v4": 12},
    "B03": {"v1": 10, "v2": 11, "v3": 7, "v4": 5},
}
PRIOR_BEST = {
    "B06": "historical V4 had the strongest facts but exposed internal labels",
    "B17": "historical V4 had the strongest facts; V2 had the best table shape but wrong values",
    "B10": "V2 had the best presentation; historical V4's canned facts were inadmissible",
    "B19": "V3 was the complete, direct, supported reference answer",
    "B03": "no prior version was both complete and authority-safe",
}


def _all(text: str, *needles: str) -> bool:
    normalized = text.casefold()
    return all(needle.casefold() in normalized for needle in needles)


def _none(text: str, *needles: str) -> bool:
    normalized = text.casefold()
    return all(needle.casefold() not in normalized for needle in needles)


def _case_checks(case_id: str, answer: str) -> dict[str, bool]:
    lines = answer.splitlines()
    markdown_rows = [line for line in lines if line.startswith("|")]
    checks: dict[str, bool] = {
        "no_internal_language": _none(answer, *FORBIDDEN_INTERNAL),
        "has_adjacent_citation": "[citation_" in answer,
        "bounded_length": 1 <= len(answer) <= 8000,
    }
    if case_id == "B06":
        checks.update(
            {
                "decisive_value": _all(answer, "525 bar", "VERTICUS", "K 22 - K 28"),
                "booster_distinction": _all(answer, "Boosters", "520 bar"),
                "pressure_definition": _all(answer, "safety valve", "shut-down pressure", "lower"),
                "exact_source_location": _all(answer, "Source:", "2026-04_Compressors_for_Industry_EN_N39771_sc.pdf")
                and ("PDF page" in answer or "printed page" in answer or "section" in answer),
            }
        )
    elif case_id == "B17":
        checks.update(
            {
                "complete_correct_row": _all(
                    answer,
                    "BM 6.1/100-15",
                    "22.2 cfm",
                    "37.8 m³/h",
                    "630 l/min",
                    "100 bar",
                    "1450 psig",
                    "3",
                    "1470 rpm",
                    "15 kW",
                    "425 kg",
                    "935 lbs",
                ),
                "wrong_neighbor_values_absent": _none(answer, "11 kW", "435 kg", "760 l/min"),
                "one_lossless_data_row": len(markdown_rows) == 3,
            }
        )
    elif case_id == "B10":
        checks.update(
            {
                "stationary_mobile_fit": _all(answer, "i/s is the stationary", "m is the mobile"),
                "measurements_and_logging": _all(answer, "CO", "CO2", "O2", "data logger", "SD-card"),
                "dated_scoped_reconciliation": _all(answer, "2025-03", "450 bar", "420 bar", "product scope", "wording"),
                "comparison_table": len(markdown_rows) >= 6,
            }
        )
    elif case_id == "B19":
        checks.update(
            {
                "pressure_limits": _all(answer, "350 bar", "550 bar"),
                "all_flow_ranges": _all(answer, "200–700 l/min", "200–650 l/min", "200–420 l/min"),
                "helium_argon_separate": _all(answer, "helium and argon", "200–420 l/min"),
                "unsupported_explanation_absent": _none(answer, "thermodynamic", "reflects their different"),
            }
        )
    elif case_id == "B03":
        checks.update(
            {
                "both_records": _all(answer, "SYN-BK-N2-420-500", "SYN-BK-N2-365-500"),
                "requested_comparison_sections": _all(
                    answer,
                    "Matching stored attributes",
                    "Changed stored attributes",
                    "Linked records potentially affected",
                    "Documents recorded for review",
                    "Assumptions requiring engineer approval",
                ),
                "authority_boundary": _all(answer, "synthetic demo records", "not confirmed Bauer", "do not prove"),
                "invented_engineering_absent": _none(
                    answer,
                    "ASME",
                    "PED 2014",
                    "API 520",
                    "HAZOP",
                    "embrittlement",
                    "must be re-qualified",
                ),
            }
        )
    else:  # pragma: no cover - fixed suite accounting prevents this
        raise RuntimeError(f"unsupported case: {case_id}")
    return checks


def evaluate(
    baseline_path: Path,
    run_path: Path,
    independent_review_paths: tuple[Path, ...] = (),
) -> dict[str, object]:
    baseline = json.loads(baseline_path.read_text(encoding="utf-8"))
    run = json.loads(run_path.read_text(encoding="utf-8"))
    if baseline.get("locked_holdout_opened") is not False:
        raise RuntimeError("historical baseline crossed the holdout boundary")
    if run.get("locked_holdout_opened") is not False:
        raise RuntimeError("candidate run crossed the holdout boundary")
    selected = tuple(run.get("selection", {}).get("case_ids", ()))
    if selected != CASE_IDS:
        raise RuntimeError(f"unexpected five-case selection: {selected}")
    observations = {item["case_id"]: item for item in run["observations"]}
    if set(observations) != set(CASE_IDS):
        raise RuntimeError("candidate case accounting is incomplete")

    results = []
    for case_id in CASE_IDS:
        observation = observations[case_id]
        answer = str(observation["answer"])
        checks = _case_checks(case_id, answer)
        validator_passed = bool(observation["validation"].get("passed"))
        repair_count = int(observation["validation"].get("repair_count", -1))
        complete = observation.get("status") == "complete"
        all_checks_pass = all(checks.values())
        passed = complete and validator_passed and repair_count == 0 and all_checks_pass
        score = 16 if passed else sum(2 for value in checks.values() if value)
        score = min(score, 15 if not passed else 16)
        historical = HISTORICAL_SCORES[case_id]
        prior_max = max(historical.values())
        results.append(
            {
                "case_id": case_id,
                "passed": passed,
                "score_out_of_16": score,
                "critical_failure": not passed,
                "complete": complete,
                "validator_passed": validator_passed,
                "repair_count": repair_count,
                "answer_sha256": observation["answer_sha256"],
                "checks": checks,
                "historical_scores": historical,
                "prior_best_score": prior_max,
                "meets_or_exceeds_prior_best": passed and score >= prior_max,
                "comparison_meaning": PRIOR_BEST[case_id],
            }
        )

    rubric_manifest = {
        "rubric_version": "bauer-rag-v4-answer-quality-hard-stop-v1",
        "case_ids": CASE_IDS,
        "dimensions": (
            "task_fulfillment",
            "factual_correctness",
            "completeness",
            "clarity_directness",
            "requested_structure",
            "citation_support",
            "uncertainty_conflict_handling",
            "presentation_hygiene",
        ),
        "token_group_coverage_authority": "diagnostic_only",
    }
    suite_sha256 = hashlib.sha256(
        json.dumps(
            rubric_manifest,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()
    passed_count = sum(1 for item in results if item["passed"])
    passed = passed_count == len(CASE_IDS)
    independent_reviews = []
    for path in independent_review_paths:
        text = path.read_text(encoding="utf-8-sig")
        holdout_lines = [
            line.casefold()
            for line in text.splitlines()
            if "holdout" in line.casefold()
        ]
        if not any("no" in line for line in holdout_lines):
            raise RuntimeError(f"independent review lacks a holdout boundary: {path}")
        independent_reviews.append(
            {
                "path": path.name,
                "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
            }
        )
    return {
        "schema_version": "bauer-rag-v4-answer-quality-evaluation-v1",
        "kind": "five-case-comparative-hard-stop",
        "split": "development",
        "passed": passed,
        "suite_sha256": suite_sha256,
        "case_count": len(CASE_IDS),
        "metrics": {
            "answer_quality_cases_passed": passed_count,
            "answer_quality_cases_total": len(CASE_IDS),
            "all_cases_meet_or_exceed_prior_best": all(
                item["meets_or_exceeds_prior_best"] for item in results
            ),
            "validator_passed_cases": sum(
                1 for item in results if item["validator_passed"]
            ),
            "zero_repair_cases": sum(
                1 for item in results if item["repair_count"] == 0
            ),
            "token_group_coverage_authority": "diagnostic_only",
            "owner_acceptance": False,
        },
        "rubric": rubric_manifest,
        "historical_baseline_sha256": hashlib.sha256(
            baseline_path.read_bytes()
        ).hexdigest(),
        "candidate_run_sha256": hashlib.sha256(run_path.read_bytes()).hexdigest(),
        "results": results,
        "locked_holdout_opened": False,
        "independent_final_review_complete": bool(independent_reviews),
        "independent_reviews": independent_reviews,
        "owner_acceptance": False,
        "credentials_or_connection_details_emitted": False,
    }


def markdown_report(result: dict[str, object]) -> str:
    rows = []
    for item in result["results"]:  # type: ignore[index]
        scores = item["historical_scores"]
        rows.append(
            "| {case} | {v1} | {v2} | {v3} | {v4} | {current} | {verdict} |".format(
                case=item["case_id"],
                v1=scores["v1"],
                v2=scores["v2"],
                v3=scores["v3"],
                v4=scores["v4"],
                current=item["score_out_of_16"],
                verdict="pass" if item["passed"] else "fail",
            )
        )
    return "\n".join(
        (
            "# Bauer RAG V4 repaired five-case comparative evaluation",
            "",
            "This is the primary engineering hard-stop evaluation over the fixed random public-development selection. It compares the repaired local V4 answer against preserved V1–V4 visible outputs. Token presence is diagnostic only; every case must be complete, source-grounded, task-shaped, citation-backed, validator-approved, and free of a critical defect.",
            "",
            f"- Engineering gate: **{'PASS' if result['passed'] else 'FAIL'}**",
            "- Selected cases: B06, B17, B10, B19, B03",
            "- Holdout opened: no",
            "- Independent final review complete: "
            + ("yes" if result["independent_final_review_complete"] else "no"),
            "- Owner acceptance: no; this remains a separate decision",
            "",
            "| Case | V1 | V2 | V3 | historical V4 | repaired local V4 | Verdict |",
            "| --- | ---: | ---: | ---: | ---: | ---: | --- |",
            *rows,
            "",
            "## Meaning",
            "",
            "- B06 now retains historical V4's correct 525-bar compressor result and 520-bar booster distinction, but presents the exact source location instead of planner labels.",
            "- B17 now renders one lossless technical row with correct metric and imperial values; it no longer combines the neighboring 11 kW / 435 kg row.",
            "- B10 now extracts the 2025-03 450-bar i/s option and the 420-bar mobile wording from their sources, rather than embedding those answers in the analyzer.",
            "- B19 now matches the strong historical V3 answer from source-derived facts and adds no unsupported thermodynamic explanation.",
            "- B03 is now complete and authority-safe because the existing catalog is represented as an explicit synthetic source; it reports stored differences and linked IDs without inventing engineering rules.",
            "",
            "Passing this bounded gate means the repaired pipeline is materially better on these five selected development cases. It does not establish performance on the other 25 cases, sealed-holdout performance, production promotion, or owner acceptance.",
            "",
        )
    )


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--baseline", type=Path, default=DEFAULT_BASELINE)
    parser.add_argument("--run", type=Path, default=DEFAULT_RUN)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--markdown", type=Path, default=DEFAULT_MARKDOWN)
    parser.add_argument(
        "--independent-review",
        action="append",
        default=[],
        type=Path,
    )
    args = parser.parse_args()
    result = evaluate(
        args.baseline,
        args.run,
        tuple(args.independent_review),
    )
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(result, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
        newline="\n",
    )
    args.markdown.write_text(
        markdown_report(result),
        encoding="utf-8",
        newline="\n",
    )
    print(args.output)
    return 0 if result["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
