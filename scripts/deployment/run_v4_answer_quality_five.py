from __future__ import annotations

import argparse
import hashlib
import json
from datetime import datetime, timezone
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


REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_CORPUS_ROOT = Path(r"D:\02_Code\Bauer Kompressoren Demo")
BASELINE_PATH = (
    REPOSITORY_ROOT
    / "evidence"
    / "bauer-rag-v4-answer-quality"
    / "baseline-five-case-v1-v4.json"
)
DEFAULT_OUTPUT = (
    REPOSITORY_ROOT
    / "evidence"
    / "bauer-rag-v4-answer-quality"
    / "local-five-case-v4-repaired.json"
)
CASE_IDS = ("B06", "B17", "B10", "B19", "B03")
SOURCES = (
    ("bauer_index/raw_docs/2026-04_Compressors_for_Industry_EN_N39771_sc.pdf", "application/pdf"),
    ("bauer_index/raw_html/0025_bm-series-100_7acdece303.html", "text/html"),
    ("bauer_index/raw_docs/2025-03_B-DETECTION_PLUS_EN_N42078_sc.pdf", "application/pdf"),
    ("bauer_index/raw_html/0015_b-detection-plus-i-und-s_7c1fd56ba6.html", "text/html"),
    ("bauer_index/raw_html/0016_b-detection-plus-m_b749dac8cd.html", "text/html"),
    ("bauer_index/raw_html/0021_b-kool_c5e62182b0.html", "text/html"),
)


def _digest(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _load_cases() -> tuple[dict, dict[str, dict]]:
    baseline = json.loads(BASELINE_PATH.read_text(encoding="utf-8"))
    if baseline.get("locked_holdout_opened") is not False:
        raise RuntimeError("holdout boundary is not sealed")
    selected = tuple(baseline.get("selection", {}).get("case_ids", ()))
    if selected != CASE_IDS:
        raise RuntimeError(f"unexpected five-case selection: {selected}")
    cases = {item["case_id"]: item for item in baseline["cases"]}
    if set(cases) != set(CASE_IDS):
        raise RuntimeError("baseline case accounting is incomplete")
    return baseline, cases


def _service(corpus_root: Path) -> tuple[AnswerService, AuthorizedScope, list[dict]]:
    compiler = CanonicalCompiler()
    projection_builder = ProjectionBuilder()
    documents = {}
    external_source_ids = {}
    indexed = []
    source_manifest = []

    source_specs = [
        (corpus_root / Path(logical_path), logical_path, media_type)
        for logical_path, media_type in SOURCES
    ]
    synthetic = (
        REPOSITORY_ROOT
        / "services"
        / "bauer-evidence-v4"
        / "resources"
        / "bauer-synthetic-demo-v1.html"
    )
    source_specs.append(
        (synthetic, "resources/bauer-synthetic-demo-v1.html", "text/html")
    )
    for index, (path, logical_path, media_type) in enumerate(source_specs, start=1):
        payload = path.read_bytes()
        compiled = compiler.compile(
            payload,
            source_path=logical_path,
            declared_media_type=media_type,
        )
        if compiled.document is None:
            raise RuntimeError(f"canonical source was not published: {logical_path}")
        document = compiled.document
        external_id = f"answer-quality-source-{index}"
        documents[document.source_sha256] = document
        external_source_ids[document.source_sha256] = external_id
        indexed.extend(
            IndexedProjection(
                projection=projection,
                tenant_id="answer-quality-tenant",
                knowledge_base_id="answer-quality-kb",
                release_id="answer-quality-release",
                authorization_source_id=external_id,
            )
            for projection in projection_builder.build(document)
        )
        source_manifest.append(
            {
                "logical_path": logical_path,
                "media_type": media_type,
                "source_sha256": document.source_sha256,
                "parser_id": document.parser_id,
                "projection_count": len(projection_builder.build(document)),
                "authority": (
                    "synthetic_demo" if "synthetic-demo" in logical_path else "public_bauer"
                ),
            }
        )

    release = ReleaseContract(
        public_id="answer-quality-release",
        source_contract_sha256=_digest("answer-quality-source-contract"),
        canonical_schema_version="4.1",
        compiler_identity=_digest("answer-quality-compiler"),
        projection_identity=_digest("answer-quality-projection"),
        embedding_identity=_digest("answer-quality-embedding"),
        reranker_identity=_digest("answer-quality-reranker"),
        gate_manifest_sha256=_digest("answer-quality-gates"),
    )
    service = AnswerService(
        candidate_generator=CandidateGenerator(tuple(indexed)),
        source_registry=SourceRegistry(
            documents_by_sha256=documents,
            external_source_ids=external_source_ids,
        ),
        release=release,
    )
    scope = AuthorizedScope(
        principal_id="answer-quality-principal",
        tenant_id="answer-quality-tenant",
        knowledge_base_id="answer-quality-kb",
        release_id="answer-quality-release",
        authorized_external_source_ids=frozenset(external_source_ids.values()),
    )
    return service, scope, source_manifest


def run(corpus_root: Path) -> dict:
    baseline, cases = _load_cases()
    service, scope, source_manifest = _service(corpus_root)
    observations = []
    for case_id in CASE_IDS:
        case = cases[case_id]
        response = service.answer(
            request_id=f"local-{case_id}-repaired",
            trace_id=f"local-trace-{case_id}",
            question=case["prompt"],
            search_hint=None,
            locale="en",
            scope=scope,
        )
        observations.append(
            {
                "case_id": case_id,
                "category": case["category"],
                "prompt": case["prompt"],
                "status": response.status,
                "answer": response.answer,
                "answer_sha256": _digest(response.answer),
                "coverage": [item.model_dump(mode="json") for item in response.coverage],
                "not_found": response.not_found,
                "citation_count": len(response.citations),
                "citation_documents": sorted(
                    {item.original_filename for item in response.citations}
                ),
                "citations": [
                    {
                        "citation_id": item.citation_id,
                        "original_filename": item.original_filename,
                        "page": item.coordinate.page,
                        "printed_page": item.coordinate.printed_page,
                        "section_path": item.coordinate.section_path,
                        "table_title": item.table_title,
                        "header_path": item.header_path,
                    }
                    for item in response.citations
                ],
                "validation": response.validation,
            }
        )
    return {
        "schema_version": "bauer-rag-v4-answer-quality-local-v1",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "kind": "local-five-case-repaired-v4",
        "split": "public-development",
        "locked_holdout_opened": False,
        "selection": baseline["selection"],
        "historical_baseline_sha256": hashlib.sha256(
            BASELINE_PATH.read_bytes()
        ).hexdigest(),
        "source_manifest": source_manifest,
        "observations": observations,
        "secrets_emitted": False,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--corpus-root", type=Path, default=DEFAULT_CORPUS_ROOT)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    args = parser.parse_args()
    result = run(args.corpus_root)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(result, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
        newline="\n",
    )
    print(args.output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
