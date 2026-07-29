from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from bauer_evidence_v4.answering import (
    AnswerService,
    SourceRegistry,
    TaskAnalyzer,
)
from bauer_evidence_v4.answering.models import AnswerDraft, Claim
from bauer_evidence_v4.answering.coverage import _normalize as normalize_coverage
from bauer_evidence_v4.answering.render import GroundedAnswerBuilder
from bauer_evidence_v4.answering.validation import (
    AnswerValidator,
    TargetedRepair,
)
from bauer_evidence_v4.compilation import CanonicalCompiler
from bauer_evidence_v4.contracts.models import ReleaseContract
from bauer_evidence_v4.indexing import ProjectionBuilder
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


def _digest(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


@pytest.fixture(scope="module")
def answer_fixture() -> tuple[
    AnswerService,
    AuthorizedScope,
    dict,
]:
    if not SOURCE_ROOT.is_dir():
        pytest.skip("authoritative original corpus is not mounted")
    fixtures = json.loads(FIXTURE_PATH.read_text(encoding="utf-8"))
    cases = json.loads(CASE_PATH.read_text(encoding="utf-8"))
    compiler = CanonicalCompiler()
    builder = ProjectionBuilder()
    documents = {}
    external_source_ids = {}
    linked_filenames = {}
    indexed = []
    all_source_ids = set()

    def add_source(source: dict, *, fixture: dict | None = None):
        path = SOURCE_ROOT.joinpath(*source["logical_path"].split("/"))
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
        all_source_ids.add(source["external_file_id"])
        indexed.extend(
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
        primary = add_source(fixture["source"], fixture=fixture)
        for linked in fixture.get("linked_sources", []):
            linked_document = add_source(linked)
            file_number = (
                "216"
                if linked_document.source_filename == "bauer_amfile_216.pdf"
                else ""
            )
            if file_number:
                linked_filenames[
                    (primary.source_sha256, file_number)
                ] = linked_document.source_filename

    release = ReleaseContract(
        public_id="release",
        source_contract_sha256=_digest("source-contract"),
        canonical_schema_version="4.1",
        compiler_identity=_digest("compiler"),
        projection_identity=_digest("projection"),
        embedding_identity=_digest("embedding"),
        reranker_identity=_digest("reranker"),
        gate_manifest_sha256=_digest("gates"),
    )
    service = AnswerService(
        candidate_generator=CandidateGenerator(tuple(indexed)),
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
        authorized_external_source_ids=frozenset(all_source_ids),
    )
    return service, scope, cases


def test_task_analyzer_derives_all_requested_fields(
    answer_fixture: tuple,
) -> None:
    _, _, cases = answer_fixture
    analyzer = TaskAnalyzer()
    for case in cases["cases"]:
        plan = analyzer.analyze(
            case["question"],
            locale=case["locale"],
        )
        assert [field.field for field in plan.fields] == case[
            "requested_fields"
        ], case["case_id"]
        assert plan.original_question == case["question"]
        assert len(plan.subquestions) == len(plan.fields)


def test_general_analysis_requires_topic_evidence_not_filenames() -> None:
    analyzer = TaskAnalyzer()
    product_plan = analyzer.analyze(
        "Find B-SELECT and report its operating pressure and functions."
    )
    assert product_plan.intent == "general"
    assert [field.field for field in product_plan.fields] == ["topic_1"]
    assert product_plan.fields[0].anchor_terms == ("B-SELECT",)
    assert product_plan.fields[0].match_terms
    assert all(field.field != "source" for field in product_plan.fields)

    absence_plan = analyzer.analyze(
        "Find Bauer order number N99999999 and report the exact component."
    )
    assert absence_plan.fields[0].anchor_terms == ("N99999999",)

    isolation_plan = analyzer.analyze(
        "Search for EPLAN public test documents from the Test Archive."
    )
    assert isolation_plan.fields[0].anchor_terms == (
        "Test Archive",
        "EPLAN",
    )
    synthetic_plan = analyzer.analyze(
        "Find the closest previous synthetic nitrogen booster project."
    )
    assert synthetic_plan.fields[0].anchor_terms == ("SYN-",)
    assert synthetic_plan.fields[0].required is True

    part_plan = analyzer.analyze(
        "Find the best compatible synthetic part; do not claim it is real."
    )
    assert part_plan.intent == "general"
    assert part_plan.fields[0].field == "synthetic_record_evidence"

    two_rows = analyzer.analyze(
        "The model I 15.11-11-V appears twice. Return both rows and footnotes."
    )
    assert two_rows.intent == "table_row"
    assert "effective_free_air_delivery" in {
        field.field for field in two_rows.fields
    }
    assert normalize_coverage("B\u2011SELECT") == normalize_coverage("B-SELECT")


def test_general_absence_is_explicit_and_has_no_source_only_success(
    answer_fixture: tuple,
) -> None:
    service, scope, _ = answer_fixture
    response = service.answer(
        request_id="request-absent-standard",
        trace_id="trace-absent-standard",
        question=(
            "Find an ISO 27001 certificate for BAUER KOMPRESSOREN GmbH. "
            "Do not substitute ISO 9001, ISO 14001, or EN ISO 3834-2."
        ),
        search_hint=None,
        locale="en",
        scope=scope,
    )
    assert response.status == "not_found"
    assert response.citations == []
    assert "not established in the authorized bauer evidence" in (
        response.answer.casefold()
    )
    assert response.coverage[0].field == "topic_1"
    assert response.coverage[0].state == "absent"


def test_direct_v4_answers_are_complete_grounded_and_cited(
    answer_fixture: tuple,
) -> None:
    service, scope, cases = answer_fixture
    for case in cases["cases"]:
        response = service.answer(
            request_id=f"request-{case['case_id']}",
            trace_id=f"trace-{case['case_id']}",
            question=case["question"],
            search_hint=None,
            locale=case["locale"],
            scope=scope,
        )
        assert response.status == "complete", (
            case["case_id"],
            response.answer,
            response.coverage,
            response.validation,
        )
        assert response.validation["passed"] is True
        assert response.validation["repair_count"] == 0
        assert {item.field for item in response.coverage} == set(
            case["requested_fields"]
        )
        assert all(item.state == "supported" for item in response.coverage)
        assert response.citations
        assert all(
            citation.coordinate.source_id
            and citation.coordinate.source_version_id
            and citation.original_filename
            and citation.excerpt
            for citation in response.citations
        )
        assert all(
            response.answer.strip() != citation.excerpt.strip()
            for citation in response.citations
        )


def test_named_answer_semantics_and_constraints(
    answer_fixture: tuple,
) -> None:
    service, scope, cases = answer_fixture
    responses = {
        case["case_id"]: service.answer(
            request_id=f"semantic-{case['case_id']}",
            trace_id=f"semantic-trace-{case['case_id']}",
            question=case["question"],
            search_hint="optional UI hint",
            locale=case["locale"],
            scope=scope,
        )
        for case in cases["cases"]
    }
    b07 = responses["B07"].answer
    assert "VERTICUS I 350 - 420 bar: 420 bar" in b07
    assert "VERTICUS I 420 - 525 bar: 525 bar" in b07
    assert "ISO 1217" in b07

    b13 = responses["B13"].answer
    assert "BAUER KOMPRESSOREN GmbH" in b13
    assert "Stäblistr. 8, 81477 Munich" in b13
    assert "bauer_amfile_216.pdf" in b13
    assert "ISO 9001" not in b13
    assert "ISO 14001" not in b13

    b17 = responses["B17"].answer
    assert "630 l/min" in b17
    assert "15 kW" in b17
    assert "425 kg" in b17
    assert "760 l/min" not in b17
    assert "435 kg" not in b17

    b23 = responses["B23"].answer
    assert "N47183" in " ".join(
        citation.document_number or ""
        for citation in responses["B23"].citations
    )
    assert "SENSOR CALIBRATION" in b23
    assert "2024-01_B-DETECTION_Sensor_calibration_EN_N47183_sc.pdf" in b23

    b29 = responses["B29"].answer
    assert "600–6800 l/min" in b29
    assert "22–110 kW" in b29
    assert "30–525 bar" in b29
    assert "PE-VE INDUSTRY" not in b29
    assert "85 – 1470 l/min" not in b29

    b30 = responses["B30"].answer
    assert "660 l/min" in b30
    assert "40 bar" in b30
    assert "11 kW" in b30
    assert "0027_bm-series-40_e0b4c6a9a3.html" in b30


def test_authorization_negative_returns_no_evidence(
    answer_fixture: tuple,
) -> None:
    service, scope, cases = answer_fixture
    denied = AuthorizedScope(
        principal_id=scope.principal_id,
        tenant_id=scope.tenant_id,
        knowledge_base_id=scope.knowledge_base_id,
        release_id=scope.release_id,
        authorized_external_source_ids=frozenset({"not-indexed"}),
    )
    response = service.answer(
        request_id="request-denied",
        trace_id="trace-denied",
        question=cases["cases"][0]["question"],
        search_hint=None,
        locale="en",
        scope=denied,
    )
    assert response.status == "not_found"
    assert response.citations == []
    assert all(item.state == "absent" for item in response.coverage)
    assert "Missing or unresolved fields:" in response.answer


def test_validator_detects_dump_and_targeted_repair_is_single_attempt(
    answer_fixture: tuple,
) -> None:
    service, scope, cases = answer_fixture
    case = cases["cases"][4]
    plan = service.analyzer.analyze(case["question"], locale="en")
    candidate_set = service.candidate_generator.retrieve(
        RetrievalRequest(
            question=case["question"],
            search_hint=None,
            subquestions=plan.subquestions,
            constraints=(),
            scope=scope,
        ),
        limit=50,
    )
    reranked = service.reranker.rerank(candidate_set, limit=10)
    from bauer_evidence_v4.answering.evidence import EvidenceMaterializer

    evidence = EvidenceMaterializer(service.source_registry).materialize(
        reranked
    )
    coverage = service.coverage_engine.evaluate(plan, evidence)
    citations_by_evidence = {
        context.unit.evidence_id: context.citation
        for context in evidence
    }
    good = GroundedAnswerBuilder().build(
        plan,
        coverage,
        citations_by_evidence,
    )
    bad = AnswerDraft(
        status=good.status,
        answer=evidence[0].unit.search_text,
        coverage=good.coverage,
        claims=good.claims
        + (
            Claim(
                field="invented",
                text="Invented: 999",
                values=("999",),
                evidence_ids=(),
            ),
        ),
        citations=good.citations,
    )
    validator = AnswerValidator()
    report = validator.validate(plan, bad, evidence)
    assert {defect.code for defect in report.defects} >= {
        "unsupported_claim",
        "evidence_dump_detected",
    }
    repairer = TargetedRepair(GroundedAnswerBuilder())
    repaired = repairer.repair(plan, bad, citations_by_evidence)
    assert repaired.repair_count == 1
    assert validator.validate(
        plan,
        repaired,
        evidence,
        repair_attempted=True,
    ).passed
    with pytest.raises(ValueError, match="only one targeted repair"):
        repairer.repair(plan, repaired, citations_by_evidence)
