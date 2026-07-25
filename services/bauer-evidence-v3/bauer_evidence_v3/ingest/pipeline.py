from __future__ import annotations

from dataclasses import dataclass, field

from .canonical import Document, attribute_items, stable_id
from .ocr import OcrEngine, apply_page_ocr
from .parsers.base import (
    DocumentParser,
    ParseContext,
    ParseError,
    ParserUnavailable,
    UnsupportedFormat,
)
from .parsers.html import HtmlParser
from .parsers.markdown import MarkdownParser
from .parsers.pdf import PdfParser
from .parsers.pdfplumber import PdfPlumberParser
from .parsers.text import TextParser
from .probe import SourceProbe, probe_source
from .render import PageRender, PageRenderer
from .quality import (
    QualityGateError,
    QualityIssue,
    QualityPolicy,
    QualityReport,
    evaluate_document,
    require_publishable,
)


def _default_parsers() -> tuple[DocumentParser, ...]:
    return (
        PdfPlumberParser(),
        PdfParser(),
        HtmlParser(),
        MarkdownParser(),
        TextParser(),
    )


@dataclass(frozen=True, slots=True)
class CompilationResult:
    probe: SourceProbe
    document: Document
    quality: QualityReport
    page_renders: tuple[PageRender, ...] = ()


@dataclass(frozen=True, slots=True)
class Compiler:
    parsers: tuple[DocumentParser, ...] = field(
        default_factory=_default_parsers
    )
    quality_policy: QualityPolicy = field(default_factory=QualityPolicy)
    ocr_engine: OcrEngine | None = None
    page_renderer: PageRenderer | None = None
    ocr_languages: tuple[str, ...] = ("de", "en")
    ocr_minimum_confidence: float = 0.80
    ocr_render_dpi: int = 150

    def __post_init__(self) -> None:
        if bool(self.ocr_engine) != bool(self.page_renderer):
            raise ValueError(
                "OCR engine and page renderer must be configured together"
            )
        if not self.ocr_languages or any(
            not value.strip() for value in self.ocr_languages
        ):
            raise ValueError("OCR languages must contain non-empty values")
        if not 0 <= self.ocr_minimum_confidence <= 1:
            raise ValueError(
                "OCR minimum confidence must be between zero and one"
            )
        if not 72 <= self.ocr_render_dpi <= 300:
            raise ValueError("OCR render DPI must be between 72 and 300")

    def compile(
        self,
        payload: bytes,
        *,
        source_name: str,
        declared_media_type: str | None = None,
        enforce_gate: bool = True,
    ) -> CompilationResult:
        probe = probe_source(
            payload,
            source_name=source_name,
            declared_media_type=declared_media_type,
        )
        if probe.byte_count > self.quality_policy.max_source_bytes:
            locator = f"source:{source_name}"
            document_id = stable_id(
                "document",
                probe.source_sha256,
                locator,
                probe.media_type,
            )
            report = QualityReport(
                document_id=document_id,
                status="fatal",
                issues=(
                    QualityIssue(
                        code="source_size_limit",
                        severity="fatal",
                        message="source exceeds the configured byte limit",
                        location=locator,
                    ),
                ),
                metrics=attribute_items({"source_size": probe.byte_count}),
            )
            raise QualityGateError(report)

        supported = tuple(candidate for candidate in self.parsers if candidate.supports(probe))
        if not supported:
            raise UnsupportedFormat(
                f"unsupported source format for {source_name!r}: "
                f"{probe.signature}"
            )
        candidates: list[tuple[Document, QualityReport]] = []
        errors: list[ParseError] = []
        for parser in supported:
            try:
                document = parser.parse(payload, ParseContext(probe=probe))
                if (
                    document.source_sha256 != probe.source_sha256
                    or document.source_name != probe.source_name
                    or document.media_type != probe.media_type
                ):
                    raise ParseError(
                        "parser returned provenance inconsistent with the source probe"
                    )
                report = evaluate_document(
                    document,
                    source_size=probe.byte_count,
                    policy=self.quality_policy,
                )
                candidates.append((document, report))
            except ParseError as error:
                errors.append(error)
        if not candidates:
            if errors and all(isinstance(error, ParserUnavailable) for error in errors):
                raise ParserUnavailable("; ".join(str(error) for error in errors))
            detail = "; ".join(str(error) for error in errors) or "all parser candidates failed"
            raise ParseError(detail)
        status_rank = {"pass": 0, "warning": 1, "quarantine": 2, "fatal": 3}
        document, report = min(
            candidates,
            key=lambda item: (
                status_rank.get(item[1].status, 4),
                len(item[1].issues),
                -sum(len(page.tables) for page in item[0].pages),
                item[0].parser_id,
            ),
        )
        page_renders: tuple[PageRender, ...] = ()
        deficient_pages = tuple(
            page.index for page in document.pages if page.ocr_needed
        )
        if (
            deficient_pages
            and self.ocr_engine is not None
            and self.page_renderer is not None
        ):
            if probe.format != "pdf":
                raise ParseError(
                    "OCR page rendering is supported only for PDF sources"
                )
            # Native parser candidates are evaluated first. Render and OCR only
            # the selected canonical candidate so a parser cascade does not
            # repeat the expensive page rasterization and model inference.
            page_renders = self.page_renderer.render_pages(
                payload,
                page_indexes=deficient_pages,
                dpi=self.ocr_render_dpi,
            )
            if {item.page_index for item in page_renders} != set(
                deficient_pages
            ):
                raise ParseError(
                    "page renderer did not return every OCR-required page"
                )
            document = apply_page_ocr(
                document,
                page_images={
                    item.page_index: item.image_bytes
                    for item in page_renders
                },
                engine=self.ocr_engine,
                languages=self.ocr_languages,
                minimum_average_confidence=self.ocr_minimum_confidence,
            )
            report = evaluate_document(
                document,
                source_size=probe.byte_count,
                policy=self.quality_policy,
            )
        if enforce_gate:
            require_publishable(report)
        return CompilationResult(
            probe=probe,
            document=document,
            quality=report,
            page_renders=page_renders,
        )


def compile_source(
    payload: bytes,
    *,
    source_name: str,
    declared_media_type: str | None = None,
    enforce_gate: bool = True,
    compiler: Compiler | None = None,
) -> CompilationResult:
    return (compiler or Compiler()).compile(
        payload,
        source_name=source_name,
        declared_media_type=declared_media_type,
        enforce_gate=enforce_gate,
    )
