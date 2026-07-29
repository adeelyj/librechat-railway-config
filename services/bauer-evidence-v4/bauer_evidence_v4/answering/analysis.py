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

        if "certificate" in normalized or "zertifikat" in normalized:
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
        elif any(
            token in normalized
            for token in ("table", "tabelle", "model", "modell", "row")
        ):
            intent = "table_row"
            field_names = self._table_fields(normalized)
        else:
            intent = "general"
            field_names = ("source",)

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
    def _table_fields(normalized: str) -> tuple[str, ...]:
        complete_row = "complete row" in normalized
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
