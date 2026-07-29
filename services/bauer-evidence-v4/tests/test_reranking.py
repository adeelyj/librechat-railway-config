from __future__ import annotations

import json
import time
from pathlib import Path

import pytest

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


REPOSITORY_ROOT = Path(__file__).resolve().parents[3]
FIXTURE_PATH = (
    REPOSITORY_ROOT
    / "evals"
    / "bauer-rag-v4"
    / "fixtures"
    / "difficult-documents.json"
)
CASE_PATH = (
    REPOSITORY_ROOT
    / "evals"
    / "bauer-rag-v4"
    / "cases"
    / "development-retrieval.json"
)
SOURCE_ROOT = Path(r"D:\02_Code\Bauer Kompressoren Demo")


@pytest.fixture(scope="module")
def candidate_sets() -> tuple[dict, dict[str, object]]:
    if not SOURCE_ROOT.is_dir():
        pytest.skip("authoritative original corpus is not mounted")
    fixture_bundle = json.loads(FIXTURE_PATH.read_text(encoding="utf-8"))
    cases = json.loads(CASE_PATH.read_text(encoding="utf-8"))
    compiler = CanonicalCompiler()
    builder = ProjectionBuilder()
    items = []
    source_ids = set()
    for fixture in fixture_bundle["documents"]:
        source = fixture["source"]
        source_ids.add(source["external_file_id"])
        path = SOURCE_ROOT.joinpath(*source["logical_path"].split("/"))
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
    sets = {
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
    return cases, sets


def _relevant(projection, expected: dict) -> bool:
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


def _metrics(reranker, cases: dict, sets: dict[str, object]) -> dict[str, float]:
    ranks = []
    for case in cases["cases"]:
        result = reranker.rerank(sets[case["case_id"]], limit=10)
        rank = next(
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
        ranks.append(rank)
    return {
        "recall_at_5": sum(
            rank is not None and rank <= 5 for rank in ranks
        )
        / len(ranks),
        "mrr": sum(1 / rank if rank else 0 for rank in ranks) / len(ranks),
    }


def test_reranker_benchmark_and_smallest_passing_selection(
    candidate_sets: tuple[dict, dict[str, object]],
) -> None:
    cases, sets = candidate_sets
    alternatives = (
        TransparentFeatureReranker(),
        LocalInteractionReranker(),
    )
    metrics = {
        reranker.model_id: _metrics(reranker, cases, sets)
        for reranker in alternatives
    }
    assert metrics["transparent-linear-v1"]["recall_at_5"] >= 0.9
    selected = select_smallest_passing(
        metrics,
        alternatives,
        minimum_recall_at_5=0.9,
    )
    assert selected.model_id == "transparent-linear-v1"
    assert selected.complexity_units == 1


def test_selected_reranker_passes_each_named_case_at_five(
    candidate_sets: tuple[dict, dict[str, object]],
) -> None:
    cases, sets = candidate_sets
    reranker = TransparentFeatureReranker()
    for case in cases["cases"]:
        result = reranker.rerank(sets[case["case_id"]], limit=5)
        assert any(
            _relevant(item.candidate.item.projection, case["relevant"])
            for item in result.ranked
        ), case["case_id"]


def test_reranking_is_deterministic_and_feature_traceable(
    candidate_sets: tuple[dict, dict[str, object]],
) -> None:
    _, sets = candidate_sets
    candidate_set = sets["B30"]
    reranker = TransparentFeatureReranker()
    first = reranker.rerank(candidate_set, limit=10)
    second = reranker.rerank(candidate_set, limit=10)
    assert first == second
    assert first.original_question == candidate_set.original_question
    assert len(first.model_identity) == 64
    assert all(item.feature_values for item in first.ranked)
    assert all(
        tuple(name for name, _ in item.feature_values)
        == tuple(sorted(name for name, _ in item.feature_values))
        for item in first.ranked
    )


def test_local_rerankers_fit_latency_budget(
    candidate_sets: tuple[dict, dict[str, object]],
) -> None:
    _, sets = candidate_sets
    started = time.perf_counter()
    for reranker in (
        TransparentFeatureReranker(),
        LocalInteractionReranker(),
    ):
        for candidate_set in sets.values():
            reranker.rerank(candidate_set, limit=10)
    assert time.perf_counter() - started < 5.0
