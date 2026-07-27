from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from enum import StrEnum
from typing import Any


class RetrievalChannel(StrEnum):
    EXACT = "exact"
    FACT = "fact"
    TABLE = "table"
    LEXICAL = "lexical"
    SEMANTIC = "semantic"
    NAVIGATION = "navigation"


class NumericComparator(StrEnum):
    EQUAL = "eq"
    GREATER_THAN = "gt"
    GREATER_THAN_OR_EQUAL = "gte"
    LESS_THAN = "lt"
    LESS_THAN_OR_EQUAL = "lte"
    BETWEEN = "between"


@dataclass(frozen=True, slots=True)
class NumericMention:
    raw: str
    value: Decimal
    unit: str | None


@dataclass(frozen=True, slots=True)
class NumericConstraint:
    raw: str
    comparator: NumericComparator
    lower_value: Decimal
    upper_value: Decimal | None = None
    unit: str | None = None

    def __post_init__(self) -> None:
        if self.comparator is NumericComparator.BETWEEN:
            if self.upper_value is None:
                raise ValueError("between constraints require an upper value")
            if self.lower_value > self.upper_value:
                raise ValueError("between constraint bounds must be ordered")
        elif self.upper_value is not None:
            raise ValueError("only between constraints may have an upper value")


@dataclass(frozen=True, slots=True)
class QueryPlan:
    query: str
    normalized_query: str
    tokens: tuple[str, ...]
    identifiers: tuple[str, ...]
    quoted_phrases: tuple[str, ...]
    numeric_mentions: tuple[NumericMention, ...]
    numeric_constraints: tuple[NumericConstraint, ...]
    channels: tuple[RetrievalChannel, ...]
    table_intent: bool
    superlative: str | None
    mandatory_constraints: dict[str, tuple[str, ...]]
    mandatory_text_constraints: dict[str, tuple[str, ...]]
    mandatory_numeric_constraints: dict[str, tuple[NumericConstraint, ...]]
    forbidden_claim_values: tuple[str, ...]
    forbidden_numeric_constraints: tuple[NumericConstraint, ...]
    constraint_failure: str | None
    top_k: int


_QUOTED_RE = re.compile(r"[\"“„](.{2,200}?)[\"”]", re.DOTALL)
_PREFIXED_CODE_TOKEN = (
    r"(?=[A-Z0-9._/+_-]{2,39}\b)"
    r"(?=[A-Z0-9._/+_-]*\d)"
    r"[A-Z0-9][A-Z0-9._/+_-]*"
)
_IDENTIFIER_RE = re.compile(
    rf"\b(?:"
    rf"(?:DOC-|SYN-){_PREFIXED_CODE_TOKEN}"
    rf"|I\s+{_PREFIXED_CODE_TOKEN}"
    rf"|K\s+{_PREFIXED_CODE_TOKEN}"
    rf"|(?:BM|PE|GIB|GI)\s*{_PREFIXED_CODE_TOKEN}"
    rf"|B-[A-Z][A-Z0-9]{{1,29}}(?:-[A-Z0-9]{{1,20}})*"
    rf"|N{_PREFIXED_CODE_TOKEN}"
    rf"|(?=[A-Z0-9._/+_-]{{3,40}}\b)"
    rf"(?=[A-Z0-9._/+_-]*[A-Z])"
    rf"(?=[A-Z0-9._/+_-]*\d)"
    rf"[A-Z0-9][A-Z0-9._/+_-]*"
    rf")\b",
    re.IGNORECASE,
)
_NUMBER_PATTERN = r"\d+(?:[.,]\d+)?"
_UNIT_PATTERN = (
    r"bar(?:g)?|psi(?:g)?|mpa|kpa|pa|l/min|l\s*min-?1|"
    r"m(?:\u00b3|3)/h|cfm|kw|w|rpm|min-?1|ppm|%|\u00b0c|c"
)
_NUMBER_UNIT_RE = re.compile(
    rf"(?<![\w.])(?P<number>{_NUMBER_PATTERN})\s*"
    rf"(?P<unit>{_UNIT_PATTERN})?(?!\w)",
    re.IGNORECASE,
)
_BETWEEN_RE = re.compile(
    rf"\bbetween\s+(?P<lower>{_NUMBER_PATTERN})\s*"
    rf"(?P<lower_unit>{_UNIT_PATTERN})?\s+"
    rf"(?:and|to)\s+(?P<upper>{_NUMBER_PATTERN})\s*"
    rf"(?P<upper_unit>{_UNIT_PATTERN})?(?!\w)",
    re.IGNORECASE,
)
_COMPARISON_RE = re.compile(
    rf"(?P<comparator>"
    rf">=|=>|<=|=<|>|<|"
    rf"greater\s+than\s+or\s+equal\s+to|"
    rf"less\s+than\s+or\s+equal\s+to|"
    rf"at\s+least|no\s+less\s+than|"
    rf"at\s+most|no\s+more\s+than|up\s+to|"
    rf"greater\s+than|more\s+than|above|over|"
    rf"less\s+than|fewer\s+than|below|under"
    rf")\s*(?P<number>{_NUMBER_PATTERN})\s*"
    rf"(?P<unit>{_UNIT_PATTERN})?(?!\w)",
    re.IGNORECASE,
)
_TOKEN_RE = re.compile(r"[\w°³/+.-]+", re.UNICODE)
_TABLE_TERMS = {
    "table",
    "row",
    "column",
    "specification",
    "specifications",
    "technical data",
    "daten",
    "tabelle",
    "zeile",
    "bestellnummer",
    "order number",
}
_FACT_TERMS = {
    "pressure",
    "capacity",
    "temperature",
    "flow",
    "power",
    "weight",
    "dimension",
    "revision",
    "standard",
    "certificate",
    "druck",
    "leistung",
    "gewicht",
    "temperatur",
}
_NAVIGATION_TERMS = {
    "category",
    "family",
    "product range",
    "overview",
    "which documents",
    "kategorie",
    "produktfamilie",
    "übersicht",
}
_MAX_TERMS = {"maximum", "max", "highest", "größte", "höchste", "maximal"}
_MIN_TERMS = {"minimum", "min", "lowest", "kleinste", "niedrigste", "minimal"}
_STOP_WORDS = {
    "a",
    "an",
    "and",
    "are",
    "der",
    "die",
    "das",
    "ein",
    "eine",
    "for",
    "für",
    "in",
    "is",
    "of",
    "the",
    "to",
    "und",
    "was",
    "what",
    "which",
    "wie",
}


def normalize_text(value: str) -> str:
    normalized = unicodedata.normalize("NFKC", value).casefold()
    normalized = re.sub(r"[^\w°³/+.-]+", " ", normalized, flags=re.UNICODE)
    return " ".join(normalized.split())


_COMPARATORS = {
    ">": NumericComparator.GREATER_THAN,
    "greater than": NumericComparator.GREATER_THAN,
    "more than": NumericComparator.GREATER_THAN,
    "above": NumericComparator.GREATER_THAN,
    "over": NumericComparator.GREATER_THAN,
    ">=": NumericComparator.GREATER_THAN_OR_EQUAL,
    "=>": NumericComparator.GREATER_THAN_OR_EQUAL,
    "greater than or equal to": NumericComparator.GREATER_THAN_OR_EQUAL,
    "at least": NumericComparator.GREATER_THAN_OR_EQUAL,
    "no less than": NumericComparator.GREATER_THAN_OR_EQUAL,
    "<": NumericComparator.LESS_THAN,
    "less than": NumericComparator.LESS_THAN,
    "fewer than": NumericComparator.LESS_THAN,
    "below": NumericComparator.LESS_THAN,
    "under": NumericComparator.LESS_THAN,
    "<=": NumericComparator.LESS_THAN_OR_EQUAL,
    "=<": NumericComparator.LESS_THAN_OR_EQUAL,
    "less than or equal to": NumericComparator.LESS_THAN_OR_EQUAL,
    "at most": NumericComparator.LESS_THAN_OR_EQUAL,
    "no more than": NumericComparator.LESS_THAN_OR_EQUAL,
    "up to": NumericComparator.LESS_THAN_OR_EQUAL,
}


def _decimal(value: str) -> Decimal | None:
    try:
        return Decimal(value.replace(",", "."))
    except InvalidOperation:
        return None


def _overlaps(span: tuple[int, int], spans: list[tuple[int, int]]) -> bool:
    return any(span[0] < existing[1] and existing[0] < span[1] for existing in spans)


def _dedupe_numeric_constraints(
    constraints: list[NumericConstraint],
) -> tuple[NumericConstraint, ...]:
    unique: list[NumericConstraint] = []
    seen: set[tuple[object, ...]] = set()
    for constraint in constraints:
        key = (
            constraint.comparator,
            constraint.lower_value,
            constraint.upper_value,
            constraint.unit,
        )
        if key not in seen:
            seen.add(key)
            unique.append(constraint)
    return tuple(unique)


def _parse_numeric_expression(
    value: str,
    *,
    include_unqualified_equality: bool = False,
) -> tuple[
    tuple[NumericMention, ...],
    tuple[NumericConstraint, ...],
    tuple[str, ...],
]:
    mentions: list[NumericMention] = []
    for match in _NUMBER_UNIT_RE.finditer(value):
        number = _decimal(match.group("number"))
        if number is None:
            continue
        unit = normalize_text(match.group("unit") or "") or None
        mentions.append(
            NumericMention(
                raw=match.group(0).strip(),
                value=number,
                unit=unit,
            )
        )

    located_constraints: list[tuple[int, NumericConstraint]] = []
    consumed_spans: list[tuple[int, int]] = []
    errors: list[str] = []
    for match in _BETWEEN_RE.finditer(value):
        span = match.span()
        consumed_spans.append(span)
        lower = _decimal(match.group("lower"))
        upper = _decimal(match.group("upper"))
        if lower is None or upper is None:
            errors.append("between constraint contains an invalid number")
            continue
        lower_unit = normalize_text(match.group("lower_unit") or "") or None
        upper_unit = normalize_text(match.group("upper_unit") or "") or None
        if lower_unit and upper_unit and lower_unit != upper_unit:
            errors.append(
                "between constraint uses different units; unit conversion is not implicit"
            )
            continue
        if lower > upper:
            errors.append("between constraint lower bound exceeds its upper bound")
            continue
        located_constraints.append(
            (
                span[0],
                NumericConstraint(
                    raw=match.group(0).strip(),
                    comparator=NumericComparator.BETWEEN,
                    lower_value=lower,
                    upper_value=upper,
                    unit=lower_unit or upper_unit,
                ),
            )
        )

    for match in _COMPARISON_RE.finditer(value):
        span = match.span()
        if _overlaps(span, consumed_spans):
            continue
        consumed_spans.append(span)
        number = _decimal(match.group("number"))
        if number is None:
            errors.append("numeric comparison contains an invalid number")
            continue
        raw_comparator = " ".join(match.group("comparator").casefold().split())
        comparator = _COMPARATORS.get(raw_comparator)
        if comparator is None:  # pragma: no cover - regex and map stay in lockstep
            errors.append(f"unsupported numeric comparator: {raw_comparator}")
            continue
        located_constraints.append(
            (
                span[0],
                NumericConstraint(
                    raw=match.group(0).strip(),
                    comparator=comparator,
                    lower_value=number,
                    unit=normalize_text(match.group("unit") or "") or None,
                ),
            )
        )

    normalized_value = value.strip()
    unqualified_is_entire_value = bool(
        re.fullmatch(rf"\s*{_NUMBER_PATTERN}\s*", normalized_value)
    )
    for match in _NUMBER_UNIT_RE.finditer(value):
        span = match.span()
        if _overlaps(span, consumed_spans):
            continue
        number = _decimal(match.group("number"))
        if number is None:
            continue
        unit = normalize_text(match.group("unit") or "") or None
        if unit is None and not (
            include_unqualified_equality and unqualified_is_entire_value
        ):
            continue
        located_constraints.append(
            (
                span[0],
                NumericConstraint(
                    raw=match.group(0).strip(),
                    comparator=NumericComparator.EQUAL,
                    lower_value=number,
                    unit=unit,
                ),
            )
        )

    located_constraints.sort(key=lambda item: item[0])
    return (
        tuple(mentions),
        _dedupe_numeric_constraints(
            [constraint for _, constraint in located_constraints]
        ),
        tuple(dict.fromkeys(errors)),
    )


def numeric_constraint_satisfied(
    constraint: NumericConstraint,
    *,
    value: Decimal,
    unit: str | None,
) -> bool:
    normalized_unit = normalize_text(unit or "") or None
    if constraint.unit is not None and normalized_unit != constraint.unit:
        return False
    if constraint.comparator is NumericComparator.EQUAL:
        return value == constraint.lower_value
    if constraint.comparator is NumericComparator.GREATER_THAN:
        return value > constraint.lower_value
    if constraint.comparator is NumericComparator.GREATER_THAN_OR_EQUAL:
        return value >= constraint.lower_value
    if constraint.comparator is NumericComparator.LESS_THAN:
        return value < constraint.lower_value
    if constraint.comparator is NumericComparator.LESS_THAN_OR_EQUAL:
        return value <= constraint.lower_value
    if constraint.comparator is NumericComparator.BETWEEN:
        assert constraint.upper_value is not None
        return constraint.lower_value <= value <= constraint.upper_value
    return False  # pragma: no cover - exhaustive enum guard


def extract_numeric_mentions(value: str) -> tuple[NumericMention, ...]:
    mentions, _, _ = _parse_numeric_expression(value)
    return mentions


def _numeric_constraint_label(constraint: NumericConstraint) -> str:
    unit = f" {constraint.unit}" if constraint.unit else ""
    lower = str(constraint.lower_value)
    if constraint.comparator is NumericComparator.BETWEEN:
        return f"between {lower} and {constraint.upper_value}{unit}"
    labels = {
        NumericComparator.EQUAL: "equal to",
        NumericComparator.GREATER_THAN: "greater than",
        NumericComparator.GREATER_THAN_OR_EQUAL: "at least",
        NumericComparator.LESS_THAN: "less than",
        NumericComparator.LESS_THAN_OR_EQUAL: "at most",
    }
    return f"{labels[constraint.comparator]} {lower}{unit}"


def analyze_query(
    query: str,
    *,
    mandatory_constraints: dict[str, Any] | None = None,
    forbidden_claim_values: tuple[str, ...] = (),
    top_k: int = 8,
) -> QueryPlan:
    if not query or not query.strip():
        raise ValueError("query is required")
    if len(query) > 4000:
        raise ValueError("query exceeds 4,000 characters")
    if not 1 <= top_k <= 20:
        raise ValueError("top_k must be between 1 and 20")

    normalized = normalize_text(query)
    tokens = tuple(
        token
        for token in (normalize_text(item) for item in _TOKEN_RE.findall(query))
        if token and token not in _STOP_WORDS
    )
    identifiers = tuple(
        dict.fromkeys(
            match.group(0).strip(" .,-").upper()
            for match in _IDENTIFIER_RE.finditer(query)
        )
    )
    quoted = tuple(
        dict.fromkeys(
            phrase
            for phrase in (normalize_text(match.group(1)) for match in _QUOTED_RE.finditer(query))
            if phrase
        )
    )
    numbers, numeric_constraints, query_constraint_errors = (
        _parse_numeric_expression(query)
    )

    table_intent = any(term in normalized for term in _TABLE_TERMS)
    fact_intent = bool(numbers) or any(term in normalized for term in _FACT_TERMS)
    navigation_intent = any(term in normalized for term in _NAVIGATION_TERMS)
    superlative = None
    if any(term in normalized.split() for term in _MAX_TERMS):
        superlative = "maximum"
        fact_intent = True
    elif any(term in normalized.split() for term in _MIN_TERMS):
        superlative = "minimum"
        fact_intent = True

    channels: list[RetrievalChannel] = []
    if identifiers or quoted:
        channels.append(RetrievalChannel.EXACT)
    if fact_intent:
        channels.append(RetrievalChannel.FACT)
    if table_intent or numeric_constraints:
        channels.append(RetrievalChannel.TABLE)
    channels.extend((RetrievalChannel.LEXICAL, RetrievalChannel.SEMANTIC))
    if navigation_intent and not (
        identifiers or numbers or table_intent
    ):
        channels.append(RetrievalChannel.NAVIGATION)

    normalized_constraints: dict[str, tuple[str, ...]] = {}
    mandatory_text_constraints: dict[str, tuple[str, ...]] = {}
    mandatory_numeric_constraints: dict[
        str, tuple[NumericConstraint, ...]
    ] = {}
    constraint_errors = list(query_constraint_errors)
    for name, raw_value in (mandatory_constraints or {}).items():
        values: tuple[object, ...]
        if isinstance(raw_value, set):
            values = tuple(sorted(raw_value, key=lambda item: str(item)))
        elif isinstance(raw_value, (list, tuple)):
            values = tuple(raw_value)
        else:
            values = (raw_value,)
        normalized_values_list: list[str] = []
        text_values: list[str] = []
        numeric_alternatives: list[NumericConstraint] = []
        for item in values:
            if item is None:
                continue
            raw_item = str(item)
            normalized_item = normalize_text(raw_item)
            if not normalized_item:
                continue
            _, parsed, errors = _parse_numeric_expression(
                raw_item,
                include_unqualified_equality=True,
            )
            constraint_errors.extend(
                f"mandatory constraint {name!s}: {error}" for error in errors
            )
            if parsed:
                if len(parsed) != 1:
                    constraint_errors.append(
                        f"mandatory constraint {name!s} has an ambiguous numeric alternative"
                    )
                    continue
                numeric_alternatives.append(parsed[0])
                normalized_values_list.append(
                    _numeric_constraint_label(parsed[0])
                )
            else:
                text_values.append(normalized_item)
                normalized_values_list.append(normalized_item)
        if text_values and numeric_alternatives:
            constraint_errors.append(
                f"mandatory constraint {name!s} mixes text and numeric alternatives"
            )
        normalized_values = tuple(dict.fromkeys(normalized_values_list))
        if normalized_values:
            normalized_constraints[str(name)] = normalized_values
        if text_values:
            mandatory_text_constraints[str(name)] = tuple(
                dict.fromkeys(text_values)
            )
        if numeric_alternatives:
            mandatory_numeric_constraints[str(name)] = (
                _dedupe_numeric_constraints(numeric_alternatives)
            )

    normalized_forbidden: list[str] = []
    forbidden_numeric: list[NumericConstraint] = []
    for item in forbidden_claim_values:
        normalized_item = normalize_text(item)
        if normalized_item:
            normalized_forbidden.append(normalized_item)
        _, parsed, errors = _parse_numeric_expression(
            item,
            include_unqualified_equality=True,
        )
        constraint_errors.extend(
            f"forbidden claim value: {error}" for error in errors
        )
        forbidden_numeric.extend(parsed)
    if (
        numeric_constraints
        or mandatory_numeric_constraints
        or forbidden_numeric
    ):
        if RetrievalChannel.FACT not in channels:
            channels.append(RetrievalChannel.FACT)
        if RetrievalChannel.TABLE not in channels:
            channels.append(RetrievalChannel.TABLE)

    return QueryPlan(
        query=query.strip(),
        normalized_query=normalized,
        tokens=tokens,
        identifiers=identifiers,
        quoted_phrases=quoted,
        numeric_mentions=numbers,
        numeric_constraints=numeric_constraints,
        channels=tuple(dict.fromkeys(channels)),
        table_intent=table_intent,
        superlative=superlative,
        mandatory_constraints=normalized_constraints,
        mandatory_text_constraints=mandatory_text_constraints,
        mandatory_numeric_constraints=mandatory_numeric_constraints,
        forbidden_claim_values=tuple(dict.fromkeys(normalized_forbidden)),
        forbidden_numeric_constraints=_dedupe_numeric_constraints(
            forbidden_numeric
        ),
        constraint_failure=(
            "; ".join(dict.fromkeys(constraint_errors))
            if constraint_errors
            else None
        ),
        top_k=top_k,
    )
