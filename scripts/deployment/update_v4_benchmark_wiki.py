from __future__ import annotations

import argparse
import hashlib
import html
import json
from pathlib import Path
from typing import Any


SYSTEMS = ("v1", "v2", "v3", "v4")
LIVE_CASE_IDS = ("B06", "B17", "B10", "B19", "B03")
LABELS = {
    "v1": "V1",
    "v2": "V2",
    "v3": "V3 private shadow",
    "v4": "V4 private shadow",
}


class WikiUpdateError(RuntimeError):
    pass


def _load(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8-sig"))
    if not isinstance(value, dict):
        raise WikiUpdateError(f"{path.name} is not a JSON object")
    return value


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _number(value: Any, *, digits: int = 4) -> str:
    if not isinstance(value, (int, float)):
        return "n/a"
    return f"{float(value):.{digits}f}"


def _seconds(value: Any) -> str:
    if not isinstance(value, (int, float)):
        return "n/a"
    return f"{float(value) / 1000:.2f} s"


def _validate(
    benchmark: dict[str, Any],
    score: dict[str, Any],
) -> None:
    observations = benchmark.get("observations")
    if (
        benchmark.get("locked_holdout_opened") is not False
        or not isinstance(observations, list)
        or len(observations) != 120
        or benchmark.get("expected_case_count") != 30
        or benchmark.get("expected_observation_count") != 120
    ):
        raise WikiUpdateError("benchmark is not the sealed 30-case four-way run")
    by_case_system = {
        (item.get("case_id"), item.get("system"))
        for item in observations
        if isinstance(item, dict)
    }
    expected = {
        (f"B{case_index:02d}", system)
        for case_index in range(1, 31)
        for system in SYSTEMS
    }
    if by_case_system != expected:
        raise WikiUpdateError("benchmark case/system accounting is incomplete")
    by_system = score.get("answers", {}).get("by_system")
    if (
        not isinstance(by_system, dict)
        or tuple(sorted(by_system)) != SYSTEMS
    ):
        raise WikiUpdateError("score does not contain exactly V1/V2/V3/V4")


def _validate_live(
    live: dict[str, Any],
    evaluation: dict[str, Any],
) -> None:
    observations = live.get("observations")
    metrics = evaluation.get("metrics")
    if (
        live.get("locked_holdout_opened") is not False
        or live.get("owner_acceptance") is not False
        or live.get("production_promotion") is not False
        or tuple(live.get("selected_case_ids", ())) != LIVE_CASE_IDS
        or live.get("selected_systems") != ["v4"]
        or not isinstance(observations, list)
        or len(observations) != len(LIVE_CASE_IDS)
        or evaluation.get("passed") is not True
        or evaluation.get("locked_holdout_opened") is not False
        or not isinstance(metrics, dict)
        or metrics.get("answer_quality_cases_passed") != len(LIVE_CASE_IDS)
        or metrics.get("all_cases_meet_or_exceed_prior_best") is not True
    ):
        raise WikiUpdateError("live five-case answer-quality evidence is not complete")
    by_case = {item.get("case_id"): item for item in observations if isinstance(item, dict)}
    results = {
        item.get("case_id"): item
        for item in evaluation.get("results", [])
        if isinstance(item, dict)
    }
    if set(by_case) != set(LIVE_CASE_IDS) or set(results) != set(LIVE_CASE_IDS):
        raise WikiUpdateError("live five-case accounting is incomplete")
    for case_id in LIVE_CASE_IDS:
        item = by_case[case_id]
        validation = item.get("validation")
        result = results[case_id]
        if (
            item.get("status") != "complete"
            or not item.get("answer")
            or item.get("conversation_deleted") is not True
            or not isinstance(validation, dict)
            or validation.get("passed") is not True
            or validation.get("repair_count") != 0
            or result.get("passed") is not True
            or result.get("score_out_of_16") != 16
            or result.get("meets_or_exceeds_prior_best") is not True
            or result.get("answer_sha256") != item.get("answer_sha256")
        ):
            raise WikiUpdateError(f"live case {case_id} did not pass its hard stop")


def _live_five_section(
    live: dict[str, Any],
    evaluation: dict[str, Any],
    *,
    live_sha256: str,
    evaluation_sha256: str,
) -> list[str]:
    observations = {item["case_id"]: item for item in live["observations"]}
    results = {item["case_id"]: item for item in evaluation["results"]}
    lines = [
        "## Current V4 answer-quality repair — live five-case result",
        "",
        "> **Engineering gate: PASS (5/5)**",
        "",
        "> **Runtime:** actual private LibreChat V4 Agent using DeepSeek",
        "",
        "> **Boundary:** private shadow only; active-release pointer empty; owner acceptance pending",
        "",
        "This is the current answer-quality evidence. Five cases were selected by a fixed seeded",
        "random choice from the public B01-B30 development set. Repaired V4 was run live through",
        "LibreChat and compared with the preserved V1, V2, V3, and historical V4 outputs. A case",
        "passed only if it fulfilled the whole task, was factually complete, used the requested",
        "shape, cited its claims, passed V4 validation, and required zero repair. Merely returning",
        "text or matching tokens was not a pass.",
        "",
        "| Case | V1 | V2 | V3 | historical V4 | repaired live V4 | Verdict |",
        "| --- | ---: | ---: | ---: | ---: | ---: | --- |",
    ]
    for case_id in LIVE_CASE_IDS:
        result = results[case_id]
        historical = result["historical_scores"]
        lines.append(
            f"| {case_id} | {historical['v1']} | {historical['v2']} | "
            f"{historical['v3']} | {historical['v4']} | "
            f"**{result['score_out_of_16']}** | **PASS** |"
        )
    lines.extend(
        [
            "",
            "What the result means:",
            "",
            "- B06 gives the 525-bar compressor maximum, distinguishes 520-bar boosters, and explains the shutdown-pressure wording.",
            "- B17 returns one correct BM 6.1/100-15 row with metric and imperial values and no neighboring-row contamination.",
            "- B10 distinguishes stationary i/s from mobile m, includes measurements and integrated data logging, and reconciles 450 versus 420 bar by source scope and date.",
            "- B19 returns both pressure limits and every gas-specific flow range without inventing a thermodynamic explanation.",
            "- B03 completes the synthetic-project comparison while explicitly preserving the synthetic-data and engineering-approval boundary.",
            "",
            "### Exact repaired live V4 outputs",
            "",
        ]
    )
    for case_id in LIVE_CASE_IDS:
        item = observations[case_id]
        result = results[case_id]
        lines.extend(
            [
                "<details>",
                f"<summary><strong>{case_id} — repaired live V4 — PASS</strong></summary>",
                "",
                "<p><strong>Prompt:</strong> "
                + html.escape(str(item["prompt"]))
                + "</p>",
                "",
                '<pre style="white-space:pre-wrap;overflow-wrap:anywhere"><code>'
                + html.escape(str(item["answer"]))
                + "</code></pre>",
                "",
                f"Validation: passed · repair count 0 · score {result['score_out_of_16']}/16 · "
                f"prior best {result['prior_best_score']}/16",
                f"Answer SHA-256: `{item['answer_sha256']}`",
                "",
                "</details>",
                "",
            ]
        )
    private_shadow = live["private_shadow"]
    lines.extend(
        [
            "### Current V4 evidence seal",
            "",
            f"- Candidate: `{private_shadow['release_public_id']}` (`{private_shadow['release_uuid']}`)",
            f"- API commit: `{private_shadow['api_commit']}`",
            f"- LibreChat overlay commit: `{private_shadow['librechat_overlay_commit']}`",
            f"- Sources / artifacts / projections: {private_shadow['source_count']} / "
            f"{private_shadow['artifact_count']} / {private_shadow['projection_count']:,}",
            "- Active-release pointers: `0`",
            f"- Exact live-output artifact SHA-256: `{live_sha256}`",
            f"- Comparative evaluation SHA-256: `{evaluation_sha256}`",
            f"- Evaluation suite SHA-256: `{evaluation['suite_sha256']}`",
            "- Locked holdout opened: `false`",
            "- Production promotion: `false`",
            "- Owner acceptance: `false`",
            "",
            "Passing this bounded five-case gate does not establish quality on the other 25 public",
            "development cases or a newly sealed holdout.",
            "",
            "---",
            "",
        ]
    )
    return lines


def _metric_table(score: dict[str, Any]) -> list[str]:
    by_system = score["answers"]["by_system"]
    rows = [
        (
            "Exact-answer accuracy",
            lambda item: _number(item.get("exact_answer_accuracy")),
        ),
        (
            "Claim-level correctness",
            lambda item: _number(item.get("claim_level_correctness")),
        ),
        (
            "Citation correctness",
            lambda item: _number(item.get("citation_correctness")),
        ),
        (
            "Constraint compliance",
            lambda item: _number(item.get("constraint_compliance")),
        ),
        (
            "Safe-refusal accuracy",
            lambda item: _number(item.get("safe_refusal_accuracy")),
        ),
        (
            "Fabrication findings",
            lambda item: str(item.get("fabrication_count", "n/a")),
        ),
        (
            "Hard failures",
            lambda item: str(item.get("hard_failure_count", "n/a")),
        ),
        (
            "End-to-end p50",
            lambda item: _seconds(item.get("latency_p50_ms")),
        ),
        (
            "End-to-end p95",
            lambda item: _seconds(item.get("latency_p95_ms")),
        ),
    ]
    result = [
        "| Measure | V1 | V2 | V3 private shadow | V4 private shadow |",
        "| --- | ---: | ---: | ---: | ---: |",
    ]
    for label, formatter in rows:
        result.append(
            "| "
            + " | ".join(
                [label]
                + [formatter(by_system[system]) for system in SYSTEMS]
            )
            + " |"
        )
    return result


def _case_summary(
    benchmark: dict[str, Any],
    score: dict[str, Any],
) -> list[str]:
    observations = {
        (item["case_id"], item["system"]): item
        for item in benchmark["observations"]
    }
    scored = {
        (item["case_id"], item["system"]): item
        for item in score["answers"]["observations"]
    }
    result = [
        "| Case | Category | V1 | V2 | V3 | V4 |",
        "| --- | --- | ---: | ---: | ---: | ---: |",
    ]
    for case_index in range(1, 31):
        case_id = f"B{case_index:02d}"
        first = observations[(case_id, "v1")]
        cells = []
        for system in SYSTEMS:
            item = observations[(case_id, system)]
            passed = bool(
                scored.get((case_id, system), {}).get(
                    "exact_answer_passed"
                )
            )
            state = "PASS" if passed else (
                "ERROR" if item.get("error_type") else "FAIL"
            )
            cells.append(
                f"**{state}** · {int(item.get('answer_character_count') or 0):,} chars"
            )
        result.append(
            "| "
            + " | ".join(
                [
                    case_id,
                    str(first.get("category") or ""),
                    *cells,
                ]
            )
            + " |"
        )
    return result


def _exact_outputs(
    benchmark: dict[str, Any],
    score: dict[str, Any],
) -> list[str]:
    observations = {
        (item["case_id"], item["system"]): item
        for item in benchmark["observations"]
    }
    scored = {
        (item["case_id"], item["system"]): item
        for item in score["answers"]["observations"]
    }
    result: list[str] = []
    for case_index in range(1, 31):
        case_id = f"B{case_index:02d}"
        first = observations[(case_id, "v1")]
        statuses = []
        for system in SYSTEMS:
            item = observations[(case_id, system)]
            passed = bool(
                scored.get((case_id, system), {}).get(
                    "exact_answer_passed"
                )
            )
            statuses.append(
                f"{LABELS[system]} "
                + (
                    "PASS"
                    if passed
                    else "ERROR"
                    if item.get("error_type")
                    else "FAIL"
                )
            )
        result.extend(
            [
                "<details>",
                (
                    "<summary><strong>"
                    + html.escape(case_id)
                    + " — "
                    + html.escape(str(first.get("category") or ""))
                    + " — "
                    + html.escape(" / ".join(statuses))
                    + "</strong></summary>"
                ),
                "",
                (
                    "<p><strong>Prompt:</strong> "
                    + html.escape(str(first.get("prompt") or ""))
                    + "</p>"
                ),
                "",
                '<div style="overflow-x:auto">',
                '<table style="min-width:1800px;table-layout:fixed;width:100%">',
                "<thead><tr>",
                *(
                    f'<th style="width:25%">{html.escape(LABELS[system])} exact output</th>'
                    for system in SYSTEMS
                ),
                "</tr></thead>",
                "<tbody><tr>",
                *(
                    '<td style="vertical-align:top"><pre '
                    'style="white-space:pre-wrap;overflow-wrap:anywhere"><code>'
                    + html.escape(
                        str(
                            observations[(case_id, system)].get("answer")
                            or "[No visible answer]"
                        )
                    )
                    + "</code></pre></td>"
                    for system in SYSTEMS
                ),
                "</tr></tbody>",
                "</table>",
                "</div>",
                "",
                "</details>",
                "",
            ]
        )
    return result


def render(
    benchmark: dict[str, Any],
    score: dict[str, Any],
    *,
    benchmark_sha256: str,
    score_sha256: str,
    live: dict[str, Any] | None = None,
    live_evaluation: dict[str, Any] | None = None,
    live_sha256: str | None = None,
    live_evaluation_sha256: str | None = None,
) -> str:
    observations = benchmark["observations"]
    completed = sum(
        bool(item.get("answer")) and not item.get("error_type")
        for item in observations
    )
    deleted = sum(
        bool(item.get("conversation_deleted"))
        for item in observations
    )
    refreshes = int(
        benchmark.get("authenticated_session_refresh_count") or 0
    )
    lines = [
        "# Bauer RAG V1 / V2 / V3 / V4 benchmark",
        "",
        "> **Status:** Current five-case repair and historical four-way development evidence complete",
        "",
        "> **Historical suite:** Same 30 interim-reviewed public development prompts, one run per Agent",
        "",
        "> **Decision:** V4 remains a private shadow. This is development evidence, not production promotion.",
        "",
        *(
            _live_five_section(
                live,
                live_evaluation,
                live_sha256=str(live_sha256),
                evaluation_sha256=str(live_evaluation_sha256),
            )
            if live is not None and live_evaluation is not None
            else []
        ),
        (
            "## Historical 2026-07-29 four-way development benchmark"
            if live is not None
            else "## Benchmark"
        ),
        "",
        *(
            [
                "The complete 30-case four-way benchmark below is retained unchanged as historical",
                "evidence. Its V4 column predates the answer-quality repair above.",
                "",
            ]
            if live is not None
            else []
        ),
        "This is the single current comparison view. It used exactly one browser-shaped login, normal",
        "in-session bearer refresh, a fresh conversation for every observation, and the exact visible",
        "assistant text returned by V1, V2, V3, and V4. The benchmark harness read only the public",
        "B01-B30 development manifest and reports `locked_holdout_opened: false`.",
        "",
        "> **Holdout process warning:** the wider implementation task's holdout boundary was contaminated",
        "> by accidental exposure to combined gold/holdout-linked entries. These development results are",
        "> not sealed-holdout evidence; promotion requires a freshly resealed holdout and independent run.",
        "",
        (
            f"**Capture integrity:** 1 login · {refreshes} in-session refreshes · "
            f"{completed}/120 visible answers retained · {deleted}/120 temporary "
            "conversations deleted · 0 authorization-scope violations."
        ),
        "",
        "**Architecture at a glance**",
        "",
        "| Version | LibreChat path | Retrieval and evidence | Storage / release |",
        "| --- | --- | --- | --- |",
        "| V1 | Existing Bauer V1 Agent | `/query_multiple`; vector similarity | Existing VectorDB; unchanged |",
        "| V2 | Existing private V2 Agent | `/query_v2`; Bauer-aware hybrid retrieval | Versioned V2 derived index; V1 unchanged |",
        "| V3 | Existing private V3 Agent | Signed `/v3/answer`; frozen canonical shadow | Fixed V3 release; unchanged |",
        "| V4 | New private V4 Agent | Full question → exact/lexical/dense → transparent reranker → coverage/validation | Isolated `bauer_rag_v4`, fixed 373-source release, `v4/` objects; active pointer empty |",
        "",
        "## Comparable answer results",
        "",
        *_metric_table(score),
        "",
        "The deterministic score uses a source-controlled B01-B30 public-development rubric only.",
        "It does not read the combined gold or holdout files. The rubric is development guidance, not",
        "independently Bauer-verified acceptance evidence.",
        "",
        "## Prompt-by-prompt summary",
        "",
        *_case_summary(benchmark, score),
        "",
        "## All exact LibreChat outputs",
        "",
        "Open a case to see the submitted prompt and all four exact visible answers. Internal message",
        "IDs, tool arguments, credentials, signing material, and bearer tokens are excluded.",
        "",
        *_exact_outputs(benchmark, score),
        "## Evidence seals",
        "",
        f"- Four-way raw benchmark SHA-256: `{benchmark_sha256}`",
        f"- Four-way score SHA-256: `{score_sha256}`",
        f"- Deployed V4 commit: `{benchmark.get('v4_candidate_backend_commit')}`",
        f"- V4 Agent: `{benchmark.get('v4_agent_id')}`",
        "- Benchmark-harness locked holdout opened: `false`",
        "- Wider task holdout process boundary contaminated: `true`",
        "- Promotion prerequisite: freshly resealed holdout and independent evaluation",
        "- Production promotion: `false`",
        "",
        "Historical V1/V2/V3 evidence remains preserved in the development workspace and Git history.",
        "",
        "## Sources",
        "",
        "- Source-controlled public-development benchmark and comparative evaluation artifacts",
        "- Authenticated LibreChat V4 private-shadow output attestation",
        "- Bauer RAG V4 runbook and independent read-only review artifacts",
        "",
    ]
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--benchmark", required=True, type=Path)
    parser.add_argument("--score", required=True, type=Path)
    parser.add_argument("--live-five", type=Path)
    parser.add_argument("--live-evaluation", type=Path)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    benchmark = _load(args.benchmark)
    score = _load(args.score)
    _validate(benchmark, score)
    if (args.live_five is None) != (args.live_evaluation is None):
        raise WikiUpdateError("--live-five and --live-evaluation must be supplied together")
    live = _load(args.live_five) if args.live_five is not None else None
    live_evaluation = (
        _load(args.live_evaluation)
        if args.live_evaluation is not None
        else None
    )
    if live is not None and live_evaluation is not None:
        _validate_live(live, live_evaluation)
    rendered = render(
        benchmark,
        score,
        benchmark_sha256=_sha256(args.benchmark),
        score_sha256=_sha256(args.score),
        live=live,
        live_evaluation=live_evaluation,
        live_sha256=_sha256(args.live_five) if args.live_five else None,
        live_evaluation_sha256=(
            _sha256(args.live_evaluation)
            if args.live_evaluation
            else None
        ),
    )
    args.output.write_text(rendered, encoding="utf-8", newline="\n")
    print(
        json.dumps(
            {
                "output": str(args.output),
                "benchmark_sha256": _sha256(args.benchmark),
                "score_sha256": _sha256(args.score),
                "locked_holdout_opened": False,
            },
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
