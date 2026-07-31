from __future__ import annotations

import hashlib
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from bauer_evidence_v4.answering import (
    AnswerService,
    SourceRegistry,
    TaskAnalyzer,
)
from bauer_evidence_v4.answering.models import AnswerDraft, Claim
from bauer_evidence_v4.answering.coverage import _normalize as normalize_coverage
from bauer_evidence_v4.answering.coverage import CoverageEngine
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
    company_plan = analyzer.analyze("what does bauer kompressoren do")
    assert company_plan.intent == "general"
    assert [field.field for field in company_plan.fields] == [
        "company_core_business",
        "company_product_portfolio",
        "company_application_scope",
    ]
    assert all(
        company_plan.original_question not in subquestion.text
        for subquestion in company_plan.subquestions
    )
    typo_plan = analyzer.analyze("list hte products from bayuer")
    assert [field.field for field in typo_plan.fields] == [
        "company_core_business",
        "company_product_portfolio",
        "company_application_scope",
    ]
    specific_plan = analyzer.analyze(
        "What does B-CLOUD do for Bauer Kompressoren?"
    )
    assert all(
        not field.field.startswith("company_")
        for field in specific_plan.fields
    )
    product_plan = analyzer.analyze(
        "Find B-SELECT and report its operating pressure and functions."
    )
    assert product_plan.intent == "general"
    assert [field.field for field in product_plan.fields] == [
        "topic_1_pressure",
        "topic_1_functions",
    ]
    assert all(
        field.anchor_terms == ("B-SELECT",)
        for field in product_plan.fields
    )
    assert all(field.match_terms for field in product_plan.fields)
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
    assert CoverageEngine._bounded_snippet(
        "B-SELECT " + ("unrelated " * 120) + "operating pressure 420 bar",
        ("operating pressure",),
        anchor_terms=("B-SELECT",),
    ) == ""
    nearby = CoverageEngine._bounded_snippet(
        "B-SELECT automatic selector. Operating pressure: 420 bar.",
        ("operating pressure",),
        anchor_terms=("B-SELECT",),
    )
    assert "B-SELECT" in nearby
    assert "420 bar" in nearby
    assert CoverageEngine._bounded_snippet(
        "Flow rate: 850 l/min. Optional automatic selector unit B-SELECT.",
        ("flow",),
        anchor_terms=("B-SELECT",),
    ) == ""
    assert CoverageEngine._bounded_snippet(
        "B-SELECT pressure regulator with stable overflow pressure.",
        ("flow",),
        anchor_terms=("B-SELECT",),
    ) == ""
    assert CoverageEngine._positions(
        "the teams work syn- over the long term",
        "syn-",
    ) == ()
    assert CoverageEngine._positions(
        "record SYN-BK-N2-420-500 is synthetic",
        "syn-",
    )
    assert CoverageEngine._bounded_snippet(
        "The teams work syn- over the long term.",
        ("SYN-",),
        anchor_terms=("SYN-",),
    ) == ""
    assert CoverageEngine._concise_value(
        "The teams work syn- over the long term.",
        synthetic_plan.fields[0],
    ) == ""
    concise_flow = CoverageEngine._concise_value(
        (
            "B-SELECT Flow rate at: P = 50 bar 2750 l/min "
            "P = 200 bar 3500 l/min P = 300 bar 3700 l/min"
        ),
        product_plan.fields[0].__class__(
            field="topic_1_flow",
            label="B-SELECT flow",
            anchor_terms=("B-SELECT",),
            match_terms=("flow", "50 bar", "200 bar", "300 bar"),
        ),
    )
    assert concise_flow == (
        "50 bar: 2750 l/min; 200 bar: 3500 l/min; "
        "300 bar: 3700 l/min"
    )
    concise_pressure = CoverageEngine._concise_value(
        (
            "B-SELECT Operating pressure: 414/420 bar "
            "Adjustment range: Pressure relief valve: 100–414/420 bar "
            "Flow rate at P = 50 bar 2750 l/min"
        ),
        product_plan.fields[0],
    )
    assert concise_pressure == (
        "Operating pressure: 414/420 bar; "
        "Adjustment range: Pressure relief valve: 100–414/420 bar"
    )
    paired_pressure = CoverageEngine._concise_value(
        "B-KOOL III Maximum operating pressure: 350 bar / 550 bar",
        product_plan.fields[0].__class__(
            field="topic_1_pressure",
            label="B-KOOL III pressure",
            anchor_terms=("B-KOOL III",),
            match_terms=("maximum operating pressure", "pressure"),
        ),
    )
    assert paired_pressure == "Maximum operating pressure: 350 bar / 550 bar"
    concise_functions = CoverageEngine._concise_value(
        (
            "B-SELECT performs 3 important functions: "
            "› Pre-filling of the cylinders from storage "
            "› Filling of the diving cylinders from the compressor "
            "› Refilling the storage bottle battery "
            "The automatic unit consists of a pressure retention valve."
        ),
        product_plan.fields[0].__class__(
            field="topic_1_functions",
            label="B-SELECT functions",
            anchor_terms=("B-SELECT",),
            match_terms=("functions", "pre-filling", "refilling"),
        ),
    )
    assert concise_functions == (
        "Pre-filling of the cylinders from storage; "
        "Filling of the diving cylinders from the compressor; "
        "Refilling the storage bottle battery"
    )
    b06_plan = analyzer.analyze(
        (
            "What is the highest documented maximum operating pressure "
            "for a Bauer compressor? Distinguish compressors from boosters "
            "and explain shutdown pressure."
        )
    )
    assert all(
        b06_plan.original_question not in subquestion.text
        for subquestion in b06_plan.subquestions
    )
    compressor_value = CoverageEngine._concise_value(
        (
            "Content: COMPRESSORS AIR COOLED | 30 - 525 BAR "
            "I Series | MINI-VERTICUS 90 - 420 bar "
            "I Series | VERTICUS 90 - 525 bar "
            "I Series | K 22 - K 28 90 - 525 bar"
        ),
        b06_plan.fields[0],
    )
    assert compressor_value == (
        "Highest compressor maximum operating pressure: 525 bar; "
        "I Series | VERTICUS: 90–525 bar; "
        "I Series | K 22 - K 28: 90–525 bar"
    )
    booster_value = CoverageEngine._concise_value(
        (
            "Content: BOOSTER AIR COOLED | 25 - 420 BAR "
            "GIB Series | VERTICUS 90 - 365 bar "
            "BOOSTER WATER COOLED | 25 - 520 BAR "
            "GIB Series | BK 23 – BK 52 90 - 520 bar"
        ),
        b06_plan.fields[1],
    )
    assert booster_value == (
        "BOOSTER AIR COOLED: 25–420 bar; "
        "BOOSTER WATER COOLED: 25–520 bar; "
        "GIB Series | BK 23 – BK 52: 90–520 bar"
    )
    assert CoverageEngine._concise_value(
        (
            "VERTICUS AND K 22 – K 28 SERIES. An intelligent air-cooling "
            "system provides reliable cooling for each compressor stage."
        ),
        b06_plan.fields[0],
    ) == ""
    assert CoverageEngine._concise_value(
        "A booster increases an existing inlet pressure.",
        b06_plan.fields[1],
    ) == ""
    assert CoverageEngine._concise_value(
        "The compressor shuts down automatically.",
        b06_plan.fields[2],
    ) == ""
    assert CoverageEngine._value_priority(
        "booster_pressure_evidence",
        booster_value,
    ) == 1520
    assert CoverageEngine._value_priority(
        "pressure_definition_evidence",
        (
            "Maximum allowable working pressure = max. setting safety valve; "
            "final pressure (shut-down pressure) lower"
        ),
    ) == 1000
    comparison_plan = analyzer.analyze(
        (
            "Compare the Bauer BM series at 40 bar and 100 bar. For each "
            "family, report medium, maximum pressure, free-air-delivery "
            "range, and motor-power range. Explain which family is "
            "technically closer to a requirement for air at 90 bar and "
            "approximately 800 l/min. Cite both product sources."
        )
    )
    assert [field.field for field in comparison_plan.fields] == [
        "bm_40_bar_evidence",
        "bm_100_bar_evidence",
        "bm_90_bar_800_l_min_fit",
    ]
    assert all(
        comparison_plan.original_question not in subquestion.text
        for subquestion in comparison_plan.subquestions
    )
    bdetection_plan = analyzer.analyze(
        (
            "Compare B-DETECTION PLUS i/s with B-DETECTION PLUS m for a "
            "stationary fire-brigade filling station. Explain which is "
            "stationary and which is mobile, what they measure, their "
            "logging capabilities, and reconcile 420 bar with 450 bar."
        )
    )
    assert [field.field for field in bdetection_plan.fields] == [
        "bdetection_stationary_evidence",
        "bdetection_mobile_evidence",
        "bdetection_measurements",
        "bdetection_logging",
        "bdetection_pressure_reconciliation",
    ]
    n7698_plan = analyzer.analyze(
        (
            "Find the catalogue entry with order number N7698. Return the "
            "compressor block applications attached to that exact order "
            "number and cite the catalogue location."
        )
    )
    assert [field.field for field in n7698_plan.fields] == [
        "n7698_compressor_block_applications"
    ]
    bcloud_plan = analyzer.analyze(
        (
            "Which Bauer system provides browser or app access to compressor "
            "status and fault notifications, and what software condition is "
            "documented for compatible B-CONTROL MICRO units?"
        )
    )
    assert [field.field for field in bcloud_plan.fields] == [
        "bcloud_access_capabilities",
        "bcloud_software_requirement",
    ]
    b18_plan = analyzer.analyze(
        (
            "The model I 15.11-11-V appears in more than one VERTICUS "
            "pressure table. Return both rows, clearly separating the "
            "420 bar and 525 bar variants and preserving footnotes 1 and 2."
        )
    )
    assert "maximum_operating_pressure" in {
        field.field for field in b18_plan.fields
    }
    assert "shutdown_pressure" not in {
        field.field for field in b18_plan.fields
    }
    b19_plan = analyzer.analyze(
        (
            "What are the documented maximum operating pressures and maximum "
            "flow rates for B-KOOL III? State the separate helium and argon "
            "flow range and cite the technical data."
        )
    )
    assert [field.field for field in b19_plan.fields] == [
        "bkool_iii_pressure",
        "bkool_iii_flow",
    ]
    b11_plan = analyzer.analyze(
        (
            "Can B-SAFE be used to fill Nitrox cylinders at 300 bar? "
            "Answer yes or no based only on the uploaded Bauer "
            "documentation."
        )
    )
    assert [field.field for field in b11_plan.fields] == [
        "bsafe_nitrox_300_decision"
    ]
    b21_plan = analyzer.analyze(
        (
            "Reconcile the B-SAFE page headline that states breathing-air "
            "use up to 300 bar and Nitrox use up to 200 bar with the later "
            "B-SAFE 300 technical-data values."
        )
    )
    assert [field.field for field in b21_plan.fields] == [
        "bsafe_wording_reconciliation"
    ]


def test_company_overview_is_concise_grounded_and_cited() -> None:
    plan = TaskAnalyzer().analyze("what does bauer kompressoren do")

    def context(rank: int, evidence_id: str, filename: str, text: str):
        return SimpleNamespace(
            ranked=SimpleNamespace(rank=rank),
            citation=SimpleNamespace(
                citation_id=f"citation-{evidence_id}",
                original_filename=filename,
            ),
            unit=SimpleNamespace(
                evidence_id=evidence_id,
                search_text=text,
            ),
        )

    core = context(
        4,
        "core",
        "2025-06_Product_overview_EN_N37488_sc.pdf",
        (
            "BAUER KOMPRESSOREN is a global leader in the manufacture of "
            "medium and high pressure air and gas compression systems. "
            "BAUER develops systems for generating breathing air for divers "
            "and firefighters."
        ),
    )
    portfolio = context(
        6,
        "portfolio",
        "2026-04_Compressors_for_Industry_EN_N39771_sc.pdf",
        (
            "BAUER KOMPRESSOREN supplies an extensive range of accessories "
            "for its compressor systems, from air and gas purification to "
            "control, storage and gas measurement."
        ),
    )
    fuel_gas = context(
        14,
        "fuel-gas",
        "0153_fuel-gas-systems_9645f76747.html",
        (
            "Biogas and Fuel Gas Systems. Whether bio-CNG, biogas, hydrogen, "
            "or LNG, our compressor systems support vehicles, industrial "
            "plants and feed-in systems."
        ),
    )
    evidence = (core, portfolio, fuel_gas)
    coverage = tuple(
        CoverageEngine._company_overview_field(field, evidence)
        for field in plan.fields
    )
    assert all(item.state == "supported" for item in coverage)
    citations = {
        item.unit.evidence_id: item.citation for item in evidence
    }
    draft = GroundedAnswerBuilder().build(plan, coverage, citations)
    assert draft.status == "complete"
    assert "Supported result" not in draft.answer
    assert "Requested-topic evidence" not in draft.answer
    assert "supplier" not in draft.answer.casefold()
    assert "medium- and high-pressure air and gas compression systems" in (
        draft.answer
    )
    assert "air and gas purification" in draft.answer
    assert "divers and firefighters" in draft.answer
    assert "bio-CNG, biogas, hydrogen, and LNG" in draft.answer
    assert len(draft.citations) == 3


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


def test_bm_family_comparison_uses_complete_overview_ranges(
    answer_fixture: tuple,
) -> None:
    service, scope, _ = answer_fixture
    response = service.answer(
        request_id="request-bm-family-comparison",
        trace_id="trace-bm-family-comparison",
        question=(
            "Compare the Bauer BM series at 40 bar and 100 bar. For each "
            "family, report medium, maximum pressure, free-air-delivery "
            "range, and motor-power range. Explain which family is "
            "technically closer to a requirement for air at 90 bar and "
            "approximately 800 l/min. Cite both product sources."
        ),
        search_hint=None,
        locale="en",
        scope=scope,
    )
    assert response.status == "complete"
    assert response.validation["passed"] is True
    assert "660\u20137390 l/min" in response.answer
    assert "11\u2013110 kW" in response.answer
    assert "630\u20137300 l/min" in response.answer
    assert "15\u2013132 kW" in response.answer
    assert "BM series 100 bar is technically closer" in response.answer
    assert "does not select or approve a specific model" in response.answer
    assert {
        citation.original_filename for citation in response.citations
    } == {
        "0025_bm-series-100_7acdece303.html",
        "0027_bm-series-40_e0b4c6a9a3.html",
    }


def test_verticus_duplicate_rows_use_operating_pressure_not_shutdown(
    answer_fixture: tuple,
) -> None:
    service, scope, _ = answer_fixture
    response = service.answer(
        request_id="request-verticus-duplicate-rows",
        trace_id="trace-verticus-duplicate-rows",
        question=(
            "The model I 15.11-11-V appears in more than one VERTICUS "
            "pressure table. Return both rows, clearly separating the "
            "420 bar and 525 bar variants and preserving footnotes 1 and 2."
        ),
        search_hint=None,
        locale="en",
        scope=scope,
    )
    assert response.status == "complete"
    assert response.validation["passed"] is True
    assert "VERTICUS I 350 - 420 bar: 420 bar" in response.answer
    assert "VERTICUS I 420 - 525 bar: 525 bar" in response.answer
    assert "ISO 1217" in response.answer
    assert "shutdown pressure is lower" in response.answer


def test_bkool_iii_uses_complete_current_technical_row() -> None:
    plan = TaskAnalyzer().analyze(
        (
            "What are the documented maximum operating pressures and maximum "
            "flow rates for B-KOOL III? State the separate helium and argon "
            "flow range and cite the technical data."
        )
    )
    context = SimpleNamespace(
        ranked=SimpleNamespace(rank=1),
        citation=SimpleNamespace(
            original_filename="0021_b-kool_c5e62182b0.html"
        ),
        unit=SimpleNamespace(
            evidence_id="evidence-bkool-iii",
            search_text=(
                "Technical Data Model designation B-KOOL III "
                "Maximum operating pressure: 350 bar / 550 bar "
                "Maximum flow rate: 200–700 l/min for 10 l cylinder "
                "filling from 0–200 bar; 200–650 l/min according to "
                "ISO 1217 for air; 200–420 l/min for helium and argon."
            ),
        ),
    )
    coverage = [
        CoverageEngine._bkool_iii_field(field, (context,))
        for field in plan.fields
    ]
    assert all(item.state == "supported" for item in coverage)
    rendered = " ".join(
        value for item in coverage for value, _ in item.values
    )
    assert "350 bar / 550 bar" in rendered
    assert "200–700 l/min" in rendered
    assert "200–650 l/min" in rendered
    assert "200–420 l/min" in rendered
    assert "500 bar" not in rendered


def test_bsafe_nitrox_limit_is_not_silently_approved() -> None:
    analyzer = TaskAnalyzer()
    context = SimpleNamespace(
        ranked=SimpleNamespace(rank=1),
        citation=SimpleNamespace(
            original_filename="0030_b-safe_ae238602bf.html"
        ),
        unit=SimpleNamespace(
            evidence_id="evidence-bsafe",
            search_text=(
                "B-SAFE Safety filling cell for breathing air applications "
                "up to 300 bar and Nitrox applications up to 200 bar. "
                "B-SAFE 300 Technical data MediumAir, Nitrox Maximum "
                "operating pressure410 bar Filling pressures 225/330 bar "
                "Variable pressure increase20–50 bar/min."
            ),
        ),
    )
    decision = analyzer.analyze(
        (
            "Can B-SAFE be used to fill Nitrox cylinders at 300 bar? "
            "Answer yes or no based only on the uploaded Bauer "
            "documentation."
        )
    )
    decision_coverage = CoverageEngine._bsafe_field(
        decision.fields[0],
        (context,),
    )
    assert decision_coverage.state == "supported"
    assert decision_coverage.values[0][0].startswith("No.")
    assert "Nitrox applications up to 200 bar" in (
        decision_coverage.values[0][0]
    )
    reconciliation = analyzer.analyze(
        (
            "Reconcile the B-SAFE page headline that states breathing-air "
            "use up to 300 bar and Nitrox use up to 200 bar with the later "
            "B-SAFE 300 technical-data values."
        )
    )
    reconciliation_coverage = CoverageEngine._bsafe_field(
        reconciliation.fields[0],
        (context,),
    )
    assert reconciliation_coverage.state == "supported"
    assert "maximum operating pressure 410 bar" in (
        reconciliation_coverage.values[0][0]
    )
    assert "not a compatibility approval" in (
        reconciliation_coverage.values[0][0]
    )


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


def test_search_hint_cannot_displace_b06_requirements(
    answer_fixture: tuple,
) -> None:
    service, scope, _ = answer_fixture
    response = service.answer(
        request_id="semantic-B06-bad-hint",
        trace_id="semantic-trace-B06-bad-hint",
        question=(
            "What is the highest documented maximum operating pressure "
            "for a Bauer compressor? Distinguish compressors from boosters "
            "and explain shutdown pressure."
        ),
        search_hint=(
            "helium recovery MINI-VERTICUS 90-350 bar brochure "
            "GIB 420 bar"
        ),
        locale="en",
        scope=scope,
    )
    assert response.status == "partial"
    assert response.validation["passed"] is True
    assert "525 bar" in response.answer
    assert "350 bar" not in response.answer
    assert set(response.not_found) == {
        "booster_pressure_evidence",
        "pressure_definition_evidence",
    }


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
