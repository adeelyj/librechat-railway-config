from __future__ import annotations

import sys
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch


SERVICE_ROOT = Path(__file__).resolve().parents[1]
if str(SERVICE_ROOT) not in sys.path:
    sys.path.insert(0, str(SERVICE_ROOT))

from bauer_evidence_v3.ingest import (
    QualityGateError,
    compile_source,
    evaluate_document,
    stable_id,
)


class _Rect(tuple):
    def __new__(cls, values):
        return super().__new__(cls, values)

    @property
    def width(self):
        return self[2] - self[0]

    @property
    def height(self):
        return self[3] - self[1]


class _NativeTable:
    bbox = (5, 20, 95, 80)

    def extract(self):
        return [
            ["Size", "Torque"],
            ["B-SAFE 300", None],
            ["M10", "30 Nm"],
        ]


class _TableResult:
    def __init__(self, tables):
        self.tables = tables


class _FakePage:
    rect = _Rect((0, 0, 100, 100))
    rotation = 0

    def __init__(self, blocks, tables=()):
        self._blocks = blocks
        self._tables = tables

    def get_text(self, kind, sort=True):
        if kind != "dict":
            raise AssertionError(kind)
        return {"blocks": self._blocks}

    def find_tables(self):
        return _TableResult(self._tables)


class _FakeDocument:
    needs_pass = False
    metadata = {"title": "Native manual", "language": "en"}

    def __init__(self):
        self.pages = [
            _FakePage(
                [
                    {
                        "type": 0,
                        "bbox": (10, 10, 90, 18),
                        "lines": [
                            {"spans": [{"text": "Native evidence"}]}
                        ],
                    }
                ],
                [_NativeTable()],
            ),
            _FakePage(
                [{"type": 1, "bbox": (0, 0, 100, 100)}],
            ),
        ]
        self.closed = False

    def __len__(self):
        return len(self.pages)

    def load_page(self, index):
        return self.pages[index]

    def close(self):
        self.closed = True


class _FakePyMuPdf:
    def __init__(self):
        self.document = _FakeDocument()

    def open(self, *, stream, filetype):
        if filetype != "pdf" or not stream.startswith(b"%PDF-"):
            raise AssertionError("unexpected open arguments")
        return self.document


class PdfAndQualityTests(unittest.TestCase):
    def test_pdf_native_adapter_emits_tables_and_ocr_signal(self) -> None:
        adapter = _FakePyMuPdf()
        with patch(
            "bauer_evidence_v3.ingest.parsers.pdf._load_pymupdf",
            return_value=adapter,
        ):
            result = compile_source(
                b"%PDF-1.7\nfake",
                source_name="manual.pdf",
                enforce_gate=False,
            )

        self.assertTrue(adapter.document.closed)
        self.assertEqual(result.document.title, "Native manual")
        self.assertEqual(len(result.document.pages), 2)
        self.assertEqual(
            result.document.pages[0].blocks[0].bbox,
            (0.1, 0.1, 0.9, 0.18),
        )
        table = result.document.pages[0].tables[0]
        group = next(
            cell for cell in table.cells if cell.role == "group_header"
        )
        self.assertEqual(group.column_span, 2)
        self.assertTrue(result.document.pages[1].ocr_needed)
        self.assertEqual(result.quality.status, "quarantine")
        self.assertIn(
            "ocr_needed",
            {issue.code for issue in result.quality.issues},
        )

    def test_gate_rejects_quarantine_and_character_damage(self) -> None:
        good = compile_source(b"Good evidence", source_name="good.txt")
        block = good.document.pages[0].blocks[0]
        damaged_text = f"{block.text}\ufffd"
        damaged = replace(
            block,
            text=damaged_text,
            block_id=stable_id(
                "block",
                good.document.source_sha256,
                block.source_locator,
                damaged_text,
            ),
        )
        page = replace(good.document.pages[0], blocks=(damaged,))
        document = replace(good.document, pages=(page,))
        report = evaluate_document(document, source_size=13)

        self.assertEqual(report.status, "quarantine")
        self.assertIn(
            "replacement_characters",
            {issue.code for issue in report.issues},
        )

    def test_quality_detects_overlapping_table_spans(self) -> None:
        result = compile_source(
            b"""<table>
<tr><th>A</th><th>B</th></tr>
<tr><td>1</td><td>2</td></tr>
</table>""",
            source_name="table.html",
        )
        page = result.document.pages[0]
        table = page.tables[0]
        original = table.cells[-1]
        duplicate_locator = f"{original.source_locator}/duplicate"
        duplicate = replace(
            original,
            cell_id=stable_id(
                "cell",
                result.document.source_sha256,
                duplicate_locator,
                original.text,
            ),
            source_locator=duplicate_locator,
        )
        damaged_table = replace(table, cells=table.cells + (duplicate,))
        damaged_page = replace(page, tables=(damaged_table,))
        damaged_document = replace(result.document, pages=(damaged_page,))

        report = evaluate_document(
            damaged_document,
            source_size=len(b"table"),
        )
        self.assertEqual(report.status, "fatal")
        self.assertIn(
            "overlapping_cell_spans",
            {issue.code for issue in report.issues},
        )

        with self.assertRaises(QualityGateError):
            from bauer_evidence_v3.ingest.quality import require_publishable

            require_publishable(report)


if __name__ == "__main__":
    unittest.main()
