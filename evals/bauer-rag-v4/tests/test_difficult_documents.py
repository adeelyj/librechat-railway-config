from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path

import pytest


FIXTURE = (
    Path(__file__).resolve().parents[1]
    / "fixtures"
    / "difficult-documents.json"
)
DEFAULT_SOURCE_ROOT = Path(r"D:\02_Code\Bauer Kompressoren Demo")
REQUIRED_CASES = {"B07", "B13", "B16", "B17", "B23", "B29", "B30"}


def _load() -> dict:
    return json.loads(FIXTURE.read_text(encoding="utf-8"))


def _all_sources(data: dict) -> list[dict]:
    sources: list[dict] = []
    for document in data["documents"]:
        sources.append(document["source"])
        sources.extend(document.get("linked_sources", []))
    return sources


def test_fixture_is_public_development_only_and_complete() -> None:
    data = _load()
    assert data["split"] == "development"
    assert data["locked_holdout_opened"] is False
    assert data["review"]["engineering_review"] == "complete"
    assert data["review"]["bauer_owner_review"] == "required"
    assert REQUIRED_CASES == {
        case_id
        for document in data["documents"]
        for case_id in document["case_ids"]
    }
    serialized = FIXTURE.read_text(encoding="utf-8").casefold()
    assert "cases/holdout" not in serialized
    assert "\\holdout" not in serialized


def test_fixture_expresses_required_relationships() -> None:
    data = _load()
    by_case = {
        case_id: document
        for document in data["documents"]
        for case_id in document["case_ids"]
    }

    b07_rows = by_case["B07"]["expected_tables"][0]["rows"]
    assert {row["values"]["maximum_operating_pressure"]["bar"] for row in b07_rows} == {
        420,
        525,
    }
    assert all("footnotes" in table for table in by_case["B07"]["expected_tables"])

    b13 = by_case["B13"]
    record = b13["expected_records"][0]
    assert record["file_number"] == "216"
    assert record["language"] == "en"
    assert b13["linked_sources"][0]["expected_facts"]["standard"] == "EN ISO 3834-2"
    assert len(b13["negative_constraints"]) == 3

    b16_row = by_case["B16"]["expected_tables"][0]["rows"][0]
    assert b16_row["values"]["effective_free_air_delivery"] == {
        "l/min": 660,
        "m³/h": 39.6,
        "cfm": 23.3,
    }
    assert by_case["B30"]["language_expectations"]["de_query_must_match_en_evidence"]

    b17 = by_case["B17"]
    assert b17["expected_tables"][0]["group"] == "BM series 100 bar – 50 Hz"
    assert b17["adjudication"]["status"] == "resolved_for_fixture"

    b23 = by_case["B23"]["expected_metadata"]
    assert b23["document_number"] == "N47183"
    assert b23["source_filename"].endswith(".pdf")

    b29 = by_case["B29"]["expected_regions"][0]
    assert b29["physical_page"] == 31
    assert {fact["predicate"] for fact in b29["family_level_facts"]} == {
        "pressure_range",
        "charging_rate",
        "motor_power",
    }
    assert by_case["B29"]["negative_constraints"]


def test_selected_original_hashes_and_sizes_match_when_corpus_is_available() -> None:
    data = _load()
    source_root = Path(os.environ.get("BAUER_SOURCE_ROOT", DEFAULT_SOURCE_ROOT))
    if not source_root.is_dir():
        pytest.skip("authoritative original corpus is not mounted")

    for source in _all_sources(data):
        path = source_root.joinpath(*source["logical_path"].split("/"))
        assert path.is_file(), source["logical_path"]
        payload = path.read_bytes()
        assert len(payload) == source["byte_size"], source["logical_path"]
        assert hashlib.sha256(payload).hexdigest() == source["sha256"]
