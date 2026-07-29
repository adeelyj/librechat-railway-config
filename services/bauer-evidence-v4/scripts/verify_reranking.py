from __future__ import annotations

import argparse
import hashlib
import json
import statistics
import time
import tracemalloc
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from bauer_evidence_v4.compilation import CanonicalCompiler
from bauer_evidence_v4.indexing import ProjectionBuilder
from bauer_evidence_v4.reranking import (
    LocalInteractionReranker,
    TransparentFeatureReranker,
    select_smallest_passing,
)
from bauer_evidence_v4.retrieval import (
    AuthorizedScope,
    CandidateGenerator,
    IndexedProjection,
    RetrievalRequest,
)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser()
    parser.add_argument("--fixture", required=True, type=Path)
    parser.add_argument("--cases", required=True, type=Path)
    parser.add_argument("--gates", required=True, type=Path)
    parser.add_argument("--source-root", required=True, type=Path)
    parser.add_argument("--output", type=Path)
    return parser


def _relevant(projection: Any, expected: dict[str, Any]) -> bool:
    return (
        projection.projection_type == expected["projection_type"]
        and (
            not expected.get("subject")
            or projection.subject == expected["subject"]
        )
        and (
            not expected.get("document_number")
            or projection.document_number == expected["document_number"]
        )
        and (
            not expected.get("group")
            or dict(projection.qualifiers).get("group") == expected["group"]
        )
        and (
            not expected.get("search_text_contains")
            or expected["search_text_contains"] in projection.search_text
        )
    )


def main() -> int:
    args = _parser().parse_args()
    fixtures = json.loads(args.fixture.read_text(encoding="utf-8"))
    cases = json.loads(args.cases.read_text(encoding="utf-8"))
    gates = json.loads(args.gates.read_text(encoding="utf-8"))
    compiler = CanonicalCompiler()
    builder = ProjectionBuilder()
    source_ids = set()
    items = []
    for fixture in fixtures["documents"]:
        source = fixture["source"]
        source_ids.add(source["external_file_id"])
        path = args.source_root.joinpath(*source["logical_path"].split("/"))
        compiled = compiler.compile(
            path.read_bytes(),
            source_path=source["logical_path"],
            declared_media_type=source["media_type"],
            fixture=fixture,
        )
        assert compiled.document is not None
        items.extend(
            IndexedProjection(
                projection=projection,
                tenant_id="tenant",
                knowledge_base_id="kb",
                release_id="release",
                authorization_source_id=source["external_file_id"],
            )
            for projection in builder.build(compiled.document)
        )
    scope = AuthorizedScope(
        principal_id="principal",
        tenant_id="tenant",
        knowledge_base_id="kb",
        release_id="release",
        authorized_external_source_ids=frozenset(source_ids),
    )
    generator = CandidateGenerator(tuple(items))
    candidate_sets = {
        case["case_id"]: generator.retrieve(
            RetrievalRequest(
                question=case["question"],
                search_hint=None,
                subquestions=(),
                constraints=(),
                scope=scope,
            ),
            limit=50,
        )
        for case in cases["cases"]
    }
    alternatives = (
        TransparentFeatureReranker(),
        LocalInteractionReranker(),
    )
    alternative_reports = []
    metric_map: dict[str, dict[str, float]] = {}
    for reranker in alternatives:
        ranks = []
        elapsed = []
        tracemalloc.start()
        for case in cases["cases"]:
            started = time.perf_counter()
            result = reranker.rerank(
                candidate_sets[case["case_id"]],
                limit=10,
            )
            elapsed.append((time.perf_counter() - started) * 1000)
            ranks.append(
                next(
                    (
                        item.rank
                        for item in result.ranked
                        if _relevant(
                            item.candidate.item.projection,
                            case["relevant"],
                        )
                    ),
                    None,
                )
            )
        _, peak_bytes = tracemalloc.get_traced_memory()
        tracemalloc.stop()
        metrics = {
            "recall_at_5": sum(
                rank is not None and rank <= 5 for rank in ranks
            )
            / len(ranks),
            "mrr": sum(1 / rank if rank else 0 for rank in ranks)
            / len(ranks),
        }
        metric_map[reranker.model_id] = metrics
        alternative_reports.append(
            {
                "model_id": reranker.model_id,
                "model_identity": reranker.model_identity,
                "complexity_units": reranker.complexity_units,
                **metrics,
                "latency_p50_ms": statistics.median(elapsed),
                "latency_p95_ms": sorted(elapsed)[
                    max(0, int(len(elapsed) * 0.95) - 1)
                ],
                "peak_memory_bytes": peak_bytes,
                "ranks": ranks,
            }
        )
    minimum = gates["minimums"]["recall_at_5"]
    selected = select_smallest_passing(
        metric_map,
        alternatives,
        minimum_recall_at_5=minimum,
    )
    selected_report = next(
        item
        for item in alternative_reports
        if item["model_id"] == selected.model_id
    )
    report = {
        "schema_version": 1,
        "kind": "bauer-rag-v4-reranker-verification",
        "generated_at_utc": datetime.now(UTC).isoformat(),
        "passed": selected_report["recall_at_5"] >= minimum,
        "locked_holdout_opened": False,
        "suite_id": cases["suite_id"],
        "suite_sha256": hashlib.sha256(args.cases.read_bytes()).hexdigest(),
        "gate_manifest_sha256": hashlib.sha256(
            args.gates.read_bytes()
        ).hexdigest(),
        "minimum_recall_at_5": minimum,
        "selected_model_id": selected.model_id,
        "selected_model_identity": selected.model_identity,
        "selection_policy": "smallest_complexity_that_passes_then_mrr",
        "alternatives": alternative_reports,
    }
    encoded = json.dumps(
        report,
        ensure_ascii=False,
        allow_nan=False,
        indent=2,
        sort_keys=True,
    ) + "\n"
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(encoded, encoding="utf-8")
    else:
        print(encoded, end="")
    return 0 if report["passed"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
