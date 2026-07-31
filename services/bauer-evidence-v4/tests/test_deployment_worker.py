from __future__ import annotations

from types import SimpleNamespace

from bauer_evidence_v3.ingest.canonical import (
    Block,
    Cell,
    Document,
    Page,
    Table,
)

from bauer_evidence_v4.deployment.ocr_fallback import compile_ocr_fallback
from bauer_evidence_v4.deployment.worker import (
    _failure_code,
    _release_scoped_id,
)


def test_failure_code_preserves_only_safe_constraint_identity() -> None:
    error = RuntimeError("sensitive database detail")
    error.diag = SimpleNamespace(  # type: ignore[attr-defined]
        constraint_name="canonical_cells_table_id_row_index_column_index_key"
    )
    assert _failure_code(error) == (
        "RuntimeError."
        "canonical_cells_table_id_row_index_column_index_key"
    )


def test_failure_code_sanitizes_and_bounds_unknown_exception_types() -> None:
    error_type = type("Unsafe Name!" * 20, (RuntimeError,), {})
    value = _failure_code(error_type())
    assert len(value) <= 128
    assert value
    assert all(character.isalnum() or character in "_.:-" for character in value)


def test_database_canonical_ids_are_deterministically_release_scoped() -> None:
    for kind in (
        "document",
        "block",
        "table",
        "cell",
        "fact",
        "record",
        "projection",
    ):
        canonical = f"{kind}_" + "a" * 32
        first = _release_scoped_id("release-one", canonical)
        assert first == _release_scoped_id("release-one", canonical)
        assert first.startswith(f"{kind}_")
        assert len(first) == len(canonical)
        assert first != canonical
        assert first != _release_scoped_id("release-two", canonical)


def test_record_evidence_ids_remain_mappable_for_html_metadata() -> None:
    evidence_ids = (
        "record_" + "1" * 32,
        "block_" + "2" * 32,
    )
    mapped = tuple(
        _release_scoped_id("release-html", evidence_id)
        for evidence_id in evidence_ids
    )
    assert mapped[0].startswith("record_")
    assert mapped[1].startswith("block_")
    assert len(set(mapped)) == len(evidence_ids)


def test_ocr_fallback_translates_page_table_and_typed_fact_provenance() -> None:
    source_sha = "ab" * 32
    table = Table(
        table_id="table_v3",
        page_index=0,
        order=1,
        row_count=2,
        column_count=2,
        cells=(
            Cell(
                cell_id="cell_header_model",
                row=0,
                column=0,
                text="Model",
                source_locator="page:0/table:0/cell:0:0",
                parser_id="pdfplumber_native",
                role="header",
            ),
            Cell(
                cell_id="cell_header_pressure",
                row=0,
                column=1,
                text="Pressure bar",
                source_locator="page:0/table:0/cell:0:1",
                parser_id="pdfplumber_native",
                role="header",
            ),
            Cell(
                cell_id="cell_model",
                row=1,
                column=0,
                text="BM 40",
                source_locator="page:0/table:0/cell:1:0",
                parser_id="pdfplumber_native",
                role="row_header",
            ),
            Cell(
                cell_id="cell_pressure",
                row=1,
                column=1,
                text="350",
                source_locator="page:0/table:0/cell:1:1",
                parser_id="pdfplumber_native",
                role="body",
            ),
        ),
        source_locator="page:0/table:0",
        parser_id="pdfplumber_native",
        caption="Technical data",
    )
    source = Document(
        document_id="document_v3",
        source_sha256=source_sha,
        source_name="scan.pdf",
        media_type="application/pdf",
        parser_id="pdfplumber_native",
        parser_version="1",
        pages=(
            Page(
                page_id="page_v3",
                index=0,
                blocks=(
                    Block(
                        block_id="block_v3",
                        page_index=0,
                        order=0,
                        kind="ocr_word",
                        text="BM 40 technical data",
                        source_locator="page:0/ocr:word:0",
                        parser_id="rapidocr",
                    ),
                ),
                tables=(table,),
                source_locator="page:0",
                parser_id="pdfplumber_native",
                printed_label="1",
                signals=(("ocr_accepted", "true"),),
            ),
        ),
        title="BM 40",
        language="en",
    )

    class FakeCompiler:
        ocr_engine = SimpleNamespace(engine_version="test-ocr")

        @staticmethod
        def compile(*_args, **_kwargs):
            return SimpleNamespace(
                document=source,
                quality=SimpleNamespace(status="warning"),
            )

    document = compile_ocr_fallback(
        FakeCompiler(),
        b"%PDF-test",
        source_path="scan.pdf",
        declared_media_type="application/pdf",
    )
    assert document.parser_id == "v3_ocr_adapter"
    assert document.page_count == 1
    assert document.blocks[0].kind == "paragraph"
    assert document.tables[0].cells[-1].unit_ucum == "bar"
    assert document.facts[0].subject == "BM 40"
    assert document.facts[0].numeric_value is not None
    assert document.facts[0].provenance_ids == (
        document.tables[0].cells[-1].cell_id,
    )
