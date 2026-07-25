from __future__ import annotations

import sys
import unittest
from pathlib import Path
from unittest.mock import patch


SERVICE_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SERVICE_ROOT))

from bauer_evidence_v3.ingest.parsers.base import ParseContext  # noqa: E402
from bauer_evidence_v3.ingest.parsers.pdfplumber import PdfPlumberParser  # noqa: E402
from bauer_evidence_v3.ingest.probe import probe_source  # noqa: E402


class FakeTable:
    bbox = (0, 10, 100, 80)

    def extract(self):
        return [
            ["Model", "Pressure"],
            ["B-SAFE 300", None],
            ["M10", "300 bar"],
        ]


class FakeDegenerateTable:
    bbox = (0, 0, 100, 8)

    def extract(self):
        return [["Decorative title box"]]


class FakePage:
    width = 100
    height = 100
    rotation = 0
    images = []

    def extract_words(self, **_kwargs):
        return [
            {"text": "Technical", "x0": 0, "top": 0, "x1": 30, "bottom": 5},
            {"text": "data", "x0": 31, "top": 0, "x1": 45, "bottom": 5},
        ]

    def find_tables(self):
        return [FakeDegenerateTable(), FakeTable()]


class FakePdf:
    metadata = {"Title": "Manual"}
    pages = [FakePage()]

    def close(self):
        self.closed = True


class FakePdfPlumber:
    def open(self, *_args, **_kwargs):
        return FakePdf()


class PdfPlumberTests(unittest.TestCase):
    def test_source_native_table_groups_and_coordinates_are_preserved(self) -> None:
        payload = b"%PDF-1.7 fake"
        probe = probe_source(payload, source_name="manual.pdf")
        with patch(
            "bauer_evidence_v3.ingest.parsers.pdfplumber._load_pdfplumber",
            return_value=FakePdfPlumber(),
        ):
            document = PdfPlumberParser().parse(payload, ParseContext(probe))
        self.assertEqual(document.title, "Manual")
        self.assertEqual(document.pages[0].blocks[0].text, "Technical data")
        self.assertIsNone(document.pages[0].printed_label)
        self.assertEqual(
            dict(document.pages[0].signals)["printed_label_source"],
            "unavailable",
        )
        self.assertEqual(
            dict(document.pages[0].signals)[
                "skipped_degenerate_table_candidate_count"
            ],
            "1",
        )
        self.assertEqual(len(document.pages[0].tables), 1)
        table = document.pages[0].tables[0]
        group = next(cell for cell in table.cells if cell.role == "group_header")
        value = next(cell for cell in table.cells if cell.text == "300 bar")
        self.assertEqual(group.column_span, 2)
        self.assertEqual(value.group, "B-SAFE 300")
        self.assertEqual(table.bbox, (0.0, 0.1, 1.0, 0.8))
        self.assertEqual(table.source_locator, "page:0/table:1")


if __name__ == "__main__":
    unittest.main()
