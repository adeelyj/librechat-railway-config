from __future__ import annotations

from datetime import UTC, datetime
from typing import Any, Mapping

from .io import EvaluationContractError
from .metrics import (
    score_answers,
    score_authorization,
    score_extraction,
    score_latency,
    score_retrieval,
    score_tables,
)


def score_run(suite: Mapping[str, Any], run: Mapping[str, Any]) -> dict[str, Any]:
    if suite.get("split") != run.get("split"):
        raise EvaluationContractError(
            f"Suite split {suite.get('split')!r} does not match run split "
            f"{run.get('split')!r}"
        )
    observations = run.get("observations")
    if not isinstance(observations, Mapping):
        raise EvaluationContractError("run.observations must be an object")

    extraction = score_extraction(
        suite.get("extraction_targets") or [],
        observations.get("extraction") or [],
    )
    retrieval = score_retrieval(
        suite.get("queries") or [],
        observations.get("retrieval") or [],
    )
    answers = score_answers(
        suite.get("queries") or [],
        observations.get("answers") or [],
    )
    tables = score_tables(
        suite.get("table_targets") or [],
        observations.get("tables") or [],
    )
    latency = score_latency(observations.get("latency") or [])
    authorization = score_authorization(
        suite.get("authorization_scope") or {},
        retrieval_observations=observations.get("retrieval") or [],
        answer_observations=observations.get("answers") or [],
        authorization_observations=observations.get("authorization") or [],
    )

    hard_failures: list[dict[str, Any]] = []
    if authorization["violation_count"]:
        hard_failures.append(
            {
                "kind": "authorization_leakage",
                "count": authorization["violation_count"],
            }
        )
    if answers["unsupported_high_risk_claims_accepted"]:
        hard_failures.append(
            {
                "kind": "unsupported_high_risk_claim_accepted",
                "count": answers["unsupported_high_risk_claims_accepted"],
            }
        )

    return {
        "schema_version": 1,
        "generated_at": datetime.now(UTC).isoformat(),
        "run_id": run.get("run_id"),
        "split": run.get("split"),
        "release_id": run.get("release_id"),
        "gold_status": suite.get("gold_status"),
        "metrics": {
            "extraction_qa": extraction,
            "retrieval": retrieval,
            "answer_grounding": answers,
            "table_integrity": tables,
            "latency": latency,
            "authorization_leakage": authorization,
        },
        "hard_failures": hard_failures,
        "promotion": {
            "evaluated": False,
            "allowed": False,
            "reason": (
                "This harness reports independent development/locked metrics; promotion requires "
                "an independent Bauer-reviewed gold manifest and a separate recorded decision."
            ),
        },
    }
