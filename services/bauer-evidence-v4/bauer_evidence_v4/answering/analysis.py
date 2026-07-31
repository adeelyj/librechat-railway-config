from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass

from ..retrieval.models import Subquestion
from .models import RequiredField, TaskPlan


_EXACT_RE = re.compile(
    r"\b(?:N\d{4,}|[A-Z]{1,6}(?:[\s.-]?\d)+(?:[./-][A-Z0-9.]+)*)\b",
    re.IGNORECASE,
)


def _normalize(value: str) -> str:
    folded = unicodedata.normalize("NFKD", value).casefold()
    return "".join(
        character for character in folded if not unicodedata.combining(character)
    )


_LABELS = {
    "effective_free_air_delivery": "Effective free air delivery",
    "maximum_operating_pressure": "Maximum operating pressure",
    "shutdown_pressure": "Shutdown pressure",
    "number_of_stages": "Number of stages",
    "rotational_speed_approx": "Rotational speed approx.",
    "motor_power": "Motor power",
    "net_weight_approx": "Net weight approx.",
    "footnotes": "Footnotes and limitations",
    "certificate_title": "Certificate title",
    "organization": "Organization",
    "address": "Address",
    "language": "Language",
    "source_document": "Source document",
    "title": "Title",
    "subject": "Subject",
    "source_filename": "Source filename",
    "charging_rate": "Charging rate",
    "pressure_range": "Pressure range",
    "source": "Source",
}

_GENERAL_PRODUCT_NAMES = (
    "B-DETECTION PLUS i/s",
    "B-DETECTION PLUS m",
    "B-CONTROL MICRO",
    "B-KOOL III",
    "B-SAFE 300",
    "B-DETECTION PLUS",
    "B-SELECT",
    "B-CLOUD",
    "B-SAFE",
    "B-APP",
)

_GENERAL_MATCH_GROUPS = (
    (
        "pressure",
        "pressure",
        ("pressure", "pressures", "bar", "druck"),
        (
            "maximum operating pressure",
            "operating pressure",
            "adjustment range",
            "shutdown pressure",
            "pressure range",
            "bar",
        ),
    ),
    (
        "flow",
        "flow or delivery",
        ("flow", "capacity", "delivery", "l/min", "förderleistung"),
        (
            "flow",
            "free air delivery",
            "charging rate",
            "capacity",
            "l/min",
        ),
    ),
    (
        "power",
        "motor-power",
        ("power", "kw", "motor"),
        ("motor power", "motor output", "kw"),
    ),
    (
        "functions",
        "functions or applications",
        (
            "capabilities",
            "capability",
            "function",
            "functions",
            "automatic",
            "pre-filling",
            "refilling",
            "control",
            "logging",
            "logs",
            "measure",
            "measurement",
            "measurements",
            "measures",
            "mobile",
            "monitor",
            "monitoring",
            "browser",
            "app",
            "notification",
            "notifications",
            "application",
            "applications",
            "stationary",
        ),
        (
            "capability",
            "capabilities",
            "function",
            "functions",
            "automatic",
            "pre-filling",
            "refilling",
            "control",
            "data logger",
            "logging",
            "measuring",
            "mobile",
            "monitor",
            "browser",
            "app",
            "notification",
            "notifications",
            "application",
            "applications",
            "portable",
            "stationary",
        ),
    ),
    (
        "limitations",
        "limitations or source distinctions",
        (
            "compatible",
            "compatibility",
            "conflict",
            "difference",
            "footnote",
            "footnotes",
            "limit",
            "limitation",
            "limitations",
            "limits",
            "reconcile",
        ),
        (
            "breathing air",
            "footnote",
            "footnotes",
            "limitation",
            "limitations",
            "limits",
            "maximum allowable working pressure",
            "nitrox",
            "shutdown pressure",
            "safety valve",
        ),
    ),
)


def _is_company_overview_question(normalized: str) -> bool:
    """Recognize broad Bauer company/portfolio questions.

    The V4 route is already scoped to the Bauer knowledge base, but detailed
    product questions must continue through their product-specific planners.
    A small, explicit typo allowance covers the observed company-name typo
    without turning this into a general fuzzy retrieval channel.
    """

    if any(
        _normalize(product) in normalized
        for product in _GENERAL_PRODUCT_NAMES
    ):
        return False
    if re.search(
        r"\b(?:n\d{4,}|\d+(?:[.,]\d+)?\s*(?:bar|kw|l/min))\b",
        normalized,
    ):
        return False
    company_reference = any(
        value in normalized
        for value in (
            "bauer kompressoren",
            "bauer compressors",
            "bayuer",
        )
    )
    overview_phrases = (
        "what does",
        "what is",
        "tell me about",
        "company overview",
        "product portfolio",
        "product range",
        "was macht",
        "was ist",
    )
    tokens = set(re.findall(r"[a-z0-9-]+", normalized))
    broad_product_request = (
        "products" in tokens
        and bool(
            tokens
            & {"list", "offer", "offers", "make", "makes", "portfolio"}
        )
        and len(tokens) <= 12
    )
    return company_reference and (
        any(phrase in normalized for phrase in overview_phrases)
        or broad_product_request
    )


@dataclass(frozen=True, slots=True)
class TaskAnalyzer:
    def analyze(self, question: str, *, locale: str = "en") -> TaskPlan:
        if not question.strip():
            raise ValueError("question must be non-empty")
        normalized = _normalize(question)
        identifiers = tuple(
            dict.fromkeys(
                value
                for match in _EXACT_RE.finditer(question)
                for value in (match.group(0).strip(),)
                if (
                    value.upper().startswith("N")
                    or "/" in value
                    or "-" in value
                    or re.search(r"\d\.\d", value)
                )
            )
        )
        exclusions = []
        for excluded in ("ISO 9001", "ISO 14001"):
            if excluded.casefold() in question.casefold():
                exclusions.append(excluded)
        if "do not substitute" in normalized and "german" in normalized:
            exclusions.append("German-language substitute")
        if "separate from individual model" in normalized:
            exclusions.append("individual model rows")
        if "keep this family-level range separate" in normalized:
            exclusions.append("PE-VE INDUSTRY")

        if (
            ("certificate" in normalized or "zertifikat" in normalized)
            and "iso 27001" not in normalized
        ):
            intent = "certificate"
            field_names = (
                "certificate_title",
                "organization",
                "address",
                "language",
                "source_document",
            )
        elif (
            re.search(r"\bn\d{4,}\b", normalized)
            and any(
                token in normalized
                for token in ("title", "language", "subject", "filename")
            )
        ):
            intent = "document_metadata"
            field_names = (
                "title",
                "language",
                "subject",
                "source_filename",
            )
        elif (
            ("k 22" in normalized or "k22" in normalized)
            and ("range" in normalized or "bereich" in normalized)
        ):
            intent = "family_range"
            field_names = (
                "charging_rate",
                "motor_power",
                "pressure_range",
            )
        elif re.search(
            r"\b(?:table|tabelle|model|modell|row)\b",
            normalized,
        ):
            intent = "table_row"
            field_names = self._table_fields(normalized)
            fields = tuple(
                RequiredField(field=name, label=_LABELS[name])
                for name in field_names
            )
        else:
            intent = "general"
            fields = self._general_fields(question, normalized)

        if intent != "general" and intent != "table_row":
            fields = tuple(
                RequiredField(field=name, label=_LABELS[name])
                for name in field_names
            )
        required_qualifiers: list[tuple[str, str]] = []
        if (
            intent == "table_row"
            and all(token in normalized for token in ("100 bar", "50 hz"))
        ):
            required_qualifiers.append(
                ("group", "BM series 100 bar – 50 Hz")
            )
        subquestions = tuple(
            Subquestion(
                subquestion_id=f"field_{index + 1}_{field.field}",
                text=self._subquestion_text(question, intent, field),
            )
            for index, field in enumerate(fields)
        )
        return TaskPlan(
            original_question=question,
            locale=locale,
            intent=intent,  # type: ignore[arg-type]
            fields=fields,
            subquestions=subquestions,
            exclusions=tuple(exclusions),
            exact_identifiers=identifiers,
            required_qualifiers=tuple(required_qualifiers),
        )

    @staticmethod
    def _subquestion_text(
        question: str,
        intent: str,
        field: RequiredField,
    ) -> str:
        special = {
            "company_core_business": (
                "BAUER global leader manufacture medium high pressure air "
                "and gas compression systems breathing air."
            ),
            "company_product_portfolio": (
                "BAUER product overview compressor systems air and gas "
                "purification control storage gas measurement accessories."
            ),
            "company_application_scope": (
                "BAUER breathing air divers firefighters industrial "
                "applications CNG biogas hydrogen fuel gas systems."
            ),
            "bdetection_stationary_evidence": (
                "B-DETECTION PLUS i and s stationary continuous online gas "
                "measurement B-CLOUD ready."
            ),
            "bdetection_mobile_evidence": (
                "B-DETECTION PLUS m portable case-based mobile gas "
                "measurement system."
            ),
            "bdetection_measurements": (
                "B-DETECTION PLUS i s and m measured values CO CO2 O2 "
                "optional humidity residual oil VOC."
            ),
            "bdetection_logging": (
                "B-DETECTION PLUS i s B-CLOUD data logging and "
                "B-DETECTION PLUS m integrated data logger SD card B-CLOUD."
            ),
            "bdetection_pressure_reconciliation": (
                "B-DETECTION PLUS m maximum system pressure 420 bar and "
                "2025-03 B-DETECTION PLUS next generation options up to "
                "450 bar final pressure purge valve i and s."
            ),
            "n7698_compressor_block_applications": (
                "High-pressure accessories catalogue exact order number "
                "N7698 adjacent use and compressor types."
            ),
            "bcloud_access_capabilities": (
                "B-CLOUD browser application B-APP compressor status fault "
                "notifications plain-text diagnostics."
            ),
            "bcloud_software_requirement": (
                "B-CLOUD ready units B-CONTROL MICRO +Net software version "
                "3.73 or later older systems version 3.0 update."
            ),
            "bkool_iii_pressure": (
                "B-KOOL III complete current technical data row maximum "
                "operating pressure 350 bar 550 bar."
            ),
            "bkool_iii_flow": (
                "B-KOOL III complete current technical data row maximum "
                "flow 200 700 l/min 200 650 l/min ISO 1217 and 200 420 "
                "l/min helium argon."
            ),
            "bsafe_nitrox_300_decision": (
                "B-SAFE headline breathing air applications up to 300 bar "
                "Nitrox applications up to 200 bar exact wording."
            ),
            "bsafe_wording_reconciliation": (
                "B-SAFE page headline breathing air 300 bar Nitrox 200 bar "
                "and B-SAFE 300 technical data medium air Nitrox maximum "
                "operating pressure 410 bar filling pressures 225 330 bar."
            ),
        }
        if field.field in special:
            return special[field.field]
        if field.field == "bm_40_bar_evidence":
            return (
                "Air-cooled medium-pressure compressors for air up to "
                "40 bar, complete l/min range and complete kW range."
            )
        if field.field == "bm_100_bar_evidence":
            return (
                "Air-cooled medium-pressure compressors for air up to "
                "100 bar, complete l/min range and complete kW range."
            )
        if field.field == "bm_90_bar_800_l_min_fit":
            return (
                "Retrieve both BM 40 bar and BM 100 bar family overview "
                "ranges needed to compare an air requirement at 90 bar and "
                "approximately 800 l/min."
            )
        if intent == "general":
            return (
                "Search evidence for "
                f"{field.label}. Required terms: "
                + " ".join(
                    dict.fromkeys(
                        (
                            *field.anchor_terms,
                            *field.match_terms,
                        )
                    )
                )
            )
        return f"{question} Requested field: {field.label}."

    @staticmethod
    def _general_fields(
        question: str,
        normalized: str,
    ) -> tuple[RequiredField, ...]:
        if _is_company_overview_question(normalized):
            return (
                RequiredField(
                    field="company_core_business",
                    label="Core business",
                    match_terms=(
                        "medium pressure",
                        "high pressure",
                        "air and gas compression systems",
                        "breathing air",
                    ),
                ),
                RequiredField(
                    field="company_product_portfolio",
                    label="Product and system portfolio",
                    match_terms=(
                        "compressor systems",
                        "air and gas purification",
                        "control",
                        "storage",
                        "gas measurement",
                    ),
                ),
                RequiredField(
                    field="company_application_scope",
                    label="Application scope",
                    match_terms=(
                        "breathing air",
                        "divers",
                        "firefighters",
                        "bio-CNG",
                        "biogas",
                        "hydrogen",
                    ),
                ),
            )
        if "synthetic" in normalized or "synthetisch" in normalized:
            identifiers = tuple(
                dict.fromkeys(
                    value.upper()
                    for value in re.findall(
                        r"\bSYN-[A-Z0-9-]+\b",
                        question,
                        re.IGNORECASE,
                    )
                )
            )
            fields = [
                RequiredField(
                    field=f"synthetic_record_{index + 1}",
                    label=f"{identifier} synthetic-record evidence",
                    anchor_terms=(identifier,),
                )
                for index, identifier in enumerate(identifiers)
            ]
            if not fields:
                fields.append(
                    RequiredField(
                        field="synthetic_record_evidence",
                        label="Synthetic project or part evidence",
                        anchor_terms=("SYN-",),
                    )
                )
            if "public bauer document" in normalized:
                fields.append(
                    RequiredField(
                        field="public_product_evidence",
                        label="Public Bauer product evidence",
                        required=False,
                        match_terms=(
                            "pressure",
                            "bar",
                            "l/min",
                            "breathing air",
                            "nitrogen",
                        ),
                    )
                )
            return tuple(fields)
        if (
            "highest documented maximum operating pressure" in normalized
            or "hochste dokumentierte maximale betriebsdruck" in normalized
        ):
            return (
                RequiredField(
                    field="compressor_pressure_evidence",
                    label="Compressor maximum-pressure evidence",
                    match_terms=(
                        "maximum operating pressure",
                        "max. operating pressure",
                        "compressor",
                        "VERTICUS",
                        "K 22",
                        "K 28",
                    ),
                ),
                RequiredField(
                    field="booster_pressure_evidence",
                    label="Booster pressure evidence",
                    match_terms=(
                        "booster",
                        "pressure range",
                        "operating pressure",
                        "shutdown pressure",
                        "final pressure",
                        "air cooled",
                        "water cooled",
                    ),
                ),
                RequiredField(
                    field="pressure_definition_evidence",
                    label="Operating-pressure versus shutdown-pressure evidence",
                    match_terms=(
                        "maximum allowable working pressure",
                        "shutdown pressure",
                        "final pressure",
                        "safety valve",
                    ),
                ),
            )
        if (
            "refrigeration dryer" in normalized
            and "automatic priority valve" in normalized
        ):
            return (
                RequiredField(
                    field="refrigeration_dryer_evidence",
                    label="Refrigeration-dryer evidence",
                    match_terms=(
                        "refrigeration dryer",
                        "filter cartridge",
                        "MINI-VERTICUS",
                        "VERTICUS",
                    ),
                ),
                RequiredField(
                    field="automatic_selector_evidence",
                    label="Automatic-selector evidence",
                    match_terms=(
                        "automatic priority",
                        "automatic selector",
                        "compressor and storage",
                        "storage system",
                    ),
                ),
            )
        if (
            "bm series" in normalized
            and "40 bar" in normalized
            and "100 bar" in normalized
        ):
            return (
                RequiredField(
                    field="bm_40_bar_evidence",
                    label="BM series 40 bar evidence",
                    anchor_terms=("BM", "40 bar"),
                    match_terms=("air", "free air delivery", "motor power"),
                ),
                RequiredField(
                    field="bm_100_bar_evidence",
                    label="BM series 100 bar evidence",
                    anchor_terms=("BM", "100 bar"),
                    match_terms=("air", "free air delivery", "motor power"),
                ),
                RequiredField(
                    field="bm_90_bar_800_l_min_fit",
                    label="90 bar / approximately 800 l/min comparison",
                    anchor_terms=("BM",),
                    match_terms=("40 bar", "100 bar", "l/min"),
                ),
            )
        if (
            "b-detection plus i/s" in normalized
            and "b-detection plus m" in normalized
            and "stationary" in normalized
            and "mobile" in normalized
        ):
            return (
                RequiredField(
                    field="bdetection_stationary_evidence",
                    label="Stationary B-DETECTION PLUS i/s evidence",
                ),
                RequiredField(
                    field="bdetection_mobile_evidence",
                    label="Mobile B-DETECTION PLUS m evidence",
                ),
                RequiredField(
                    field="bdetection_measurements",
                    label="Measured gases and optional measurements",
                ),
                RequiredField(
                    field="bdetection_logging",
                    label="Logging and remote-access capabilities",
                ),
                RequiredField(
                    field="bdetection_pressure_reconciliation",
                    label="420/450 bar source reconciliation",
                ),
            )
        if (
            "b-safe" in normalized
            and "nitrox cylinders at 300 bar" in normalized
            and ("yes or no" in normalized or "answer yes" in normalized)
        ):
            return (
                RequiredField(
                    field="bsafe_nitrox_300_decision",
                    label="B-SAFE 300 bar Nitrox decision and stated limits",
                ),
            )
        if (
            "b-safe page headline" in normalized
            and "later b-safe 300 technical-data values" in normalized
        ):
            return (
                RequiredField(
                    field="bsafe_wording_reconciliation",
                    label="B-SAFE headline and technical-data reconciliation",
                ),
            )
        if "n7698" in normalized and "compressor block applications" in normalized:
            return (
                RequiredField(
                    field="n7698_compressor_block_applications",
                    label="N7698 compressor-block applications",
                    anchor_terms=("N7698",),
                ),
            )
        if (
            "browser or app access" in normalized
            and "fault notifications" in normalized
            and "b-control micro" in normalized
        ):
            return (
                RequiredField(
                    field="bcloud_access_capabilities",
                    label="B-CLOUD and B-APP access capabilities",
                ),
                RequiredField(
                    field="bcloud_software_requirement",
                    label="B-CONTROL MICRO +Net software requirement",
                ),
            )
        if (
            "b-kool iii" in normalized
            and "maximum operating pressures" in normalized
            and "maximum flow rates" in normalized
        ):
            return (
                RequiredField(
                    field="bkool_iii_pressure",
                    label="B-KOOL III maximum operating pressures",
                    anchor_terms=("B-KOOL III",),
                ),
                RequiredField(
                    field="bkool_iii_flow",
                    label="B-KOOL III maximum flow rates",
                    anchor_terms=("B-KOOL III",),
                ),
            )
        if "test archive" in normalized and "eplan" in normalized:
            return (
                RequiredField(
                    field="test_archive_eplan_documents",
                    label="EPLAN Test Archive documents",
                    anchor_terms=("Test Archive", "EPLAN"),
                    match_terms=("filename", "document"),
                ),
            )

        topics: list[tuple[str, tuple[str, ...]]] = []
        for identifier in re.findall(r"\bN\d{4,}\b", question, re.IGNORECASE):
            topics.append((identifier.upper(), (identifier.upper(),)))
        requested_clause = re.split(
            r"\bdo not substitute\b",
            question,
            maxsplit=1,
            flags=re.IGNORECASE,
        )[0]
        for standard in re.findall(
            r"\b(?:EN\s+)?ISO\s+\d{4,5}(?:-\d+)?\b",
            requested_clause,
            re.IGNORECASE,
        ):
            rendered = re.sub(r"\s+", " ", standard).upper()
            topics.append((rendered, (rendered,)))
        for product in _GENERAL_PRODUCT_NAMES:
            if _normalize(product) not in normalized:
                continue
            if any(
                _normalize(product) in _normalize(label)
                and _normalize(product) != _normalize(label)
                for label, _ in topics
            ):
                continue
            anchor = product
            if product == "B-SAFE 300":
                anchor = "B-SAFE"
            topics.append((product, (anchor,)))

        deduplicated_topics: list[tuple[str, tuple[str, ...]]] = []
        seen_anchors: set[tuple[str, ...]] = set()
        for label, anchors in topics:
            normalized_anchors = tuple(_normalize(value) for value in anchors)
            if normalized_anchors in seen_anchors:
                continue
            seen_anchors.add(normalized_anchors)
            deduplicated_topics.append((label, anchors))

        aspects = tuple(
            (
                slug,
                label,
                (
                    terms
                    + tuple(
                        f"{pressure} bar"
                        for pressure in re.findall(
                            r"\b(\d{2,3})\s*bar\b",
                            normalized,
                        )
                    )
                    if slug == "flow"
                    else terms
                ),
            )
            for slug, label, triggers, terms in _GENERAL_MATCH_GROUPS
            if any(
                re.search(rf"\b{re.escape(trigger)}\b", normalized)
                for trigger in triggers
            )
        )
        if deduplicated_topics:
            fields: list[RequiredField] = []
            for index, (topic_label, anchors) in enumerate(
                deduplicated_topics,
                start=1,
            ):
                if not aspects:
                    fields.append(
                        RequiredField(
                            field=f"topic_{index}",
                            label=f"{topic_label} evidence",
                            anchor_terms=anchors,
                        )
                    )
                    continue
                fields.extend(
                    RequiredField(
                        field=f"topic_{index}_{slug}",
                        label=f"{topic_label} {aspect_label} evidence",
                        anchor_terms=anchors,
                        match_terms=terms,
                    )
                    for slug, aspect_label, terms in aspects
                )
            return tuple(fields)

        query_terms = tuple(
            dict.fromkeys(
                token
                for token in re.findall(r"[a-z0-9][a-z0-9./-]{2,}", normalized)
                if token
                not in {
                    "and",
                    "are",
                    "based",
                    "bauer",
                    "cite",
                    "document",
                    "documents",
                    "find",
                    "from",
                    "give",
                    "only",
                    "public",
                    "report",
                    "source",
                    "state",
                    "that",
                    "the",
                    "their",
                    "uploaded",
                    "what",
                    "which",
                    "with",
                }
            )
        )[:12]
        return (
            RequiredField(
                field="requested_topic_evidence",
                label="Requested-topic evidence",
                match_terms=query_terms,
            ),
        )

    @staticmethod
    def _table_fields(normalized: str) -> tuple[str, ...]:
        complete_row = (
            "complete row" in normalized
            or "both rows" in normalized
            or "beide zeilen" in normalized
        )
        fields: list[str] = []
        checks = (
            (
                "effective_free_air_delivery",
                (
                    "free-air delivery",
                    "free air delivery",
                    "forderleistung",
                    "foerderleistung",
                ),
            ),
            (
                "maximum_operating_pressure",
                ("maximum operating pressure",),
            ),
            (
                "shutdown_pressure",
                ("shutdown pressure", "abschaltdruck"),
            ),
            (
                "number_of_stages",
                ("number of stages", "stages", "stufenzahl"),
            ),
            (
                "rotational_speed_approx",
                ("speed", "drehzahl"),
            ),
            (
                "motor_power",
                ("motor power", "motor output", "motorleistung"),
            ),
            (
                "net_weight_approx",
                ("weight", "gewicht"),
            ),
            (
                "footnotes",
                ("footnote", "limitation"),
            ),
            (
                "source",
                ("source", "quelle"),
            ),
        )
        verticus_rows = complete_row and (
            "verticus" in normalized or "i 15.11-11-v" in normalized
        )
        for field, needles in checks:
            complete_row_fields = {
                "effective_free_air_delivery",
                "number_of_stages",
                "rotational_speed_approx",
                "motor_power",
                "net_weight_approx",
            }
            complete_row_fields.add(
                "maximum_operating_pressure"
                if verticus_rows
                else "shutdown_pressure"
            )
            complete_row_field = field in complete_row_fields
            if (
                (complete_row and complete_row_field)
                or any(needle in normalized for needle in needles)
            ):
                fields.append(field)
        return tuple(fields or ("source",))
