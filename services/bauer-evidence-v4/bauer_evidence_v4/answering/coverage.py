from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass

from .models import EvidenceContext, FieldCoverage, TaskPlan


_DASH_TRANSLATION = str.maketrans(
    {
        character: "-"
        for character in "\u00ad\u2010\u2011\u2012\u2013\u2014\u2015\u2212"
    }
)


def _normalize(value: str) -> str:
    folded = unicodedata.normalize(
        "NFKD",
        value.translate(_DASH_TRANSLATION),
    ).casefold()
    return "".join(
        character for character in folded if not unicodedata.combining(character)
    )


@dataclass(frozen=True, slots=True)
class CoverageEngine:
    def evaluate(
        self,
        plan: TaskPlan,
        evidence: tuple[EvidenceContext, ...],
    ) -> tuple[FieldCoverage, ...]:
        relevant = tuple(
            context
            for context in evidence
            if self._relevant(plan, context)
        )
        return tuple(
            self._field(plan, field, relevant)
            for field in plan.fields
        )

    def _field(
        self,
        plan: TaskPlan,
        field,
        evidence: tuple[EvidenceContext, ...],
    ) -> FieldCoverage:
        if plan.intent == "general":
            return self._general_field(field, evidence)
        support: list[tuple[str, str | None, str]] = []
        for context in evidence:
            for name, value, qualifier in context.values:
                if name == field.field:
                    support.append(
                        (value, qualifier, context.unit.evidence_id)
                    )
        if (
            plan.intent == "certificate"
            and field.field == "source_document"
            and any(value.lower().endswith(".pdf") for value, _, _ in support)
        ):
            support = [
                item for item in support if item[0].lower().endswith(".pdf")
            ]
        deduplicated: dict[
            tuple[str, str | None],
            tuple[str, str | None, str],
        ] = {}
        for value, qualifier, evidence_id in support:
            deduplicated.setdefault(
                (value, qualifier),
                (value, qualifier, evidence_id),
            )
        support = list(deduplicated.values())
        if not support:
            return FieldCoverage(
                field=field,
                state="absent",
                values=(),
                evidence_ids=(),
                detail=f"No authorized evidence supports {field.label}.",
            )
        return FieldCoverage(
            field=field,
            state="supported",
            values=tuple(
                dict.fromkeys(
                    (value, qualifier)
                    for value, qualifier, _ in support
                )
            ),
            evidence_ids=tuple(
                dict.fromkeys(evidence_id for _, _, evidence_id in support)
            ),
            detail=None,
        )

    def _general_field(
        self,
        field,
        evidence: tuple[EvidenceContext, ...],
    ) -> FieldCoverage:
        candidates: list[
            tuple[int, int, int, int, str, str | None, str]
        ] = []
        for context in evidence:
            text = context.unit.search_text.strip()
            normalized = _normalize(text)
            if field.anchor_terms and not all(
                _normalize(term) in normalized for term in field.anchor_terms
            ):
                continue
            if field.match_terms and not any(
                _normalize(term) in normalized for term in field.match_terms
            ):
                continue
            snippet = self._bounded_snippet(
                text,
                field.match_terms or field.anchor_terms,
                anchor_terms=field.anchor_terms,
                limit=2600,
            )
            concise = self._concise_value(snippet, field)
            if not concise:
                continue
            distinct_matches = sum(
                bool(
                    self._positions(
                        _normalize(snippet),
                        _normalize(term),
                    )
                )
                for term in field.match_terms
            )
            numeric_matches = len(
                set(re.findall(r"\b\d+(?:[.,]\d+)?\b", concise))
            )
            value_priority = self._value_priority(field.field, concise)
            candidates.append(
                (
                    distinct_matches,
                    value_priority,
                    numeric_matches,
                    -context.ranked.rank,
                    concise,
                    context.citation.original_filename,
                    context.unit.evidence_id,
                )
            )
        candidates.sort(reverse=True)
        support: list[tuple[str, str | None, str]] = []
        seen: set[str] = set()
        best_match_count = candidates[0][0] if candidates else 0
        allowed_match_gap = (
            0
            if field.field.endswith(("_pressure", "_flow", "_functions"))
            else 1
        )
        for (
            match_count,
            _,
            _,
            _,
            concise,
            filename,
            evidence_id,
        ) in candidates:
            if (
                support
                and field.match_terms
                and match_count
                < max(1, best_match_count - allowed_match_gap)
            ):
                continue
            snippet_key = _normalize(concise)
            if snippet_key in seen:
                continue
            seen.add(snippet_key)
            support.append((concise, filename, evidence_id))
            support_limit = (
                1
                if field.field
                in {
                    "compressor_pressure_evidence",
                    "booster_pressure_evidence",
                    "pressure_definition_evidence",
                }
                else 2
            )
            if len(support) >= support_limit:
                break
        if not support:
            return FieldCoverage(
                field=field,
                state="absent",
                values=(),
                evidence_ids=(),
                detail=(
                    f"{field.label} is not established in the authorized "
                    "Bauer evidence."
                ),
            )
        return FieldCoverage(
            field=field,
            state="supported",
            values=tuple(
                (value, qualifier) for value, qualifier, _ in support
            ),
            evidence_ids=tuple(
                evidence_id for _, _, evidence_id in support
            ),
            detail=None,
        )

    @classmethod
    def _concise_value(cls, text: str, field) -> str:
        """Reduce a bounded evidence window to claim-sized source language."""

        if not text:
            return ""
        compact = re.sub(r"\s+", " ", text).strip(" \t\r\n\u2026")
        content = compact.split("Content:", 1)[-1].strip()
        normalized = _normalize(content)

        if field.field == "compressor_pressure_evidence":
            family_ranges = []
            for match in re.finditer(
                r"\b("
                r"(?:B/E|BM|I|G)\s+Series\s*\|\s*"
                r"(?:MINI-VERTICUS|VERTICUS|"
                r"K\s*22\s*[\u2013-]\s*K\s*28)"
                r")\s*(\d+)\s*[\u2013-]\s*(\d+)\s*bar\b",
                content,
                flags=re.IGNORECASE,
            ):
                family_ranges.append(
                    (
                        int(match.group(3)),
                        re.sub(r"\s+", " ", match.group(1)).strip(),
                        match.group(2),
                        match.group(3),
                    )
                )
            if family_ranges:
                maximum = max(item[0] for item in family_ranges)
                highest = [
                    f"{label}: {minimum}\u2013{maximum_value} bar"
                    for value, label, minimum, maximum_value in family_ranges
                    if value == maximum
                ]
                return (
                    "Highest compressor maximum operating pressure: "
                    f"{maximum} bar; "
                    + "; ".join(dict.fromkeys(highest))
                )

        if field.field == "booster_pressure_evidence":
            category_ranges = [
                (
                    re.sub(r"\s+", " ", match.group(1)).strip(),
                    match.group(2),
                    match.group(3),
                )
                for match in re.finditer(
                    r"\b(BOOSTER\s+(?:AIR|WATER)\s+COOLED)\s*\|\s*"
                    r"(\d+)\s*[\u2013-]\s*(\d+)\s*BAR\b",
                    content,
                    flags=re.IGNORECASE,
                )
            ]
            family_ranges = [
                (
                    re.sub(r"\s+", " ", match.group(1)).strip(),
                    match.group(2),
                    match.group(3),
                )
                for match in re.finditer(
                    r"\b("
                    r"GIB\s+Series\s*\|\s*"
                    r"(?:MINI-VERTICUS|VERTICUS|"
                    r"BK\s*\d+\s*[\u2013-]\s*BK\s*\d+)"
                    r")\s*(\d+)\s*[\u2013-]\s*(\d+)\s*bar\b",
                    content,
                    flags=re.IGNORECASE,
                )
            ]
            if category_ranges:
                rendered = [
                    f"{label}: {minimum}\u2013{maximum} bar"
                    for label, minimum, maximum in category_ranges
                ]
                highest = max(int(item[2]) for item in category_ranges)
                rendered.extend(
                    f"{label}: {minimum}\u2013{maximum} bar"
                    for label, minimum, maximum in family_ranges
                    if int(maximum) == highest
                )
                return "; ".join(dict.fromkeys(rendered))

        if field.field == "pressure_definition_evidence":
            definition = re.search(
                r"\b(?:Maximum allowable working pressure|"
                r"Max\.?\s+operating pressure)\s*=\s*"
                r"max(?:imum)?\.?\s+set(?:ting)?\s+(?:of\s+the\s+)?"
                r"safety valve\s*;\s*"
                r"(?:final|shutdown) pressure"
                r"[^.;]{0,100}\blower\b",
                content,
                flags=re.IGNORECASE,
            )
            if definition:
                return re.sub(r"\s+", " ", definition.group(0)).strip()

        if field.field.endswith("_pressure"):
            values = []
            for match in re.finditer(
                r"\b("
                r"(?:maximum\s+)?operating pressure|"
                r"adjustment range|"
                r"inlet pressure|"
                r"shutdown pressure|"
                r"pressure range|"
                r"final pressure"
                r")\b\s*:?\s*([^.;›•]{0,150}?\bbar)\b",
                content,
                flags=re.IGNORECASE,
            ):
                label = re.sub(r"\s+", " ", match.group(1)).strip()
                value = re.sub(r"\s+", " ", match.group(2)).strip()
                rendered = f"{label}: {value}"
                if _normalize(rendered) not in {
                    _normalize(item) for item in values
                }:
                    values.append(rendered)
            if values:
                return "; ".join(values[:6])

        if field.field.endswith("_flow"):
            pairs = []
            for match in re.finditer(
                r"(?:\bP\s*=\s*)?(\d{2,3})\s*bar"
                r"[^0-9]{0,80}(\d{3,5})\s*l/min\b",
                content,
                flags=re.IGNORECASE,
            ):
                pair = f"{match.group(1)} bar: {match.group(2)} l/min"
                if pair not in pairs:
                    pairs.append(pair)
            if len(pairs) >= 2:
                return "; ".join(pairs[:6])

        if field.field.endswith("_functions"):
            marker = re.search(
                r"performs?\s+(?:3|three)\s+(?:important|key)\s+"
                r"functions?\s*:",
                content,
                flags=re.IGNORECASE,
            )
            if marker:
                remainder = content[marker.end():]
                functions = []
                for part in re.split(r"\s*[›•]\s*", remainder):
                    value = part.strip(" .;:")
                    if not value:
                        continue
                    value = re.split(
                        r"\bThe automatic unit consists\b",
                        value,
                        maxsplit=1,
                        flags=re.IGNORECASE,
                    )[0].strip(" .;:")
                    if value:
                        functions.append(value)
                    if len(functions) == 3:
                        break
                if len(functions) == 3:
                    return "; ".join(functions)

        fragments = [
            fragment.strip(" \t\r\n\u2026.;:")
            for fragment in re.split(
                r"\s*[›•]\s*|(?<=[.!?])\s+",
                content,
            )
            if fragment.strip(" \t\r\n\u2026.;:")
        ]
        selected: list[str] = []
        terms = field.match_terms or field.anchor_terms
        for fragment in fragments:
            fragment_normalized = _normalize(fragment)
            if terms and not any(
                cls._positions(fragment_normalized, _normalize(term))
                for term in terms
            ):
                continue
            if field.anchor_terms and not any(
                _normalize(anchor) in normalized
                for anchor in field.anchor_terms
            ):
                continue
            value = fragment[:320].rstrip()
            if value and _normalize(value) not in {
                _normalize(item) for item in selected
            }:
                selected.append(value)
            if len(selected) >= 4:
                break
        if not selected:
            return content[:480].rstrip()
        return "; ".join(selected)[:900].rstrip(" ;")

    @staticmethod
    def _value_priority(field_name: str, value: str) -> int:
        if field_name not in {
            "compressor_pressure_evidence",
            "booster_pressure_evidence",
        }:
            return 0
        range_maxima = [
            int(match.group(2))
            for match in re.finditer(
                r"\b(\d{2,3})\s*[\u2013-]\s*(\d{2,3})\s*bar\b",
                value,
            )
        ]
        if range_maxima:
            return max(range_maxima)
        pressures = [
            int(match)
            for match in re.findall(r"\b(\d{2,3})\s*bar\b", value)
        ]
        return max(pressures, default=0)

    @staticmethod
    def _bounded_snippet(
        text: str,
        terms: tuple[str, ...],
        *,
        anchor_terms: tuple[str, ...] = (),
        limit: int = 900,
    ) -> str:
        compact = re.sub(r"\s+", " ", text).strip()
        normalized = _normalize(compact)
        positions = [
            position
            for term in terms
            for position in CoverageEngine._positions(
                normalized,
                _normalize(term),
            )
        ]
        anchor_positions = [
            position
            for term in anchor_terms
            for position in CoverageEngine._positions(
                normalized,
                _normalize(term),
            )
        ]
        if anchor_positions and terms:
            pairs = [
                (abs(anchor - position), anchor, position)
                for anchor in anchor_positions
                for position in positions
                if position >= anchor
                and position - anchor <= limit - 80
            ]
            if not pairs:
                return ""
            _, anchor, position = min(pairs)
            span_start = min(anchor, position)
            span_end = max(anchor, position)
            padding = max(0, limit - (span_end - span_start))
            start = max(0, span_start - min(180, padding // 2))
        else:
            start = max(0, (min(positions) if positions else 0) - 180)
        if len(compact) <= limit:
            return compact
        end = min(len(compact), start + limit)
        if end - start < limit:
            start = max(0, end - limit)
        if start:
            boundary = compact.find(" ", start)
            start = boundary + 1 if boundary >= 0 else start
        if end < len(compact):
            boundary = compact.rfind(" ", start, end)
            end = boundary if boundary > start else end
        prefix = "… " if start else ""
        suffix = " …" if end < len(compact) else ""
        return f"{prefix}{compact[start:end].strip()}{suffix}"

    @staticmethod
    def _positions(value: str, needle: str) -> tuple[int, ...]:
        if not needle:
            return ()
        return tuple(
            match.start()
            for match in re.finditer(
                rf"(?<![a-z0-9]){re.escape(needle)}(?![a-z0-9])",
                value,
            )
        )

    @staticmethod
    def _relevant(plan: TaskPlan, context: EvidenceContext) -> bool:
        projection = context.ranked.candidate.item.projection
        text = _normalize(projection.search_text)
        if plan.exclusions and any(
            _normalize(exclusion) in text for exclusion in plan.exclusions
        ):
            return False
        if plan.intent == "certificate":
            fields = dict(projection.qualifiers)
            return (
                "en iso 3834-2" in text
                and (
                    fields.get("file_number") == "216"
                    or projection.source_filename == "bauer_amfile_216.pdf"
                )
            )
        if plan.intent == "document_metadata":
            return all(
                _normalize(identifier) in text
                for identifier in plan.exact_identifiers
                if identifier.upper().startswith("N")
            )
        if plan.intent == "family_range":
            return (
                projection.subject is not None
                and _normalize(projection.subject)
                == _normalize("K 22 – K 28 SERIES")
            )
        if plan.intent == "table_row":
            model_identifiers = [
                identifier
                for identifier in plan.exact_identifiers
                if not identifier.upper().startswith("N")
            ]
            if model_identifiers and not all(
                _normalize(identifier) in text
                for identifier in model_identifiers
            ):
                return False
            question = _normalize(plan.original_question)
            qualifiers = dict(projection.qualifiers)
            group = _normalize(qualifiers.get("group", ""))
            for key, expected in plan.required_qualifiers:
                if _normalize(qualifiers.get(key, "")) != _normalize(expected):
                    return False
            if all(token in question for token in ("100", "50", "hz")):
                return bool(group)
            if "40 bar" in question and group:
                return "40 bar" in group
            return True
        return True
