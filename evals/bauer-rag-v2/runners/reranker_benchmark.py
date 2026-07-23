from __future__ import annotations

import argparse
import json
import time
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


EVAL_ROOT = Path(__file__).resolve().parents[1]
EXACT_CATEGORIES = {
    "exact_table",
    "exact_table_ambiguity",
    "exact_certificate",
    "exact_document_number",
    "exact_part_number",
    "bilingual_exact",
}


def load_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def normalize(value: Any) -> str:
    return " ".join(str(value or "").casefold().split())


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


def percentile(values: list[float], fraction: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    index = round((len(ordered) - 1) * fraction)
    return round(ordered[index], 2)


def aggregate(observations: list[dict[str, Any]], rank_key: str) -> dict[str, Any]:
    if not observations:
        return {
            "observations": 0,
            "recall_at_1": None,
            "recall_at_3": None,
            "recall_at_5": None,
            "recall_at_10": None,
            "mean_reciprocal_rank": None,
            "exact_metadata_success": None,
        }
    recalls: dict[int, list[float]] = {1: [], 3: [], 5: [], 10: []}
    reciprocal_ranks: list[float] = []
    exact_success: list[bool] = []
    for observation in observations:
        ranks = observation[rank_key]
        for cutoff in recalls:
            recalls[cutoff].append(
                sum(rank is not None and rank <= cutoff for rank in ranks) / len(ranks)
            )
        reciprocal_ranks.append(
            1 / min(rank for rank in ranks if rank is not None)
            if any(rank is not None for rank in ranks)
            else 0
        )
        if observation["category"] in EXACT_CATEGORIES:
            exact_success.append(bool(ranks) and all(rank == 1 for rank in ranks))
    return {
        "observations": len(observations),
        **{
            f"recall_at_{cutoff}": round(sum(values) / len(values), 4)
            for cutoff, values in recalls.items()
        },
        "mean_reciprocal_rank": round(
            sum(reciprocal_ranks) / len(reciprocal_ranks), 4
        ),
        "exact_metadata_success": (
            round(sum(exact_success) / len(exact_success), 4)
            if exact_success
            else None
        ),
    }


def request_rerank(
    *,
    endpoint: str,
    query: str,
    documents: list[str],
    timeout: float,
) -> tuple[list[int], float, str]:
    payload = json.dumps(
        {"query": query, "documents": documents, "top_n": len(documents)}
    ).encode("utf-8")
    request = urllib.request.Request(
        endpoint,
        data=payload,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    started = time.perf_counter()
    with urllib.request.urlopen(request, timeout=timeout) as response:
        body = json.loads(response.read().decode("utf-8"))
    elapsed_ms = (time.perf_counter() - started) * 1000
    ranked_indices = [int(item["index"]) for item in body.get("results", [])]
    if sorted(ranked_indices) != list(range(len(documents))):
        raise ValueError("Reranker response did not return every candidate exactly once.")
    return ranked_indices, elapsed_ms, str(body.get("model") or "unknown")


def main() -> None:
    parser = argparse.ArgumentParser(
        description=(
            "Compare a reranker with the checked-in deterministic ordering on a "
            "frozen Bauer V2 development retrieval run."
        )
    )
    parser.add_argument("--retrieval-run", type=Path, required=True)
    parser.add_argument(
        "--endpoint",
        default="http://127.0.0.1:18104/v1/rerank",
    )
    parser.add_argument("--timeout", type=float, default=120)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--benchmark-code-commit")
    parser.add_argument("--model-artifact")
    parser.add_argument("--model-sha256")
    args = parser.parse_args()

    run = load_json(args.retrieval_run)
    cases = {
        case["id"]: case
        for case in load_json(EVAL_ROOT / "cases" / "development.yaml")["cases"]
    }
    gold = {
        case["id"]: case
        for case in load_json(EVAL_ROOT / "gold" / "evidence.yaml")["cases"]
    }
    benchmark_observations = []
    latencies = []
    model = None
    for observation in run.get("observations", []):
        if observation.get("system") != "v2":
            continue
        case_id = observation["case_id"]
        required = gold.get(case_id, {}).get("required_evidence", [])
        results = observation.get("results") or []
        if not required or not results:
            continue
        query = next(
            str(cases[case_id][field])
            for field in ("prompt_en", "prompt_de")
            if cases[case_id].get(field)
        )
        ranked_indices, elapsed_ms, response_model = request_rerank(
            endpoint=args.endpoint,
            query=query,
            documents=[str(result.get("content") or "") for result in results],
            timeout=args.timeout,
        )
        model = model or response_model
        latencies.append(elapsed_ms)
        reranked = [results[index] for index in ranked_indices]

        def ranks_for(ordered: list[dict[str, Any]]) -> list[int | None]:
            return [
                next(
                    (
                        rank
                        for rank, result in enumerate(ordered, start=1)
                        if location_match(result, gold_item)
                    ),
                    None,
                )
                for gold_item in required
            ]

        benchmark_observations.append(
            {
                "case_id": case_id,
                "category": observation.get("category"),
                "repetition": observation.get("repetition"),
                "deterministic_ranks": ranks_for(results),
                "cross_encoder_ranks": ranks_for(reranked),
                "rerank_elapsed_ms": round(elapsed_ms, 2),
            }
        )
        print(
            f"{case_id} repetition={observation.get('repetition')} "
            f"rerank_ms={elapsed_ms:.2f}"
        )

    deterministic = aggregate(benchmark_observations, "deterministic_ranks")
    cross_encoder = aggregate(benchmark_observations, "cross_encoder_ranks")
    regressions = [
        item["case_id"]
        for item in benchmark_observations
        if any(
            cross_rank is None
            or (
                deterministic_rank is not None
                and cross_rank > deterministic_rank
            )
            for deterministic_rank, cross_rank in zip(
                item["deterministic_ranks"],
                item["cross_encoder_ranks"],
            )
        )
    ]
    report = {
        "schema_version": 1,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "source_run": str(args.retrieval_run.resolve()),
        "source_run_id": run.get("run_id"),
        "candidate_source_commit": run.get("git_commit"),
        "benchmark_code_commit": args.benchmark_code_commit,
        "split": "development",
        "candidate_set": "frozen_v2_top_10",
        "model": model,
        "model_artifact": args.model_artifact,
        "model_sha256": args.model_sha256,
        "endpoint_recorded_without_credentials": args.endpoint,
        "deterministic_fallback": deterministic,
        "cross_encoder": cross_encoder,
        "cross_encoder_latency_ms": {
            "p50": percentile(latencies, 0.5),
            "p95": percentile(latencies, 0.95),
        },
        "cross_encoder_regression_case_ids": sorted(set(regressions)),
        "recommendation": (
            "configure_remote_reranker"
            if (
                cross_encoder["recall_at_5"] is not None
                and deterministic["recall_at_5"] is not None
                and cross_encoder["recall_at_5"] >= deterministic["recall_at_5"]
                and cross_encoder["exact_metadata_success"] is not None
                and deterministic["exact_metadata_success"] is not None
                and cross_encoder["exact_metadata_success"]
                >= deterministic["exact_metadata_success"]
                and not regressions
            )
            else "retain_deterministic_fallback"
        ),
        "observations": benchmark_observations,
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    with args.output.open("x", encoding="utf-8") as handle:
        json.dump(report, handle, ensure_ascii=False, indent=2)
        handle.write("\n")
    print(args.output)


if __name__ == "__main__":
    main()
