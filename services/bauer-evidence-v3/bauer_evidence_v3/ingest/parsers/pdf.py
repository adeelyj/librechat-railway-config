from __future__ import annotations

import importlib
from dataclasses import dataclass
from typing import Any

from ..canonical import (
    Block,
    Cell,
    Document,
    Page,
    Table,
    attribute_items,
    clean_text,
    rounded_bbox,
    stable_id,
)
from ..probe import SourceProbe
from .base import ParseContext, ParseError, ParserUnavailable


def _load_pymupdf() -> Any:
    """Load the optional native PDF adapter only inside a PDF parse."""

    try:
        return importlib.import_module("pymupdf")
    except ImportError as error:
        raise ParserUnavailable(
            "PDF ingestion requires the optional PyMuPDF dependency"
        ) from error


def _sequence_bbox(value: Any) -> tuple[float, float, float, float] | None:
    if value is None:
        return None
    try:
        coordinates = tuple(float(item) for item in value)
    except (TypeError, ValueError):
        return None
    if len(coordinates) != 4:
        return None
    return coordinates  # type: ignore[return-value]


def _normalized_bbox(
    value: Any,
    *,
    width: float,
    height: float,
) -> tuple[float, float, float, float] | None:
    bbox = _sequence_bbox(value)
    if bbox is None or width <= 0 or height <= 0:
        return None
    normalized = (
        bbox[0] / width,
        bbox[1] / height,
        bbox[2] / width,
        bbox[3] / height,
    )
    clipped = tuple(min(1.0, max(0.0, coordinate)) for coordinate in normalized)
    if clipped[0] >= clipped[2] or clipped[1] >= clipped[3]:
        return None
    return rounded_bbox(
        clipped
    )


def _block_text(raw_block: dict[str, Any]) -> str:
    lines: list[str] = []
    for line in raw_block.get("lines", ()):
        spans = line.get("spans", ()) if isinstance(line, dict) else ()
        value = "".join(
            str(span.get("text", ""))
            for span in spans
            if isinstance(span, dict)
        )
        lines.append(value)
    if not lines and "text" in raw_block:
        lines.append(str(raw_block["text"]))
    return clean_text("\n".join(lines)).strip()


def _text_quality(text: str) -> tuple[float, float]:
    if not text:
        return 1.0, 0.0
    printable = sum(
        character.isprintable() or character in "\n\t" for character in text
    )
    replacements = text.count("\ufffd")
    return printable / len(text), replacements / len(text)


@dataclass(frozen=True, slots=True)
class PdfParser:
    parser_id: str = "pymupdf_native"
    parser_version: str = "1"

    def supports(self, probe: SourceProbe) -> bool:
        return probe.format == "pdf"

    def parse(self, payload: bytes, context: ParseContext) -> Document:
        pymupdf = _load_pymupdf()
        try:
            pdf = pymupdf.open(stream=payload, filetype="pdf")
        except Exception as error:
            raise ParseError(f"PyMuPDF could not open the PDF: {error}") from error

        try:
            if bool(getattr(pdf, "needs_pass", False)):
                raise ParseError("encrypted PDF requires a password")
            metadata = getattr(pdf, "metadata", {}) or {}
            pages: list[Page] = []
            ocr_page_count = 0
            for page_index in range(len(pdf)):
                page = (
                    pdf.load_page(page_index)
                    if hasattr(pdf, "load_page")
                    else pdf[page_index]
                )
                parsed_page = self._parse_page(
                    page=page,
                    page_index=page_index,
                    source_sha256=context.probe.source_sha256,
                )
                pages.append(parsed_page)
                ocr_page_count += int(parsed_page.ocr_needed)

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
                pages=tuple(pages),
                title=str(metadata.get("title") or "").strip() or None,
                language=str(metadata.get("language") or "").strip() or None,
                attributes=attribute_items(
                    {
                        "native_page_count": len(pages),
                        "ocr_needed_page_count": ocr_page_count,
                    }
                ),
            )
        except ParseError:
            raise
        except Exception as error:
            raise ParseError(f"PyMuPDF extraction failed: {error}") from error
        finally:
            close = getattr(pdf, "close", None)
            if callable(close):
                close()

    def _parse_page(
        self,
        *,
        page: Any,
        page_index: int,
        source_sha256: str,
    ) -> Page:
        rect = getattr(page, "rect", None)
        width = float(getattr(rect, "width", 0.0) or 0.0)
        height = float(getattr(rect, "height", 0.0) or 0.0)
        if width <= 0 or height <= 0:
            rect_bbox = _sequence_bbox(rect)
            if rect_bbox:
                width = rect_bbox[2] - rect_bbox[0]
                height = rect_bbox[3] - rect_bbox[1]

        try:
            raw = page.get_text("dict", sort=True)
        except TypeError:
            raw = page.get_text("dict")
        raw_blocks = raw.get("blocks", ()) if isinstance(raw, dict) else ()
        sorted_blocks = sorted(
            (block for block in raw_blocks if isinstance(block, dict)),
            key=lambda block: tuple(block.get("bbox", (0, 0, 0, 0))),
        )
        blocks: list[Block] = []
        image_area = 0.0
        image_count = 0
        native_parts: list[str] = []
        for raw_block in sorted_blocks:
            block_type = int(raw_block.get("type", 0) or 0)
            bbox = _normalized_bbox(
                raw_block.get("bbox"), width=width, height=height
            )
            if block_type == 1:
                image_count += 1
                if bbox is not None:
                    image_area += max(0.0, bbox[2] - bbox[0]) * max(
                        0.0, bbox[3] - bbox[1]
                    )
                continue
            if block_type != 0:
                continue
            text = _block_text(raw_block)
            if not text:
                continue
            native_parts.append(text)
            locator = f"page:{page_index}/text_block:{len(blocks)}"
            blocks.append(
                Block(
                    block_id=stable_id(
                        "block", source_sha256, locator, text
                    ),
                    page_index=page_index,
                    order=len(blocks),
                    kind="paragraph",
                    text=text,
                    source_locator=locator,
                    parser_id=self.parser_id,
                    bbox=bbox,
                    attributes=attribute_items(
                        {"native_block_type": block_type}
                    ),
                )
            )

        table_error: str | None = None
        try:
            tables = self._extract_tables(
                page=page,
                page_index=page_index,
                source_sha256=source_sha256,
                first_order=len(blocks),
                width=width,
                height=height,
            )
        except Exception as error:
            tables = []
            table_error = type(error).__name__

        native_text = "\n".join(native_parts)
        printable_ratio, replacement_ratio = _text_quality(native_text)
        native_character_count = len(native_text)
        image_coverage = min(image_area, 1.0)
        ocr_needed = (
            image_count > 0
            and image_coverage >= 0.35
            and native_character_count < 50
        ) or (
            native_character_count > 0
            and (printable_ratio < 0.85 or replacement_ratio > 0.02)
        )
        signals: dict[str, object] = {
            "image_count": image_count,
            "image_coverage": image_coverage,
            "native_character_count": native_character_count,
            "printable_ratio": printable_ratio,
            "replacement_ratio": replacement_ratio,
            "table_count": len(tables),
        }
        if table_error:
            signals["table_extraction_error"] = table_error
        locator = f"page:{page_index}"
        fingerprint = "\n".join(
            block.text for block in blocks
        ) + "\n" + "\n".join(
            cell.text for table in tables for cell in table.cells
        )
        return Page(
            page_id=stable_id("page", source_sha256, locator, fingerprint),
            index=page_index,
            blocks=tuple(blocks),
            tables=tuple(tables),
            source_locator=locator,
            parser_id=self.parser_id,
            printed_label=str(page_index + 1),
            width=width or None,
            height=height or None,
            rotation=int(getattr(page, "rotation", 0) or 0),
            ocr_needed=ocr_needed,
            signals=attribute_items(signals),
        )

    def _extract_tables(
        self,
        *,
        page: Any,
        page_index: int,
        source_sha256: str,
        first_order: int,
        width: float,
        height: float,
    ) -> list[Table]:
        find_tables = getattr(page, "find_tables", None)
        if not callable(find_tables):
            return []
        result = find_tables()
        native_tables = getattr(result, "tables", result)
        if native_tables is None:
            return []

        parsed: list[Table] = []
        for table_index, native_table in enumerate(native_tables):
            extract = getattr(native_table, "extract", None)
            if not callable(extract):
                continue
            raw_rows = extract() or []
            rows = [
                tuple(
                    clean_text("" if value is None else str(value)).strip()
                    for value in row
                )
                for row in raw_rows
            ]
            column_count = max((len(row) for row in rows), default=0)
            if column_count == 0:
                continue
            rows = [
                row + ("",) * (column_count - len(row))
                for row in rows
            ]
            locator = f"page:{page_index}/table:{table_index}"
            table_content = "\n".join("\t".join(row) for row in rows)
            cells: list[Cell] = []
            current_group: str | None = None
            first_data_row = next(
                (
                    row_index
                    for row_index, row in enumerate(rows)
                    if sum(bool(value) for value in row) != 1
                ),
                0,
            )
            for row_index, row in enumerate(rows):
                nonempty = [
                    (column_index, value)
                    for column_index, value in enumerate(row)
                    if value
                ]
                is_group = (
                    len(nonempty) == 1
                    and nonempty[0][0] == 0
                    and column_count > 1
                )
                if is_group:
                    origins = ((0, nonempty[0][1], column_count),)
                    current_group = nonempty[0][1]
                else:
                    origins = tuple(
                        (column_index, value, 1)
                        for column_index, value in enumerate(row)
                    )
                for column_index, value, column_span in origins:
                    role = (
                        "group_header"
                        if is_group
                        else "header"
                        if row_index == first_data_row
                        else "body"
                    )
                    cell_locator = (
                        f"{locator}/row:{row_index}/column:{column_index}"
                    )
                    cells.append(
                        Cell(
                            cell_id=stable_id(
                                "cell",
                                source_sha256,
                                cell_locator,
                                value,
                            ),
                            row=row_index,
                            column=column_index,
                            text=value,
                            source_locator=cell_locator,
                            parser_id=self.parser_id,
                            column_span=column_span,
                            role=role,
                            group=None if is_group else current_group,
                        )
                    )

            parsed.append(
                Table(
                    table_id=stable_id(
                        "table", source_sha256, locator, table_content
                    ),
                    page_index=page_index,
                    order=first_order + table_index,
                    row_count=len(rows),
                    column_count=column_count,
                    cells=tuple(cells),
                    source_locator=locator,
                    parser_id=self.parser_id,
                    bbox=_normalized_bbox(
                        getattr(native_table, "bbox", None),
                        width=width,
                        height=height,
                    ),
                    attributes=attribute_items(
                        {"syntax": "pymupdf_find_tables"}
                    ),
                )
            )
        return parsed
