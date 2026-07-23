from __future__ import annotations

import re
from dataclasses import asdict, dataclass
from typing import Any, Iterable

from .extraction import normalize_for_search


CITATION_RE = re.compile(r"\[(V2-\d+)\]", re.IGNORECASE)
QUOTATION_RE = re.compile(r'["“„](.{8,500}?)["”]', re.DOTALL)
HIGH_RISK_NUMBER_RE = re.compile(
    r"\b\d+(?:[.,]\d+)?(?:\s*[–-]\s*\d+(?:[.,]\d+)?)?\s*"
    r"(?:bar|barg|psi|psig|mpa|kpa|l/min|m[³3]/h|cfm|kw|rpm|ppm|%|°c)\b",
    re.IGNORECASE,
)
IDENTIFIER_RE = re.compile(
    r"\b(?:SYN-[A-Z0-9-]+|DOC-[A-Z0-9-]+|N\d{4,7}(?:[_-]\d+)?|"
    r"(?:BM|K|GIB|GI|PE)\s+[A-Z0-9][A-Z0-9./_-]*|"
    r"(?=[A-Z0-9./_-]{3,40}\b)(?=[A-Z0-9./_-]*[A-Z])(?=[A-Z0-9./_-]*\d)"
    r"[A-Z0-9][A-Z0-9./_-]*)\b",
    re.IGNORECASE,
)
SAFE_REFUSAL_RE = re.compile(
    r"\b(?:not established|not found|no compatible|no matching|insufficient evidence|"
    r"cannot confirm|cannot determine|does not exist|nicht belegt|nicht gefunden|"
    r"nicht bestätigt|keine ausreichenden belege|kein kompatib\w*)\b",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class Violation:
    code: str
    severity: str
    message: str
    value: str | None = None


def _evidence_text(evidence: Iterable[dict[str, Any]]) -> str:
    parts: list[str] = []
    for item in evidence:
        parts.extend(
            (
                str(item.get("content", "")),
                str(item.get("filename", "")),
                str(item.get("title", "")),
                str(item.get("row_label", "")),
                " ".join(str(value) for value in item.get("row_values", []) or []),
            )
        )
    return normalize_for_search("\n".join(parts))


def validate_answer(
    *,
    answer: str,
    evidence: list[dict[str, Any]],
    mandatory_constraints: dict[str, Any] | None = None,
    safe_refusal_expected: bool = False,
) -> dict[str, Any]:
    mandatory_constraints = mandatory_constraints or {}
    violations: list[Violation] = []
    evidence_by_id = {
        str(item.get("citation_id", "")).upper(): item
        for item in evidence
        if item.get("citation_id")
    }
    citations = [match.group(1).upper() for match in CITATION_RE.finditer(answer)]
    for citation in citations:
        if citation not in evidence_by_id:
            violations.append(
                Violation(
                    "citation_not_retrieved",
                    "high",
                    "Citation does not map to an authorized retrieved evidence item.",
                    citation,
                )
            )

    normalized_evidence = _evidence_text(evidence)
    for match in QUOTATION_RE.finditer(answer):
        quotation = normalize_for_search(match.group(1))
        if quotation and quotation not in normalized_evidence:
            violations.append(
                Violation(
                    "quotation_not_in_evidence",
                    "high",
                    "Quoted text is not present in the retrieved evidence.",
                    match.group(1).strip(),
                )
            )

    for pattern, code, message in (
        (
            HIGH_RISK_NUMBER_RE,
            "unsupported_number",
            "Important number and unit are not present in retrieved evidence.",
        ),
        (
            IDENTIFIER_RE,
            "unsupported_identifier",
            "Identifier is not present in retrieved evidence.",
        ),
    ):
        seen: set[str] = set()
        for match in pattern.finditer(answer):
            raw = match.group(0)
            normalized = normalize_for_search(raw)
            if normalized.startswith("v2 "):
                continue
            if normalized in seen:
                continue
            seen.add(normalized)
            if normalized not in normalized_evidence:
                violations.append(Violation(code, "high", message, raw))

    answer_normalized = normalize_for_search(answer)
    synthetic_items = [item for item in evidence if item.get("source_type") == "synthetic_demo"]
    if synthetic_items and "synthetic" not in answer_normalized and "synthet" not in answer_normalized:
        violations.append(
            Violation(
                "synthetic_source_unlabelled",
                "high",
                "Answer uses synthetic evidence without labelling it as synthetic demo data.",
            )
        )

    for name, value in mandatory_constraints.items():
        if value is None:
            continue
        values = value if isinstance(value, list) else [value]
        if not any(normalize_for_search(str(item)) in answer_normalized for item in values):
            if not SAFE_REFUSAL_RE.search(answer):
                violations.append(
                    Violation(
                        "mandatory_constraint_missing",
                        "high",
                        f"Mandatory constraint '{name}' is absent from the answer.",
                        ", ".join(str(item) for item in values),
                    )
                )

    has_refusal = bool(SAFE_REFUSAL_RE.search(answer))
    if safe_refusal_expected and not has_refusal:
        violations.append(
            Violation(
                "required_safe_refusal_missing",
                "high",
                "The gold case requires a qualified no-answer or safe refusal.",
            )
        )
    if not citations and not has_refusal and (
        HIGH_RISK_NUMBER_RE.search(answer) or IDENTIFIER_RE.search(answer)
    ):
        violations.append(
            Violation(
                "important_claim_without_citation",
                "medium",
                "An important numerical or identifier claim has no V2 evidence citation.",
            )
        )

    high = sum(item.severity == "high" for item in violations)
    status = "pass"
    if high:
        status = "refuse" if any(
            item.code
            in {
                "unsupported_number",
                "unsupported_identifier",
                "required_safe_refusal_missing",
                "mandatory_constraint_missing",
            }
            for item in violations
        ) else "qualify"
    elif violations:
        status = "qualify"
    return {
        "status": status,
        "valid": not violations,
        "safe_refusal_detected": has_refusal,
        "citation_count": len(citations),
        "violations": [asdict(item) for item in violations],
    }
