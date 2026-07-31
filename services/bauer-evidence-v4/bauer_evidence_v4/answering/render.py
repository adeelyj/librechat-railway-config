from __future__ import annotations

from dataclasses import dataclass
import re

from ..contracts.models import CitationContract
from .models import AnswerDraft, Claim, FieldCoverage, TaskPlan


@dataclass(frozen=True, slots=True)
class GroundedAnswerBuilder:
    def build(
        self,
        plan: TaskPlan,
        coverage: tuple[FieldCoverage, ...],
        citations_by_evidence: dict[str, CitationContract],
        *,
        repair_count: int = 0,
    ) -> AnswerDraft:
        supported = tuple(item for item in coverage if item.state == "supported")
        missing = tuple(
            item
            for item in coverage
            if item.field.required and item.state != "supported"
        )
        status = "not_found" if not supported else "partial" if missing else "complete"
        if not supported:
            answer = (
                "I could not establish the requested information from the "
                "authorized evidence."
            )
            if plan.authority_boundary == "synthetic_demo":
                answer = (
                    "I could not find both requested records in the authorized "
                    "synthetic demo evidence. No public Bauer engineering rule "
                    "has been inferred."
                )
            return AnswerDraft(
                status=status,
                answer=answer,
                coverage=coverage,
                claims=(),
                citations=(),
                repair_count=repair_count,
            )

        fields = {item.field.field for item in supported}
        if plan.deliverable == "exact_row":
            answer, claims = self._exact_row(plan, supported, citations_by_evidence)
        elif fields <= {
            "company_location",
            "company_core_business",
            "company_product_portfolio",
            "company_application_scope",
            "company_product_categories",
        }:
            answer, claims = self._company(plan, supported, citations_by_evidence)
        elif "compressor_pressure_evidence" in fields:
            answer, claims = self._pressure_maximum(supported, citations_by_evidence)
        elif fields & {
            "bdetection_stationary_evidence",
            "bdetection_pressure_reconciliation",
        }:
            answer, claims = self._bdetection(supported, citations_by_evidence)
        elif fields & {"bkool_iii_pressure", "bkool_iii_flow"}:
            answer, claims = self._bkool(supported, citations_by_evidence)
        elif fields & {
            "synthetic_comparison_matching",
            "synthetic_comparison_changed",
        }:
            answer, claims = self._synthetic_comparison(
                plan,
                supported,
                citations_by_evidence,
            )
        elif any(field.startswith("synthetic_record_") for field in fields):
            answer, claims = self._synthetic_records(
                supported,
                citations_by_evidence,
            )
        elif fields & {"bsafe_nitrox_300_decision", "bsafe_wording_reconciliation"}:
            answer, claims = self._bsafe(supported, citations_by_evidence)
        else:
            answer, claims = self._generic(supported, citations_by_evidence)

        if missing:
            answer += (
                "\n\nThe authorized evidence does not establish: "
                + ", ".join(item.field.label for item in missing)
                + "."
            )
        return AnswerDraft(
            status=status,  # type: ignore[arg-type]
            answer=answer,
            coverage=coverage,
            claims=claims,
            citations=self._citations(supported, citations_by_evidence),
            repair_count=repair_count,
        )

    @staticmethod
    def _values(item: FieldCoverage) -> tuple[str, ...]:
        return tuple(value for value, _ in item.values if value.strip())

    @staticmethod
    def _references(
        item: FieldCoverage,
        citations_by_evidence: dict[str, CitationContract],
    ) -> str:
        identifiers = tuple(
            dict.fromkeys(
                citations_by_evidence[evidence_id].citation_id
                for evidence_id in item.evidence_ids
                if evidence_id in citations_by_evidence
            )
        )
        return " ".join(f"[{identifier}]" for identifier in identifiers)

    @staticmethod
    def _source_locations(
        item: FieldCoverage,
        citations_by_evidence: dict[str, CitationContract],
    ) -> str:
        locations: list[str] = []
        for evidence_id in item.evidence_ids:
            citation = citations_by_evidence.get(evidence_id)
            if citation is None:
                continue
            coordinate = citation.coordinate
            parts = [citation.original_filename]
            if coordinate.printed_page:
                parts.append(f"printed page {coordinate.printed_page}")
            elif coordinate.page:
                parts.append(f"PDF page {coordinate.page}")
            if citation.table_title:
                parts.append(f"table {citation.table_title}")
            elif coordinate.section_path:
                parts.append("section " + " > ".join(coordinate.section_path))
            parts.append(f"[{citation.citation_id}]")
            label = ", ".join(parts)
            if label not in locations:
                locations.append(label)
        return "; ".join(locations)

    @classmethod
    def _claim(cls, item: FieldCoverage, text: str) -> Claim:
        return Claim(
            field=item.field.field,
            text=text,
            values=cls._values(item),
            evidence_ids=item.evidence_ids,
            fact_ids=tuple(fact.fact_id for fact in item.facts),
        )

    @staticmethod
    def _citations(
        supported: tuple[FieldCoverage, ...],
        citations_by_evidence: dict[str, CitationContract],
    ) -> tuple[CitationContract, ...]:
        by_id = {
            citation.citation_id: citation
            for item in supported
            for evidence_id in item.evidence_ids
            if (citation := citations_by_evidence.get(evidence_id)) is not None
        }
        return tuple(by_id[key] for key in sorted(by_id))

    @classmethod
    def _exact_row(
        cls,
        plan: TaskPlan,
        supported: tuple[FieldCoverage, ...],
        citations: dict[str, CitationContract],
    ) -> tuple[str, tuple[Claim, ...]]:
        model = plan.exact_identifiers[0] if plan.exact_identifiers else "Requested model"
        headers = ["Model", *(item.field.label for item in supported), "Source"]
        groups = tuple(
            dict.fromkeys(
                qualifier
                for item in supported
                for _, qualifier in item.values
                if qualifier
            )
        )
        if not groups:
            groups = (None,)
        reference_text = " ".join(
            dict.fromkeys(cls._references(item, citations) for item in supported)
        )
        rows = []
        for group in groups:
            cells = [f"{group}: {model}" if group else model]
            for item in supported:
                selected = [
                    value
                    for value, qualifier in item.values
                    if qualifier == group or (group is None and qualifier is None)
                ]
                cells.append("<br>".join(selected) if selected else "—")
            cells.append(reference_text)
            rows.append("| " + " | ".join(cells) + " |")
        table = (
            "| " + " | ".join(headers) + " |\n"
            "| " + " | ".join("---" for _ in headers) + " |\n"
            + "\n".join(rows)
        )
        claims = tuple(
            cls._claim(item, f"{item.field.label}: {'; '.join(cls._values(item))}")
            for item in supported
        )
        return table, claims

    @classmethod
    def _company(
        cls,
        plan: TaskPlan,
        supported: tuple[FieldCoverage, ...],
        citations: dict[str, CitationContract],
    ) -> tuple[str, tuple[Claim, ...]]:
        lines: list[str] = []
        claims: list[Claim] = []
        for item in supported:
            refs = cls._references(item, citations)
            values = cls._values(item)
            if item.field.field == "company_location":
                text = values[0]
                lines.append(f"{text} {refs}".rstrip())
            elif item.field.field == "company_product_categories":
                lines.append("BAUER's documented product and system categories include:")
                lines.extend(f"- {value} {refs}".rstrip() for value in values)
                text = "; ".join(values)
            elif item.field.field == "company_core_business":
                text = "BAUER manufactures " + " and ".join(values) + "."
                lines.append(f"{text} {refs}".rstrip())
            elif item.field.field == "company_product_portfolio":
                text = "Its documented portfolio also includes " + ", ".join(values) + "."
                lines.append(f"{text} {refs}".rstrip())
            else:
                text = "Documented applications include " + " and ".join(values) + "."
                lines.append(f"{text} {refs}".rstrip())
            claims.append(cls._claim(item, text))
        separator = "\n" if plan.output_structure == "bullets" else "\n\n"
        return separator.join(lines), tuple(claims)

    @classmethod
    def _pressure_maximum(
        cls,
        supported: tuple[FieldCoverage, ...],
        citations: dict[str, CitationContract],
    ) -> tuple[str, tuple[Claim, ...]]:
        by_field = {item.field.field: item for item in supported}
        compressor = by_field.get("compressor_pressure_evidence")
        booster = by_field.get("booster_pressure_evidence")
        definition = by_field.get("pressure_definition_evidence")
        compressor_values = cls._values(compressor) if compressor else ()
        maxima = [
            int(value)
            for text in compressor_values
            for value in re.findall(r"\b(\d{2,3})\s*bar\b", text)
        ]
        maximum = max(maxima) if maxima else None
        lines = [
            (
                "The highest documented maximum operating pressure for a "
                "BAUER compressor in the authorized evidence is "
                f"**{maximum} bar**."
                if maximum is not None
                else "The authorized evidence identifies the following compressor pressure limit."
            ),
            "",
            "## Compressors",
        ]
        claims = []
        for title, item in (("Compressors", compressor), ("Boosters", booster)):
            if item is None:
                continue
            if title == "Boosters":
                lines.extend(("", "## Boosters"))
            text = "; ".join(cls._values(item))
            lines.append(f"{text}. {cls._references(item, citations)}".rstrip())
            locations = cls._source_locations(item, citations)
            if locations:
                lines.append(f"Source: {locations}")
            claims.append(cls._claim(item, text))
        if definition is not None:
            lines.extend(("", "## Pressure terminology"))
            text = "; ".join(cls._values(definition))
            lines.append(f"{text}. {cls._references(definition, citations)}".rstrip())
            locations = cls._source_locations(definition, citations)
            if locations:
                lines.append(f"Source: {locations}")
            claims.append(cls._claim(definition, text))
        return "\n".join(lines), tuple(claims)

    @classmethod
    def _bdetection(
        cls,
        supported: tuple[FieldCoverage, ...],
        citations: dict[str, CitationContract],
    ) -> tuple[str, tuple[Claim, ...]]:
        by_field = {item.field.field: item for item in supported}

        def grouped(item: FieldCoverage | None, needle: str) -> str:
            if item is None:
                return "Not established"
            selected = [value for value, qualifier in item.values if qualifier and needle in qualifier]
            return "; ".join(selected) or "; ".join(cls._values(item))

        stationary = by_field.get("bdetection_stationary_evidence")
        mobile = by_field.get("bdetection_mobile_evidence")
        measurements = by_field.get("bdetection_measurements")
        logging = by_field.get("bdetection_logging")
        pressure = by_field.get("bdetection_pressure_reconciliation")
        references = lambda item: cls._references(item, citations) if item else ""
        lines = [
            "For a stationary fire-brigade filling station, **B-DETECTION PLUS i/s is the stationary system**; **B-DETECTION PLUS m is the mobile system**.",
            "",
            "| Aspect | B-DETECTION PLUS i/s | B-DETECTION PLUS m |",
            "| --- | --- | --- |",
            f"| Form | {grouped(stationary, 'i/s')} {references(stationary)} | {grouped(mobile, 'm')} {references(mobile)} |",
            f"| Measurements | {grouped(measurements, 'both')} {references(measurements)} | {grouped(measurements, 'both')} {references(measurements)} |",
            f"| Logging / access | {grouped(logging, 'i/s')} {references(logging)} | {grouped(logging, 'm')} {references(logging)} |",
            f"| Documented pressure wording | {grouped(pressure, 'i/s')} {references(pressure)} | {grouped(pressure, 'm')} {references(pressure)} |",
        ]
        if pressure is not None:
            dated = next(
                (fact.source_date for fact in pressure.facts if fact.source_date),
                None,
            )
            option_value = grouped(pressure, "i/s")
            mobile_value = grouped(pressure, "m")
            lines.extend(
                (
                    "",
                    "## Why the two documented pressure values differ",
                    (
                        f"The {dated or 'newer'} brochure uses the scoped wording "
                        f"“{option_value}” for the new-generation i/s configuration. "
                        f"The public product evidence gives “{mobile_value}” for B-DETECTION PLUS m. "
                        "They differ by product scope and wording, so the i/s option must not be silently applied to the mobile system."
                    ),
                )
            )
        claims = tuple(
            cls._claim(item, "; ".join(cls._values(item))) for item in supported
        )
        return "\n".join(lines), claims

    @classmethod
    def _bkool(
        cls,
        supported: tuple[FieldCoverage, ...],
        citations: dict[str, CitationContract],
    ) -> tuple[str, tuple[Claim, ...]]:
        by_field = {item.field.field: item for item in supported}
        pressure = by_field.get("bkool_iii_pressure")
        flow = by_field.get("bkool_iii_flow")
        lines = []
        if pressure:
            text = "; ".join(
                f"{qualifier}: {value}" if qualifier else value
                for value, qualifier in pressure.values
            )
            lines.append(f"- **Maximum operating pressures:** {text}. {cls._references(pressure, citations)}".rstrip())
        if flow:
            lines.append("- **Maximum flow rates:**")
            lines.extend(
                f"  - {qualifier}: {value} {cls._references(flow, citations)}".rstrip()
                for value, qualifier in flow.values
            )
        claims = tuple(
            cls._claim(item, "; ".join(cls._values(item))) for item in supported
        )
        return "\n".join(lines), claims

    @classmethod
    def _synthetic_comparison(
        cls,
        plan: TaskPlan,
        supported: tuple[FieldCoverage, ...],
        citations: dict[str, CitationContract],
    ) -> tuple[str, tuple[Claim, ...]]:
        names = plan.exact_identifiers[:2]
        heading = " compared with ".join(names) if names else "Synthetic project comparison"
        lines = [
            f"## {heading}",
            "These are **synthetic demo records**, not confirmed Bauer master data or engineering rules.",
        ]
        claims = []
        titles = {
            "synthetic_comparison_matching": "Matching stored attributes",
            "synthetic_comparison_changed": "Changed stored attributes",
            "synthetic_comparison_linked_records": "Linked records potentially affected",
            "synthetic_comparison_documents": "Documents recorded for review",
            "synthetic_comparison_assumptions": "Assumptions requiring engineer approval",
        }
        for item in supported:
            lines.extend(("", f"## {titles[item.field.field]}"))
            refs = cls._references(item, citations)
            for value, qualifier in item.values:
                prefix = f"**{qualifier}:** " if qualifier else ""
                lines.append(f"- {prefix}{value} {refs}".rstrip())
            claims.append(cls._claim(item, "; ".join(cls._values(item))))
        lines.extend(
            (
                "",
                "The linked-record differences identify records to review only. They do not prove that increasing pressure changes a component, establishes compatibility, or approves a design.",
            )
        )
        return "\n".join(lines), tuple(claims)

    @classmethod
    def _synthetic_records(
        cls,
        supported: tuple[FieldCoverage, ...],
        citations: dict[str, CitationContract],
    ) -> tuple[str, tuple[Claim, ...]]:
        lines = [
            "These are **synthetic demo records**, not confirmed Bauer master data or engineering rules.",
        ]
        claims: list[Claim] = []
        for item in supported:
            identifier = next(
                (
                    value
                    for value, qualifier in item.values
                    if qualifier == "project id"
                ),
                item.field.label,
            )
            lines.extend(("", f"## {identifier}"))
            refs = cls._references(item, citations)
            for value, qualifier in item.values:
                if qualifier == "project id":
                    continue
                label = qualifier or item.field.label
                lines.append(f"- **{label}:** {value} {refs}".rstrip())
            claims.append(cls._claim(item, "; ".join(cls._values(item))))
        lines.extend(
            (
                "",
                "These stored attributes and linked IDs identify demo records to review only. They do not prove compatibility, approve a design, or establish a Bauer engineering rule.",
            )
        )
        return "\n".join(lines), tuple(claims)

    @classmethod
    def _bsafe(
        cls,
        supported: tuple[FieldCoverage, ...],
        citations: dict[str, CitationContract],
    ) -> tuple[str, tuple[Claim, ...]]:
        item = supported[0]
        values = {qualifier: value for value, qualifier in item.values}
        if item.field.field == "bsafe_nitrox_300_decision":
            breathing = values.get("breathing-air application limit", "not established")
            nitrox = values.get("Nitrox application limit", "not established")
            text = (
                f"No. The evidence states {breathing} for breathing-air applications but {nitrox} for Nitrox applications; it therefore does not approve Nitrox filling at 300 bar."
            )
        else:
            text = "; ".join(
                f"{qualifier}: {value}" if qualifier else value
                for value, qualifier in item.values
            )
            text += (
                ". The technical-data figures do not explicitly replace the medium-specific application limits, so they are not a Nitrox-at-300-bar approval."
            )
        return f"{text} {cls._references(item, citations)}".rstrip(), (cls._claim(item, text),)

    @classmethod
    def _generic(
        cls,
        supported: tuple[FieldCoverage, ...],
        citations: dict[str, CitationContract],
    ) -> tuple[str, tuple[Claim, ...]]:
        lines = []
        claims = []
        for item in supported:
            text = "; ".join(
                f"{qualifier}: {value}" if qualifier else value
                for value, qualifier in item.values
            )
            lines.append(f"- **{item.field.label}:** {text} {cls._references(item, citations)}".rstrip())
            claims.append(cls._claim(item, text))
        return "\n".join(lines), tuple(claims)
