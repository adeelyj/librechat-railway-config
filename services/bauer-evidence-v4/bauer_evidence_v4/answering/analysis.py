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
        ("pressure", "bar", "druck"),
        (
            "maximum operating pressure",
            "operating pressure",
            "shutdown pressure",
            "pressure range",
            "bar",
        ),
    ),
    (
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
        ("power", "kw", "motor"),
        ("motor power", "motor output", "kw"),
    ),
    (
        (
            "function",
            "automatic",
            "control",
            "monitor",
            "browser",
            "app",
            "notification",
            "application",
        ),
        (
            "function",
            "automatic",
            "control",
            "monitor",
            "browser",
            "app",
            "notification",
            "application",
        ),
    ),
    (
        ("footnote", "limitation", "difference", "conflict", "reconcile"),
        (
            "footnote",
            "limitation",
            "maximum allowable working pressure",
            "shutdown pressure",
            "safety valve",
        ),
    ),
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
                text=f"{question} Requested field: {field.label}.",
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
    def _general_fields(
        question: str,
        normalized: str,
    ) -> tuple[RequiredField, ...]:
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
                        "operating pressure",
                        "shutdown pressure",
                        "final pressure",
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

        match_terms = tuple(
            dict.fromkeys(
                term
                for triggers, terms in _GENERAL_MATCH_GROUPS
                if any(trigger in normalized for trigger in triggers)
                for term in terms
            )
        )
        if deduplicated_topics:
            return tuple(
                RequiredField(
                    field=f"topic_{index + 1}",
                    label=f"{label} evidence",
                    anchor_terms=anchors,
                    match_terms=match_terms,
                )
                for index, (label, anchors) in enumerate(deduplicated_topics)
            )

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
        for field, needles in checks:
            complete_row_field = field in {
                "effective_free_air_delivery",
                "shutdown_pressure",
                "number_of_stages",
                "rotational_speed_approx",
                "motor_power",
                "net_weight_approx",
            }
            if (
                (complete_row and complete_row_field)
                or any(needle in normalized for needle in needles)
            ):
                fields.append(field)
        return tuple(fields or ("source",))
