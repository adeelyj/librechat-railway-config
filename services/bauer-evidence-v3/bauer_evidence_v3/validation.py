from __future__ import annotations

import re
from dataclasses import dataclass
from enum import StrEnum

from .evidence import EvidencePackage
from .planner import (
    QueryPlan,
    extract_numeric_mentions,
    normalize_text,
    numeric_constraint_satisfied,
)


class ValidationDisposition(StrEnum):
    PASS = "pass"
    REPAIR = "repair"
    QUALIFY = "qualify"
    REFUSE = "refuse"


@dataclass(frozen=True, slots=True)
class ValidationViolation:
    code: str
    severity: str
    message: str
    value: str | None = None
    sentence: str | None = None


@dataclass(frozen=True, slots=True)
class ValidationResult:
    valid: bool
    disposition: ValidationDisposition
    citations: tuple[str, ...]
    safe_refusal_detected: bool
    violations: tuple[ValidationViolation, ...]


_CITATION_RE = re.compile(r"\[(E-[A-F0-9]{12})\]", re.IGNORECASE)
_QUOTATION_RE = re.compile(r"[\"“„](.{4,500}?)[\"”]", re.DOTALL)
_HIGH_RISK_NUMBER_RE = re.compile(
    r"\b\d+(?:[.,]\d+)?(?:\s*[–-]\s*\d+(?:[.,]\d+)?)?\s*"
    r"(?:bar(?:g)?|psi(?:g)?|mpa|kpa|pa|l/min|m[³3]/h|cfm|kw|w|rpm|ppm|%|°c)\b",
    re.IGNORECASE,
)
_IDENTIFIER_RE = re.compile(
    r"\b(?:SYN-[A-Z0-9-]+|DOC-[A-Z0-9-]+|N\d{4,7}(?:[_-]\d+)?|"
    r"(?:BM|K|GIB|GI|PE)\s+[A-Z0-9][A-Z0-9./_-]*|"
    r"(?=[A-Z0-9./_-]{3,40}\b)(?=[A-Z0-9./_-]*[A-Z])"
    r"(?=[A-Z0-9./_-]*\d)[A-Z0-9][A-Z0-9./_-]*)\b",
    re.IGNORECASE,
)
_SAFE_REFUSAL_RE = re.compile(
    r"\b(?:not established|not found|not supported|unsupported|does not support|"
    r"not documented|no evidence|no compatible|no matching|insufficient evidence|"
    r"cannot confirm|cannot determine|unable to confirm|nicht belegt|nicht gefunden|"
    r"nicht bestätigt|keine ausreichenden belege|kein kompatib\w*)\b",
    re.IGNORECASE,
)
_SENTENCE_RE = re.compile(r"(?<=[.!?])\s+|\n+")
_WORD_RE = re.compile(r"\b[\w°³/+.-]+\b", re.UNICODE)
_ADVERSATIVE_RE = re.compile(
    r"\b(?:but|however|yet|nevertheless|though|aber|jedoch|dennoch)\b",
    re.IGNORECASE,
)
_NON_CLAIM_TERMS = frozenset(
    {
        "a",
        "an",
        "and",
        "are",
        "as",
        "at",
        "based",
        "be",
        "by",
        "claim",
        "condition",
        "confirmed",
        "data",
        "das",
        "dem",
        "den",
        "der",
        "die",
        "documented",
        "documentation",
        "ein",
        "eine",
        "einer",
        "evidence",
        "for",
        "from",
        "für",
        "has",
        "have",
        "in",
        "is",
        "it",
        "its",
        "listed",
        "mit",
        "of",
        "on",
        "or",
        "provided",
        "question",
        "requested",
        "reported",
        "states",
        "whether",
        "supplied",
        "that",
        "the",
        "this",
        "to",
        "und",
        "vom",
        "von",
        "was",
        "were",
        "with",
        "zum",
        "zur",
    }
)


def validate_answer(
    *,
    answer: str,
    package: EvidencePackage,
    plan: QueryPlan,
) -> ValidationResult:
    violations: list[ValidationViolation] = []
    citation_map = {item.citation_id.upper(): item for item in package.citations}
    citations = tuple(match.group(1).upper() for match in _CITATION_RE.finditer(answer))
    for citation in citations:
        if citation not in citation_map:
            violations.append(
                ValidationViolation(
                    "citation_not_retrieved",
                    "high",
                    "Citation is not part of the authorized evidence package.",
                    citation,
                )
            )

    normalized_answer = normalize_text(answer)
    sentences = [sentence.strip() for sentence in _SENTENCE_RE.split(answer) if sentence.strip()]
    sentence_refusals = [
        _is_safe_refusal_sentence(_CITATION_RE.sub("", sentence))
        for sentence in sentences
    ]
    has_refusal = any(sentence_refusals)
    full_refusal = bool(sentence_refusals) and all(sentence_refusals)
    constraint_terms = {
        term
        for values in plan.mandatory_text_constraints.values()
        for value in values
        for term in _claim_terms(value)
    }
    package_support_terms = {
        term
        for citation in package.citations
        for term in _claim_terms(citation.support_text)
    }
    constraint_terms.update(
        _claim_terms(package.question).intersection(package_support_terms)
    )
    question_important_claims = {
        normalize_text(match.group(0))
        for pattern in (_HIGH_RISK_NUMBER_RE, _IDENTIFIER_RE)
        for match in pattern.finditer(package.question)
    }
    for sentence in sentences:
        sentence_citations = [
            match.group(1).upper() for match in _CITATION_RE.finditer(sentence)
        ]
        cited_support = "\n".join(
            citation_map[citation].support_text
            for citation in sentence_citations
            if citation in citation_map
        )
        normalized_support = normalize_text(cited_support)
        uncited_sentence = _CITATION_RE.sub("", sentence)
        is_refusal_sentence = _is_safe_refusal_sentence(uncited_sentence)

        important_claims: list[tuple[str, str]] = []
        important_claims.extend(
            ("unsupported_number", match.group(0))
            for match in _HIGH_RISK_NUMBER_RE.finditer(uncited_sentence)
        )
        important_claims.extend(
            ("unsupported_identifier", match.group(0))
            for match in _IDENTIFIER_RE.finditer(uncited_sentence)
        )
        for code, raw_claim in important_claims:
            normalized_claim = normalize_text(raw_claim)
            if (
                is_refusal_sentence
                and normalized_claim
                and normalized_claim in question_important_claims
            ):
                continue
            if not sentence_citations:
                violations.append(
                    ValidationViolation(
                        "important_claim_without_adjacent_citation",
                        "high",
                        "Important technical claim lacks a citation in the same sentence.",
                        raw_claim,
                        sentence,
                    )
                )
            elif not normalized_claim or normalized_claim not in normalized_support:
                violations.append(
                    ValidationViolation(
                        code,
                        "high",
                        "Cited evidence does not contain the claimed identifier or number.",
                        raw_claim,
                        sentence,
                    )
                )

        for match in _QUOTATION_RE.finditer(uncited_sentence):
            quotation = normalize_text(match.group(1))
            if not sentence_citations or quotation not in normalized_support:
                violations.append(
                    ValidationViolation(
                        "unsupported_quotation",
                        "high",
                        "Quotation is not present in the citation attached to its sentence.",
                        match.group(1).strip(),
                        sentence,
                    )
                )

        claim_terms = _claim_terms(uncited_sentence).difference(constraint_terms)
        if claim_terms and not is_refusal_sentence:
            if not sentence_citations:
                violations.append(
                    ValidationViolation(
                        "factual_claim_without_adjacent_citation",
                        "high",
                        "Every factual sentence must cite authorized evidence in the same sentence.",
                        " ".join(sorted(claim_terms)[:12]),
                        sentence,
                    )
                )
            else:
                support_terms = _claim_terms(normalized_support)
                unsupported_terms = sorted(claim_terms.difference(support_terms))
                if unsupported_terms:
                    violations.append(
                        ValidationViolation(
                            "unsupported_factual_claim",
                            "high",
                            "Cited evidence does not contain the substantive terms in this claim.",
                            ", ".join(unsupported_terms[:12]),
                            sentence,
                        )
                    )

    synthetic_used = any(item.source_type == "synthetic_demo" for item in package.citations)
    if synthetic_used and "synthetic" not in normalized_answer and "synthet" not in normalized_answer:
        violations.append(
            ValidationViolation(
                "synthetic_source_unlabelled",
                "high",
                "Synthetic evidence is used without an explicit synthetic-data label.",
            )
        )

    for name, values in plan.mandatory_text_constraints.items():
        if not any(value in normalized_answer for value in values) and not full_refusal:
            violations.append(
                ValidationViolation(
                    "mandatory_constraint_missing",
                    "high",
                    f"Mandatory query constraint '{name}' disappeared from the answer.",
                    ", ".join(values),
                )
            )

    answer_numeric_mentions = extract_numeric_mentions(answer)
    required_numeric_groups = [
        (f"query numeric constraint {index + 1}", (constraint,))
        for index, constraint in enumerate(plan.numeric_constraints)
    ]
    required_numeric_groups.extend(
        plan.mandatory_numeric_constraints.items()
    )
    for name, constraints in required_numeric_groups:
        if full_refusal:
            continue
        if not any(
            numeric_constraint_satisfied(
                constraint,
                value=mention.value,
                unit=mention.unit,
            )
            for constraint in constraints
            for mention in answer_numeric_mentions
        ):
            violations.append(
                ValidationViolation(
                    "numeric_constraint_missing",
                    "high",
                    f"Numeric query constraint '{name}' is not satisfied by the answer.",
                    ", ".join(constraint.raw for constraint in constraints),
                )
            )

    for sentence, is_refusal_sentence in zip(sentences, sentence_refusals):
        if is_refusal_sentence:
            continue
        normalized_sentence = normalize_text(sentence)
        for forbidden in plan.forbidden_claim_values:
            if forbidden and forbidden in normalized_sentence:
                violations.append(
                    ValidationViolation(
                        "forbidden_claim_value",
                        "high",
                        "Answer asserts a value explicitly forbidden by the query contract.",
                        forbidden,
                    )
                )
        for mention in extract_numeric_mentions(sentence):
            for forbidden in plan.forbidden_numeric_constraints:
                if numeric_constraint_satisfied(
                    forbidden,
                    value=mention.value,
                    unit=mention.unit,
                ):
                    violations.append(
                        ValidationViolation(
                            "forbidden_claim_value",
                            "high",
                            "Answer asserts a numeric value explicitly forbidden by the query contract.",
                            mention.raw,
                            sentence,
                        )
                    )

    unique: list[ValidationViolation] = []
    seen: set[tuple[str, str | None, str | None]] = set()
    for item in violations:
        key = (item.code, item.value, item.sentence)
        if key not in seen:
            seen.add(key)
            unique.append(item)

    if not unique:
        disposition = ValidationDisposition.PASS
    elif any(
        item.code
        in {
            "unsupported_number",
            "unsupported_identifier",
            "mandatory_constraint_missing",
            "numeric_constraint_missing",
            "forbidden_claim_value",
            "important_claim_without_adjacent_citation",
            "factual_claim_without_adjacent_citation",
            "unsupported_factual_claim",
        }
        for item in unique
    ):
        disposition = ValidationDisposition.REFUSE if has_refusal else ValidationDisposition.REPAIR
    else:
        disposition = ValidationDisposition.QUALIFY
    return ValidationResult(
        valid=not unique,
        disposition=disposition,
        citations=citations,
        safe_refusal_detected=has_refusal,
        violations=tuple(unique),
    )


def _is_safe_refusal_sentence(sentence: str) -> bool:
    normalized = sentence.strip()
    return bool(_SAFE_REFUSAL_RE.search(normalized)) and not bool(
        _ADVERSATIVE_RE.search(normalized)
    )


def _claim_terms(value: str) -> set[str]:
    normalized = normalize_text(value)
    return {
        token
        for token in _WORD_RE.findall(normalized)
        if token not in _NON_CLAIM_TERMS
        and (len(token) >= 3 or any(character.isdigit() for character in token))
    }
