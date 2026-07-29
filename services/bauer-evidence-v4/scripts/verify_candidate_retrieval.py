from __future__ import annotations

import argparse
import hashlib
import json
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from bauer_evidence_v4.compilation import CanonicalCompiler
from bauer_evidence_v4.indexing import ProjectionBuilder
from bauer_evidence_v4.retrieval import (
    AuthorizedScope,
    CandidateGenerator,
    IndexedProjection,
    RetrievalRequest,
)


TENANT = "tenant-bauer-development"
KNOWLEDGE_BASE = "kb-public-development"
RELEASE = "v4-development-fixtures"


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser()
    parser.add_argument("--fixture", required=True, type=Path)
    parser.add_argument("--cases", required=True, type=Path)
    parser.add_argument("--gates", required=True, type=Path)
    parser.add_argument("--source-root", required=True, type=Path)
    parser.add_argument("--output", type=Path)
    return parser


def _relevant(projection: Any, expected: dict[str, Any]) -> bool:
    if projection.projection_type != expected["projection_type"]:
        return False
    if expected.get("subject") != projection.subject and expected.get("subject"):
        return False
    if (
        expected.get("document_number") != projection.document_number
        and expected.get("document_number")
    ):
        return False
    if (
        expected.get("group")
        and dict(projection.qualifiers).get("group") != expected["group"]
    ):
        return False
    if (
        expected.get("search_text_contains")
        and expected["search_text_contains"] not in projection.search_text
    ):
        return False
    return True


def main() -> int:
    args = _parser().parse_args()
    fixture_bundle = json.loads(args.fixture.read_text(encoding="utf-8"))
    case_bundle = json.loads(args.cases.read_text(encoding="utf-8"))
    gates = json.loads(args.gates.read_text(encoding="utf-8"))
    compiler = CanonicalCompiler()
    builder = ProjectionBuilder()
    items: list[IndexedProjection] = []
    source_ids: set[str] = set()
    for fixture in fixture_bundle["documents"]:
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
                tenant_id=TENANT,
                knowledge_base_id=KNOWLEDGE_BASE,
                release_id=RELEASE,
                authorization_source_id=source["external_file_id"],
            )
            for projection in builder.build(compiled.document)
        )
    generator = CandidateGenerator(tuple(items))
    scope = AuthorizedScope(
        principal_id="verification-principal",
        tenant_id=TENANT,
        knowledge_base_id=KNOWLEDGE_BASE,
        release_id=RELEASE,
        authorized_external_source_ids=frozenset(source_ids),
    )
    results = []
    channel_counts: Counter[str] = Counter()
    for case in case_bundle["cases"]:
        candidates = generator.retrieve(
            RetrievalRequest(
                question=case["question"],
                search_hint=None,
                subquestions=(),
                constraints=(),
                scope=scope,
            ),
            limit=10,
        )
        channel_counts.update(dict(candidates.channel_hit_counts))
        relevant_ranks = [
            index
            for index, candidate in enumerate(candidates.candidates, start=1)
            if _relevant(candidate.item.projection, case["relevant"])
        ]
        results.append(
            {
                "case_id": case["case_id"],
                "first_relevant_rank": min(relevant_ranks)
                if relevant_ranks
                else None,
                "recall_at_5": any(rank <= 5 for rank in relevant_ranks),
                "recall_at_10": any(rank <= 10 for rank in relevant_ranks),
                "candidate_count": len(candidates.candidates),
            }
        )
    recall_5 = sum(item["recall_at_5"] for item in results) / len(results)
    recall_10 = sum(item["recall_at_10"] for item in results) / len(results)

    denied_scope = AuthorizedScope(
        principal_id=scope.principal_id,
        tenant_id="denied",
        knowledge_base_id=scope.knowledge_base_id,
        release_id=scope.release_id,
        authorized_external_source_ids=scope.authorized_external_source_ids,
    )
    unauthorized_returned = sum(
        len(
            generator.retrieve(
                RetrievalRequest(
                    question=case["question"],
                    search_hint=None,
                    subquestions=(),
                    constraints=(),
                    scope=denied_scope,
                ),
                limit=10,
            ).candidates
        )
        for case in case_bundle["cases"]
    )
    minimum = gates["minimums"]["recall_at_10"]
    report = {
        "schema_version": 1,
        "kind": "bauer-rag-v4-candidate-retrieval-verification",
        "generated_at_utc": datetime.now(UTC).isoformat(),
        "passed": (
            recall_10 >= minimum and unauthorized_returned == 0
        ),
        "locked_holdout_opened": False,
        "suite_id": case_bundle["suite_id"],
        "suite_sha256": hashlib.sha256(args.cases.read_bytes()).hexdigest(),
        "gate_manifest_sha256": hashlib.sha256(
            args.gates.read_bytes()
        ).hexdigest(),
        "candidate_family_count": 3,
        "candidate_families": [
            "exact_structured",
            "lexical_trigram",
            "dense",
        ],
        "recall_at_5": recall_5,
        "recall_at_10": recall_10,
        "minimum_recall_at_10": minimum,
        "unauthorized_evidence_returned": unauthorized_returned,
        "channel_hit_counts": dict(sorted(channel_counts.items())),
        "results": results,
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
