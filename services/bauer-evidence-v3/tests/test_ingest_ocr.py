from __future__ import annotations

import sys
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch


SERVICE_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SERVICE_ROOT))

from bauer_evidence_v3.ingest import Compiler, compile_source  # noqa: E402
from bauer_evidence_v3.ids import sha256_bytes  # noqa: E402
from bauer_evidence_v3.ingest.canonical import (  # noqa: E402
    Document,
    Page,
    attribute_items,
    stable_id,
)
from bauer_evidence_v3.ingest.ocr import (  # noqa: E402
    OcrResult,
    OcrWord,
    RapidOcrEngine,
    apply_page_ocr,
)
from bauer_evidence_v3.ingest.render import PageRender  # noqa: E402


class FakeOcr:
    engine_id = "fake_ocr"
    engine_version = "1"

    def recognize(self, image, *, languages):
        self.image = image
        self.languages = languages
        return OcrResult(
            self.engine_id,
            self.engine_version,
            (
                OcrWord("BM 40", (0.1, 0.1, 0.3, 0.2), 0.95),
                OcrWord("350 bar", (0.4, 0.1, 0.7, 0.2), 0.90),
            ),
        )


class FakeScannedPdfParser:
    parser_id = "fake_scanned_pdf"
    parser_version = "1"

    def supports(self, probe):
        return probe.format == "pdf"

    def parse(self, payload, context):
        del payload
        locator = "pdf:page:1"
        page = Page(
            page_id=stable_id(
                "page",
                context.probe.source_sha256,
                locator,
                "",
            ),
            index=0,
            blocks=(),
            tables=(),
            source_locator=locator,
            parser_id=self.parser_id,
            printed_label="1",
            width=612,
            height=792,
            ocr_needed=True,
            signals=attribute_items({"native_character_count": 0}),
        )
        document_locator = f"source:{context.probe.source_name}"
        return Document(
            document_id=stable_id(
                "document",
                context.probe.source_sha256,
                document_locator,
                context.probe.media_type,
            ),
            source_sha256=context.probe.source_sha256,
            source_name=context.probe.source_name,
            media_type=context.probe.media_type,
            parser_id=self.parser_id,
            parser_version=self.parser_version,
            pages=(page,),
        )


class FakePageRenderer:
    renderer_id = "fake_renderer"
    renderer_version = "1"

    def render_pages(self, payload, *, page_indexes, dpi):
        self.calls = getattr(self, "calls", 0) + 1
        self.payload = payload
        self.page_indexes = page_indexes
        self.dpi = dpi
        return (
            PageRender(
                page_index=0,
                image_bytes=b"immutable-rendered-page",
                width_pixels=1275,
                height_pixels=1650,
                dpi=dpi,
            ),
        )


class OcrTests(unittest.TestCase):
    def test_rapidocr_models_are_checksum_verified_before_loading(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            paths = []
            hashes = []
            for name in ("detect.onnx", "recognize.onnx", "classify.onnx"):
                path = root / name
                content = f"pinned-{name}".encode()
                path.write_bytes(content)
                paths.append(path)
                hashes.append(sha256_bytes(content))
            runtime = SimpleNamespace(RapidOCR=lambda **kwargs: kwargs)
            with (
                patch(
                    "bauer_evidence_v3.ingest.ocr.importlib.metadata.version",
                    return_value="1.4.4",
                ),
                patch(
                    "bauer_evidence_v3.ingest.ocr.importlib.import_module",
                    return_value=runtime,
                ) as load_runtime,
            ):
                engine = RapidOcrEngine(
                    detection_model=paths[0],
                    detection_model_sha256=hashes[0],
                    recognition_model=paths[1],
                    recognition_model_sha256=hashes[1],
                    classification_model=paths[2],
                    classification_model_sha256=hashes[2],
                )

            load_runtime.assert_called_once_with("rapidocr_onnxruntime")
            self.assertTrue(engine.engine_version.startswith("1.4.4+models."))
            self.assertEqual(
                engine._engine["det_model_path"],
                str(paths[0].resolve()),
            )

            with (
                patch(
                    "bauer_evidence_v3.ingest.ocr.importlib.import_module"
                ) as load_runtime,
                self.assertRaisesRegex(ValueError, "checksum mismatch"),
            ):
                RapidOcrEngine(
                    detection_model=paths[0],
                    detection_model_sha256="0" * 64,
                    recognition_model=paths[1],
                    recognition_model_sha256=hashes[1],
                    classification_model=paths[2],
                    classification_model_sha256=hashes[2],
                )
            load_runtime.assert_not_called()

    def test_compiler_renders_and_enriches_only_deficient_pdf_pages(self) -> None:
        payload = b"%PDF-1.7\nscanned-test"
        renderer = FakePageRenderer()
        result = Compiler(
            parsers=(FakeScannedPdfParser(),),
            ocr_engine=FakeOcr(),
            page_renderer=renderer,
            ocr_render_dpi=150,
        ).compile(
            payload,
            source_name="scan.pdf",
            declared_media_type="application/pdf",
        )

        self.assertEqual(renderer.payload, payload)
        self.assertEqual(renderer.page_indexes, (0,))
        self.assertEqual(renderer.dpi, 150)
        self.assertEqual(len(result.page_renders), 1)
        self.assertFalse(result.document.pages[0].ocr_needed)
        self.assertEqual(result.quality.status, "pass")
        self.assertEqual(
            [block.text for block in result.document.pages[0].blocks],
            ["BM 40", "350 bar"],
        )
        self.assertAlmostEqual(
            float(
                dict(result.document.pages[0].signals)[
                    "ocr_average_confidence"
                ]
            ),
            0.925,
        )

    def test_parser_cascade_renders_selected_candidate_only_once(self) -> None:
        class SecondScannedPdfParser(FakeScannedPdfParser):
            parser_id = "second_fake_scanned_pdf"

        renderer = FakePageRenderer()
        result = Compiler(
            parsers=(
                FakeScannedPdfParser(),
                SecondScannedPdfParser(),
            ),
            ocr_engine=FakeOcr(),
            page_renderer=renderer,
        ).compile(
            b"%PDF-1.7\nscanned-test",
            source_name="scan.pdf",
        )

        self.assertEqual(renderer.calls, 1)
        self.assertEqual(len(result.page_renders), 1)

    def test_only_deficient_page_is_enriched_and_native_text_is_preserved(self) -> None:
        compiled = compile_source(
            b"Native first page\f",
            source_name="mixed.txt",
            enforce_gate=False,
        )
        first, second = compiled.document.pages
        deficient = replace(second, ocr_needed=True)
        document = replace(compiled.document, pages=(first, deficient))
        engine = FakeOcr()
        enriched = apply_page_ocr(
            document,
            page_images={1: b"rendered-page"},
            engine=engine,
        )
        self.assertEqual(enriched.pages[0], first)
        self.assertFalse(enriched.pages[1].ocr_needed)
        self.assertEqual(
            [block.text for block in enriched.pages[1].blocks],
            ["BM 40", "350 bar"],
        )
        self.assertEqual(engine.languages, ("de", "en"))

    def test_low_confidence_result_remains_quarantined(self) -> None:
        class LowConfidence(FakeOcr):
            def recognize(self, image, *, languages):
                return OcrResult(
                    self.engine_id,
                    self.engine_version,
                    (OcrWord("uncertain", (0, 0, 1, 1), 0.2),),
                )

        compiled = compile_source(b"\f", source_name="scan.txt", enforce_gate=False)
        page = replace(compiled.document.pages[0], ocr_needed=True)
        document = replace(compiled.document, pages=(page,))
        enriched = apply_page_ocr(
            document,
            page_images={0: b"image"},
            engine=LowConfidence(),
        )
        self.assertTrue(enriched.pages[0].ocr_needed)
        self.assertFalse(enriched.pages[0].blocks)


if __name__ == "__main__":
    unittest.main()
