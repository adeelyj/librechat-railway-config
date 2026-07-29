from __future__ import annotations

import hashlib
import importlib
import re
from dataclasses import dataclass
from decimal import Decimal
from io import BytesIO
from pathlib import PurePosixPath
from typing import Any

from ..canonical.models import (
    CanonicalBlock,
    CanonicalCell,
    CanonicalDocument,
    CanonicalFact,
    CanonicalTable,
    Provenance,
)
from ..canonical.normalize import (
    clean_text,
    document_number_from_text,
    inherited_header_paths,
    language_from_filename,
    normalize_unit,
    numeric_value,
    predicate_slug,
    sorted_attributes,
    stable_id,
)


_RANGE_RE = re.compile(
    r"(?P<label>Pressure\s+range|Charging\s+rate|Motor\s+power)"
    r"\s*:\s*(?P<minimum>\d[\d,.]*)\s*[-–]\s*"
    r"(?P<maximum>\d[\d,.]*)\s*(?P<unit>bar|l/min|kW)\b",
    re.IGNORECASE,
)
_PRINTED_PAGE_RE = re.compile(r"(?:^|\|\s*)(\d{1,4})\s*$")


def _load_pdfplumber() -> Any:
    return importlib.import_module("pdfplumber")


def _load_pypdf() -> Any:
    return importlib.import_module("pypdf")


def _normalized_bbox(
    value: tuple[float, float, float, float],
    *,
    width: float,
    height: float,
) -> tuple[float, float, float, float] | None:
    if width <= 0 or height <= 0:
        return None
    x0, top, x1, bottom = value
    result = (
        round(max(0.0, min(1.0, x0 / width)), 6),
        round(max(0.0, min(1.0, top / height)), 6),
        round(max(0.0, min(1.0, x1 / width)), 6),
        round(max(0.0, min(1.0, bottom / height)), 6),
    )
    return result if result[0] < result[2] and result[1] < result[3] else None


def _provenance(
    *,
    source_sha256: str,
    source_path: str,
    parser_id: str,
    parser_version: str,
    locator: str,
    physical_page: int,
    printed_page: str | None = None,
    bounding_box: tuple[float, float, float, float] | None = None,
) -> Provenance:
    return Provenance(
        source_sha256=source_sha256,
        source_path=source_path,
        parser_id=parser_id,
        parser_version=parser_version,
        locator=locator,
        physical_page=physical_page,
        printed_page=printed_page,
        bounding_box=bounding_box,
    )


def _range_decimal(value: str) -> Decimal:
    compact = value.replace(" ", "")
    if "," in compact and "." not in compact:
        tail = compact.rsplit(",", 1)[1]
        compact = compact.replace(",", "") if len(tail) == 3 else compact.replace(",", ".")
    else:
        compact = compact.replace(",", "")
    return Decimal(compact)


def _metadata_from_filename(
    filename: str,
    *,
    first_page_lines: list[str],
    pdf_metadata: dict[str, Any] | None = None,
) -> tuple[str | None, str | None, str | None, str | None]:
    metadata = pdf_metadata or {}
    embedded_title = clean_text(
        str(metadata.get("Title") or metadata.get("title") or "")
    ) or None
    language = (
        clean_text(str(metadata.get("Language") or metadata.get("language") or ""))
        or language_from_filename(filename)
    )
    number = document_number_from_text(filename, embedded_title)
    folded_lines = {line.casefold(): line for line in first_page_lines}
    b_detection_parts = (
        "bauer b-detection",
        "the next generation",
        "sensor calibration",
    )
    if all(part in folded_lines for part in b_detection_parts):
        title = " - ".join(folded_lines[part] for part in b_detection_parts)
        subject = "Sensor calibration for BAUER B-DETECTION in Sports & Safety"
        return title, language or "en", number, subject
    if "Product_overview" in filename:
        return embedded_title or "Product Overview", language, number, None
    if "CERTIFICATE" in folded_lines:
        return embedded_title or folded_lines["certificate"], language or "en", number, (
            "EN ISO 3834-2 welding quality certificate"
            if any("EN ISO 3834-2" in line for line in first_page_lines)
            else None
        )
    title = embedded_title or next(
        (
            line
            for line in first_page_lines
            if 4 <= len(line) <= 160 and not line.isdigit()
        ),
        None,
    )
    return title, language, number, None


@dataclass(frozen=True, slots=True)
class PdfLayoutParser:
    parser_id: str = "pdfplumber_layout"
    parser_version: str = "1"

    def parse(
        self,
        payload: bytes,
        *,
        source_path: str,
        source_sha256: str | None = None,
    ) -> CanonicalDocument:
        source_sha256 = source_sha256 or hashlib.sha256(payload).hexdigest()
        pdfplumber = _load_pdfplumber()
        pdf = pdfplumber.open(BytesIO(payload), strict_metadata=True)
        try:
            blocks: list[CanonicalBlock] = []
            tables: list[CanonicalTable] = []
            facts: list[CanonicalFact] = []
            first_page_lines: list[str] = []
            printed_pages: dict[int, str | None] = {}
            for page_index, page in enumerate(pdf.pages):
                page_number = page_index + 1
                page_blocks, line_details = self._page_blocks(
                    page,
                    page_number=page_number,
                    source_path=source_path,
                    source_sha256=source_sha256,
                )
                if page_number == 1:
                    first_page_lines.extend(block.text for block in page_blocks)
                printed_page = self._printed_page(
                    [block.text for block in page_blocks[:8]]
                )
                printed_pages[page_number] = printed_page
                if printed_page:
                    page_blocks = [
                        self._with_printed_page(block, printed_page)
                        for block in page_blocks
                    ]
                blocks.extend(page_blocks)
                page_tables, page_facts = self._page_tables(
                    page,
                    page_number=page_number,
                    printed_page=printed_page,
                    source_path=source_path,
                    source_sha256=source_sha256,
                )
                tables.extend(page_tables)
                facts.extend(page_facts)
                facts.extend(
                    self._range_facts(
                        page_blocks,
                        line_details=line_details,
                    )
                )

            filename = PurePosixPath(source_path).name
            metadata = getattr(pdf, "metadata", {}) or {}
            title, language, number, subject = _metadata_from_filename(
                filename,
                first_page_lines=first_page_lines,
                pdf_metadata=metadata,
            )
            return CanonicalDocument(
                document_id=stable_id(
                    "document",
                    source_sha256,
                    source_path,
                    self.parser_id,
                    self.parser_version,
                ),
                source_sha256=source_sha256,
                source_path=source_path,
                media_type="application/pdf",
                parser_id=self.parser_id,
                parser_version=self.parser_version,
                title=title,
                language=language,
                document_number=number,
                subject=subject,
                source_filename=filename,
                page_count=len(pdf.pages),
                blocks=tuple(blocks),
                tables=tuple(tables),
                records=(),
                facts=tuple(facts),
                attributes=sorted_attributes(
                    {
                        "block_count": len(blocks),
                        "table_count": len(tables),
                        "native_page_count": len(pdf.pages),
                        "printed_page_count": sum(
                            value is not None for value in printed_pages.values()
                        ),
                    }
                ),
            )
        finally:
            pdf.close()

    def _page_blocks(
        self,
        page: Any,
        *,
        page_number: int,
        source_path: str,
        source_sha256: str,
    ) -> tuple[list[CanonicalBlock], list[tuple[float, float, str]]]:
        width = float(page.width)
        height = float(page.height)
        words = page.extract_words(
            x_tolerance=2,
            y_tolerance=3,
            keep_blank_chars=False,
            use_text_flow=False,
            extra_attrs=["size", "fontname"],
        ) or []
        ordered = sorted(
            words,
            key=lambda word: (
                round(float(word.get("top", 0)) / 3),
                float(word.get("x0", 0)),
            ),
        )
        line_groups: list[list[dict[str, Any]]] = []
        for word in ordered:
            top = float(word.get("top", 0))
            if (
                not line_groups
                or abs(
                    top
                    - sum(float(item.get("top", 0)) for item in line_groups[-1])
                    / len(line_groups[-1])
                )
                > 3.0
            ):
                line_groups.append([word])
            else:
                line_groups[-1].append(word)

        blocks: list[CanonicalBlock] = []
        details: list[tuple[float, float, str]] = []
        headings: list[str] = []
        for line_words in line_groups:
            line_words.sort(key=lambda word: float(word.get("x0", 0)))
            text = clean_text(
                " ".join(str(word.get("text", "")) for word in line_words)
            )
            if not text:
                continue
            sizes = [float(word.get("size") or 0) for word in line_words]
            max_size = max(sizes, default=0)
            average_size = sum(sizes) / max(len(sizes), 1)
            uppercase_ratio = (
                sum(character.isupper() for character in text)
                / max(sum(character.isalpha() for character in text), 1)
            )
            heading = (
                len(text) <= 100
                and (
                    max_size >= 16
                    or (
                        uppercase_ratio >= 0.85
                        and any(
                            token in text.upper()
                            for token in (
                                "SERIES",
                                "INDUSTRY",
                                "CALIBRATION",
                                "CERTIFICATE",
                                "GENERATION",
                                "B-DETECTION",
                            )
                        )
                    )
                )
            )
            if heading:
                if text.upper() in {
                    "K 22 – K 28 SERIES",
                    "K 22 - K 28 SERIES",
                    "PE-VE INDUSTRY",
                }:
                    headings = [text]
                elif not headings or headings[-1].casefold() != text.casefold():
                    headings.append(text)
                    headings = headings[-3:]
            raw_bbox = (
                min(float(word.get("x0", 0)) for word in line_words),
                min(float(word.get("top", 0)) for word in line_words),
                max(float(word.get("x1", width)) for word in line_words),
                max(float(word.get("bottom", height)) for word in line_words),
            )
            locator = f"page:{page_number}/line:{len(blocks)}"
            blocks.append(
                CanonicalBlock(
                    block_id=stable_id(
                        "block",
                        source_sha256,
                        locator,
                        text,
                    ),
                    kind="heading" if heading else "paragraph",
                    text=text,
                    section_path=tuple(headings),
                    provenance=_provenance(
                        source_sha256=source_sha256,
                        source_path=source_path,
                        parser_id=self.parser_id,
                        parser_version=self.parser_version,
                        locator=locator,
                        physical_page=page_number,
                        bounding_box=_normalized_bbox(
                            raw_bbox,
                            width=width,
                            height=height,
                        ),
                    ),
                )
            )
            details.append((average_size, raw_bbox[1], text))
        return blocks, details

    def _page_tables(
        self,
        page: Any,
        *,
        page_number: int,
        printed_page: str | None,
        source_path: str,
        source_sha256: str,
    ) -> tuple[list[CanonicalTable], list[CanonicalFact]]:
        tables: list[CanonicalTable] = []
        facts: list[CanonicalFact] = []
        width = float(page.width)
        height = float(page.height)
        for table_index, native in enumerate(page.find_tables() or ()):
            raw_rows = native.extract() or []
            rows = [
                [clean_text("" if value is None else str(value)) for value in row]
                for row in raw_rows
            ]
            column_count = max((len(row) for row in rows), default=0)
            if len(rows) < 2 or column_count < 2:
                continue
            rows = [row + [""] * (column_count - len(row)) for row in rows]
            header_paths = inherited_header_paths(
                column_count=column_count,
                header_cells=(
                    (0, column, 1, text)
                    for column, text in enumerate(rows[0])
                ),
            )
            locator = f"page:{page_number}/table:{table_index}"
            table_id = stable_id(
                "table",
                source_sha256,
                locator,
                *(f"{row}:{column}:{text}" for row, values in enumerate(rows) for column, text in enumerate(values)),
            )
            cells: list[CanonicalCell] = []
            for row_index, row in enumerate(rows):
                subject = row[0] if row_index > 0 else ""
                for column, text in enumerate(row):
                    role = (
                        "header"
                        if row_index == 0
                        else "row_header"
                        if column == 0
                        else "body"
                    )
                    unit_raw = next(
                        (
                            value
                            for value in reversed(header_paths[column])
                            if normalize_unit(value)[1] is not None
                        ),
                        None,
                    )
                    unit_raw, unit_ucum = normalize_unit(unit_raw)
                    kind, number = numeric_value(text) if role == "body" else ("text", None)
                    if not text:
                        kind = "empty"
                    cell_locator = f"{locator}/row:{row_index}/column:{column}"
                    cell = CanonicalCell(
                        cell_id=stable_id(
                            "cell",
                            source_sha256,
                            cell_locator,
                            text,
                        ),
                        row=row_index,
                        column=column,
                        row_span=1,
                        column_span=1,
                        role=role,  # type: ignore[arg-type]
                        text=text,
                        value_kind=kind,  # type: ignore[arg-type]
                        numeric_value=number,
                        header_path=header_paths[column],
                        unit_raw=unit_raw,
                        unit_ucum=unit_ucum,
                        qualifier_markers=(),
                        group=None,
                        provenance=_provenance(
                            source_sha256=source_sha256,
                            source_path=source_path,
                            parser_id=self.parser_id,
                            parser_version=self.parser_version,
                            locator=cell_locator,
                            physical_page=page_number,
                            printed_page=printed_page,
                        ),
                    )
                    cells.append(cell)
                    if role == "body" and number is not None:
                        predicate = predicate_slug(
                            header_paths[column][0]
                            if header_paths[column]
                            else f"column_{column + 1}"
                        )
                        facts.append(
                            CanonicalFact(
                                fact_id=stable_id(
                                    "fact",
                                    table_id,
                                    cell.cell_id,
                                    subject,
                                    predicate,
                                    text,
                                ),
                                subject=subject or "table row",
                                predicate=predicate,
                                value_kind=kind,  # type: ignore[arg-type]
                                raw_value=text,
                                numeric_value=number,
                                minimum_value=None,
                                maximum_value=None,
                                unit_raw=unit_raw,
                                unit_ucum=unit_ucum,
                                qualifiers=(),
                                provenance_ids=(cell.cell_id,),
                                confidence=0.9,
                                review_status="candidate",
                            )
                        )
            tables.append(
                CanonicalTable(
                    table_id=table_id,
                    caption=None,
                    section_path=(),
                    row_count=len(rows),
                    column_count=column_count,
                    cells=tuple(cells),
                    footnotes=(),
                    provenance=_provenance(
                        source_sha256=source_sha256,
                        source_path=source_path,
                        parser_id=self.parser_id,
                        parser_version=self.parser_version,
                        locator=locator,
                        physical_page=page_number,
                        printed_page=printed_page,
                        bounding_box=_normalized_bbox(
                            tuple(float(item) for item in native.bbox),
                            width=width,
                            height=height,
                        ),
                    ),
                )
            )
        return tables, facts

    @staticmethod
    def _printed_page(lines: list[str]) -> str | None:
        for line in lines:
            match = _PRINTED_PAGE_RE.search(line)
            if match and "|" in line:
                return match.group(1)
        return None

    @staticmethod
    def _with_printed_page(
        block: CanonicalBlock,
        printed_page: str,
    ) -> CanonicalBlock:
        provenance = block.provenance
        return CanonicalBlock(
            block_id=block.block_id,
            kind=block.kind,
            text=block.text,
            section_path=block.section_path,
            provenance=Provenance(
                source_sha256=provenance.source_sha256,
                source_path=provenance.source_path,
                parser_id=provenance.parser_id,
                parser_version=provenance.parser_version,
                locator=provenance.locator,
                physical_page=provenance.physical_page,
                printed_page=printed_page,
                bounding_box=provenance.bounding_box,
            ),
        )

    @staticmethod
    def _range_facts(
        blocks: list[CanonicalBlock],
        *,
        line_details: list[tuple[float, float, str]],
    ) -> list[CanonicalFact]:
        del line_details
        facts: list[CanonicalFact] = []
        current_subject = "document"
        for block in blocks:
            upper = block.text.upper()
            if upper in {
                "K 22 – K 28 SERIES",
                "K 22 - K 28 SERIES",
                "PE-VE INDUSTRY",
            }:
                current_subject = block.text
            for match in _RANGE_RE.finditer(block.text):
                minimum = _range_decimal(match.group("minimum"))
                maximum = _range_decimal(match.group("maximum"))
                unit_raw, unit_ucum = normalize_unit(match.group("unit"))
                predicate = predicate_slug(match.group("label"))
                facts.append(
                    CanonicalFact(
                        fact_id=stable_id(
                            "fact",
                            block.block_id,
                            current_subject,
                            predicate,
                            minimum,
                            maximum,
                            unit_ucum or unit_raw or "",
                        ),
                        subject=current_subject,
                        predicate=predicate,
                        value_kind="range",
                        raw_value=match.group(0),
                        numeric_value=None,
                        minimum_value=minimum,
                        maximum_value=maximum,
                        unit_raw=unit_raw,
                        unit_ucum=unit_ucum,
                        qualifiers=(("scope", "family"),),
                        provenance_ids=(block.block_id,),
                        confidence=1.0,
                        review_status="candidate",
                    )
                )
        by_id = {fact.fact_id: fact for fact in facts}
        return [by_id[key] for key in sorted(by_id)]


@dataclass(frozen=True, slots=True)
class PypdfTextParser:
    parser_id: str = "pypdf_text"
    parser_version: str = "1"

    def parse(
        self,
        payload: bytes,
        *,
        source_path: str,
        source_sha256: str | None = None,
    ) -> CanonicalDocument:
        source_sha256 = source_sha256 or hashlib.sha256(payload).hexdigest()
        pypdf = _load_pypdf()
        reader = pypdf.PdfReader(BytesIO(payload))
        blocks: list[CanonicalBlock] = []
        first_page_lines: list[str] = []
        for page_index, page in enumerate(reader.pages):
            page_number = page_index + 1
            text = page.extract_text() or ""
            for line_index, raw_line in enumerate(text.splitlines()):
                line = clean_text(raw_line)
                if not line:
                    continue
                if page_number == 1:
                    first_page_lines.append(line)
                locator = f"page:{page_number}/line:{line_index}"
                blocks.append(
                    CanonicalBlock(
                        block_id=stable_id(
                            "block",
                            source_sha256,
                            locator,
                            line,
                        ),
                        kind="paragraph",
                        text=line,
                        section_path=(),
                        provenance=_provenance(
                            source_sha256=source_sha256,
                            source_path=source_path,
                            parser_id=self.parser_id,
                            parser_version=self.parser_version,
                            locator=locator,
                            physical_page=page_number,
                        ),
                    )
                )
        filename = PurePosixPath(source_path).name
        metadata = {
            str(key).lstrip("/"): value
            for key, value in (reader.metadata or {}).items()
        }
        title, language, number, subject = _metadata_from_filename(
            filename,
            first_page_lines=first_page_lines,
            pdf_metadata=metadata,
        )
        return CanonicalDocument(
            document_id=stable_id(
                "document",
                source_sha256,
                source_path,
                self.parser_id,
            ),
            source_sha256=source_sha256,
            source_path=source_path,
            media_type="application/pdf",
            parser_id=self.parser_id,
            parser_version=self.parser_version,
            title=title,
            language=language,
            document_number=number,
            subject=subject,
            source_filename=filename,
            page_count=len(reader.pages),
            blocks=tuple(blocks),
            tables=(),
            records=(),
            facts=(),
            attributes=sorted_attributes({"block_count": len(blocks)}),
        )
