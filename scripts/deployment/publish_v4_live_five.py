from __future__ import annotations

import argparse
import hashlib
import json
import re
from pathlib import Path
from typing import Any


REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
EVIDENCE_ROOT = REPOSITORY_ROOT / "evidence" / "bauer-rag-v4-answer-quality"
CASE_IDS = ("B06", "B17", "B10", "B19", "B03")
RELEASE_UUID = "ccb9e8ea-5894-405f-aac4-c4eae5b0d661"
RELEASE_PUBLIC_ID = "bauer-rag-v4-private-20260731-aq-r2"
API_DEPLOYMENT_ID = "4a8ce7b7-968e-480f-a879-09606af9c70e"
WORKER_DEPLOYMENT_ID = "7f9806e1-32b8-4cb2-bfcb-ca9d2e9c8cf1"
LIBRECHAT_DEPLOYMENT_ID = "301bef74-9f25-4428-bfc6-46d48123245c"
API_COMMIT = "84e63d76d7a91aa73fe963ad8a054761c5bde432"
OVERLAY_COMMIT = "e9c9f0916773059faf9d0c184e5c93540057c742"
SHA256 = re.compile(r"^[0-9a-f]{64}$")


class PublicationError(RuntimeError):
    pass


def _load(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8-sig"))
    if not isinstance(value, dict):
        raise PublicationError(f"{path.name} must contain a JSON object")
    return value


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _citation_summary(observation: dict[str, Any]) -> list[dict[str, str]]:
    result: list[dict[str, str]] = []
    seen: set[tuple[str, str]] = set()
    for item in observation.get("evidence", []):
        if not isinstance(item, dict) or not item.get("citation_id"):
            continue
        citation_id = str(item["citation_id"])
        filename = str(item.get("filename") or item.get("file_id") or "")
        key = (citation_id, filename)
        if key in seen:
            continue
        seen.add(key)
        result.append({"citation_id": citation_id, "filename": filename})
    return result


def _source_locations(answer: str) -> list[str]:
    return [
        line.strip()
        for line in answer.splitlines()
        if line.lstrip().startswith("Source:")
    ]


def _validate(run: dict[str, Any], evaluation: dict[str, Any]) -> None:
    if run.get("locked_holdout_opened") is not False:
        raise PublicationError("live run crossed the locked-holdout boundary")
    if run.get("credentials_or_tokens_emitted") is not False:
        raise PublicationError("live run emitted credentials or tokens")
    if run.get("selected_systems") != ["v4"]:
        raise PublicationError("live run is not V4-only")
    if tuple(run.get("selected_case_ids", ())) != CASE_IDS:
        raise PublicationError("live run does not contain the pre-registered case order")
    if run.get("expected_observation_count") != len(CASE_IDS):
        raise PublicationError("unexpected observation count")
    if (
        run.get("completed_observation_count") != len(CASE_IDS)
        or run.get("error_observation_count") != 0
        or run.get("authorization_violations") != 0
        or run.get("temporary_conversations_deleted") is not True
    ):
        raise PublicationError("live run did not finish cleanly")
    if run.get("v4_candidate_backend_commit") != API_COMMIT:
        raise PublicationError("live run used an unexpected API commit")
    if run.get("librechat_overlay_commit") != OVERLAY_COMMIT:
        raise PublicationError("live run used an unexpected LibreChat overlay")

    observations = run.get("observations")
    if not isinstance(observations, list) or len(observations) != len(CASE_IDS):
        raise PublicationError("live observation accounting is incomplete")
    by_case = {item.get("case_id"): item for item in observations if isinstance(item, dict)}
    if set(by_case) != set(CASE_IDS):
        raise PublicationError("live observation IDs are incomplete")
    for case_id in CASE_IDS:
        item = by_case[case_id]
        validation = item.get("validation")
        if (
            item.get("system") != "v4"
            or item.get("status") != "complete"
            or item.get("release_id") != RELEASE_PUBLIC_ID
            or not item.get("answer")
            or item.get("conversation_deleted") is not True
            or item.get("unauthorized_result_count") != 0
            or not isinstance(validation, dict)
            or validation.get("passed") is not True
            or validation.get("repair_count") != 0
            or not SHA256.fullmatch(str(validation.get("validation_fingerprint") or ""))
            or not SHA256.fullmatch(str(item.get("answer_sha256") or ""))
        ):
            raise PublicationError(f"{case_id} is not a clean attested observation")

    metrics = evaluation.get("metrics")
    results = evaluation.get("results")
    if (
        evaluation.get("passed") is not True
        or evaluation.get("locked_holdout_opened") is not False
        or not isinstance(metrics, dict)
        or metrics.get("answer_quality_cases_passed") != len(CASE_IDS)
        or metrics.get("all_cases_meet_or_exceed_prior_best") is not True
        or metrics.get("validator_passed_cases") != len(CASE_IDS)
        or metrics.get("zero_repair_cases") != len(CASE_IDS)
        or not isinstance(results, list)
        or len(results) != len(CASE_IDS)
    ):
        raise PublicationError("comparative hard-stop evaluation did not pass")
    evaluated = {item.get("case_id"): item for item in results if isinstance(item, dict)}
    for case_id in CASE_IDS:
        item = evaluated.get(case_id, {})
        if (
            item.get("passed") is not True
            or item.get("score_out_of_16") != 16
            or item.get("meets_or_exceeds_prior_best") is not True
            or item.get("answer_sha256") != by_case[case_id].get("answer_sha256")
        ):
            raise PublicationError(f"{case_id} did not pass the comparative hard stop")


def publish(
    run_path: Path,
    evaluation_path: Path,
    status_path: Path,
    review_paths: list[Path],
) -> dict[str, Any]:
    run = _load(run_path)
    evaluation = _load(evaluation_path)
    status = _load(status_path)
    _validate(run, evaluation)
    counts = status.get("counts")
    if (
        status.get("release_id") != RELEASE_UUID
        or status.get("release_status") != "ready"
        or status.get("release_ready_at_present") is not True
        or status.get("source_accounting_complete") is not True
        or status.get("active_release_pointer_count") != 0
        or status.get("locked_holdout_opened") is not False
        or status.get("credentials_or_connection_details_emitted") is not False
        or not isinstance(counts, dict)
        or counts.get("release_sources") != 374
        or counts.get("compiled_artifacts") != 374
        or counts.get("canonical_documents") != 374
        or counts.get("search_projections") != 25131
    ):
        raise PublicationError("private-shadow status is not ready, complete, and unpointed")
    if not review_paths:
        raise PublicationError("at least one independent review is required")
    reviews = []
    for path in review_paths:
        path = path.resolve()
        text = path.read_text(encoding="utf-8-sig")
        if "Owner acceptance represented: no" not in text and "owner acceptance" not in text.casefold():
            raise PublicationError(f"{path.name} does not preserve the owner-acceptance boundary")
        reviews.append({"path": path.relative_to(REPOSITORY_ROOT).as_posix(), "sha256": _sha256(path)})

    by_case = {item["case_id"]: item for item in run["observations"]}
    scored = {item["case_id"]: item for item in evaluation["results"]}
    observations = []
    for case_id in CASE_IDS:
        item = by_case[case_id]
        score = scored[case_id]
        answer = str(item["answer"])
        observations.append(
            {
                "case_id": case_id,
                "category": item.get("category"),
                "prompt": item["prompt"],
                "answer": answer,
                "answer_sha256": item["answer_sha256"],
                "status": item["status"],
                "release_id": item["release_id"],
                "validation": item["validation"],
                "evidence_count": item["evidence_count"],
                "citations": _citation_summary(item),
                "source_locations_in_answer": _source_locations(answer),
                "conversation_deleted": item["conversation_deleted"],
                "score_out_of_16": score["score_out_of_16"],
                "prior_best_score": score["prior_best_score"],
                "historical_scores": score["historical_scores"],
                "meets_or_exceeds_prior_best": score["meets_or_exceeds_prior_best"],
                "comparison_meaning": score["comparison_meaning"],
            }
        )
    return {
        "schema_version": "bauer-rag-v4-live-five-attestation-v1",
        "status": "verified_private_shadow",
        "meaning": (
            "Five selected public-development cases passed a case-level answer-quality hard stop "
            "against preserved V1-V4 outputs; this is not evidence for the other 25 cases."
        ),
        "split": "development",
        "selection": run["selection"],
        "selected_case_ids": list(CASE_IDS),
        "selected_systems": ["v4"],
        "observations": observations,
        "evaluation": {
            "passed": True,
            "suite_sha256": evaluation["suite_sha256"],
            "metrics": evaluation["metrics"],
        },
        "private_shadow": {
            "release_uuid": RELEASE_UUID,
            "release_public_id": RELEASE_PUBLIC_ID,
            "api_commit": API_COMMIT,
            "librechat_overlay_commit": OVERLAY_COMMIT,
            "api_deployment_id": API_DEPLOYMENT_ID,
            "worker_deployment_id": WORKER_DEPLOYMENT_ID,
            "librechat_deployment_id": LIBRECHAT_DEPLOYMENT_ID,
            "source_count": counts["release_sources"],
            "artifact_count": counts["compiled_artifacts"],
            "projection_count": counts["search_projections"],
            "active_release_pointer_count": 0,
            "status_generated_at_utc": status["generated_at_utc"],
        },
        "capture": {
            "login_attempt_count": run["login_attempt_count"],
            "completed_observation_count": run["completed_observation_count"],
            "error_observation_count": run["error_observation_count"],
            "authorization_violations": run["authorization_violations"],
            "temporary_conversations_deleted": run["temporary_conversations_deleted"],
            "credentials_or_tokens_emitted": run["credentials_or_tokens_emitted"],
        },
        "independent_reviews": reviews,
        "source_attestation": {
            "live_run_sha256": _sha256(run_path),
            "evaluation_sha256": _sha256(evaluation_path),
            "private_shadow_status_sha256": _sha256(status_path),
        },
        "locked_holdout_opened": False,
        "production_promotion": False,
        "owner_acceptance": False,
        "rejected_intermediate_runs": (
            "Earlier r2/r3/r4 development attempts were diagnostic only. The attested run is the "
            "first persisted run proving complete, validator-passed, zero-repair outputs after the "
            "B-DETECTION coverage correction."
        ),
    }


def markdown_report(result: dict[str, Any]) -> str:
    rows = []
    details = []
    for item in result["observations"]:
        scores = item["historical_scores"]
        rows.append(
            f"| {item['case_id']} | {scores['v1']} | {scores['v2']} | {scores['v3']} | "
            f"{scores['v4']} | {item['score_out_of_16']} | pass |"
        )
        details.extend(
            (
                "<details>",
                f"<summary><strong>{item['case_id']} exact live V4 output</strong></summary>",
                "",
                f"**Prompt:** {item['prompt']}",
                "",
                "```text",
                item["answer"],
                "```",
                "",
                f"Answer SHA-256: `{item['answer_sha256']}`",
                "",
                f"Validation: passed, mode `{item['validation']['answer_mode']}`, repair count 0",
                "",
                f"Prior best: {item['prior_best_score']}/16; repaired live V4: 16/16",
                "",
                "</details>",
                "",
            )
        )
    shadow = result["private_shadow"]
    return "\n".join(
        (
            "# Bauer RAG V4 live five-case answer-quality attestation",
            "",
            "Status: **verified private shadow; not production-promoted; owner acceptance pending**.",
            "",
            result["meaning"],
            "",
            "| Case | V1 | V2 | V3 | historical V4 | repaired live V4 | Verdict |",
            "| --- | ---: | ---: | ---: | ---: | ---: | --- |",
            *rows,
            "",
            "## What passed",
            "",
            "- 5/5 exact live LibreChat answers completed with no execution error.",
            "- 5/5 passed the existing V4 answer validator with zero repair attempts.",
            "- 5/5 met or exceeded the best preserved V1-V4 result under the case-level hard stop.",
            "- Every temporary conversation created by the authoritative run was deleted.",
            "- No authorization violation occurred; no credential or token was emitted; the holdout was not opened.",
            "",
            "## Exact outputs",
            "",
            *details,
            "## Deployment boundary",
            "",
            f"- Candidate release: `{shadow['release_public_id']}` (`{shadow['release_uuid']}`)",
            f"- API / worker / LibreChat deployments: `{shadow['api_deployment_id']}` / "
            f"`{shadow['worker_deployment_id']}` / `{shadow['librechat_deployment_id']}`",
            f"- Source/artifact/projection counts: {shadow['source_count']} / "
            f"{shadow['artifact_count']} / {shadow['projection_count']:,}",
            "- Active-release pointers: 0",
            "- Production promotion: no",
            "- Owner acceptance: no; this remains a separate owner decision",
            "",
            "## Limits",
            "",
            "This bounded result does not establish quality for the other 25 public-development cases, a newly sealed holdout, or production. Token presence was diagnostic only; each case had to pass its complete task-specific checklist.",
            "",
        )
    )


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--run", required=True, type=Path)
    parser.add_argument("--evaluation", required=True, type=Path)
    parser.add_argument("--status", required=True, type=Path)
    parser.add_argument("--independent-review", action="append", default=[], type=Path)
    parser.add_argument(
        "--output",
        type=Path,
        default=EVIDENCE_ROOT / "live-five-case-v4-attested.json",
    )
    parser.add_argument(
        "--markdown",
        type=Path,
        default=EVIDENCE_ROOT / "live-five-case-v4-attested.md",
    )
    args = parser.parse_args()
    result = publish(
        args.run,
        args.evaluation,
        args.status,
        args.independent_review,
    )
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(result, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
        newline="\n",
    )
    args.markdown.write_text(markdown_report(result), encoding="utf-8", newline="\n")
    print(json.dumps({"output": str(args.output), "passed": True}, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
