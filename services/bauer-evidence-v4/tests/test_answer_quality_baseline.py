from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest


SCRIPT = (
    Path(__file__).parents[3]
    / "scripts"
    / "deployment"
    / "build_v4_answer_quality_baseline.py"
)
SPEC = importlib.util.spec_from_file_location("answer_quality_baseline", SCRIPT)
assert SPEC is not None and SPEC.loader is not None
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


def _source() -> dict:
    observations = []
    for case_id in MODULE.SELECTED_CASES:
        for system in MODULE.SYSTEMS:
            answer = f"{case_id} {system} visible answer"
            observations.append(
                {
                    "case_id": case_id,
                    "category": "fixture",
                    "prompt": f"Prompt {case_id}",
                    "system": system,
                    "answer": answer,
                    "answer_sha256": MODULE._sha256_text(answer),
                    "answer_character_count": len(answer),
                    "evidence_count": 1,
                    "elapsed_ms": 10,
                    "http_status": 200,
                    "conversation_deleted": True,
                    "unauthorized_result_count": 0,
                }
            )
    return {
        "split": "development",
        "locked_holdout_opened": False,
        "development_cases_sha256": MODULE.DEVELOPMENT_CASES_SHA256,
        "observations": observations,
    }


def test_selection_is_frozen_and_all_twenty_answers_are_retained() -> None:
    baseline = MODULE.build_baseline(_source())
    assert MODULE._selected_case_order() == MODULE.SELECTED_CASES
    assert [case["case_id"] for case in baseline["cases"]] == list(
        MODULE.SELECTED_CASES
    )
    assert sum(len(case["systems"]) for case in baseline["cases"]) == 20
    assert baseline["locked_holdout_opened"] is False


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("split", "holdout"),
        ("locked_holdout_opened", True),
        ("development_cases_sha256", "wrong"),
    ],
)
def test_non_development_or_unfrozen_sources_are_rejected(
    field: str,
    value,
) -> None:
    source = _source()
    source[field] = value
    with pytest.raises(ValueError):
        MODULE.build_baseline(source)
