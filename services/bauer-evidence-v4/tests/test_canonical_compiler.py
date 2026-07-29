from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from bauer_evidence_v4.compilation import (
    CanonicalCompiler,
    CompilationQuarantined,
)


REPOSITORY_ROOT = Path(__file__).resolve().parents[3]
FIXTURE_PATH = (
    REPOSITORY_ROOT
    / "evals"
    / "bauer-rag-v4"
    / "fixtures"
    / "difficult-documents.json"
)
SOURCE_ROOT = Path(r"D:\02_Code\Bauer Kompressoren Demo")


@pytest.fixture(scope="module")
def fixture_bundle() -> dict:
    return json.loads(FIXTURE_PATH.read_text(encoding="utf-8"))


@pytest.fixture(scope="module")
def compiled(
    fixture_bundle: dict,
) -> dict[str, tuple[dict, object]]:
    if not SOURCE_ROOT.is_dir():
        pytest.skip("authoritative original corpus is not mounted")
    compiler = CanonicalCompiler()
    results: dict[str, tuple[dict, object]] = {}
    for fixture in fixture_bundle["documents"]:
        source = fixture["source"]
        path = SOURCE_ROOT.joinpath(*source["logical_path"].split("/"))
        results[fixture["fixture_id"]] = (
            fixture,
            compiler.compile(
                path.read_bytes(),
                source_path=source["logical_path"],
                declared_media_type=source["media_type"],
                fixture=fixture,
            ),
        )
    return results


def test_all_difficult_documents_pass_semantic_compiler_gates(
    compiled: dict[str, tuple[dict, object]],
) -> None:
    for fixture, result in compiled.values():
        assert result.status == "published", fixture["fixture_id"]
        assert result.document is not None
        assert result.selected_parser_id in {
            "html_dom_grid",
            "pdfplumber_layout",
        }
        assert len(result.candidates) == 2
        selected = next(
            item
            for item in result.candidates
            if item.parser_id == result.selected_parser_id
        )
        assert selected.quality.status in {"pass", "warning"}
        assert not selected.quality.issues


def test_table_grids_have_inherited_headers_units_footnotes_and_typed_facts(
    compiled: dict[str, tuple[dict, object]],
) -> None:
    for fixture_id in (
        "verticus-i-technical-data",
        "bm-40-technical-data",
        "bm-100-technical-data",
    ):
        document = compiled[fixture_id][1].document
        assert document is not None
        requested_models = {
            row["model"]
            for table in compiled[fixture_id][0]["expected_tables"]
            for row in table["rows"]
        }
        assert all(
            any(
                cell.role == "row_header" and cell.text == model
                for table in document.tables
                for cell in table.cells
            )
            for model in requested_models
        )
        typed_cells = [
            cell
            for table in document.tables
            for cell in table.cells
            if cell.numeric_value is not None
        ]
        assert typed_cells
        assert any(cell.unit_raw == "m³/h" for cell in typed_cells)
        assert any(cell.unit_ucum == "m3/h" for cell in typed_cells)
        assert any(cell.header_path for cell in typed_cells)
        assert any(table.footnotes for table in document.tables)
        assert document.facts


def test_every_fact_is_traceable_to_canonical_evidence(
    compiled: dict[str, tuple[dict, object]],
) -> None:
    for _, result in compiled.values():
        document = result.document
        assert document is not None
        evidence_ids = {
            block.block_id for block in document.blocks
        } | {
            cell.cell_id
            for table in document.tables
            for cell in table.cells
        }
        for fact in document.facts:
            assert fact.provenance_ids
            assert set(fact.provenance_ids) <= evidence_ids
            assert 0 <= fact.confidence <= 1


def test_exact_certificate_record_is_not_substituted(
    compiled: dict[str, tuple[dict, object]],
) -> None:
    document = compiled["english-en-iso-3834-2-certificate"][1].document
    assert document is not None
    record = next(
        item for item in document.records if item.field("file_number") == "216"
    )
    assert record.field("language") == "en"
    assert record.field("organization") == "BAUER KOMPRESSOREN GmbH"
    assert record.field("address") == "Stäblistr. 8, 81477 Munich"
    assert "EN ISO 3834-2" in (record.field("title") or "")
    assert "ISO 9001" not in (record.field("title") or "")
    assert "ISO 14001" not in (record.field("title") or "")


def test_pdf_metadata_and_family_scope_are_preserved(
    compiled: dict[str, tuple[dict, object]],
) -> None:
    b23 = compiled["b-detection-sensor-calibration-n47183"][1].document
    assert b23 is not None
    assert b23.document_number == "N47183"
    assert b23.language == "en"
    assert b23.title == (
        "BAUER B-DETECTION - THE NEXT GENERATION - SENSOR CALIBRATION"
    )
    assert b23.source_filename.endswith(".pdf")

    b29 = compiled["product-overview-k22-k28-range"][1].document
    assert b29 is not None
    k_facts = [
        fact for fact in b29.facts if fact.subject == "K 22 – K 28 SERIES"
    ]
    assert {
        (
            fact.predicate,
            fact.minimum_value,
            fact.maximum_value,
            fact.unit_raw,
        )
        for fact in k_facts
    } >= {
        ("pressure_range", 30, 525, "bar"),
        ("charging_rate", 600, 6800, "l/min"),
        ("motor_power", 22, 110, "kW"),
    }
    assert all(fact.subject != "PE-VE INDUSTRY" for fact in k_facts)


def test_regeneration_is_byte_deterministic(
    fixture_bundle: dict,
) -> None:
    if not SOURCE_ROOT.is_dir():
        pytest.skip("authoritative original corpus is not mounted")
    fixture = next(
        item
        for item in fixture_bundle["documents"]
        if item["fixture_id"] == "bm-40-technical-data"
    )
    source = fixture["source"]
    path = SOURCE_ROOT.joinpath(*source["logical_path"].split("/"))
    payload = path.read_bytes()
    compiler = CanonicalCompiler()
    first = compiler.compile(
        payload,
        source_path=source["logical_path"],
        declared_media_type=source["media_type"],
        fixture=fixture,
    )
    second = compiler.compile(
        payload,
        source_path=source["logical_path"],
        declared_media_type=source["media_type"],
        fixture=fixture,
    )
    assert first.document is not None
    assert second.document is not None
    assert first.document.to_json() == second.document.to_json()
    assert hashlib.sha256(first.document.to_json().encode()).hexdigest() == (
        hashlib.sha256(second.document.to_json().encode()).hexdigest()
    )


def test_unresolved_source_is_quarantined() -> None:
    compiler = CanonicalCompiler()
    with pytest.raises(ValueError, match="unsupported source media type"):
        compiler.compile(
            b"\x00\x01\x02\x03",
            source_path="unknown.bin",
        )

    oversized = CanonicalCompiler(max_source_bytes=2)
    with pytest.raises(CompilationQuarantined) as error:
        oversized.compile(
            b"<html><body>valid but too large</body></html>",
            source_path="too-large.html",
            declared_media_type="text/html",
        )
    assert error.value.result.status == "quarantined"
