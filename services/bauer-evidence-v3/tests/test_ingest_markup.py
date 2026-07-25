from __future__ import annotations

import sys
import unittest
from pathlib import Path


SERVICE_ROOT = Path(__file__).resolve().parents[1]
if str(SERVICE_ROOT) not in sys.path:
    sys.path.insert(0, str(SERVICE_ROOT))

from bauer_evidence_v3.ingest import compile_source


class MarkupParserTests(unittest.TestCase):
    def test_markdown_preserves_sections_paragraphs_and_pipe_tables(self) -> None:
        payload = (
            "# Product\n"
            "Line one\n"
            "line two\n\n"
            "| Size | Note |\n"
            "| --- | --- |\n"
            "| M10 | left \\| right |\n"
        ).encode()

        result = compile_source(payload, source_name="product.md")
        page = result.document.pages[0]

        self.assertEqual(result.document.title, "Product")
        self.assertEqual(
            [block.kind for block in page.blocks],
            ["heading", "paragraph"],
        )
        self.assertEqual(page.blocks[1].text, "Line one\nline two")
        self.assertEqual(page.blocks[1].section_path, ("Product",))
        self.assertEqual(page.tables[0].row_count, 2)
        self.assertEqual(page.tables[0].column_count, 2)
        self.assertEqual(page.tables[0].cells[-1].text, "left | right")
        self.assertEqual(result.quality.status, "pass")

    def test_html_uses_main_content_and_preserves_spans_and_groups(self) -> None:
        payload = b"""<!doctype html>
<html lang="en"><head><title>Manual</title></head><body>
<nav><p>Do not index navigation</p></nav>
<main>
  <h1 id="tools">Torque tools</h1>
  <p>Use the specified torque.</p>
  <ul><li>Clean threads</li></ul>
  <table>
    <tr><th colspan="3">B-SAFE 300</th></tr>
    <tr><th>Size</th><th rowspan="2">Torque</th><th>Unit</th></tr>
    <tr><td>M10</td><td>Nm</td></tr>
  </table>
</main>
<footer><p>Do not index footer</p></footer>
</body></html>"""

        result = compile_source(payload, source_name="manual.html")
        page = result.document.pages[0]
        table = page.tables[0]
        texts = [block.text for block in page.blocks]

        self.assertEqual(result.document.title, "Manual")
        self.assertEqual(result.document.language, "en")
        self.assertNotIn("Do not index navigation", texts)
        self.assertNotIn("Do not index footer", texts)
        self.assertEqual(
            [block.kind for block in page.blocks],
            ["heading", "paragraph", "list_item"],
        )
        self.assertEqual((table.row_count, table.column_count), (3, 3))
        group_header = table.cells[0]
        self.assertEqual(group_header.role, "group_header")
        self.assertEqual(group_header.column_span, 3)
        inherited = [
            cell.group
            for cell in table.cells
            if cell.role != "group_header"
        ]
        self.assertEqual(set(inherited), {"B-SAFE 300"})
        torque = next(cell for cell in table.cells if cell.text == "Torque")
        self.assertEqual(torque.row_span, 2)
        self.assertEqual(result.quality.status, "pass")


if __name__ == "__main__":
    unittest.main()
