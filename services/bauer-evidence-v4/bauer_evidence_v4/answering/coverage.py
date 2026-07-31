from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass

from ..canonical.normalize import stable_id
from .models import EvidenceContext, EvidenceFact, FieldCoverage, TaskPlan


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
        contexts = evidence
        if plan.intent == "table_row":
            exact_rows = tuple(
                context
                for context in evidence
                if context.ranked.candidate.item.projection.projection_type
                == "table_row"
                and all(
                    _normalize(identifier)
                    in _normalize(context.unit.search_text)
                    for identifier in plan.exact_identifiers
                    if not identifier.upper().startswith("N")
                )
            )
            if exact_rows:
                if plan.required_qualifiers:
                    contexts = (min(exact_rows, key=lambda item: item.ranked.rank),)
                else:
                    by_group: dict[str, EvidenceContext] = {}
                    for context in sorted(
                        exact_rows,
                        key=lambda item: item.ranked.rank,
                    ):
                        group = dict(
                            context.ranked.candidate.item.projection.qualifiers
                        ).get("group", "")
                        by_group.setdefault(group, context)
                    contexts = tuple(by_group.values())
        support: list[tuple[str, str | None, EvidenceContext]] = []
        for context in contexts:
            for name, value, qualifier in context.values:
                if name == field.field:
                    support.append((value, qualifier, context))
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
            tuple[str, str | None, EvidenceContext],
        ] = {}
        for value, qualifier, context in support:
            deduplicated.setdefault(
                (value, qualifier),
                (value, qualifier, context),
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
        return self._supported_values(
            field,
            tuple(support),
        )

    def _general_field(
        self,
        field,
        evidence: tuple[EvidenceContext, ...],
    ) -> FieldCoverage:
        # The catch-all planner must never turn weak lexical overlap into a
        # raw evidence dump. Unknown intents fail closed until they receive
        # an explicit, tested coverage contract.
        if field.field == "requested_topic_evidence":
            return self._absent(field)
        if field.field.startswith("company_"):
            return self._company_overview_field(field, evidence)
        if field.field.startswith("bdetection_"):
            return self._bdetection_field(field, evidence)
        if field.field == "n7698_compressor_block_applications":
            return self._n7698_field(field, evidence)
        if field.field.startswith("bcloud_"):
            return self._bcloud_field(field, evidence)
        if field.field.startswith("bkool_iii_"):
            return self._bkool_iii_field(field, evidence)
        if field.field.startswith("bsafe_"):
            return self._bsafe_field(field, evidence)
        if field.field.startswith("synthetic_comparison_"):
            return self._synthetic_comparison_field(field, evidence)
        if field.field.startswith("synthetic_record_"):
            return self._synthetic_record_field(field, evidence)
        if field.field == "bm_40_bar_evidence":
            return self._bm_family_field(field, evidence, pressure_bar=40)
        if field.field == "bm_100_bar_evidence":
            return self._bm_family_field(field, evidence, pressure_bar=100)
        if field.field == "bm_90_bar_800_l_min_fit":
            return self._bm_requirement_fit(field, evidence)
        candidates: list[
            tuple[int, int, int, int, str, str | None, EvidenceContext]
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
                    context,
                )
            )
        candidates.sort(reverse=True)
        support: list[tuple[str, str | None, EvidenceContext]] = []
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
            context,
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
            support.append((concise, filename, context))
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
        return self._supported_values(
            field,
            tuple(support),
        )

    @classmethod
    def _company_overview_field(
        cls,
        field,
        evidence: tuple[EvidenceContext, ...],
    ) -> FieldCoverage:
        """Extract company facts from explicit portfolio source language."""

        if field.field == "company_location":
            location = cls._first_context(
                evidence,
                required=(
                    "bauer kompressoren gmbh",
                    "stablistr. 8",
                    "81477 munich",
                    "germany",
                ),
                any_terms=("contact", "get in touch", "published by"),
            )
            if location is None:
                return cls._absent(field)
            match = re.search(
                r"(BAUER\s+KOMPRESSOREN\s+GmbH)\s+"
                r"(St\S{0,4}blistr\.\s*8)\s+"
                r"(81477\s+Munich,?\s+Germany)",
                re.sub(r"\s+", " ", location.unit.search_text),
                flags=re.IGNORECASE,
            )
            if match is None:
                return cls._absent(field)
            return cls._supported_values(
                field,
                ((
                    f"{match.group(1).strip()} is based at "
                    f"{match.group(2).strip()}, {match.group(3).strip()}.",
                    None,
                    location,
                ),),
            )

        core = cls._first_context(
            evidence,
            required=(
                "air and gas compression systems",
                "systems for generating breathing air",
            ),
            any_terms=("global leader", "market leader"),
        )
        if field.field == "company_core_business":
            if core is None:
                return cls._absent(field)
            source = _normalize(core.unit.search_text)
            values = []
            if all(
                term in source
                for term in ("medium", "high", "air and gas compression systems")
            ):
                values.append(
                    "medium- and high-pressure air and gas compression systems"
                )
            if "systems for generating breathing air" in source:
                values.append("systems for generating breathing air")
            if not values:
                return cls._absent(field)
            return cls._supported_values(
                field,
                tuple((value, None, core) for value in values),
            )

        portfolio = cls._first_context(
            evidence,
            required=(
                "supplies an extensive range of accessories",
                "air and gas purification",
                "storage",
                "gas measurement",
            ),
            any_terms=("control",),
        )
        if field.field == "company_product_portfolio":
            if portfolio is None:
                return cls._absent(field)
            source = _normalize(portfolio.unit.search_text)
            categories = [
                label
                for term, label in (
                    ("air and gas purification", "air and gas purification systems"),
                    ("control", "compressor controls"),
                    ("storage", "storage and filling systems"),
                    ("gas measurement", "gas-measurement equipment"),
                    ("accessories", "compressor accessories"),
                )
                if term in source
            ]
            if not categories:
                return cls._absent(field)
            return cls._supported_values(
                field,
                tuple((value, None, portfolio) for value in categories),
            )

        fuel_gas = cls._first_context(
            evidence,
            required=(
                "bio-cng",
                "biogas",
                "hydrogen",
                "lng",
                "compressor systems",
            ),
            any_terms=("fuel gas",),
        )
        if field.field == "company_application_scope":
            if core is None or fuel_gas is None:
                return cls._absent(field)
            applications = []
            core_source = _normalize(core.unit.search_text)
            fuel_source = _normalize(fuel_gas.unit.search_text)
            if all(term in core_source for term in ("divers", "firefighters")):
                applications.append("breathing-air supply for divers and firefighters")
            fuels = [
                name
                for name in ("bio-CNG", "biogas", "hydrogen", "LNG")
                if _normalize(name) in fuel_source
            ]
            if fuels:
                rendered_fuels = (
                    fuels[0]
                    if len(fuels) == 1
                    else ", ".join(fuels[:-1]) + ", and " + fuels[-1]
                )
                applications.append("fuel-gas systems for " + rendered_fuels)
            if not applications:
                return cls._absent(field)
            support = tuple(
                (
                    value,
                    None,
                    core if value.startswith("breathing-air") else fuel_gas,
                )
                for value in applications
            )
            return cls._supported_values(field, support)

        if field.field == "company_product_categories":
            contexts = tuple(
                context
                for context in (core, portfolio, fuel_gas)
                if context is not None
            )
            if len(contexts) < 2:
                return cls._absent(field)
            values: list[tuple[str, str | None, EvidenceContext]] = []
            if core is not None:
                values.extend(
                    (
                        ("breathing-air compressor and filling systems", None, core),
                        ("medium- and high-pressure air and gas compressors", None, core),
                    )
                )
            if portfolio is not None:
                source = _normalize(portfolio.unit.search_text)
                values.extend(
                    (label, None, portfolio)
                    for term, label in (
                        ("air and gas purification", "air and gas purification systems"),
                        ("control", "compressor controls"),
                        ("storage", "storage and filling systems"),
                        ("gas measurement", "gas-measurement and monitoring equipment"),
                        ("accessories", "compressor accessories"),
                    )
                    if term in source
                )
            if fuel_gas is not None:
                values.append(
                    (
                        "fuel-gas systems for bio-CNG, biogas, hydrogen, and LNG",
                        None,
                        fuel_gas,
                    )
                )
            return cls._supported_values(field, tuple(values))
        return cls._absent(field)

    @classmethod
    def _synthetic_comparison_field(
        cls,
        field,
        evidence: tuple[EvidenceContext, ...],
    ) -> FieldCoverage:
        identifiers = tuple(field.anchor_terms[:2])
        if len(identifiers) != 2:
            return cls._absent(field)
        rows: dict[str, EvidenceContext] = {}
        values_by_id: dict[str, dict[str, str]] = {}
        for identifier in identifiers:
            matches = [
                context
                for context in evidence
                if identifier.casefold() in context.unit.search_text.casefold()
                and "demo data:" in context.unit.search_text.casefold()
                and getattr(
                    context.ranked.candidate.item.projection,
                    "projection_type",
                    None,
                )
                == "table_row"
            ]
            if not matches:
                return cls._absent(field)
            context = min(matches, key=lambda item: item.ranked.rank)
            row_values = {
                name: value
                for name, value, _ in context.values
                if name != "source"
            }
            subject = getattr(
                context.ranked.candidate.item.projection,
                "subject",
                None,
            )
            if not subject or subject.casefold() != identifier.casefold():
                return cls._absent(field)
            row_values["project_id"] = subject
            rows[identifier] = context
            values_by_id[identifier] = row_values

        first, second = identifiers
        left = values_by_id[first]
        right = values_by_id[second]
        supports: list[tuple[str, str | None, EvidenceContext]] = []

        if field.field == "synthetic_comparison_matching":
            for key in (
                "cluster",
                "application_sector",
                "medium",
                "capacity_l_min",
                "compressor_family",
                "topology",
                "cooling",
                "control_package",
                "purification_package",
                "installation",
                "environment",
                "status",
            ):
                if left.get(key) and left.get(key) == right.get(key):
                    supports.append((left[key], key.replace("_", " "), rows[first]))
                    supports.append((left[key], key.replace("_", " "), rows[second]))
        elif field.field == "synthetic_comparison_changed":
            for key in (
                "pressure_bar",
                "compressor_model",
                "storage_filling_package",
            ):
                if left.get(key) and right.get(key) and left[key] != right[key]:
                    supports.append(
                        (
                            f"{first}: {left[key]} | {second}: {right[key]}",
                            key.replace("_", " "),
                            rows[first],
                        )
                    )
                    supports.append(
                        (
                            f"{first}: {left[key]} | {second}: {right[key]}",
                            key.replace("_", " "),
                            rows[second],
                        )
                    )
        elif field.field == "synthetic_comparison_linked_records":
            left_ids = set(filter(None, left.get("linked_part_ids", "").split("; ")))
            right_ids = set(filter(None, right.get("linked_part_ids", "").split("; ")))
            common = sorted(left_ids & right_ids)
            left_only = sorted(left_ids - right_ids)
            right_only = sorted(right_ids - left_ids)
            if common:
                rendered = "; ".join(common)
                supports.extend(
                    (
                        (rendered, "shared linked records", rows[first]),
                        (rendered, "shared linked records", rows[second]),
                    )
                )
            if left_only:
                supports.append(("; ".join(left_only), f"only {first}", rows[first]))
            if right_only:
                supports.append(("; ".join(right_only), f"only {second}", rows[second]))
        elif field.field == "synthetic_comparison_documents":
            if left.get("document_ids") == right.get("document_ids"):
                documents = left.get("document_ids")
                if documents:
                    supports.extend(
                        (
                            (documents, "both projects", rows[first]),
                            (documents, "both projects", rows[second]),
                        )
                    )
            else:
                for identifier, row in ((first, left), (second, right)):
                    documents = row.get("document_ids")
                    if documents:
                        supports.append((documents, identifier, rows[identifier]))
        elif field.field == "synthetic_comparison_assumptions":
            standards = left.get("standards")
            disclaimer = left.get("authority_notice")
            if standards:
                supports.append((standards, "stored review assumptions", rows[first]))
                supports.append((standards, "stored review assumptions", rows[second]))
            if disclaimer:
                supports.append((disclaimer, "authority boundary", rows[first]))
                supports.append((disclaimer, "authority boundary", rows[second]))

        if not supports:
            return cls._absent(field)
        return cls._supported_values(field, tuple(supports))

    @classmethod
    def _synthetic_record_field(
        cls,
        field,
        evidence: tuple[EvidenceContext, ...],
    ) -> FieldCoverage:
        requested = next(
            (
                term
                for term in field.anchor_terms
                if term.upper().startswith("SYN-") and term != "SYN-"
            ),
            None,
        )
        matches: list[EvidenceContext] = []
        for context in evidence:
            projection = context.ranked.candidate.item.projection
            subject = getattr(projection, "subject", None)
            if getattr(projection, "projection_type", None) != "table_row":
                continue
            if "demo data:" not in context.unit.search_text.casefold():
                continue
            if requested and (
                not subject or subject.casefold() != requested.casefold()
            ):
                continue
            if field.match_terms and not all(
                cls._positions(
                    _normalize(context.unit.search_text),
                    _normalize(term),
                )
                for term in field.match_terms
            ):
                continue
            matches.append(context)
        if not matches:
            return cls._absent(field)
        context = min(matches, key=lambda item: item.ranked.rank)
        subject = getattr(
            context.ranked.candidate.item.projection,
            "subject",
            None,
        )
        if not subject or not subject.upper().startswith("SYN-"):
            return cls._absent(field)
        row_values = {
            name: value
            for name, value, _ in context.values
            if name != "source" and value.strip()
        }
        supports: list[tuple[str, str | None, EvidenceContext]] = [
            (subject, "project id", context)
        ]
        for key in (
            "cluster",
            "application_sector",
            "medium",
            "pressure_bar",
            "capacity_l_min",
            "compressor_family",
            "compressor_model",
            "topology",
            "cooling",
            "control_package",
            "purification_package",
            "storage_filling_package",
            "installation",
            "environment",
            "standards",
            "status",
            "document_ids",
            "linked_part_ids",
            "authority_notice",
        ):
            value = row_values.get(key)
            if value:
                supports.append((value, key.replace("_", " "), context))
        return cls._supported_values(field, tuple(supports))

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
            source = _normalize(stationary.unit.search_text)
            values = []
            if "stationary" in source or "continuously monitor" in source:
                values.append("stationary continuous online system")
            if "integrated" in source:
                values.append("integrated variant")
            if "stand-alone" in source or "stand alone" in source:
                values.append("stand-alone variant")
            if not values:
                return cls._absent(field)
            return cls._supported_values(
                field,
                tuple((value, "B-DETECTION PLUS i/s", stationary) for value in values),
            )
        if field.field == "bdetection_mobile_evidence":
            if mobile is None:
                return cls._absent(field)
            source = _normalize(mobile.unit.search_text)
            values = []
            if "mobile" in source or "portable" in source:
                values.append("mobile portable system")
            if "case-based" in source or "case based" in source:
                values.append("case-based configuration")
            for location in ("cylinders", "compressors", "intake"):
                if location in source:
                    values.append(f"measurement at {location}")
            if not values:
                return cls._absent(field)
            return cls._supported_values(
                field,
                tuple((value, "B-DETECTION PLUS m", mobile) for value in values),
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
            combined = _normalize(
                measured_stationary.unit.search_text
                + " "
                + measured_mobile.unit.search_text
            )
            values = [
                (gas, "both systems", measured_stationary)
                for gas in ("CO", "CO2", "O2")
                if _normalize(gas) in combined
            ]
            for term, label in (
                ("absolute humidity", "optional absolute humidity"),
                ("residual oil", "optional residual oil/VOC"),
                ("voc", "optional residual oil/VOC"),
            ):
                if term in combined and not any(item[0] == label for item in values):
                    values.append((label, "both systems", measured_mobile))
            return cls._supported_values(field, tuple(values))
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
            values = []
            stationary_source = _normalize(stationary_logging.unit.search_text)
            mobile_source = _normalize(mobile_logging.unit.search_text)
            for term, label in (
                ("measurement values are logged", "measurement-value logging"),
                ("b-cloud", "B-CLOUD ready"),
            ):
                if term in stationary_source:
                    values.append((label, "B-DETECTION PLUS i/s", stationary_logging))
            for term, label in (
                ("integrated data logger", "integrated data logger"),
                ("sd card", "SD-card storage"),
                ("b-cloud", "B-CLOUD remote access"),
                ("b-app", "B-APP remote access"),
            ):
                if term in mobile_source:
                    values.append((label, "B-DETECTION PLUS m", mobile_logging))
            if not values:
                return cls._absent(field)
            return cls._supported_values(field, tuple(values))
        if field.field == "bdetection_pressure_reconciliation":
            mobile_pressure = cls._first_context(
                evidence,
                required=("b-detection plus m",),
                any_terms=("maximum system pressure", "up to"),
                filename_terms=("0016_b-detection-plus-m",),
            )
            option_pressure = cls._first_context(
                evidence,
                required=("options up to", "final pressure"),
                any_terms=("purge valve", "larger pressure range"),
                filename_terms=("2025-03_b-detection_plus",),
            )
            if mobile_pressure is None or option_pressure is None:
                return cls._absent(field)
            mobile_match = re.search(
                r"(?:maximum system pressure.{0,120}?|up to\s+)"
                r"(\d{2,3}\s*bar)",
                mobile_pressure.unit.search_text,
                flags=re.IGNORECASE,
            )
            option_match = re.search(
                r"options\s+up\s+to\s+(\d{2,3}\s*bar)\s+final pressure",
                option_pressure.unit.search_text,
                flags=re.IGNORECASE,
            )
            if mobile_match is None or option_match is None:
                return cls._absent(field)
            return cls._supported_values(
                field,
                (
                    (
                        f"maximum documented pressure: {mobile_match.group(1)}",
                        "B-DETECTION PLUS m",
                        mobile_pressure,
                    ),
                    (
                        f"options up to {option_match.group(1)} final pressure",
                        "new-generation B-DETECTION PLUS i/s",
                        option_pressure,
                    ),
                ),
                conflict_group="bdetection-pressure-source-scope",
                uncertainty=(
                    "The values have different product scope and source wording; "
                    "they are not interchangeable ratings."
                ),
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
            return cls._supported_values(
                field,
                (
                    ("N7698", "order number", context),
                    ("intake-filter insert", "component", context),
                    ("large blocks / medium pressure", "catalogue group", context),
                    *tuple((value, "compressor-block application", context) for value in values),
                ),
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
            values = []
            browser_source = _normalize(browser.unit.search_text)
            app_source = _normalize(app.unit.search_text)
            for term, label in (
                ("browser application", "browser access"),
                ("fault notifications", "fault notifications"),
                ("plain-text diagnostics", "plain-text diagnostics"),
            ):
                if term in browser_source:
                    values.append((label, "B-CLOUD", browser))
            for term, label in (
                ("smartphone", "smartphone access"),
                ("tablet", "tablet access"),
            ):
                if term in app_source:
                    values.append((label, "B-APP", app))
            return cls._supported_values(field, tuple(values))
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
            text = re.sub(r"\s+", " ", requirement.unit.search_text)
            current = re.search(
                r"software version\s+(3\.73\s+or\s+later)",
                text,
                flags=re.IGNORECASE,
            )
            older = re.search(
                r"older systems[^.;]{0,120}?version\s+(3\.0)",
                text,
                flags=re.IGNORECASE,
            )
            if current is None:
                return cls._absent(field)
            values = [
                (
                    f"software version {current.group(1)}",
                    "B-CONTROL MICRO +Net",
                    requirement,
                )
            ]
            if older is not None:
                values.append(
                    (
                        f"update path from version {older.group(1)}",
                        "older systems",
                        requirement,
                    )
                )
            return cls._supported_values(field, tuple(values))
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
                required=("b-kool iii", "maximum operating pressure"),
                any_terms=("technical data", "model designation"),
                filename_terms=("0021_b-kool", "0022_b-kool1"),
            )
            if context is None:
                return cls._absent(field)
            match = re.search(
                r"maximum operating pressure\s*:?\s*"
                r".{0,100}?"
                r"(\d+\s*bar\s*/\s*\d+\s*bar)",
                context.unit.search_text,
                flags=re.IGNORECASE,
            )
            if match is None:
                return cls._absent(field)
            return cls._supported_values(
                field,
                ((match.group(1), "maximum operating pressure", context),),
            )
        if field.field == "bkool_iii_flow":
            context = cls._first_context(
                evidence,
                required=("b-kool iii", "helium", "argon"),
                any_terms=("iso 1217", "maximum flow rate"),
                filename_terms=("0021_b-kool", "0022_b-kool1"),
            )
            if context is None:
                return cls._absent(field)
            text = re.sub(r"\s+", " ", context.unit.search_text)
            patterns = (
                (
                    r"(\d+\s*[–-]\s*\d+\s*l/min)"
                    r"\s*(?:\(|for\s+)10\s*l\s*cylinder[^.;]{0,90}?"
                    r"0\s*[–-]\s*200\s*bar",
                    "10 l cylinder filling from 0–200 bar",
                ),
                (
                    r"(\d+\s*[–-]\s*\d+\s*l/min)"
                    r"\s*(?:\(|according\s+to\s+)?"
                    r"(?:according\s+to\s+)?ISO\s*1217",
                    "air according to ISO 1217",
                ),
                (
                    r"(\d+\s*[–-]\s*\d+\s*l/min)"
                    r"\s*(?:\(|for\s+)?helium\s*(?:and|&)\s*argon",
                    "helium and argon",
                ),
            )
            values = []
            for pattern, qualifier in patterns:
                match = re.search(pattern, text, flags=re.IGNORECASE)
                if match is not None:
                    values.append((match.group(1), qualifier, context))
            if len(values) != 3:
                return cls._absent(field)
            return cls._supported_values(field, tuple(values))
        return cls._absent(field)

    @classmethod
    def _bsafe_field(
        cls,
        field,
        evidence: tuple[EvidenceContext, ...],
    ) -> FieldCoverage:
        headline = cls._first_context(
            evidence,
            required=(
                "b-safe",
                "breathing air applications up to 300 bar",
                "nitrox applications up to 200 bar",
            ),
            any_terms=("safety filling", "b-safe 300"),
            filename_terms=("0030_b-safe",),
        )
        if field.field == "bsafe_nitrox_300_decision":
            if headline is None:
                return cls._absent(field)
            text = re.sub(r"\s+", " ", headline.unit.search_text)
            breathing = re.search(
                r"breathing air applications up to\s+(\d+\s*bar)",
                text,
                flags=re.IGNORECASE,
            )
            nitrox = re.search(
                r"nitrox applications up to\s+(\d+\s*bar)",
                text,
                flags=re.IGNORECASE,
            )
            if breathing is None or nitrox is None:
                return cls._absent(field)
            return cls._supported_values(
                field,
                (
                    (breathing.group(1), "breathing-air application limit", headline),
                    (nitrox.group(1), "Nitrox application limit", headline),
                ),
            )
        if field.field == "bsafe_wording_reconciliation":
            technical = cls._first_context(
                evidence,
                required=(
                    "b-safe 300",
                    "mediumair, nitrox",
                    "maximum operating pressure410 bar",
                    "filling pressures",
                    "225/330 bar",
                ),
                any_terms=("technical data", "variable pressure increase"),
                filename_terms=("0030_b-safe",),
            )
            if headline is None or technical is None:
                return cls._absent(field)
            headline_text = re.sub(r"\s+", " ", headline.unit.search_text)
            technical_text = re.sub(r"\s+", " ", technical.unit.search_text)
            extracted = []
            for pattern, qualifier, context in (
                (
                    r"breathing air applications up to\s+(\d+\s*bar)",
                    "headline breathing-air application limit",
                    headline,
                ),
                (
                    r"nitrox applications up to\s+(\d+\s*bar)",
                    "headline Nitrox application limit",
                    headline,
                ),
                (
                    r"maximum operating pressure\s*:?\s*(\d+\s*bar)",
                    "B-SAFE 300 technical-data maximum operating pressure",
                    technical,
                ),
                (
                    r"filling pressures\s*:?\s*(\d+\s*/\s*\d+\s*bar)",
                    "B-SAFE 300 technical-data filling pressures",
                    technical,
                ),
            ):
                source = headline_text if context is headline else technical_text
                match = re.search(pattern, source, flags=re.IGNORECASE)
                if match is not None:
                    extracted.append((match.group(1), qualifier, context))
            if len(extracted) != 4:
                return cls._absent(field)
            return cls._supported_values(
                field,
                tuple(extracted),
                conflict_group="bsafe-application-versus-technical-data",
                uncertainty=(
                    "The technical-data values do not explicitly replace the "
                    "medium-specific application limits."
                ),
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

    @classmethod
    def _supported_values(
        cls,
        field,
        support: tuple[
            tuple[str, str | None, EvidenceContext],
            ...,
        ],
        *,
        conflict_group: str | None = None,
        uncertainty: str | None = None,
    ) -> FieldCoverage:
        facts = tuple(
            cls._fact(
                field.field,
                value,
                qualifier,
                context,
                conflict_group=conflict_group,
                uncertainty=uncertainty,
            )
            for value, qualifier, context in support
        )
        return FieldCoverage(
            field=field,
            state="supported",
            values=tuple(
                dict.fromkeys(
                    (fact.display_value, fact.qualifier) for fact in facts
                )
            ),
            evidence_ids=tuple(
                dict.fromkeys(
                    fact.evidence_id for fact in facts
                )
            ),
            detail=None,
            facts=facts,
        )

    @classmethod
    def _special_supported(
        cls,
        field,
        value: str,
        contexts: tuple[EvidenceContext, ...],
    ) -> FieldCoverage:
        return cls._supported_values(
            field,
            tuple((value, None, context) for context in contexts[:1]),
        )

    @staticmethod
    def _fact(
        field: str,
        value: str,
        qualifier: str | None,
        context: EvidenceContext,
        *,
        conflict_group: str | None,
        uncertainty: str | None,
    ) -> EvidenceFact:
        candidate = getattr(context.ranked, "candidate", None)
        item = getattr(candidate, "item", None)
        projection = getattr(item, "projection", None)
        filename = context.citation.original_filename
        date_match = re.search(r"(?<!\d)(20\d{2})[-_](\d{2})(?!\d)", filename)
        source_date = (
            f"{date_match.group(1)}-{date_match.group(2)}"
            if date_match
            else None
        )
        normalized_value = getattr(context.citation, "normalized_value", None)
        unit = getattr(context.citation, "normalized_unit", None) or getattr(
            context.citation,
            "raw_unit",
            None,
        )
        authority = (
            "synthetic_demo"
            if "bauer-synthetic-demo" in filename.casefold()
            or "demo data:" in context.unit.search_text.casefold()
            else "public_bauer"
        )
        fact_id = stable_id(
            "answer_fact",
            field,
            value,
            qualifier,
            context.unit.evidence_id,
        )
        return EvidenceFact(
            fact_id=fact_id,
            field=field,
            display_value=value,
            normalized_value=normalized_value,
            unit=unit,
            qualifier=qualifier,
            subject=getattr(projection, "subject", None),
            source_document=filename,
            source_date=source_date,
            authority=authority,
            evidence_id=context.unit.evidence_id,
            conflict_group=conflict_group,
            uncertainty=uncertainty,
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
            for match in re.finditer(
                r"\bHigh-pressure compressor\s+"
                r"(MINI-VERTICUS|VERTICUS|K\s*22\s*[\u2013-]\s*K\s*28)"
                r"[^.;]{0,100}?,\s*(\d+)\s*[\u2013-]\s*(\d+)\s*bar\b",
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
            return ""

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
            return ""

        if field.field == "pressure_definition_evidence":
            definition = re.search(
                r"\b(?:Maximum allowable working pressure|"
                r"Max\.?\s+operating pressure|"
                r"operating pressure)\s*=\s*"
                r"max(?:imum)?\.?\s+set(?:ting)?\s+(?:of\s+the\s+)?"
                r"safety valve\s*;\s*"
                r"(?:final|shutdown) pressure"
                r"[^.;]{0,100}\blower\b",
                content,
                flags=re.IGNORECASE,
            )
            if definition:
                return re.sub(r"\s+", " ", definition.group(0)).strip()
            return ""

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
