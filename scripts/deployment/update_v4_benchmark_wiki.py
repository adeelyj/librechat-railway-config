from __future__ import annotations

import argparse
import hashlib
import html
import json
from pathlib import Path
from typing import Any


SYSTEMS = ("v1", "v2", "v3", "v4")
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
        "> **Status:** Comparable authenticated LibreChat API development benchmark complete  ",
        "> **Suite:** Same 30 interim-reviewed public development prompts, one run per Agent  ",
        "> **Decision:** V4 remains a private shadow. This is development evidence, not production promotion.",
        "",
        "## Benchmark",
        "",
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
    ]
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--benchmark", required=True, type=Path)
    parser.add_argument("--score", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    benchmark = _load(args.benchmark)
    score = _load(args.score)
    _validate(benchmark, score)
    rendered = render(
        benchmark,
        score,
        benchmark_sha256=_sha256(args.benchmark),
        score_sha256=_sha256(args.score),
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
