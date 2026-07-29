from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path

import pytest

from bauer_evidence_v4.compilation import CanonicalCompiler
from bauer_evidence_v4.indexing import ProjectionBuilder
from bauer_evidence_v4.retrieval import (
    AuthorizedScope,
    CandidateGenerator,
    IndexedProjection,
    RetrievalRequest,
    StructuredConstraint,
    Subquestion,
)
from bauer_evidence_v4.retrieval.candidate_generation import _EXACT_RE


REPOSITORY_ROOT = Path(__file__).resolve().parents[3]
FIXTURE_PATH = (
    REPOSITORY_ROOT
    / "evals"
    / "bauer-rag-v4"
    / "fixtures"
    / "difficult-documents.json"
)
SOURCE_ROOT = Path(r"D:\02_Code\Bauer Kompressoren Demo")
TENANT = "tenant-bauer"
KNOWLEDGE_BASE = "kb-public-development"
RELEASE = "v4-development-fixtures"

CASES = {
    "B07": (
        "Find the Bauer model I 15.11-11-V. Report its free-air delivery, "
        "maximum operating pressure, number of stages, and motor output.",
        lambda projection: (
            projection.projection_type == "table_row"
            and projection.subject == "I 15.11-11-V"
        ),
    ),
    "B13": (
        "Find the English EN ISO 3834-2 certificate for Bauer "
        "Kompressoren in Munich.",
        lambda projection: (
            projection.projection_type == "metadata"
            and "File Number: 216." in projection.search_text
        ),
    ),
    "B16": (
        "In the BM series 40 bar technical table, give the complete row "
        "for model BM 6.1/40-11.",
        lambda projection: (
            projection.projection_type == "table_row"
            and projection.subject == "BM 6.1/40-11"
        ),
    ),
    "B17": (
        "In the BM series 100 bar 50 Hz technical table, give the complete "
        "row for BM 6.1/100-15.",
        lambda projection: (
            projection.projection_type == "table_row"
            and projection.subject == "BM 6.1/100-15"
            and dict(projection.qualifiers).get("group")
            == "BM series 100 bar – 50 Hz"
        ),
    ),
    "B23": (
        "Find document N47183 and identify its title, language, subject, "
        "and source filename.",
        lambda projection: (
            projection.projection_type == "metadata"
            and projection.document_number == "N47183"
        ),
    ),
    "B29": (
        "What free-air-delivery, motor-power, and pressure range is "
        "documented for the K 22–K 28 series?",
        lambda projection: (
            projection.projection_type == "fact"
            and projection.subject == "K 22 – K 28 SERIES"
        ),
    ),
    "B30": (
        "Welche Förderleistung, Abschaltdruck, Stufenzahl und "
        "Motorleistung hat das Modell BM 6.1/40-11?",
        lambda projection: (
            projection.projection_type == "table_row"
            and projection.subject == "BM 6.1/40-11"
        ),
    ),
}


@pytest.fixture(scope="module")
def retrieval_fixture() -> tuple[
    CandidateGenerator,
    AuthorizedScope,
    tuple[IndexedProjection, ...],
]:
    if not SOURCE_ROOT.is_dir():
        pytest.skip("authoritative original corpus is not mounted")
    bundle = json.loads(FIXTURE_PATH.read_text(encoding="utf-8"))
    compiler = CanonicalCompiler()
    builder = ProjectionBuilder()
    authorized: list[IndexedProjection] = []
    unauthorized: list[IndexedProjection] = []
    source_ids: set[str] = set()
    for fixture in bundle["documents"]:
        source = fixture["source"]
        source_ids.add(source["external_file_id"])
        path = SOURCE_ROOT.joinpath(*source["logical_path"].split("/"))
        result = compiler.compile(
            path.read_bytes(),
            source_path=source["logical_path"],
            declared_media_type=source["media_type"],
            fixture=fixture,
        )
        assert result.document is not None
        for projection in builder.build(result.document):
            authorized.append(
                IndexedProjection(
                    projection=projection,
                    tenant_id=TENANT,
                    knowledge_base_id=KNOWLEDGE_BASE,
                    release_id=RELEASE,
                    authorization_source_id=source["external_file_id"],
                )
            )
            unauthorized.append(
                IndexedProjection(
                    projection=projection,
                    tenant_id="tenant-other",
                    knowledge_base_id="kb-other",
                    release_id="release-other",
                    authorization_source_id=f"other-{source['external_file_id']}",
                )
            )
    items = tuple(authorized + unauthorized)
    scope = AuthorizedScope(
        principal_id="principal-test",
        tenant_id=TENANT,
        knowledge_base_id=KNOWLEDGE_BASE,
        release_id=RELEASE,
        authorized_external_source_ids=frozenset(source_ids),
    )
    return CandidateGenerator(items), scope, items


def _request(
    question: str,
    scope: AuthorizedScope,
    *,
    search_hint: str | None = None,
    subquestions: tuple[Subquestion, ...] = (),
    constraints: tuple[StructuredConstraint, ...] = (),
) -> RetrievalRequest:
    return RetrievalRequest(
        question=question,
        search_hint=search_hint,
        subquestions=subquestions,
        constraints=constraints,
        scope=scope,
    )


def test_candidate_recall_at_10_passes_before_reranking(
    retrieval_fixture: tuple,
) -> None:
    generator, scope, _ = retrieval_fixture
    recalls = []
    for case_id, (question, relevant) in CASES.items():
        result = generator.retrieve(_request(question, scope), limit=10)
        recalled = any(
            relevant(candidate.item.projection)
            for candidate in result.candidates
        )
        recalls.append(recalled)
        assert recalled, case_id
    assert sum(recalls) / len(recalls) >= 0.95


def test_all_three_candidate_families_contribute(
    retrieval_fixture: tuple,
) -> None:
    generator, scope, _ = retrieval_fixture
    result = generator.retrieve(
        _request(CASES["B07"][0], scope),
        limit=30,
    )
    counts = dict(result.channel_hit_counts)
    assert counts["exact"] > 0
    assert counts["lexical"] > 0
    assert counts["dense"] > 0


def test_exact_channel_recognizes_bauer_product_codes_not_numeric_prose(
    retrieval_fixture: tuple,
) -> None:
    _, scope, items = retrieval_fixture
    base = next(
        item
        for item in items
        if item.tenant_id == scope.tenant_id
        and item.authorization_source_id
        in scope.authorized_external_source_ids
    )
    projection = replace(
        base.projection,
        projection_id="projection_b_select_regression",
        search_text=(
            "Document title: B-SELECT. B-SELECT is an automatic selector "
            "unit with documented flow rates at 50, 200, and 300 bar."
        ),
        exact_terms=(),
    )
    generator = CandidateGenerator((replace(base, projection=projection),))
    query = (
        "Find B-SELECT and report flow rates at 50, 200, and 300 bar."
    )
    assert [match.group(0) for match in _EXACT_RE.finditer(query)] == [
        "B-SELECT"
    ]
    hits = generator.exact.search(
        query,
        subquestion_id="product",
        scope=scope,
        constraints=(),
        limit=10,
    )
    assert [hit.item.projection.projection_id for hit in hits] == [
        "projection_b_select_regression"
    ]


def test_each_channel_filters_tenant_kb_release_and_source_scope(
    retrieval_fixture: tuple,
) -> None:
    generator, scope, _ = retrieval_fixture
    denied_scopes = (
        AuthorizedScope(
            principal_id=scope.principal_id,
            tenant_id="tenant-denied",
            knowledge_base_id=scope.knowledge_base_id,
            release_id=scope.release_id,
            authorized_external_source_ids=scope.authorized_external_source_ids,
        ),
        AuthorizedScope(
            principal_id=scope.principal_id,
            tenant_id=scope.tenant_id,
            knowledge_base_id="kb-denied",
            release_id=scope.release_id,
            authorized_external_source_ids=scope.authorized_external_source_ids,
        ),
        AuthorizedScope(
            principal_id=scope.principal_id,
            tenant_id=scope.tenant_id,
            knowledge_base_id=scope.knowledge_base_id,
            release_id="release-denied",
            authorized_external_source_ids=scope.authorized_external_source_ids,
        ),
        AuthorizedScope(
            principal_id=scope.principal_id,
            tenant_id=scope.tenant_id,
            knowledge_base_id=scope.knowledge_base_id,
            release_id=scope.release_id,
            authorized_external_source_ids=frozenset({"not-a-source"}),
        ),
    )
    for denied in denied_scopes:
        for channel in (generator.exact, generator.lexical, generator.dense):
            assert channel.search(
                CASES["B23"][0],
                subquestion_id="denied",
                scope=denied,
                constraints=(),
                limit=10,
            ) == []
        result = generator.retrieve(
            _request(CASES["B23"][0], denied),
            limit=10,
        )
        assert result.candidates == ()
        assert result.authorized_projection_count == 0


def test_original_question_is_separate_from_search_hint(
    retrieval_fixture: tuple,
) -> None:
    generator, scope, _ = retrieval_fixture
    question = CASES["B30"][0]
    hint = "BM technical data English table"
    result = generator.retrieve(
        _request(question, scope, search_hint=hint),
        limit=10,
    )
    assert result.original_question == question
    assert result.search_hint == hint
    assert question not in {hint}


def test_per_subquestion_retrieval_preserves_both_needs(
    retrieval_fixture: tuple,
) -> None:
    generator, scope, _ = retrieval_fixture
    question = (
        "Give the BM 6.1/40-11 technical row and identify document N47183."
    )
    result = generator.retrieve(
        _request(
            question,
            scope,
            subquestions=(
                Subquestion("bm_row", CASES["B16"][0]),
                Subquestion("document", CASES["B23"][0]),
            ),
        ),
        limit=20,
    )
    assert any(
        candidate.item.projection.subject == "BM 6.1/40-11"
        and "bm_row" in candidate.subquestion_ids
        for candidate in result.candidates
    )
    assert any(
        candidate.item.projection.document_number == "N47183"
        and "document" in candidate.subquestion_ids
        for candidate in result.candidates
    )


def test_candidate_union_preserves_each_channel_head_per_subquestion(
    retrieval_fixture: tuple,
) -> None:
    generator, scope, _ = retrieval_fixture
    subquestions = (
        Subquestion("row", CASES["B16"][0]),
        Subquestion("document", CASES["B23"][0]),
    )
    result = generator.retrieve(
        _request(
            "Return the requested row and document.",
            scope,
            subquestions=subquestions,
        ),
        limit=20,
    )
    selected = {
        candidate.item.canonical_key for candidate in result.candidates
    }
    for subquestion in subquestions:
        for channel in (generator.exact, generator.lexical, generator.dense):
            head = channel.search(
                subquestion.text,
                subquestion_id=subquestion.subquestion_id,
                scope=scope,
                constraints=(),
                limit=1,
            )
            if head:
                assert head[0].item.canonical_key in selected
                assert next(
                    candidate
                    for candidate in result.candidates
                    if candidate.item.canonical_key
                    == head[0].item.canonical_key
                ).preserved_channel_head


def test_structured_constraints_apply_inside_every_channel(
    retrieval_fixture: tuple,
) -> None:
    generator, scope, _ = retrieval_fixture
    constraints = (
        StructuredConstraint(
            field="subject",
            operator="eq",
            value="BM 6.1/40-11",
        ),
    )
    result = generator.retrieve(
        _request(CASES["B16"][0], scope, constraints=constraints),
        limit=20,
    )
    assert result.candidates
    assert all(
        candidate.item.projection.subject == "BM 6.1/40-11"
        for candidate in result.candidates
    )
