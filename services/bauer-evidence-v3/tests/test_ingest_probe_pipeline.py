from __future__ import annotations

import sys
import unittest
from pathlib import Path
from unittest.mock import patch


SERVICE_ROOT = Path(__file__).resolve().parents[1]
if str(SERVICE_ROOT) not in sys.path:
    sys.path.insert(0, str(SERVICE_ROOT))

from bauer_evidence_v3.ingest import (
    Compiler,
    QualityGateError,
    QualityPolicy,
    compile_source,
    probe_source,
)
from bauer_evidence_v3.ingest.parsers import (
    ParseError,
    ParserUnavailable,
    UnsupportedFormat,
)


class ProbeAndPipelineTests(unittest.TestCase):
    def test_signature_wins_over_declared_type_and_extension(self) -> None:
        probe = probe_source(
            b"%PDF-1.7\nbody",
            source_name="wrong.txt",
            declared_media_type="text/plain",
        )

        self.assertEqual(probe.format, "pdf")
        self.assertEqual(probe.media_type, "application/pdf")
        self.assertEqual(probe.signature, "pdf_magic")
        self.assertIn(("declared_type_mismatch", "true"), probe.attributes)

    def test_probe_routes_html_markdown_text_and_unknown_binary(self) -> None:
        cases = (
            (b"<html><body>x</body></html>", "x.bin", "html"),
            (b"# Heading\n", "x.md", "markdown"),
            (b"plain evidence", "x.txt", "text"),
        )
        for payload, source_name, expected in cases:
            with self.subTest(source_name=source_name):
                self.assertEqual(
                    probe_source(payload, source_name=source_name).format,
                    expected,
                )

        with self.assertRaises(UnsupportedFormat):
            compile_source(b"\x00\x01\x02", source_name="opaque.bin")

    def test_text_parser_preserves_form_feed_pages_and_strict_utf8(self) -> None:
        result = compile_source(
            b"first line\nsame paragraph\fsecond page",
            source_name="pages.txt",
        )

        self.assertEqual(len(result.document.pages), 2)
        self.assertEqual(
            result.document.pages[0].blocks[0].text,
            "first line\nsame paragraph",
        )
        self.assertEqual(result.quality.status, "pass")

        with self.assertRaises(ParseError):
            compile_source(
                b"\xff",
                source_name="invalid.txt",
                declared_media_type="text/plain",
            )

    def test_pdf_dependency_is_lazy_and_failure_is_explicit(self) -> None:
        with patch(
            "bauer_evidence_v3.ingest.parsers.pdf.importlib.import_module",
            side_effect=ImportError("not installed"),
        ):
            with self.assertRaises(ParserUnavailable):
                compile_source(b"%PDF-1.7\n", source_name="manual.pdf")

    def test_size_limit_fails_before_parsing(self) -> None:
        compiler = Compiler(
            quality_policy=QualityPolicy(max_source_bytes=3)
        )

        with self.assertRaises(QualityGateError) as caught:
            compiler.compile(b"four", source_name="large.txt")
        self.assertEqual(caught.exception.report.status, "fatal")
        self.assertEqual(
            caught.exception.report.issues[0].code,
            "source_size_limit",
        )


if __name__ == "__main__":
    unittest.main()
