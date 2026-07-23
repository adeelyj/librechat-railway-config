from __future__ import annotations

import hashlib
import re
import unicodedata
from dataclasses import dataclass, field, replace
from typing import Any, Iterable

from . import EXTRACTOR_VERSION


PAGE_RE = re.compile(r"^\s*#{0,3}\s*PAGE\s+(\d+)\s*$", re.IGNORECASE)
MARKDOWN_PAGE_RE = re.compile(r"^\s*#{1,4}\s*Page\s+(\d+)\s*$", re.IGNORECASE)
HEADING_RE = re.compile(r"^\s*(#{1,6})\s+(.+?)\s*$")
NUMBER_RE = re.compile(r"^[<>~±+\-]?\d+(?:[.,]\d+)?(?:\s*[–-]\s*\d+(?:[.,]\d+)?)?$")
UNIT_RE = re.compile(
    r"^(?:bar|barg|psi|psig|mpa|kpa|pa|l/min|l\/min|m[³3]/h|cfm|rpm|kw|kg|lbs?|"
    r"°c|c|f|ppm|%|mm|cm|m|v|hz)$",
    re.IGNORECASE,
)
MODEL_ROW_RE = re.compile(
    r"^(?=.{2,100}$)(?=.*[A-Za-zÄÖÜäöü])(?=.*\d)"
    r"[A-Za-zÄÖÜäöü0-9][A-Za-zÄÖÜäöü0-9 .+/_()–—-]*$"
)
FOOTNOTE_RE = re.compile(
    r"^\s*(?:[¹²³⁴⁵⁶⁷⁸⁹]|\d{1,2}(?:[.)]|\s))\s*\D.{3,}$"
)

STANDARD_RE = re.compile(
    r"\b(?:(?:DIN\s+)?EN\s+ISO\s+\d{3,5}(?:-\d+)?|ISO\s+\d{3,5}(?::\d{4})?|"
    r"DIN\s+EN\s+\d{3,5}(?::\d{4})?|EN\s+\d{3,5}(?::\d{4})?|"
    r"AD\s+2000(?:-[A-Za-z]+)?(?:\s+[A-Z]{1,3}\s*\d+)?)\b",
    re.IGNORECASE,
)
DOCUMENT_NUMBER_RE = re.compile(r"\b(?:N\d{4,7}(?:[_-]\d+)?|DOC-[A-Z0-9-]+)\b", re.IGNORECASE)
REVISION_RE = re.compile(
    r"\b(?:rev(?:ision)?|ausgabe|version)\s*[:.]?\s*([A-Z0-9][A-Z0-9._/-]{0,20})\b",
    re.IGNORECASE,
)
PRESSURE_RE = re.compile(
    r"\b(\d+(?:[.,]\d+)?(?:\s*[–-]\s*\d+(?:[.,]\d+)?)?)\s*(bar|barg|psi|psig|MPa|kPa)\b",
    re.IGNORECASE,
)
CAPACITY_RE = re.compile(
    r"\b(\d+(?:[.,]\d+)?(?:\s*[–-]\s*\d+(?:[.,]\d+)?)?)\s*(l/min|l\/min|m[³3]/h|cfm)\b",
    re.IGNORECASE,
)
PART_NUMBER_RE = re.compile(
    r"\b(?:art(?:icle)?\.?\s*(?:nr|no)\.?|part\s*(?:nr|no|number)|sku)\s*[:#]?\s*"
    r"([A-Z0-9][A-Z0-9._/-]{2,})\b",
    re.IGNORECASE,
)
MODEL_RE = re.compile(
    r"\b(?:B-(?:DETECTION(?:\s+PLUS)?|SAFE|SELECT|KOOL|CONTROL(?:\s+(?:MICRO|SMART|II|III))?)|"
    r"(?:BM|K|GIB|GI|PE|PE-VE|VERTICUS|MINI-VERTICUS)\s+[A-Z0-9][A-Z0-9./-]*)\b",
    re.IGNORECASE,
)
ADDRESS_RE = re.compile(
    r"(?im)^.*\b\d{5}\s+(?:München|Munich)\b.*$|^.*\bStäbl(?:i|istraße|istr\.)[^,\n]*"
    r"(?:,\s*)?\d{5}\s+(?:München|Munich).*$"
)
CERTIFICATE_LINE_RE = re.compile(r"(?im)^.*\b(?:certificate|zertifikat)\b.*$")
PUBLICATION_DATE_RE = re.compile(
    r"(?<!\d)(20\d{2})[-_.](0?[1-9]|1[0-2])(?!\d)"
)
MEDIUM_VARIANTS = (
    ("breathing air", re.compile(r"\b(?:breathing air|atemluft)\b", re.IGNORECASE)),
    ("compressed air", re.compile(r"\b(?:compressed air|druckluft)\b", re.IGNORECASE)),
    ("nitrogen", re.compile(r"\b(?:nitrogen|stickstoff|N2)\b", re.IGNORECASE)),
    ("nitrox", re.compile(r"\bnitrox\b", re.IGNORECASE)),
    ("helium", re.compile(r"\bhelium\b", re.IGNORECASE)),
    ("argon", re.compile(r"\bargon\b", re.IGNORECASE)),
    ("methane", re.compile(r"\b(?:methane|methan)\b", re.IGNORECASE)),
)
COMPONENT_VARIANTS = (
    ("compressor", re.compile(r"\b(?:compressor|kompressor)\w*\b", re.IGNORECASE)),
    ("booster", re.compile(r"\b(?:booster|nachverdichter)\w*\b", re.IGNORECASE)),
    ("dryer", re.compile(r"\b(?:dryer|trockner)\w*\b", re.IGNORECASE)),
    ("filter", re.compile(r"\bfilter\w*\b", re.IGNORECASE)),
    ("valve", re.compile(r"\b(?:valve|ventil)\w*\b", re.IGNORECASE)),
    ("sensor", re.compile(r"\b(?:sensor|transmitter)\w*\b", re.IGNORECASE)),
    ("control", re.compile(r"\b(?:control|steuerung)\w*\b", re.IGNORECASE)),
)


@dataclass(frozen=True)
class ExtractedEntity:
    kind: str
    raw_value: str
    normalized_value: str
    numeric_value: float | None = None
    unit: str | None = None
    source_span: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class ExtractedChunk:
    chunk_id: str
    ordinal: int
    chunk_kind: str
    content: str
    search_text: str
    page: int | None = None
    section_path: tuple[str, ...] = ()
    table_title: str | None = None
    row_label: str | None = None
    headers: tuple[str, ...] = ()
    row_values: tuple[str, ...] = ()
    units: tuple[str, ...] = ()
    footnotes: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class ParsedDocument:
    file_id: str
    checksum: str
    filename: str
    namespace: str
    title: str | None
    language: str
    source_type: str
    source_path: str | None
    document_number: str | None
    certificate: str | None
    certificates: tuple[str, ...]
    revision: str | None
    publication_date: str | None
    organization: str | None
    address: str | None
    product_families: tuple[str, ...]
    media: tuple[str, ...]
    component_categories: tuple[str, ...]
    standards: tuple[str, ...]
    chunks: tuple[ExtractedChunk, ...]
    entities: tuple[ExtractedEntity, ...]
    metadata: dict[str, Any]


def normalize_for_search(value: str) -> str:
    normalized = unicodedata.normalize("NFKC", value).casefold()
    normalized = normalized.replace("ß", "ss")
    normalized = re.sub(r"[\s_./\\:;,+()[\]{}|]+", " ", normalized)
    normalized = re.sub(r"(?<=\w)[–—-](?=\w)", " ", normalized)
    normalized = re.sub(r"[^\w%°-]+", " ", normalized, flags=re.UNICODE)
    return re.sub(r"\s+", " ", normalized).strip()


def _unique(values: Iterable[str]) -> tuple[str, ...]:
    seen: set[str] = set()
    result: list[str] = []
    for value in values:
        clean = re.sub(r"\s+", " ", value).strip(" \t\r\n,;")
        key = normalize_for_search(clean)
        if clean and key not in seen:
            seen.add(key)
            result.append(clean)
    return tuple(result)


def _first_match(pattern: re.Pattern[str], text: str, group: int = 0) -> str | None:
    match = pattern.search(text)
    return match.group(group).strip() if match else None


def detect_language(filename: str, text: str) -> str:
    upper = filename.upper()
    for code in ("DE", "EN", "FR", "ES", "IT", "CN"):
        if re.search(rf"(?:^|[_-]){code}(?:[_-]|\.|$)", upper):
            return code.lower()
    sample = f" {text[:5000].casefold()} "
    scores = {
        "de": sum(sample.count(token) for token in (" der ", " die ", " und ", " für ", " mit ")),
        "en": sum(sample.count(token) for token in (" the ", " and ", " for ", " with ", " pressure ")),
        "fr": sum(sample.count(token) for token in (" le ", " la ", " et ", " pour ", " avec ")),
        "es": sum(sample.count(token) for token in (" el ", " la ", " y ", " para ", " con ")),
    }
    winner = max(scores, key=scores.get)
    return winner if scores[winner] > 1 else "und"


def _parse_float(value: str) -> float | None:
    first = re.split(r"\s*[–-]\s*", value.strip())[0].replace(",", ".")
    try:
        return float(first)
    except ValueError:
        return None


def extract_entities(text: str, *, page: int | None = None, chunk_id: str | None = None) -> tuple[ExtractedEntity, ...]:
    entities: list[ExtractedEntity] = []

    def add(
        kind: str,
        raw: str,
        start: int,
        numeric: float | None = None,
        unit: str | None = None,
        normalized: str | None = None,
    ) -> None:
        entities.append(
            ExtractedEntity(
                kind=kind,
                raw_value=raw.strip(),
                normalized_value=normalized or normalize_for_search(raw),
                numeric_value=numeric,
                unit=unit.casefold() if unit else None,
                source_span={"start": start, "end": start + len(raw), "page": page, "chunk_id": chunk_id},
            )
        )

    for kind, pattern in (
        ("standard", STANDARD_RE),
        ("document_number", DOCUMENT_NUMBER_RE),
        ("model", MODEL_RE),
    ):
        for match in pattern.finditer(text):
            add(kind, match.group(0), match.start())

    for match in PART_NUMBER_RE.finditer(text):
        add("part_number", match.group(1), match.start(1))

    for kind, pattern in (("pressure", PRESSURE_RE), ("capacity", CAPACITY_RE)):
        for match in pattern.finditer(text):
            raw = match.group(0)
            add(kind, raw, match.start(), _parse_float(match.group(1)), match.group(2))

    for match in REVISION_RE.finditer(text):
        add("revision", match.group(1), match.start(1))

    for match in ADDRESS_RE.finditer(text):
        add("address", match.group(0), match.start())

    for match in CERTIFICATE_LINE_RE.finditer(text):
        line = match.group(0).strip()
        if len(line) <= 300:
            add("certificate", line, match.start())

    for canonical, pattern in MEDIUM_VARIANTS:
        for match in pattern.finditer(text):
            add("medium", match.group(0), match.start(), normalized=canonical)

    for canonical, pattern in COMPONENT_VARIANTS:
        for match in pattern.finditer(text):
            add("component_category", match.group(0), match.start(), normalized=canonical)

    deduped: list[ExtractedEntity] = []
    seen: set[tuple[str, str, float | None, str | None]] = set()
    for entity in entities:
        key = (entity.kind, entity.normalized_value, entity.numeric_value, entity.unit)
        if key not in seen:
            seen.add(key)
            deduped.append(entity)
    return tuple(deduped)


def _page_at(lines: list[str], index: int) -> int | None:
    for position in range(index, -1, -1):
        match = PAGE_RE.match(lines[position]) or MARKDOWN_PAGE_RE.match(lines[position])
        if match:
            return int(match.group(1))
    return None


def _page_marker_at(lines: list[str], index: int) -> int | None:
    for position in range(index, -1, -1):
        if PAGE_RE.match(lines[position]) or MARKDOWN_PAGE_RE.match(lines[position]):
            return position
    return None


def _section_at(lines: list[str], index: int) -> tuple[str, ...]:
    headings: dict[int, str] = {}
    for position in range(0, index + 1):
        match = HEADING_RE.match(lines[position])
        if not match:
            continue
        level = len(match.group(1))
        headings = {key: value for key, value in headings.items() if key < level}
        headings[level] = match.group(2).strip()
    return tuple(headings[level] for level in sorted(headings))


def _table_title(lines: list[str], index: int) -> str:
    for position in range(index - 1, max(-1, index - 12), -1):
        value = lines[position].strip()
        if not value:
            continue
        heading = HEADING_RE.match(value)
        return heading.group(2).strip() if heading else value
    return "Technical data"


def _stable_chunk_id(checksum: str, kind: str, page: int | None, ordinal: int, label: str = "") -> str:
    material = f"{checksum}\0{kind}\0{page or ''}\0{ordinal}\0{normalize_for_search(label)}"
    return hashlib.sha256(material.encode("utf-8")).hexdigest()


def _table_footnotes(lines: list[str], anchor: int) -> str | None:
    """Collect qualification lines on the same source page as a table."""
    lower = anchor
    while lower > 0:
        if lower != anchor and (PAGE_RE.match(lines[lower]) or MARKDOWN_PAGE_RE.match(lines[lower])):
            break
        lower -= 1
    upper = anchor + 1
    while upper < len(lines):
        if PAGE_RE.match(lines[upper]) or MARKDOWN_PAGE_RE.match(lines[upper]):
            break
        upper += 1
    candidates = []
    for line in lines[lower:upper]:
        value = line.strip().replace("\x07", "")
        if FOOTNOTE_RE.match(value):
            candidates.append(value)
    footnotes = _unique(candidates)
    return "\n".join(footnotes) if footnotes else None


def _parse_pipe_tables(lines: list[str], checksum: str) -> tuple[list[ExtractedChunk], set[int]]:
    chunks: list[ExtractedChunk] = []
    consumed: set[int] = set()
    ordinal = 0
    index = 0
    while index + 2 < len(lines):
        header_line = lines[index].strip()
        separator = lines[index + 1].strip()
        if not (
            header_line.startswith("|")
            and header_line.endswith("|")
            and re.fullmatch(r"\|?[\s:|-]+\|?", separator)
        ):
            index += 1
            continue
        headers = tuple(cell.strip() for cell in header_line.strip("|").split("|"))
        index += 2
        while index < len(lines) and lines[index].strip().startswith("|"):
            raw = lines[index].strip()
            cells = tuple(cell.strip() for cell in raw.strip("|").split("|"))
            row_label = cells[0] if cells else f"row-{ordinal + 1}"
            units = _unique(unit for cell in headers for unit in re.findall(
                r"\b(?:bar|psi|psig|MPa|kPa|l/min|m[³3]/h|cfm|rpm|kW|kg|lb|ppm|%)\b",
                cell,
                re.IGNORECASE,
            ))
            page = _page_at(lines, index)
            section = _section_at(lines, index)
            footnotes = _table_footnotes(lines, index)
            content = (
                f"Table: {_table_title(lines, index)}\n"
                f"Headers: {' | '.join(headers)}\n"
                f"Row: {' | '.join(cells)}"
            )
            if footnotes:
                content += f"\nFootnotes: {footnotes}"
            chunk_id = _stable_chunk_id(checksum, "table_row", page, ordinal, row_label)
            chunks.append(
                ExtractedChunk(
                    chunk_id=chunk_id,
                    ordinal=ordinal,
                    chunk_kind="table_row",
                    content=content,
                    search_text=content,
                    page=page,
                    section_path=section,
                    table_title=_table_title(lines, index),
                    row_label=row_label,
                    headers=headers,
                    row_values=cells,
                    units=units,
                    footnotes=footnotes,
                    metadata={"parser": "markdown_pipe_table", "extractor_version": EXTRACTOR_VERSION},
                )
            )
            consumed.add(index)
            ordinal += 1
            index += 1
    return chunks, consumed


def _parse_flat_tables(
    lines: list[str], checksum: str, start_ordinal: int
) -> tuple[list[ExtractedChunk], set[int]]:
    row_starts: list[int] = []
    for index, line in enumerate(lines[:-3]):
        label = line.strip()
        if not MODEL_ROW_RE.match(label) or FOOTNOTE_RE.match(label):
            continue
        following = [lines[index + offset].strip() for offset in range(1, min(8, len(lines) - index))]
        if sum(bool(NUMBER_RE.match(value)) for value in following[:5]) >= 3:
            row_starts.append(index)

    chunks: list[ExtractedChunk] = []
    consumed: set[int] = set()
    for offset, row_start in enumerate(row_starts):
        row_label = lines[row_start].strip()
        values: list[str] = []
        cursor = row_start + 1
        while cursor < len(lines) and len(values) < 24:
            value = lines[cursor].strip()
            if not value:
                if values:
                    break
                cursor += 1
                continue
            if NUMBER_RE.match(value) or UNIT_RE.match(value):
                values.append(value)
                cursor += 1
                continue
            break
        if len([value for value in values if NUMBER_RE.match(value)]) < 3:
            continue

        technical_index = 0
        for position in range(row_start, -1, -1):
            if re.search(r"\b(?:technical data|technische daten|caractéristiques techniques)\b", lines[position], re.IGNORECASE):
                technical_index = position
                break
        previous_row = max((position for position in row_starts if position < row_start), default=technical_index)
        header_start = technical_index + 1 if previous_row == technical_index else max(technical_index + 1, row_start - 30)
        header_candidates = [
            line.strip()
            for line in lines[header_start:row_start]
            if line.strip() and not NUMBER_RE.match(line.strip())
        ]
        headers = tuple(header_candidates[-20:])
        page = _page_at(lines, row_start)
        page_marker = _page_marker_at(lines, row_start)
        unit_start = max(
            technical_index + 1,
            page_marker + 1 if page_marker is not None else 0,
        )
        units = _unique(
            value
            for value in (
                line.strip() for line in lines[unit_start:row_start]
            )
            if UNIT_RE.match(value)
        )
        section = _section_at(lines, row_start)
        table_title = _table_title(lines, technical_index + 1)
        footnotes = _table_footnotes(lines, row_start)
        content = (
            f"Document section: {' / '.join(section)}\n"
            f"Table: {table_title}\n"
            f"Headers and units: {' | '.join(headers)}\n"
            f"Row: {row_label}\n"
            f"Values: {' | '.join(values)}"
        )
        if footnotes:
            content += f"\nFootnotes: {footnotes}"
        ordinal = start_ordinal + len(chunks)
        chunks.append(
            ExtractedChunk(
                chunk_id=_stable_chunk_id(checksum, "table_row", page, ordinal, row_label),
                ordinal=ordinal,
                chunk_kind="table_row",
                content=content,
                search_text=content,
                page=page,
                section_path=section,
                table_title=table_title,
                row_label=row_label,
                headers=headers,
                row_values=tuple(values),
                units=units,
                footnotes=footnotes,
                metadata={"parser": "flattened_technical_table", "extractor_version": EXTRACTOR_VERSION},
            )
        )
        consumed.update(range(row_start, cursor))
    return chunks, consumed


def _technical_index_before(lines: list[str], row_start: int, window: int = 16) -> int | None:
    for position in range(row_start - 1, max(-1, row_start - window), -1):
        if re.search(
            r"\b(?:technical data|technische daten|caractéristiques techniques)\b",
            lines[position],
            re.IGNORECASE,
        ):
            return position
    return None


def _parse_key_value_tables(
    lines: list[str],
    checksum: str,
    start_ordinal: int,
    already_consumed: set[int],
) -> tuple[list[ExtractedChunk], set[int]]:
    """Parse web-derived technical tables expressed as alternating field/value lines."""
    chunks: list[ExtractedChunk] = []
    consumed: set[int] = set()
    stop_re = re.compile(
        r"^(?:details(?:\s*&\s*features)?|downloads?|support|contact|technical data)$",
        re.IGNORECASE,
    )
    for row_start, raw_label in enumerate(lines):
        row_label = raw_label.strip()
        is_product_row = bool(
            MODEL_ROW_RE.match(row_label)
            or re.fullmatch(
                r"B-[A-Z][A-Z0-9 -]{1,40}(?:I|II|III|IV|V|VI)",
                row_label,
                re.IGNORECASE,
            )
        )
        if (
            row_start in already_consumed
            or not is_product_row
            or FOOTNOTE_RE.match(row_label)
        ):
            continue
        technical_index = _technical_index_before(lines, row_start)
        if technical_index is None:
            continue
        headers: list[str] = []
        values: list[str] = []
        cursor = row_start + 1
        while cursor + 1 < len(lines) and len(headers) < 20:
            header = lines[cursor].strip()
            value = lines[cursor + 1].strip()
            if (
                not header
                or not value
                or stop_re.match(header)
                or PAGE_RE.match(header)
                or MARKDOWN_PAGE_RE.match(header)
                or HEADING_RE.match(header)
            ):
                break
            headers.append(header)
            values.append(value)
            cursor += 2
        if len(headers) < 3:
            continue
        page = _page_at(lines, row_start)
        section = _section_at(lines, row_start)
        footnotes = _table_footnotes(lines, row_start)
        units = _unique(
            unit
            for value in (*headers, *values)
            for unit in re.findall(
                r"\b(?:bar|psi|psig|MPa|kPa|l/min|m[³3]/h|cfm|rpm|kW|kg|lb|ppm|%)\b",
                value,
                re.IGNORECASE,
            )
        )
        content = (
            f"Document section: {' / '.join(section)}\n"
            f"Table: {_table_title(lines, technical_index + 1)}\n"
            f"Row: {row_label}\n"
            + "\n".join(
                f"{header}: {value}" for header, value in zip(headers, values)
            )
        )
        if footnotes:
            content += f"\nFootnotes: {footnotes}"
        ordinal = start_ordinal + len(chunks)
        chunks.append(
            ExtractedChunk(
                chunk_id=_stable_chunk_id(
                    checksum, "table_row", page, ordinal, row_label
                ),
                ordinal=ordinal,
                chunk_kind="table_row",
                content=content,
                search_text=content,
                page=page,
                section_path=section,
                table_title=_table_title(lines, technical_index + 1),
                row_label=row_label,
                headers=tuple(headers),
                row_values=tuple(values),
                units=units,
                footnotes=footnotes,
                metadata={
                    "parser": "key_value_technical_table",
                    "extractor_version": EXTRACTOR_VERSION,
                },
            )
        )
        consumed.update(range(row_start, cursor))
    return chunks, consumed


def _parse_identifier_tables(
    lines: list[str],
    checksum: str,
    start_ordinal: int,
    already_consumed: set[int],
) -> tuple[list[ExtractedChunk], set[int]]:
    """Preserve rows whose primary key is an exact N-number/order number."""
    chunks: list[ExtractedChunk] = []
    consumed: set[int] = set()
    page_start = 0
    previous_identifier: int | None = None
    for index, line in enumerate(lines):
        if PAGE_RE.match(line) or MARKDOWN_PAGE_RE.match(line):
            page_start = index
            previous_identifier = None
            continue
        identifier = line.strip()
        if not re.fullmatch(r"N\d{3,7}(?:[_-]\d+)?", identifier, re.IGNORECASE):
            continue
        header_index = next(
            (
                position
                for position in range(index - 1, max(page_start - 1, index - 100), -1)
                if re.fullmatch(
                    r"(?:order|article|part)\s*(?:number|no\.?|nr\.?)",
                    lines[position].strip(),
                    re.IGNORECASE,
                )
            ),
            None,
        )
        if header_index is None:
            continue
        row_start = (
            previous_identifier + 1
            if previous_identifier is not None and previous_identifier >= header_index
            else header_index + 1
        )
        row_values = tuple(
            value
            for value in (item.strip() for item in lines[row_start : index + 1])
            if value
        )[-12:]
        if len(row_values) < 2:
            previous_identifier = index
            continue
        header_start = max(page_start, header_index - 8)
        headers = tuple(
            value
            for value in (
                item.strip() for item in lines[header_start : header_index + 1]
            )
            if value
        )
        page = _page_at(lines, index)
        section = _section_at(lines, index)
        footnotes = _table_footnotes(lines, index)
        units = _unique(
            unit
            for value in (*headers, *row_values)
            for unit in re.findall(
                r"\b(?:bar|psi|psig|MPa|kPa|l/min|m[³3]/h|cfm|rpm|kW|kg|lb|ppm|%)\b",
                value,
                re.IGNORECASE,
            )
        )
        content = (
            f"Document section: {' / '.join(section)}\n"
            f"Table: {' | '.join(headers)}\n"
            f"Row identifier: {identifier}\n"
            f"Values: {' | '.join(row_values)}"
        )
        if footnotes:
            content += f"\nFootnotes: {footnotes}"
        ordinal = start_ordinal + len(chunks)
        chunks.append(
            ExtractedChunk(
                chunk_id=_stable_chunk_id(
                    checksum, "table_row", page, ordinal, identifier
                ),
                ordinal=ordinal,
                chunk_kind="table_row",
                content=content,
                search_text=content,
                page=page,
                section_path=section,
                table_title=" | ".join(headers),
                row_label=identifier,
                headers=headers,
                row_values=row_values,
                units=units,
                footnotes=footnotes,
                metadata={
                    "parser": "identifier_table",
                    "extractor_version": EXTRACTOR_VERSION,
                },
            )
        )
        consumed.update(range(row_start, index + 1))
        previous_identifier = index
    return chunks, consumed


def _split_prose(
    lines: list[str],
    checksum: str,
    consumed: set[int],
    start_ordinal: int,
    *,
    max_chars: int = 1400,
    overlap_chars: int = 160,
) -> list[ExtractedChunk]:
    chunks: list[ExtractedChunk] = []
    buffer: list[tuple[int, str]] = []

    def flush(*, retain_overlap: bool = True) -> None:
        nonlocal buffer
        if not buffer:
            return
        text = "\n".join(value for _, value in buffer).strip()
        if not text:
            buffer = []
            return
        first_index = buffer[0][0]
        page = _page_at(lines, first_index)
        section = _section_at(lines, first_index)
        ordinal = start_ordinal + len(chunks)
        chunks.append(
            ExtractedChunk(
                chunk_id=_stable_chunk_id(checksum, "prose", page, ordinal, text[:80]),
                ordinal=ordinal,
                chunk_kind="prose",
                content=text,
                search_text=f"{' / '.join(section)}\n{text}",
                page=page,
                section_path=section,
                metadata={"parser": "structure_aware_prose", "extractor_version": EXTRACTOR_VERSION},
            )
        )
        if retain_overlap and len(text) > overlap_chars:
            overlap = text[-overlap_chars:]
            buffer = [(buffer[-1][0], overlap)]
        else:
            buffer = []

    for index, line in enumerate(lines):
        if index in consumed:
            continue
        value = line.rstrip()
        if (PAGE_RE.match(value) or MARKDOWN_PAGE_RE.match(value) or HEADING_RE.match(value)) and buffer:
            # Do not carry overlap across a page or section boundary. Retaining
            # the previous line index attributed page N+1 content to page N.
            flush(retain_overlap=False)
        projected = sum(len(item[1]) + 1 for item in buffer) + len(value)
        if projected > max_chars and buffer:
            flush()
        buffer.append((index, value))
    flush()
    return chunks


def parse_markdown_document(
    *,
    text: str,
    file_id: str,
    checksum: str,
    filename: str,
    namespace: str,
    source_type: str = "public_document",
) -> ParsedDocument:
    lines = text.replace("\r\n", "\n").replace("\r", "\n").split("\n")
    title = None
    for line in lines:
        match = HEADING_RE.match(line)
        if match:
            title = match.group(2).strip()
            break
    source_path = _first_match(
        re.compile(r"(?im)^\s*-\s*Original source:\s*`?(.+?)`?\s*$"), text, 1
    )
    original_checksum = _first_match(
        re.compile(r"(?im)^\s*-\s*Original SHA-256:\s*`?([a-f0-9]{64})`?\s*$"), text, 1
    )
    document_number = _first_match(DOCUMENT_NUMBER_RE, f"{filename}\n{text[:5000]}")
    revision = _first_match(REVISION_RE, text, 1)
    publication_match = PUBLICATION_DATE_RE.search(filename) or PUBLICATION_DATE_RE.search(
        text[:3000]
    )
    publication_date = (
        f"{publication_match.group(1)}-{int(publication_match.group(2)):02d}-01"
        if publication_match
        else None
    )
    certificate_lines = _unique(match.group(0) for match in CERTIFICATE_LINE_RE.finditer(text))
    primary_certificate = max(
        certificate_lines,
        key=lambda value: (
            bool(STANDARD_RE.search(value)),
            bool(ADDRESS_RE.search(value)),
            len(value),
        ),
        default=None,
    )
    address = _first_match(ADDRESS_RE, text)
    standards = _unique(match.group(0) for match in STANDARD_RE.finditer(text))
    product_families = _unique(match.group(0) for match in MODEL_RE.finditer(f"{title or ''}\n{text}"))

    pipe_chunks, pipe_consumed = _parse_pipe_tables(lines, checksum)
    flat_chunks, flat_consumed = _parse_flat_tables(lines, checksum, len(pipe_chunks))
    key_value_chunks, key_value_consumed = _parse_key_value_tables(
        lines,
        checksum,
        len(pipe_chunks) + len(flat_chunks),
        pipe_consumed | flat_consumed,
    )
    identifier_chunks, identifier_consumed = _parse_identifier_tables(
        lines,
        checksum,
        len(pipe_chunks) + len(flat_chunks) + len(key_value_chunks),
        pipe_consumed | flat_consumed | key_value_consumed,
    )
    table_chunks = (
        pipe_chunks + flat_chunks + key_value_chunks + identifier_chunks
    )
    prose_chunks = _split_prose(
        lines,
        checksum,
        pipe_consumed | flat_consumed | key_value_consumed | identifier_consumed,
        len(table_chunks),
    )
    chunks = table_chunks + prose_chunks
    if title:
        chunks = [
            replace(
                chunk,
                content=f"Document: {title}\n{chunk.content}",
                search_text=f"Document: {title}\n{chunk.search_text}",
            )
            for chunk in chunks
        ]

    entities: list[ExtractedEntity] = list(
        extract_entities(f"{filename}\n{title or ''}\n{text}")
    )
    for chunk in chunks:
        entities.extend(extract_entities(chunk.content, page=chunk.page, chunk_id=chunk.chunk_id))
    entity_keys: set[tuple[str, str, str | None]] = set()
    unique_entities: list[ExtractedEntity] = []
    for entity in entities:
        key = (entity.kind, entity.normalized_value, entity.source_span.get("chunk_id"))
        if key not in entity_keys:
            entity_keys.add(key)
            unique_entities.append(entity)

    return ParsedDocument(
        file_id=file_id,
        checksum=checksum,
        filename=filename,
        namespace=namespace,
        title=title,
        language=detect_language(filename, text),
        source_type=source_type,
        source_path=source_path,
        document_number=document_number,
        certificate=primary_certificate,
        certificates=certificate_lines,
        revision=revision,
        publication_date=publication_date,
        organization="BAUER KOMPRESSOREN" if re.search(r"BAUER\s+KOMPRESSOREN", text, re.IGNORECASE) else None,
        address=address,
        product_families=product_families,
        media=_unique(
            entity.normalized_value for entity in unique_entities if entity.kind == "medium"
        ),
        component_categories=_unique(
            entity.normalized_value
            for entity in unique_entities
            if entity.kind == "component_category"
        ),
        standards=standards,
        chunks=tuple(chunks),
        entities=tuple(unique_entities),
        metadata={
            "extractor_version": EXTRACTOR_VERSION,
            "original_checksum": original_checksum,
            "table_row_count": len(table_chunks),
            "prose_chunk_count": len(prose_chunks),
        },
    )
