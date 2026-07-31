#!/usr/bin/env python3
"""Extract the pre-registered five-case V1-V4 development baseline.

The full browser benchmark is intentionally local and ignored by Git.  This
utility promotes only the exact prompts and visible answers needed by the
answer-quality repair into a small, reviewable artifact.  It never accepts a
holdout run and never copies retrieved evidence bodies or authorization data.
"""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
from typing import Any


DEVELOPMENT_CASES_SHA256 = (
    "3619c40bf56955b1fe7f2d689d7d45ed6d5b8d828b87083c58b3d02d2c106e7b"
)
SELECTION_SEED_SHA256 = (
    "f5729c6ed7ce85ffc67191d43e455329bc90f666ffccf79e6827d5b68ef038ad"
)
SELECTED_CASES = ("B06", "B17", "B10", "B19", "B03")
SYSTEMS = ("v1", "v2", "v3", "v4")


def _sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _selected_case_order() -> tuple[str, ...]:
    ranked = sorted(
        (f"B{index:02d}" for index in range(1, 31)),
        key=lambda case_id: _sha256_text(
            f"{SELECTION_SEED_SHA256}|{case_id}"
        ),
    )
    return tuple(ranked[:5])


def build_baseline(source: dict[str, Any]) -> dict[str, Any]:
    if source.get("split") != "development":
        raise ValueError("only the public development split is permitted")
    if source.get("locked_holdout_opened") is not False:
        raise ValueError("refusing a run that opened or omits holdout state")
    if source.get("development_cases_sha256") != DEVELOPMENT_CASES_SHA256:
        raise ValueError("development case-set hash does not match the freeze")
    if _selected_case_order() != SELECTED_CASES:
        raise AssertionError("pre-registered case selection no longer matches")

    by_case: dict[str, dict[str, Any]] = {}
    for observation in source.get("observations", []):
        case_id = observation.get("case_id")
        system = observation.get("system")
        if case_id not in SELECTED_CASES or system not in SYSTEMS:
            continue
        case = by_case.setdefault(
            case_id,
            {
                "case_id": case_id,
                "category": observation.get("category"),
                "prompt": observation.get("prompt"),
                "systems": {},
            },
        )
        if case["prompt"] != observation.get("prompt"):
            raise ValueError(f"prompt changed within case {case_id}")
        systems = case["systems"]
        if system in systems:
            raise ValueError(f"duplicate {case_id}/{system} observation")
        answer = str(observation.get("answer") or "")
        answer_sha256 = observation.get("answer_sha256")
        if answer_sha256 != _sha256_text(answer):
            raise ValueError(f"answer hash mismatch for {case_id}/{system}")
        systems[system] = {
            "answer": answer,
            "answer_sha256": answer_sha256,
            "answer_character_count": observation.get(
                "answer_character_count"
            ),
            "evidence_count": observation.get("evidence_count"),
            "elapsed_ms": observation.get("elapsed_ms"),
            "http_status": observation.get("http_status"),
            "conversation_deleted": observation.get("conversation_deleted"),
            "unauthorized_result_count": observation.get(
                "unauthorized_result_count"
            ),
        }

    missing = [
        f"{case_id}/{system}"
        for case_id in SELECTED_CASES
        for system in SYSTEMS
        if system not in by_case.get(case_id, {}).get("systems", {})
    ]
    if missing:
        raise ValueError(f"missing frozen observations: {missing}")

    return {
        "schema_version": "bauer-rag-v4-answer-quality-baseline-v1",
        "kind": "sanitized-five-case-visible-answer-baseline",
        "split": "public-development",
        "locked_holdout_opened": False,
        "source": {
            "run_id": source.get("run_id"),
            "completed_at_utc": source.get("completed_at_utc"),
            "development_cases_sha256": DEVELOPMENT_CASES_SHA256,
            "librechat_overlay_commit": source.get(
                "librechat_overlay_commit"
            ),
            "v4_candidate_backend_commit": source.get(
                "v4_candidate_backend_commit"
            ),
        },
        "selection": {
            "method": "sort B01-B30 by SHA-256(seed_sha256 + '|' + case_id)",
            "seed_sha256": SELECTION_SEED_SHA256,
            "case_ids": list(SELECTED_CASES),
        },
        "systems": list(SYSTEMS),
        "cases": [by_case[case_id] for case_id in SELECTED_CASES],
        "limitations": [
            "This artifact contains visible answers, not retrieved evidence bodies.",
            "Five public-development cases do not establish broad accuracy.",
            "Token-group presence is diagnostic only and cannot pass a case.",
            "Owner acceptance remains separate.",
        ],
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("source", type=Path)
    parser.add_argument("output", type=Path)
    args = parser.parse_args()
    source = json.loads(args.source.read_text(encoding="utf-8"))
    baseline = build_baseline(source)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(baseline, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    print(
        json.dumps(
            {
                "output": str(args.output),
                "case_ids": list(SELECTED_CASES),
                "observation_count": sum(
                    len(case["systems"]) for case in baseline["cases"]
                ),
                "locked_holdout_opened": False,
            }
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
