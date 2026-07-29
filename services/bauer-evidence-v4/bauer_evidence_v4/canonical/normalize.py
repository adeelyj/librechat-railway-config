from __future__ import annotations

import hashlib
import json
import re
import unicodedata
from decimal import Decimal, InvalidOperation
from typing import Any, Iterable


_NUMBER_RE = re.compile(r"^[+-]?(?:\d+(?:[.,]\d+)?|[.,]\d+)$")
_FOOTNOTE_SUFFIX_RE = re.compile(r"(?:\s*[\u00b9\u00b2\u00b3\u2070-\u2079]+)\s*$")
_DOCUMENT_NUMBER_RE = re.compile(r"(?<![A-Za-z0-9])(N\d{4,})(?!\d)", re.IGNORECASE)
_UNIT_MAP = {
    "l/min": "L/min",
    "l / min": "L/min",
    "m³/h": "m3/h",
    "m3/h": "m3/h",
    "cfm": "[ft_i]3/min",
    "bar": "bar",
    "psig": "psi",
    "psi": "psi",
    "rpm": "r/min",
    "kw": "kW",
    "kg": "kg",
    "lbs": "[lb_av]",
    "lb": "[lb_av]",
}
_UNIT_ALIASES = {
    "mł/h": "m³/h",
    "mâ³/h": "m³/h",
    "m&sup3;/h": "m³/h",
}
_SUPERSCRIPT_TRANSLATION = str.maketrans("⁰¹²³⁴⁵⁶⁷⁸⁹", "0123456789")


def clean_text(value: str) -> str:
    normalized = unicodedata.normalize("NFC", value)
    # PDF extractors can encode a visible heading hyphen as U+00AD. Keep the
    # semantic boundary so downstream section attribution is deterministic.
    normalized = normalized.replace("\u00ad", "-")
    normalized = normalized.replace("\r\n", "\n").replace("\r", "\n")
    return " ".join(normalized.split())


def stable_id(kind: str, *parts: object) -> str:
    material = "\0".join(
        ("bauer-evidence-v4", kind, *(clean_text(str(part)) for part in parts))
    )
    return f"{kind}_{hashlib.sha256(material.encode('utf-8')).hexdigest()[:32]}"


def sorted_attributes(values: dict[str, Any] | None = None) -> tuple[tuple[str, str], ...]:
    result: list[tuple[str, str]] = []
    for key, value in sorted((values or {}).items()):
        if isinstance(value, bool):
            encoded = "true" if value else "false"
        elif value is None:
            encoded = ""
        elif isinstance(value, (str, int, float)):
            encoded = str(value)
        else:
            encoded = json.dumps(
                value,
                ensure_ascii=False,
                allow_nan=False,
                sort_keys=True,
                separators=(",", ":"),
            )
        result.append((str(key), encoded))
    return tuple(result)


def normalize_unit(value: str | None) -> tuple[str | None, str | None]:
    if value is None:
        return None, None
    raw = clean_text(value)
    raw = _UNIT_ALIASES.get(raw.casefold(), raw)
    return (raw, _UNIT_MAP.get(raw.casefold()))


def numeric_value(value: str) -> tuple[str, Decimal | None]:
    text = clean_text(value)
    if not _NUMBER_RE.fullmatch(text):
        return "text", None
    normalized = text.replace(",", ".")
    try:
        number = Decimal(normalized)
    except InvalidOperation:
        return "text", None
    if number == number.to_integral_value():
        return "integer", number
    return "decimal", number


def header_label_and_markers(value: str) -> tuple[str, tuple[str, ...]]:
    text = clean_text(value)
    match = _FOOTNOTE_SUFFIX_RE.search(text)
    if not match:
        return text, ()
    suffix = match.group(0).strip()
    markers = tuple(character.translate(_SUPERSCRIPT_TRANSLATION) for character in suffix)
    return text[: match.start()].strip(), markers


def predicate_slug(value: str) -> str:
    normalized = unicodedata.normalize("NFKD", value).casefold()
    normalized = "".join(character for character in normalized if not unicodedata.combining(character))
    return re.sub(r"[^a-z0-9]+", "_", normalized).strip("_")


def document_number_from_text(*values: str | None) -> str | None:
    for value in values:
        if not value:
            continue
        match = _DOCUMENT_NUMBER_RE.search(value)
        if match:
            return match.group(1).upper()
    return None


def language_from_filename(filename: str) -> str | None:
    upper = f"_{filename.upper()}_"
    for token, language in (
        ("_EN_", "en"),
        ("_DE_", "de"),
        ("_FR_", "fr"),
        ("_IT_", "it"),
    ):
        if token in upper:
            return language
    return None


def inherited_header_paths(
    *,
    column_count: int,
    header_cells: Iterable[tuple[int, int, int, str]],
) -> tuple[tuple[str, ...], ...]:
    """Return one de-duplicated multi-row header path per table column.

    Cells are ``(row, column, column_span, text)`` tuples. Blank labels do not
    erase labels inherited through a spanning parent header.
    """

    paths: list[list[tuple[int, str]]] = [[] for _ in range(column_count)]
    for row, column, span, raw_text in sorted(header_cells):
        label, _ = header_label_and_markers(raw_text)
        if not label:
            continue
        for target in range(column, min(column_count, column + max(1, span))):
            paths[target].append((row, label))
    result: list[tuple[str, ...]] = []
    for path in paths:
        labels: list[str] = []
        for _, label in sorted(path):
            if not labels or labels[-1].casefold() != label.casefold():
                labels.append(label)
        result.append(tuple(labels))
    return tuple(result)
