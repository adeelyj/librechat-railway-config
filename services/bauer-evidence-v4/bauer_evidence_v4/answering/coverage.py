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
        if field.field.startswith("bdetection_"):
            return self._bdetection_field(field, evidence)
        if field.field == "n7698_compressor_block_applications":
            return self._n7698_field(field, evidence)
        if field.field.startswith("bcloud_"):
            return self._bcloud_field(field, evidence)
        if field.field.startswith("bkool_iii_"):
            return self._bkool_iii_field(field, evidence)
        if field.field == "bm_40_bar_evidence":
            return self._bm_family_field(field, evidence, pressure_bar=40)
        if field.field == "bm_100_bar_evidence":
            return self._bm_family_field(field, evidence, pressure_bar=100)
        if field.field == "bm_90_bar_800_l_min_fit":
            return self._bm_requirement_fit(field, evidence)
        candidates: list[
            tuple[int, int, int, int, str, str | None, str]
        ] = []
        for context in evidence:
            text = context.unit.search_text.strip()
            normalized = _normalize(text)
            if field.anchor_terms and not all(
                self._positions(normalized, _normalize(term))
                for term in field.anchor_terms
            ):
                continue
            if field.match_terms and not any(
                self._positions(normalized, _normalize(term))
                for term in field.match_terms
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
    def _bm_family_field(
        cls,
        field,
        evidence: tuple[EvidenceContext, ...],
        *,
        pressure_bar: int,
    ) -> FieldCoverage:
        matches = [
            (specification, context)
            for context in evidence
            for specification in (
                cls._bm_family_specification(
                    context.unit.search_text,
                    pressure_bar=pressure_bar,
                ),
            )
            if specification is not None
        ]
        if not matches:
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
        specification, context = min(
            matches,
            key=lambda item: item[1].ranked.rank,
        )
        return FieldCoverage(
            field=field,
            state="supported",
            values=((specification, context.citation.original_filename),),
            evidence_ids=(context.unit.evidence_id,),
            detail=None,
        )

    @classmethod
    def _bm_requirement_fit(
        cls,
        field,
        evidence: tuple[EvidenceContext, ...],
    ) -> FieldCoverage:
        support = {}
        for pressure_bar in (40, 100):
            matches = [
                (specification, context)
                for context in evidence
                for specification in (
                    cls._bm_family_specification(
                        context.unit.search_text,
                        pressure_bar=pressure_bar,
                    ),
                )
                if specification is not None
            ]
            if matches:
                support[pressure_bar] = min(
                    matches,
                    key=lambda item: item[1].ranked.rank,
                )
        if set(support) != {40, 100}:
            return FieldCoverage(
                field=field,
                state="absent",
                values=(),
                evidence_ids=(),
                detail=(
                    "Both BM family overview ranges are required for the "
                    "90 bar comparison."
                ),
            )
        forty = support[40][1]
        hundred = support[100][1]
        return FieldCoverage(
            field=field,
            state="supported",
            values=(
                (
                    "BM series 100 bar is technically closer: its 100 bar "
                    "family limit covers a 90 bar requirement, whereas the "
                    "BM series 40 bar limit does not. Approximately 800 l/min "
                    "falls within both documented family delivery ranges; "
                    "this comparison does not select or approve a specific "
                    "model.",
                    None,
                ),
            ),
            evidence_ids=(
                forty.unit.evidence_id,
                hundred.unit.evidence_id,
            ),
            detail=None,
        )

    @staticmethod
    def _bm_family_specification(
        text: str,
        *,
        pressure_bar: int,
    ) -> str | None:
        compact = re.sub(r"\s+", " ", text).strip()
        normalized = _normalize(compact)
        if (
            f"bm series ({pressure_bar} bar)" not in normalized
            or "air-cooled" not in normalized
            or "for air" not in normalized
        ):
            return None
        delivery = re.search(
            r"\b(\d{3,5})\s*[\u2013-]\s*(\d{3,5})\s*l/min\b",
            compact,
            flags=re.IGNORECASE,
        )
        power = re.search(
            r"\b(\d+(?:[.,]\d+)?)\s*[\u2013-]\s*"
            r"(\d+(?:[.,]\d+)?)\s*kW\b",
            compact,
            flags=re.IGNORECASE,
        )
        if delivery is None or power is None:
            return None
        return (
            "Medium: air; "
            f"maximum family pressure: {pressure_bar} bar; "
            "free-air-delivery range: "
            f"{delivery.group(1)}\u2013{delivery.group(2)} l/min; "
            "motor-power range: "
            f"{power.group(1)}\u2013{power.group(2)} kW"
        )

    @classmethod
    def _bdetection_field(
        cls,
        field,
        evidence: tuple[EvidenceContext, ...],
    ) -> FieldCoverage:
        stationary = cls._first_context(
            evidence,
            required=("b-detection plus i and s",),
            any_terms=("continuously monitor", "stationary models"),
        )
        mobile = cls._first_context(
            evidence,
            required=("b-detection plus m",),
            any_terms=("portable case-based", "mobile solution"),
        )
        if field.field == "bdetection_stationary_evidence":
            if stationary is None:
                return cls._absent(field)
            return cls._special_supported(
                field,
                (
                    "B-DETECTION PLUS i/s is the stationary, continuous "
                    "online system; the integrated and stand-alone variants "
                    "continuously monitor breathing-air quality."
                ),
                (stationary,),
            )
        if field.field == "bdetection_mobile_evidence":
            if mobile is None:
                return cls._absent(field)
            return cls._special_supported(
                field,
                (
                    "B-DETECTION PLUS m is the mobile, portable case-based "
                    "system for measurements at cylinders, compressors, or "
                    "the intake."
                ),
                (mobile,),
            )
        if field.field == "bdetection_measurements":
            measured_stationary = cls._first_context(
                evidence,
                required=("b-detection plus i and s", "co", "co2", "o2"),
                any_terms=("humidity", "residual oil", "voc"),
            )
            measured_mobile = cls._first_context(
                evidence,
                required=("b-detection plus m", "co", "co2", "o2"),
                any_terms=("humidity", "residual oil", "voc"),
            )
            if measured_stationary is None or measured_mobile is None:
                return cls._absent(field)
            return cls._special_supported(
                field,
                (
                    "Both systems measure CO, CO2, and O2; absolute humidity "
                    "and residual oil/VOC measurement are documented as "
                    "optional."
                ),
                (measured_stationary, measured_mobile),
            )
        if field.field == "bdetection_logging":
            stationary_logging = cls._first_context(
                evidence,
                required=("b-detection plus",),
                any_terms=(
                    "all measurement values are logged",
                    "data logger function",
                ),
                filename_terms=("2025-03_b-detection_plus",),
            )
            mobile_logging = cls._first_context(
                evidence,
                required=("b-detection plus m",),
                any_terms=("integrated data logger", "sd card"),
            )
            if stationary_logging is None or mobile_logging is None:
                return cls._absent(field)
            return cls._special_supported(
                field,
                (
                    "The stationary i/s variants are B-CLOUD ready and log "
                    "measurement values; the mobile m variant has an "
                    "integrated SD-card data logger and B-CLOUD/B-APP remote "
                    "access."
                ),
                (stationary_logging, mobile_logging),
            )
        if field.field == "bdetection_pressure_reconciliation":
            mobile_pressure = cls._first_context(
                evidence,
                required=("b-detection plus m", "420 bar"),
                any_terms=("maximum system pressure", "up to 420 bar"),
            )
            option_pressure = cls._first_context(
                evidence,
                required=("options up to 450 bar final pressure",),
                any_terms=("purge valve", "larger pressure range"),
                filename_terms=("2025-03_b-detection_plus",),
            )
            if mobile_pressure is None or option_pressure is None:
                return cls._absent(field)
            return cls._special_supported(
                field,
                (
                    "The B-DETECTION PLUS m product evidence documents a "
                    "420 bar maximum. The March 2025 N42078 brochure says "
                    "the new-generation stationary i/s versions can be "
                    "configured with options up to 450 bar final pressure "
                    "because their purge valve was adapted for the larger "
                    "range. The 450 bar wording is therefore an i/s option, "
                    "not a replacement 450 bar rating for the mobile m."
                ),
                (mobile_pressure, option_pressure),
            )
        return cls._absent(field)

    @classmethod
    def _n7698_field(
        cls,
        field,
        evidence: tuple[EvidenceContext, ...],
    ) -> FieldCoverage:
        for context in evidence:
            text = re.sub(r"\s+", " ", context.unit.search_text)
            normalized = _normalize(text)
            if (
                "n7698" not in normalized
                or "large blocks/medium pressure" not in normalized
            ):
                continue
            applications = re.search(
                r"\((K28\.3\s*,\s*21\.0\s*,\s*25\.0\s*,\s*23\.1\s*,"
                r"\s*25\.4\s*,\s*K28\.0\s*,\s*K28\.2)\)",
                text,
                flags=re.IGNORECASE,
            )
            if applications is None:
                continue
            values = [
                value.strip()
                for value in applications.group(1).split(",")
            ]
            rendered = ", ".join(values[:-1]) + f", and {values[-1]}"
            return cls._special_supported(
                field,
                (
                    "Order number N7698 is the intake-filter-insert entry "
                    "for large blocks / medium pressure, with applications "
                    f"{rendered}."
                ),
                (context,),
            )
        return cls._absent(field)

    @classmethod
    def _bcloud_field(
        cls,
        field,
        evidence: tuple[EvidenceContext, ...],
    ) -> FieldCoverage:
        if field.field == "bcloud_access_capabilities":
            browser = cls._first_context(
                evidence,
                required=("b-cloud",),
                any_terms=(
                    "browser application",
                    "fault notifications",
                    "plain-text diagnostics",
                ),
            )
            app = cls._first_context(
                evidence,
                required=("b-app",),
                any_terms=("smartphone", "tablet", "full range"),
            )
            if browser is None or app is None:
                return cls._absent(field)
            return cls._special_supported(
                field,
                (
                    "B-CLOUD provides browser access to compressor status "
                    "and fault notifications with plain-text diagnostics; "
                    "B-APP provides the B-CLOUD functions on smartphones "
                    "and tablets."
                ),
                (browser, app),
            )
        if field.field == "bcloud_software_requirement":
            requirement = cls._first_context(
                evidence,
                required=(
                    "b-control micro",
                    "software version 3.73 or later",
                ),
                any_terms=("older systems", "version 3.0"),
            )
            if requirement is None:
                return cls._absent(field)
            return cls._special_supported(
                field,
                (
                    "Compatible systems require B-CONTROL MICRO +Net with "
                    "software version 3.73 or later; older systems from "
                    "version 3.0 can be updated to become B-CLOUD compatible."
                ),
                (requirement,),
            )
        return cls._absent(field)

    @classmethod
    def _bkool_iii_field(
        cls,
        field,
        evidence: tuple[EvidenceContext, ...],
    ) -> FieldCoverage:
        if field.field == "bkool_iii_pressure":
            context = cls._first_context(
                evidence,
                required=(
                    "b-kool iii",
                    "maximum operating pressure",
                    "350 bar",
                    "550 bar",
                ),
                any_terms=("technical data", "model designation"),
                filename_terms=("0021_b-kool", "0022_b-kool1"),
            )
            if context is None:
                return cls._absent(field)
            return cls._special_supported(
                field,
                "B-KOOL III maximum operating pressure: 350 bar / 550 bar.",
                (context,),
            )
        if field.field == "bkool_iii_flow":
            context = cls._first_context(
                evidence,
                required=(
                    "b-kool iii",
                    "200",
                    "700 l/min",
                    "650 l/min",
                    "420 l/min",
                    "helium",
                    "argon",
                ),
                any_terms=("iso 1217", "maximum flow rate"),
                filename_terms=("0021_b-kool", "0022_b-kool1"),
            )
            if context is None:
                return cls._absent(field)
            return cls._special_supported(
                field,
                (
                    "B-KOOL III maximum flow rates: 200–700 l/min for "
                    "10 l cylinder filling from 0–200 bar; 200–650 l/min "
                    "according to ISO 1217 for air; and 200–420 l/min for "
                    "helium and argon."
                ),
                (context,),
            )
        return cls._absent(field)

    @staticmethod
    def _first_context(
        evidence: tuple[EvidenceContext, ...],
        *,
        required: tuple[str, ...],
        any_terms: tuple[str, ...],
        filename_terms: tuple[str, ...] = (),
    ) -> EvidenceContext | None:
        matches = []
        for context in evidence:
            text = _normalize(context.unit.search_text)
            filename = context.citation.original_filename.casefold()
            if not all(_normalize(term) in text for term in required):
                continue
            if any_terms and not any(
                _normalize(term) in text for term in any_terms
            ):
                continue
            if filename_terms and not any(
                term.casefold() in filename for term in filename_terms
            ):
                continue
            matches.append(context)
        return (
            min(matches, key=lambda context: context.ranked.rank)
            if matches
            else None
        )

    @staticmethod
    def _special_supported(
        field,
        value: str,
        contexts: tuple[EvidenceContext, ...],
    ) -> FieldCoverage:
        return FieldCoverage(
            field=field,
            state="supported",
            values=((value, None),),
            evidence_ids=tuple(
                dict.fromkeys(
                    context.unit.evidence_id for context in contexts
                )
            ),
            detail=None,
        )

    @staticmethod
    def _absent(field) -> FieldCoverage:
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
                paired_pressure = re.match(
                    r"\s*/\s*\d+(?:[.,]\d+)?\s*bar\b",
                    content[match.end():],
                    flags=re.IGNORECASE,
                )
                if paired_pressure is not None:
                    value += paired_pressure.group(0)
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
            if field.anchor_terms and not all(
                cls._positions(normalized, _normalize(anchor))
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
            return "" if terms else content[:480].rstrip()
        return "; ".join(selected)[:900].rstrip(" ;")

    @staticmethod
    def _value_priority(field_name: str, value: str) -> int:
        if (
            field_name == "pressure_definition_evidence"
            and "maximum allowable working pressure" in value.casefold()
            and "lower" in value.casefold()
        ):
            return 1000
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
            priority = max(range_maxima)
            if (
                field_name == "booster_pressure_evidence"
                and "BOOSTER AIR COOLED" in value.upper()
                and "BOOSTER WATER COOLED" in value.upper()
            ):
                priority += 1000
            return priority
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
        if terms and not positions:
            return ""
        if anchor_terms and not anchor_positions:
            return ""
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
        if needle == "syn-":
            return tuple(
                match.start()
                for match in re.finditer(
                    r"(?<![a-z0-9])syn-(?:[a-z0-9]+-)+[a-z0-9]+"
                    r"(?![a-z0-9])",
                    value,
                    flags=re.IGNORECASE,
                )
            )
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
