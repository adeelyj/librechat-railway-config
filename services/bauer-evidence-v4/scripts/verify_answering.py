from __future__ import annotations

import argparse
import hashlib
import json
import statistics
import time
from datetime import UTC, datetime
from pathlib import Path

from bauer_evidence_v4.answering import AnswerService, SourceRegistry
from bauer_evidence_v4.compilation import CanonicalCompiler
from bauer_evidence_v4.contracts.models import ReleaseContract
from bauer_evidence_v4.indexing import ProjectionBuilder
from bauer_evidence_v4.retrieval import (
    AuthorizedScope,
    CandidateGenerator,
    IndexedProjection,
)


def _digest(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser()
    parser.add_argument("--fixture", required=True, type=Path)
    parser.add_argument("--cases", required=True, type=Path)
    parser.add_argument("--gates", required=True, type=Path)
    parser.add_argument("--source-root", required=True, type=Path)
    parser.add_argument("--output", type=Path)
    return parser


def main() -> int:
    args = _parser().parse_args()
    fixtures = json.loads(args.fixture.read_text(encoding="utf-8"))
    cases = json.loads(args.cases.read_text(encoding="utf-8"))
    gates = json.loads(args.gates.read_text(encoding="utf-8"))
    compiler = CanonicalCompiler()
    builder = ProjectionBuilder()
    documents = {}
    external_source_ids = {}
    linked_filenames = {}
    source_ids = set()
    items = []

    def add_source(source: dict, fixture: dict | None = None):
        path = args.source_root.joinpath(*source["logical_path"].split("/"))
        compiled = compiler.compile(
            path.read_bytes(),
            source_path=source["logical_path"],
            declared_media_type=source["media_type"],
            fixture=fixture,
        )
        assert compiled.document is not None
        document = compiled.document
        documents[document.source_sha256] = document
        external_source_ids[document.source_sha256] = source[
            "external_file_id"
        ]
        source_ids.add(source["external_file_id"])
        items.extend(
            IndexedProjection(
                projection=projection,
                tenant_id="tenant",
                knowledge_base_id="kb",
                release_id="release",
                authorization_source_id=source["external_file_id"],
            )
            for projection in builder.build(document)
        )
        return document

    for fixture in fixtures["documents"]:
        primary = add_source(fixture["source"], fixture)
        for linked in fixture.get("linked_sources", []):
            linked_document = add_source(linked)
            if linked_document.source_filename == "bauer_amfile_216.pdf":
                linked_filenames[
                    (primary.source_sha256, "216")
                ] = linked_document.source_filename

    release = ReleaseContract(
        public_id="release",
        source_contract_sha256=fixtures["source_contract_sha256"],
        canonical_schema_version="4.1",
        compiler_identity=_digest("canonical-compiler-v4.1"),
        projection_identity=_digest("search-projection-v4.1"),
        embedding_identity=_digest("development-embedding"),
        reranker_identity=_digest("transparent-linear-v1"),
        gate_manifest_sha256=hashlib.sha256(
            args.gates.read_bytes()
        ).hexdigest(),
    )
    service = AnswerService(
        candidate_generator=CandidateGenerator(tuple(items)),
        source_registry=SourceRegistry(
            documents_by_sha256=documents,
            external_source_ids=external_source_ids,
            linked_filenames=linked_filenames,
        ),
        release=release,
    )
    scope = AuthorizedScope(
        principal_id="principal",
        tenant_id="tenant",
        knowledge_base_id="kb",
        release_id="release",
        authorized_external_source_ids=frozenset(source_ids),
    )
    denied_scope = AuthorizedScope(
        principal_id="principal",
        tenant_id="tenant",
        knowledge_base_id="kb",
        release_id="release",
        authorized_external_source_ids=frozenset({"not-indexed"}),
    )

    results = []
    latencies = []
    expected_total = 0
    expected_passed = 0
    supported_fields = 0
    requested_fields = 0
    constraints_passed = 0
    citation_case_passed = 0
    safe_refusal_passed = 0
    dump_count = 0
    unhandled = 0
    for case in cases["cases"]:
        try:
            started = time.perf_counter()
            response = service.answer(
                request_id=f"verify-{case['case_id']}",
                trace_id=f"verify-trace-{case['case_id']}",
                question=case["question"],
                search_hint=None,
                locale=case["locale"],
                scope=scope,
            )
            latencies.append(time.perf_counter() - started)
            expected = case.get("expected_answer_contains", [])
            forbidden = case.get("expected_answer_not_contains", [])
            expected_total += len(expected)
            case_expected_passed = sum(
                snippet in response.answer for snippet in expected
            )
            expected_passed += case_expected_passed
            requested_fields += len(case["requested_fields"])
            supported_fields += sum(
                item.state == "supported"
                for item in response.coverage
                if item.field in case["requested_fields"]
            )
            constraint_ok = not any(
                snippet in response.answer for snippet in forbidden
            )
            constraints_passed += constraint_ok
            returned_citations = {
                citation.citation_id: citation
                for citation in response.citations
            }
            citation_ok = all(
                citation_id in returned_citations
                for item in response.coverage
                if item.state == "supported"
                for citation_id in item.citation_ids
            ) and all(
                citation.coordinate.source_id
                and citation.coordinate.source_version_id
                and citation.original_filename
                and citation.excerpt
                for citation in response.citations
            )
            citation_case_passed += citation_ok
            dump_detected = any(
                response.answer.strip() == citation.excerpt.strip()
                for citation in response.citations
            )
            dump_count += dump_detected

            denied = service.answer(
                request_id=f"verify-denied-{case['case_id']}",
                trace_id=f"verify-denied-trace-{case['case_id']}",
                question=case["question"],
                search_hint=None,
                locale=case["locale"],
                scope=denied_scope,
            )
            safe = (
                denied.status in {"not_found", "refused"}
                and not denied.citations
                and all(item.state in {"absent", "refused"} for item in denied.coverage)
            )
            safe_refusal_passed += safe
            results.append(
                {
                    "case_id": case["case_id"],
                    "status": response.status,
                    "supported_field_count": sum(
                        item.state == "supported"
                        for item in response.coverage
                    ),
                    "requested_field_count": len(case["requested_fields"]),
                    "expected_snippets_passed": case_expected_passed,
                    "expected_snippet_count": len(expected),
                    "constraint_compliance": constraint_ok,
                    "citation_correctness": citation_ok,
                    "safe_refusal": safe,
                    "validation_passed": response.validation["passed"],
                    "repair_count": response.validation["repair_count"],
                    "answer_sha256": hashlib.sha256(
                        response.answer.encode("utf-8")
                    ).hexdigest(),
                }
            )
        except Exception as error:
            unhandled += 1
            results.append(
                {
                    "case_id": case["case_id"],
                    "error_type": type(error).__name__,
                }
            )

    case_count = len(cases["cases"])
    claim_correctness = expected_passed / max(expected_total, 1)
    coverage = supported_fields / max(requested_fields, 1)
    constraint_compliance = constraints_passed / case_count
    citation_correctness = citation_case_passed / case_count
    safe_refusal_accuracy = safe_refusal_passed / case_count
    p50 = statistics.median(latencies) if latencies else float("inf")
    p95 = (
        sorted(latencies)[max(0, int(len(latencies) * 0.95) - 1)]
        if latencies
        else float("inf")
    )
    minimums = gates["minimums"]
    passed = (
        claim_correctness >= minimums["claim_correctness"]
        and coverage >= minimums["multi_part_requested_field_coverage"]
        and constraint_compliance >= minimums["constraint_compliance"]
        and citation_correctness
        >= minimums["high_severity_citation_correctness"]
        and safe_refusal_accuracy >= minimums["safe_refusal_accuracy"]
        and p50 <= minimums["end_to_end_p50_seconds"]
        and p95 <= minimums["end_to_end_p95_seconds"]
        and dump_count == 0
        and unhandled == minimums["unhandled_request_failures"]
    )
    report = {
        "schema_version": 1,
        "kind": "bauer-rag-v4-answering-verification",
        "generated_at_utc": datetime.now(UTC).isoformat(),
        "passed": passed,
        "locked_holdout_opened": False,
        "suite_id": cases["suite_id"],
        "suite_sha256": hashlib.sha256(args.cases.read_bytes()).hexdigest(),
        "gate_manifest_sha256": hashlib.sha256(
            args.gates.read_bytes()
        ).hexdigest(),
        "claim_correctness": claim_correctness,
        "multi_part_requested_field_coverage": coverage,
        "constraint_compliance": constraint_compliance,
        "high_severity_citation_correctness": citation_correctness,
        "safe_refusal_accuracy": safe_refusal_accuracy,
        "question_preservation": 1.0 if unhandled == 0 else 0.0,
        "evidence_dump_count": dump_count,
        "unhandled_request_failures": unhandled,
        "end_to_end_p50_seconds": p50,
        "end_to_end_p95_seconds": p95,
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
    return 0 if passed else 2


if __name__ == "__main__":
    raise SystemExit(main())
