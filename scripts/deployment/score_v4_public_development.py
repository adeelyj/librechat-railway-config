from __future__ import annotations

import argparse
import hashlib
import json
import math
import re
import statistics
import unicodedata
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


SYSTEMS = ("v1", "v2", "v3", "v4")
CASE_IDS = tuple(f"B{index:02d}" for index in range(1, 31))
ABSENCE = (
    "not established",
    "not found",
    "no supported result",
    "no compatible",
    "could not find",
    "cannot find",
    "missing or unresolved",
    "nicht belegt",
    "nicht gefunden",
)

# This rubric intentionally contains only public-development expectations for
# B01-B30. It must never import or read the combined gold or holdout files.
RUBRIC: dict[str, tuple[tuple[str, ...], ...]] = {
    "B01": (
        ("din en 12021",),
        ("b-detection", "continuous online", "continuous gas"),
        ABSENCE,
        ("engineering questions", "must be confirmed", "before selecting"),
    ),
    "B02": (
        ABSENCE,
        ("public bauer", "public document", "uploaded public"),
        ("420 bar",),
        ("500 l/min", "approximately 500"),
    ),
    "B03": (
        ("syn-bk-n2-420-500",),
        ("syn-bk-n2-365-500",),
        ABSENCE,
    ),
    "B04": (
        ("syn-bk-n2-420-500",),
        ABSENCE,
        ("public bauer", "public document", "uploaded public"),
    ),
    "B05": (
        ABSENCE,
        ("synthetic",),
        ("pressure transmitter",),
    ),
    "B06": (
        ("525 bar",),
        ("compressor",),
        ("booster",),
        ("520 bar",),
        ("shutdown pressure is lower", "shut-down pressure lower"),
    ),
    "B07": (
        ("i 15.11-11-v",),
        ("420 l/min",),
        ("420 bar", "525 bar"),
        (
            "4 stages",
            "number of stages: 4",
            "stages: 4",
            "number of stages: i 15.11-11-v: 4",
        ),
        ("11 kw",),
        ("iso 1217",),
    ),
    "B08": (
        ("b-kool",),
        ("550 bar",),
        ("filter cartridge",),
        ("mini-verticus", "verticus"),
        ("b-select",),
        ("420 bar",),
        ("priority valve", "automatic selector"),
    ),
    "B09": (
        ("40 bar",),
        ("660-7390 l/min",),
        ("11-110 kw",),
        ("100 bar",),
        ("630-7300 l/min",),
        ("15-132 kw",),
        ("100 bar is technically closer", "100 bar family"),
    ),
    "B10": (
        ("i/s", "i and s"),
        ("stationary",),
        ("plus m",),
        ("mobile", "portable"),
        ("co",),
        ("co2",),
        ("o2",),
        ("data logger", "logged", "logging"),
        ("420 bar",),
        ("450 bar",),
        ("march 2025", "2025-03", "n42078"),
        ("purge valve",),
    ),
    "B11": (
        ("no", "not supported", "does not establish"),
        ("breathing air",),
        ("300 bar",),
        ("nitrox",),
        ("200 bar",),
    ),
    "B12": (
        ABSENCE,
        ("synthetic",),
        ("english", "german", "englisch", "deutsch"),
    ),
    "B13": (
        ("en iso 3834-2",),
        ("bauer kompressoren gmbh",),
        ("stablistr. 8", "stäblistr. 8"),
        ("81477 munich", "81477 münchen"),
        ("english", "language: en"),
        ("bauer_amfile_216.pdf",),
    ),
    "B14": (
        ABSENCE,
        ("helium",),
        ("synthetic",),
    ),
    "B15": (
        ("en iso 3834-2",),
        ("bauer kompressoren gmbh",),
        ("stablistr. 8", "stäblistr. 8"),
        ("81477 munich",),
        ("english", "language: en"),
        ("bauer_amfile_216.pdf",),
    ),
    "B16": (
        ("bm 6.1/40-11",),
        ("660 l/min",),
        ("40 bar",),
        (
            "3 stages",
            "number of stages: 3",
            "stages: 3",
            "number of stages: bm 6.1/40-11: 3",
            "number of stages: bm series 40 bar - 50 hz: 3",
        ),
        ("1470 rpm",),
        ("11 kw",),
        ("425 kg",),
    ),
    "B17": (
        ("bm 6.1/100-15",),
        ("630 l/min",),
        ("100 bar",),
        (
            "3 stages",
            "number of stages: 3",
            "stages: 3",
            "number of stages: bm 6.1/100-15: 3",
            "number of stages: bm series 100 bar - 50 hz: 3",
        ),
        ("1470 rpm",),
        ("15 kw",),
        ("425 kg",),
    ),
    "B18": (
        ("i 15.11-11-v",),
        ("420 bar",),
        ("525 bar",),
        ("420 l/min",),
        (
            "4 stages",
            "number of stages: 4",
            "stages: 4",
            "number of stages: i 15.11-11-v: 4",
            "number of stages: verticus i 350 - 420 bar: 4",
        ),
        ("1320 rpm",),
        ("11 kw",),
        ("426 kg",),
        ("iso 1217",),
        ("shutdown pressure is lower", "shutdown pressure"),
    ),
    "B19": (
        ("b-kool iii",),
        ("350 bar",),
        ("550 bar",),
        ("200-700 l/min",),
        ("200-650 l/min",),
        ("iso 1217",),
        ("200-420 l/min",),
        ("helium",),
        ("argon",),
    ),
    "B20": (
        ("b-select",),
        ("414/420 bar",),
        ("100-414/420 bar",),
        ("2750 l/min",),
        ("3500 l/min",),
        ("3700 l/min",),
        ("pre-filling", "prefilling"),
        ("diving cylinders",),
        ("refilling",),
    ),
    "B21": (
        ("300 bar",),
        ("200 bar",),
        ("410 bar",),
        ("225/330 bar", "225 bar",),
        ("unresolved", "does not establish", "not an approval"),
    ),
    "B22": (
        ("en iso 3834-2",),
        ("bauer kompressoren gmbh",),
        ("stablistr. 8", "stäblistr. 8"),
        ("81477 munich", "81477 münchen"),
        ("english", "englischen", "language: en"),
        ("bauer_amfile_216.pdf",),
    ),
    "B23": (
        ("n47183",),
        ("bauer b-detection",),
        ("sensor calibration",),
        ("language: en", "english"),
        ("2024-01_b-detection_sensor_calibration_en_n47183_sc.pdf",),
    ),
    "B24": (
        ("n7698",),
        ("large blocks",),
        ("medium pressure",),
        ("k28.3",),
        ("21.0",),
        ("25.0",),
        ("23.1",),
        ("25.4",),
        ("k28.0",),
        ("k28.2",),
    ),
    "B25": (
        ("b-cloud",),
        ("browser",),
        ("b-app",),
        ("smartphone", "tablet", "app"),
        ("fault notifications",),
        ("plain-text diagnostics", "plain text diagnostics"),
        ("3.73",),
        ("version 3.0", "from 3.0"),
        ("update", "updated"),
    ),
    "B26": (
        ("iso 27001",),
        ABSENCE,
    ),
    "B27": (
        ("n99999999",),
        ABSENCE,
    ),
    "B28": (
        ("eplan",),
        ABSENCE,
        ("test archive",),
    ),
    "B29": (
        ("k 22-k 28", "k 22 - k 28"),
        ("600-6800 l/min",),
        ("22-110 kw",),
        ("30-525 bar",),
        ("family-level", "family level", "series"),
    ),
    "B30": (
        ("bm 6.1/40-11",),
        ("660 l/min",),
        ("40 bar",),
        (
            "3 stages",
            "stufenzahl: 3",
            "stages: 3",
            "number of stages: bm 6.1/40-11: 3",
        ),
        ("11 kw",),
        ("0027_bm-series-40",),
    ),
}

SAFE_CASES = frozenset(
    {"B01", "B02", "B03", "B04", "B05", "B12", "B14", "B26", "B27", "B28"}
)


class DevelopmentScoreError(RuntimeError):
    pass


def _load(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8-sig"))
    if not isinstance(value, dict):
        raise DevelopmentScoreError(f"{path.name} is not a JSON object")
    return value


def _normalized(value: str) -> str:
    folded = unicodedata.normalize("NFKD", value).casefold()
    without_marks = "".join(
        character
        for character in folded
        if not unicodedata.combining(character)
    )
    without_marks = (
        without_marks.replace("\u2010", "-")
        .replace("\u2011", "-")
        .replace("\u2012", "-")
        .replace("\u2013", "-")
        .replace("\u2014", "-")
        .replace("\u2212", "-")
    )
    without_marks = re.sub(r"\s*-\s*", "-", without_marks)
    return re.sub(r"\s+", " ", without_marks).strip()


def _matches(answer: str, alternatives: tuple[str, ...]) -> bool:
    normalized = _normalized(answer)
    tokenized = re.sub(r"[^0-9a-z]+", " ", normalized).strip()
    padded = f" {tokenized} "
    return any(
        f" {re.sub(r'[^0-9a-z]+', ' ', _normalized(value)).strip()} "
        in padded
        for value in alternatives
    )


def _percentile(values: list[float], percentile: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    if len(ordered) == 1:
        return ordered[0]
    position = (len(ordered) - 1) * percentile
    lower = math.floor(position)
    upper = math.ceil(position)
    if lower == upper:
        return ordered[lower]
    weight = position - lower
    return ordered[lower] * (1 - weight) + ordered[upper] * weight


def _has_citations(observation: dict[str, Any]) -> bool:
    if int(observation.get("evidence_count") or 0) > 0:
        return True
    evidence = observation.get("evidence")
    return isinstance(evidence, list) and bool(evidence)


def _validate_benchmark(benchmark: dict[str, Any]) -> None:
    observations = benchmark.get("observations")
    if benchmark.get("split") != "development":
        raise DevelopmentScoreError("refusing to score a non-development split")
    if benchmark.get("locked_holdout_opened") is not False:
        raise DevelopmentScoreError("benchmark reports a holdout boundary violation")
    if not isinstance(observations, list) or len(observations) != 120:
        raise DevelopmentScoreError("expected exactly 120 four-way observations")
    observed = {
        (str(item.get("case_id")), str(item.get("system")))
        for item in observations
        if isinstance(item, dict)
    }
    expected = {
        (case_id, system)
        for case_id in CASE_IDS
        for system in SYSTEMS
    }
    if observed != expected:
        raise DevelopmentScoreError("case/system accounting is incomplete")


def score(benchmark: dict[str, Any], *, source_path: Path) -> dict[str, Any]:
    _validate_benchmark(benchmark)
    scored: list[dict[str, Any]] = []
    for observation in benchmark["observations"]:
        case_id = str(observation["case_id"])
        answer = str(observation.get("answer") or "")
        groups = RUBRIC[case_id]
        claim_results = [
            {
                "id": f"public_development_signal_{index + 1}",
                "passed": _matches(answer, alternatives),
                "grounded": (
                    _matches(answer, alternatives)
                    and (
                        case_id in SAFE_CASES
                        or _has_citations(observation)
                    )
                ),
            }
            for index, alternatives in enumerate(groups)
        ]
        absence_passed = any(
            _matches(answer, group)
            for group in groups
            if group is ABSENCE
        )
        unauthorized_count = int(
            observation.get("unauthorized_result_count") or 0
        )
        error = bool(observation.get("error_type"))
        forbidden: list[str] = []
        if unauthorized_count:
            forbidden.append("authorization_scope_violation")
        if case_id in SAFE_CASES and not absence_passed:
            forbidden.append("required_safe_absence_missing")
        exact_passed = (
            bool(answer)
            and not error
            and not forbidden
            and all(item["passed"] for item in claim_results)
        )
        citation_correct = (
            unauthorized_count == 0
            and (
                case_id in SAFE_CASES
                or _has_citations(observation)
            )
        )
        hard_failures = []
        if error:
            hard_failures.append("execution_error")
        if forbidden:
            hard_failures.extend(forbidden)
        if not exact_passed and not hard_failures:
            hard_failures.append("public_development_signal_miss")
        scored.append(
            {
                "case_id": case_id,
                "system": observation["system"],
                "repetition": int(observation.get("repetition") or 1),
                "exact_answer_passed": exact_passed,
                "claim_results": claim_results,
                "forbidden_claims_found": forbidden,
                "incompatible_engineering_claims_found": [],
                "citation_correct": citation_correct,
                "citation_complete": citation_correct,
                "source_type_passed": unauthorized_count == 0,
                "constraint_passed": not error and not forbidden,
                "safe_refusal_passed": (
                    absence_passed if case_id in SAFE_CASES else True
                ),
                "unsupported_identifiers": [],
                "unsupported_numbers": [],
                "validator": {
                    "status": "public_development_rubric",
                    "valid": exact_passed,
                    "safe_refusal_detected": absence_passed,
                    "citation_count": int(
                        observation.get("evidence_count")
                        or len(observation.get("evidence") or [])
                    ),
                    "violations": [
                        {
                            "code": value,
                            "severity": "high",
                            "message": value.replace("_", " "),
                            "value": None,
                        }
                        for value in hard_failures
                    ],
                },
                "hard_failures": hard_failures,
            }
        )

    by_system: dict[str, Any] = {}
    for system in SYSTEMS:
        system_scores = [
            item for item in scored if item["system"] == system
        ]
        system_observations = [
            item
            for item in benchmark["observations"]
            if item["system"] == system
        ]
        all_claims = [
            claim
            for item in system_scores
            for claim in item["claim_results"]
        ]
        safe_scores = [
            item["safe_refusal_passed"]
            for item in system_scores
            if item["case_id"] in SAFE_CASES
        ]
        latencies = [
            float(item.get("elapsed_ms") or 0)
            for item in system_observations
        ]
        by_system[system] = {
            "observations": len(system_scores),
            "exact_answer_accuracy": round(
                statistics.mean(
                    item["exact_answer_passed"]
                    for item in system_scores
                ),
                4,
            ),
            "claim_level_correctness": round(
                statistics.mean(claim["passed"] for claim in all_claims),
                4,
            ),
            "grounded_claim_rate": round(
                statistics.mean(claim["grounded"] for claim in all_claims),
                4,
            ),
            "citation_correctness": round(
                statistics.mean(
                    item["citation_correct"] for item in system_scores
                ),
                4,
            ),
            "citation_completeness": round(
                statistics.mean(
                    item["citation_complete"] for item in system_scores
                ),
                4,
            ),
            "source_type_discipline": round(
                statistics.mean(
                    item["source_type_passed"] for item in system_scores
                ),
                4,
            ),
            "constraint_compliance": round(
                statistics.mean(
                    item["constraint_passed"] for item in system_scores
                ),
                4,
            ),
            "safe_refusal_accuracy": round(
                statistics.mean(safe_scores),
                4,
            ),
            "fabrication_count": sum(
                bool(item["forbidden_claims_found"])
                for item in system_scores
            ),
            "hard_failure_count": sum(
                bool(item["hard_failures"]) for item in system_scores
            ),
            "latency_p50_ms": round(_percentile(latencies, 0.50), 2),
            "latency_p95_ms": round(_percentile(latencies, 0.95), 2),
        }

    rubric_bytes = json.dumps(
        RUBRIC,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return {
        "schema_version": 1,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "source_runs": [str(source_path)],
        "source_run_id": benchmark.get("run_id"),
        "source_run_kind": benchmark.get("kind"),
        "gold_status": (
            "public-development B01-B30 rubric only; locked holdout and "
            "combined gold were not read"
        ),
        "rubric_sha256": hashlib.sha256(rubric_bytes).hexdigest(),
        "retrieval": {},
        "answers": {
            "by_system": by_system,
            "observations": scored,
        },
        "promotion": {
            "private_shadow_only": True,
            "production_promotion": False,
            "locked_holdout_opened": False,
        },
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--benchmark", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    benchmark = _load(args.benchmark)
    result = score(benchmark, source_path=args.benchmark)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(result, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
        newline="\n",
    )
    print(
        json.dumps(
            {
                "output": str(args.output),
                "rubric_sha256": result["rubric_sha256"],
                "locked_holdout_opened": False,
            },
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
