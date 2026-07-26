from __future__ import annotations

import importlib
from dataclasses import dataclass
from io import BytesIO
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


def _load_pdfplumber() -> Any:
    try:
        return importlib.import_module("pdfplumber")
    except ImportError as exc:
        raise ParserUnavailable(
            "PDF ingestion requires pdfplumber or an approved alternate PDF adapter"
        ) from exc


def _bbox(
    value: Any,
    *,
    width: float,
    height: float,
) -> tuple[float, float, float, float] | None:
    try:
        x0, y0, x1, y1 = (float(item) for item in value)
    except (TypeError, ValueError):
        return None
    if width <= 0 or height <= 0:
        return None
    normalized = (x0 / width, y0 / height, x1 / width, y1 / height)
    clipped = tuple(min(1.0, max(0.0, coordinate)) for coordinate in normalized)
    if clipped[0] >= clipped[2] or clipped[1] >= clipped[3]:
        return None
    return rounded_bbox(clipped)


@dataclass(frozen=True, slots=True)
class PdfPlumberParser:
    parser_id: str = "pdfplumber_native"
    parser_version: str = "2"

    def supports(self, probe: SourceProbe) -> bool:
        return probe.format == "pdf"

    def parse(self, payload: bytes, context: ParseContext) -> Document:
        pdfplumber = _load_pdfplumber()
        try:
            pdf = pdfplumber.open(BytesIO(payload), strict_metadata=True)
        except Exception as exc:
            raise ParseError(f"pdfplumber could not open the PDF: {exc}") from exc
        try:
            metadata = getattr(pdf, "metadata", {}) or {}
            pages = tuple(
                self._parse_page(
                    page=page,
                    page_index=index,
                    source_sha256=context.probe.source_sha256,
                )
                for index, page in enumerate(pdf.pages)
            )
            locator = f"source:{context.probe.source_name}"
            return Document(
                document_id=stable_id(
                    "document",
                    context.probe.source_sha256,
                    locator,
                    context.probe.media_type,
                ),
                source_sha256=context.probe.source_sha256,
                source_name=context.probe.source_name,
                media_type=context.probe.media_type,
                parser_id=self.parser_id,
                parser_version=self.parser_version,
                pages=pages,
                title=str(metadata.get("Title") or metadata.get("title") or "").strip() or None,
                language=str(metadata.get("Language") or metadata.get("language") or "").strip() or None,
                attributes=attribute_items(
                    {
                        "native_page_count": len(pages),
                        "ocr_needed_page_count": sum(page.ocr_needed for page in pages),
                    }
                ),
            )
        except ParseError:
            raise
        except Exception as exc:
            raise ParseError(f"pdfplumber extraction failed: {exc}") from exc
        finally:
            pdf.close()

    def _parse_page(self, *, page: Any, page_index: int, source_sha256: str) -> Page:
        width = float(page.width)
        height = float(page.height)
        words = page.extract_words(
            x_tolerance=2,
            y_tolerance=3,
            keep_blank_chars=False,
            use_text_flow=True,
        ) or []
        line_groups: dict[int, list[dict[str, Any]]] = {}
        for word in words:
            line_groups.setdefault(round(float(word.get("top", 0)) / 3), []).append(word)
        blocks: list[Block] = []
        for _, line_words in sorted(line_groups.items()):
            line_words.sort(key=lambda word: float(word.get("x0", 0)))
            text = clean_text(" ".join(str(word.get("text", "")) for word in line_words)).strip()
            if not text:
                continue
            raw_bbox = (
                min(float(word.get("x0", 0)) for word in line_words),
                min(float(word.get("top", 0)) for word in line_words),
                max(float(word.get("x1", width)) for word in line_words),
                max(float(word.get("bottom", height)) for word in line_words),
            )
            locator = f"page:{page_index}/text_line:{len(blocks)}"
            blocks.append(
                Block(
                    block_id=stable_id("block", source_sha256, locator, text),
                    page_index=page_index,
                    order=len(blocks),
                    kind="paragraph",
                    text=text,
                    source_locator=locator,
                    parser_id=self.parser_id,
                    bbox=_bbox(raw_bbox, width=width, height=height),
                )
            )

        tables, skipped_table_candidates = self._tables(
            page=page,
            page_index=page_index,
            source_sha256=source_sha256,
            first_order=len(blocks),
            width=width,
            height=height,
        )
        images = getattr(page, "images", ()) or ()
        image_area = 0.0
        for image in images:
            try:
                image_area += max(0.0, float(image["x1"]) - float(image["x0"])) * max(
                    0.0,
                    float(image.get("bottom", image.get("y1"))) - float(image.get("top", image.get("y0"))),
                )
            except (KeyError, TypeError, ValueError):
                continue
        image_coverage = min(1.0, image_area / max(width * height, 1))
        native_text = "\n".join(block.text for block in blocks)
        printable = (
            sum(character.isprintable() or character in "\n\t" for character in native_text)
            / len(native_text)
            if native_text
            else 1.0
        )
        replacement = native_text.count("\ufffd") / len(native_text) if native_text else 0.0
        ocr_needed = (
            bool(images) and image_coverage >= 0.35 and len(native_text) < 50
        ) or (bool(native_text) and (printable < 0.85 or replacement > 0.02))
        locator = f"page:{page_index}"
        fingerprint = "\n".join(
            [*(block.text for block in blocks), *(cell.text for table in tables for cell in table.cells)]
        )
        return Page(
            page_id=stable_id("page", source_sha256, locator, fingerprint),
            index=page_index,
            blocks=tuple(blocks),
            tables=tuple(tables),
            source_locator=locator,
            parser_id=self.parser_id,
            # The physical index is authoritative. pdfplumber does not expose
            # PDF page-label metadata, so do not fabricate a printed label from
            # the physical page number.
            printed_label=None,
            width=width,
            height=height,
            rotation=int(getattr(page, "rotation", 0) or 0),
            ocr_needed=ocr_needed,
            signals=attribute_items(
                {
                    "image_count": len(images),
                    "image_coverage": image_coverage,
                    "native_character_count": len(native_text),
                    "printable_ratio": printable,
                    "replacement_ratio": replacement,
                    "table_count": len(tables),
                    "skipped_degenerate_table_candidate_count": (
                        skipped_table_candidates
                    ),
                    "printed_label_source": "unavailable",
                }
            ),
        )

    def _tables(
        self,
        *,
        page: Any,
        page_index: int,
        source_sha256: str,
        first_order: int,
        width: float,
        height: float,
    ) -> tuple[list[Table], int]:
        parsed: list[Table] = []
        skipped_degenerate = 0
        for table_index, native in enumerate(page.find_tables() or ()):
            raw_rows = native.extract() or []
            rows = [
                tuple(clean_text("" if value is None else str(value)).strip() for value in row)
                for row in raw_rows
            ]
            column_count = max((len(row) for row in rows), default=0)
            if not column_count:
                continue
            rows = [row + ("",) * (column_count - len(row)) for row in rows]
            # Native table discovery can return decorative rules, title boxes,
            # or single-axis text regions. They are not trustworthy table grids
            # and their text is already retained by the page's native blocks.
            # Reject them at the candidate boundary so a false detector result
            # cannot quarantine an otherwise valid source document.
            if len(rows) < 2 or column_count < 2:
                skipped_degenerate += 1
                continue
            locator = f"page:{page_index}/table:{table_index}"
            cells: list[Cell] = []
            active_group: str | None = None
            for row_index, row in enumerate(rows):
                nonempty = [(column, text) for column, text in enumerate(row) if text]
                group = len(nonempty) == 1 and nonempty[0][0] == 0 and column_count > 1
                if group:
                    origins = ((0, nonempty[0][1], column_count),)
                    active_group = nonempty[0][1]
                else:
                    origins = tuple((column, text, 1) for column, text in enumerate(row))
                for column, text, span in origins:
                    role = "group_header" if group else "header" if row_index == 0 else "body"
                    cell_locator = f"{locator}/row:{row_index}/column:{column}"
                    cells.append(
                        Cell(
                            cell_id=stable_id("cell", source_sha256, cell_locator, text),
                            row=row_index,
                            column=column,
                            text=text,
                            source_locator=cell_locator,
                            parser_id=self.parser_id,
                            column_span=span,
                            role=role,
                            group=None if group else active_group,
                        )
                    )
            table_text = "\n".join("\t".join(row) for row in rows)
            parsed.append(
                Table(
                    table_id=stable_id("table", source_sha256, locator, table_text),
                    page_index=page_index,
                    order=first_order + table_index,
                    row_count=len(rows),
                    column_count=column_count,
                    cells=tuple(cells),
                    source_locator=locator,
                    parser_id=self.parser_id,
                    bbox=_bbox(native.bbox, width=width, height=height),
                    attributes=attribute_items({"syntax": "pdfplumber_find_tables"}),
                )
            )
        return parsed, skipped_degenerate
