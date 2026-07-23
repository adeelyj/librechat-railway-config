import sys
import unittest
from pathlib import Path


SERVICE_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SERVICE_ROOT))

from bauer_rag_v2.extraction import (  # noqa: E402
    detect_language,
    normalize_for_search,
    parse_markdown_document,
)


FLAT_TABLE = """# BM series (40 bar)

- Original source: `bauer_index/raw_html/bm-40.html`
- Original SHA-256: `aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa`

## Page 3

## Technical Data

Medium-pressure compressor BM series
1 Volume flow rate according to ISO 1217.
2 Maximum allowable working pressure; shutdown pressure is lower.
Model designation
Effective free air delivery
Shutdown pressure
Number of stages
l/min
bar
BM 6.1/40-11
660
40
3
1470
11
BM 10.1/40-15
1080
40
3
1480
15

The values marked 1 are effective free air delivery.
"""


class V2ExtractionTests(unittest.TestCase):
    def parse(self, text=FLAT_TABLE):
        return parse_markdown_document(
            text=text,
            file_id="file-bm-40",
            checksum="b" * 64,
            filename="0193-bm-series-40_EN.md",
            namespace="agent-bauer-v2",
        )

    def test_normalization_preserves_identifier_boundaries(self):
        self.assertEqual(normalize_for_search("B-DETECTION_PLUS / K 28"), "b detection plus k 28")
        self.assertNotEqual(normalize_for_search("K 25"), normalize_for_search("K 28"))

    def test_language_uses_filename_marker_before_heuristics(self):
        self.assertEqual(detect_language("certificate_EN_N123.md", "der die und"), "en")
        self.assertEqual(detect_language("manual_DE.md", "the and for"), "de")

    def test_flattened_technical_rows_keep_headers_units_values_and_location(self):
        parsed = self.parse()
        rows = [chunk for chunk in parsed.chunks if chunk.chunk_kind == "table_row"]
        self.assertEqual([row.row_label for row in rows], ["BM 6.1/40-11", "BM 10.1/40-15"])
        self.assertIn("Effective free air delivery", rows[0].headers)
        self.assertIn("l/min", rows[0].units)
        self.assertIn("40", rows[0].row_values)
        self.assertIn("ISO 1217", rows[0].footnotes)
        self.assertIn("shutdown pressure is lower", rows[0].content)
        self.assertEqual(rows[0].page, 3)
        self.assertIn("Technical Data", rows[0].section_path)
        self.assertIn("Headers and units:", rows[0].content)
        self.assertIn("l/min", rows[1].units)
        self.assertIn("bar", rows[1].units)

    def test_prose_overlap_never_crosses_page_boundary(self):
        parsed = self.parse(
            """# Product overview
## Page 30
Previous page material with a 950 l/min range.
## Page 31
Pressure range: 30 - 525 bar
Charging rate: 600 - 6,800 l/min
K 22 - K 28 SERIES
"""
        )
        page_31 = next(
            chunk for chunk in parsed.chunks if "600 - 6,800 l/min" in chunk.content
        )
        self.assertEqual(page_31.page, 31)
        self.assertIn("Page 31", page_31.section_path)
        self.assertNotIn("Previous page material", page_31.content)

    def test_markdown_pipe_table_becomes_one_independent_chunk_per_row(self):
        parsed = self.parse(
            """# K range
## Page 7
## Technical data
| Model | Medium | Maximum pressure |
|---|---|---:|
| K 25 | Air | 420 bar |
| K 28 | Air | 525 bar |
1 Maximum allowable working pressure; shutdown pressure is lower.
"""
        )
        rows = [chunk for chunk in parsed.chunks if chunk.chunk_kind == "table_row"]
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[1].row_label, "K 28")
        self.assertEqual(rows[1].row_values[-1], "525 bar")
        self.assertEqual(rows[1].page, 7)
        self.assertIn("shutdown pressure is lower", rows[1].footnotes)

    def test_key_value_web_table_preserves_model_fields_as_one_row(self):
        parsed = self.parse(
            """# B-KOOL
## Web page content
Technical Data
B-KOOL Refrigeration dryer
Model designation
B-KOOL III
Medium
Compressed Air, Nitrogen, Helium
Weight approx.
58 kg
Maximum operating pressure
350 bar / 550 bar
Maximum flow rate
200–700 l/min
Details & Features
"""
        )
        row = next(chunk for chunk in parsed.chunks if chunk.row_label == "B-KOOL III")
        self.assertEqual(row.metadata["parser"], "key_value_technical_table")
        self.assertIn("Maximum operating pressure", row.headers)
        self.assertIn("350 bar / 550 bar", row.row_values)
        self.assertIn("bar", row.units)
        self.assertIn("Maximum flow rate: 200–700 l/min", row.content)

    def test_order_number_table_preserves_identifier_and_adjacent_values(self):
        parsed = self.parse(
            """# Accessories
## Page 4
Use
Order number
Small systems
N4823
Large blocks/medium pressure
N7698
"""
        )
        row = next(chunk for chunk in parsed.chunks if chunk.row_label == "N7698")
        self.assertEqual(row.metadata["parser"], "identifier_table")
        self.assertEqual(row.page, 4)
        self.assertIn("Large blocks/medium pressure", row.row_values)
        self.assertIn("Order number", row.headers)

    def test_certificate_standard_address_and_document_number_are_extracted(self):
        parsed = self.parse(
            """# Download Certificates
EN ISO 3834-2 Certificate: BAUER KOMPRESSOREN GmbH, Stäblistr. 8, 81477 Munich EN
Document N47183 revision: 03
Pressure limit 420 bar and capacity 500 l/min.
"""
        )
        kinds = {}
        for entity in parsed.entities:
            kinds.setdefault(entity.kind, set()).add(entity.normalized_value)
        self.assertIn("en iso 3834 2", kinds["standard"])
        self.assertIn("n47183", kinds["document_number"])
        self.assertIn("03", kinds["revision"])
        self.assertIn("420 bar", kinds["pressure"])
        self.assertIn("500 l min", kinds["capacity"])
        self.assertIsNotNone(parsed.certificate)
        self.assertIn("EN ISO 3834-2", parsed.certificate)
        self.assertIn(parsed.certificate, parsed.certificates)
        self.assertIn("81477 Munich", parsed.address)

    def test_document_metadata_includes_date_medium_category_and_chunk_title(self):
        parsed = parse_markdown_document(
            text="# Nitrogen booster\nThe compressor handles nitrogen at 420 bar.",
            file_id="file-n2",
            checksum="c" * 64,
            filename="2026-04_Nitrogen_Booster_EN_N12345.md",
            namespace="agent-bauer-v2",
        )
        self.assertEqual(parsed.publication_date, "2026-04-01")
        self.assertIn("nitrogen", parsed.media)
        self.assertIn("booster", parsed.component_categories)
        self.assertIn("compressor", parsed.component_categories)
        self.assertTrue(
            all(chunk.content.startswith("Document: Nitrogen booster") for chunk in parsed.chunks)
        )

    def test_chunk_ids_and_counts_are_deterministic(self):
        first = self.parse()
        second = self.parse()
        self.assertEqual(
            [chunk.chunk_id for chunk in first.chunks],
            [chunk.chunk_id for chunk in second.chunks],
        )
        self.assertEqual(first.metadata["table_row_count"], 2)
        self.assertGreater(first.metadata["prose_chunk_count"], 0)


if __name__ == "__main__":
    unittest.main()
