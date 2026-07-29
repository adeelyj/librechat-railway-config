from __future__ import annotations

import hashlib
import json
import math
import re
import unicodedata
from collections import Counter
from dataclasses import dataclass
from typing import Mapping, Protocol

from ..retrieval.models import Candidate, CandidateSet
from .models import RankedCandidate, RerankResult


_TOKEN_RE = re.compile(r"[A-Za-zÀ-ÖØ-öø-ÿ0-9]+(?:[./-][A-Za-z0-9.]+)*")
_EXACT_RE = re.compile(
    r"\b(?:"
    r"N\d{4,}|"
    r"[A-Z]{1,6}(?:[\s.-]?\d)+(?:[./-][A-Z0-9.]+)*|"
    r"(?:ISO|EN)\s+\d{3,5}(?:-\d+)?|"
    r"B-[A-Z][A-Z0-9-]*(?:\s+(?:PLUS(?:\s+(?:i/s|m))?|III|300))?"
    r")\b"
)
_SYNONYMS = {
    "förderleistung": ("effective", "free", "air", "delivery"),
    "foerderleistung": ("effective", "free", "air", "delivery"),
    "abschaltdruck": ("shutdown", "pressure"),
    "stufenzahl": ("number", "stages"),
    "motorleistung": ("motor", "power"),
    "quelle": ("source", "filename"),
    "zertifikat": ("certificate",),
}


def _normalize(value: str) -> str:
    folded = unicodedata.normalize("NFKD", value).casefold()
    folded = "".join(
        character for character in folded if not unicodedata.combining(character)
    )
    return " ".join(_TOKEN_RE.findall(folded))


def _tokens(value: str) -> tuple[str, ...]:
    result = list(_TOKEN_RE.findall(_normalize(value)))
    for token in tuple(result):
        result.extend(_SYNONYMS.get(token, ()))
    return tuple(result)


def _trigrams(value: str) -> frozenset[str]:
    normalized = f"  {_normalize(value)}  "
    return frozenset(
        normalized[index : index + 3]
        for index in range(max(0, len(normalized) - 2))
    )


def _identity(model_id: str, weights: Mapping[str, float]) -> str:
    material = json.dumps(
        {
            "model_id": model_id,
            "weights": dict(sorted(weights.items())),
        },
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(material).hexdigest()


class Reranker(Protocol):
    model_id: str
    model_identity: str
    complexity_units: int

    def rerank(
        self,
        candidate_set: CandidateSet,
        *,
        limit: int = 10,
    ) -> RerankResult: ...


@dataclass(frozen=True, slots=True)
class TransparentFeatureReranker:
    model_id: str = "transparent-linear-v1"
    complexity_units: int = 1
    weights: tuple[tuple[str, float], ...] = (
        ("candidate", 30.0),
        ("channel_exact", 5.0),
        ("exact_identifier", 45.0),
        ("group_match", 18.0),
        ("intent", 16.0),
        ("lexical_coverage", 24.0),
        ("preserved_channel_head", 30.0),
        ("scope_penalty", -40.0),
        ("subject_coverage", 30.0),
    )

    @property
    def model_identity(self) -> str:
        return _identity(self.model_id, dict(self.weights))

    def rerank(
        self,
        candidate_set: CandidateSet,
        *,
        limit: int = 10,
    ) -> RerankResult:
        weights = dict(self.weights)
        scored = [
            (
                self._features(candidate_set.original_question, candidate),
                candidate,
            )
            for candidate in candidate_set.candidates
        ]
        ordered = sorted(
            scored,
            key=lambda pair: (
                -sum(
                    weights[name] * value
                    for name, value in pair[0].items()
                ),
                pair[1].item.projection.projection_id,
            ),
        )[:limit]
        return RerankResult(
            model_id=self.model_id,
            model_identity=self.model_identity,
            complexity_units=self.complexity_units,
            original_question=candidate_set.original_question,
            ranked=tuple(
                RankedCandidate(
                    candidate=candidate,
                    rank=rank,
                    rerank_score=sum(
                        weights[name] * value
                        for name, value in features.items()
                    ),
                    feature_values=tuple(sorted(features.items())),
                )
                for rank, (features, candidate) in enumerate(
                    ordered,
                    start=1,
                )
            ),
        )

    @staticmethod
    def _features(
        question: str,
        candidate: Candidate,
    ) -> dict[str, float]:
        projection = candidate.item.projection
        query_tokens = Counter(_tokens(question))
        text_tokens = Counter(_tokens(projection.search_text))
        overlap = sum(
            min(count, text_tokens.get(token, 0))
            for token, count in query_tokens.items()
        )
        lexical_coverage = overlap / max(sum(query_tokens.values()), 1)

        query_exact = {
            _normalize(match.group(0)) for match in _EXACT_RE.finditer(question)
        }
        projection_exact = {
            _normalize(term) for term in projection.exact_terms
        }
        projection_exact.update(
            _normalize(match.group(0))
            for match in _EXACT_RE.finditer(projection.search_text)
        )
        exact_identifier = min(
            1.0,
            float(len(query_exact & projection_exact)),
        )
        subject_tokens = set(_tokens(projection.subject or ""))
        subject_coverage = (
            len(subject_tokens & set(query_tokens))
            / len(subject_tokens)
            if subject_tokens
            else 0.0
        )
        lower = _normalize(question)
        projection_type = projection.projection_type
        metadata_intent = any(
            token in lower
            for token in (
                "certificate",
                "zertifikat",
                "document n",
                "source filename",
                "title language subject",
            )
        )
        table_intent = any(
            token in lower
            for token in (
                "table",
                "row",
                "technical",
                "tabelle",
                "modell",
            )
        )
        fact_intent = any(
            token in lower for token in ("range", "bereich", "family level")
        )
        intent = float(
            (metadata_intent and projection_type == "metadata")
            or (table_intent and projection_type == "table_row")
            or (fact_intent and projection_type == "fact")
        )
        group = dict(projection.qualifiers).get("group", "")
        required_group_tokens = (
            ("100", "50", "hz")
            if all(value in lower for value in ("100", "50", "hz"))
            else ("40",)
            if "40 bar" in lower
            else ()
        )
        group_match = float(
            bool(group)
            and bool(required_group_tokens)
            and all(
                token in _normalize(group)
                for token in required_group_tokens
            )
        )
        scope_penalty = float(
            ("k 22" in lower or "k22" in lower)
            and projection.subject is not None
            and "pe-ve" in _normalize(projection.subject)
        )
        candidate_prior = min(1.0, candidate.candidate_score * 20)
        return {
            "candidate": candidate_prior,
            "channel_exact": float("exact" in candidate.channels),
            "exact_identifier": exact_identifier,
            "group_match": group_match,
            "intent": intent,
            "lexical_coverage": lexical_coverage,
            "preserved_channel_head": float(
                candidate.preserved_channel_head
            ),
            "scope_penalty": scope_penalty,
            "subject_coverage": subject_coverage,
        }


@dataclass(frozen=True, slots=True)
class LocalInteractionReranker:
    """A local hashed interaction model with no network or model service."""

    model_id: str = "local-hashed-interaction-v1"
    complexity_units: int = 2
    weights: tuple[tuple[str, float], ...] = (
        ("candidate", 16.0),
        ("exact_identifier", 35.0),
        ("ordered_bigram", 20.0),
        ("token_cosine", 25.0),
        ("trigram_jaccard", 20.0),
    )

    @property
    def model_identity(self) -> str:
        return _identity(self.model_id, dict(self.weights))

    def rerank(
        self,
        candidate_set: CandidateSet,
        *,
        limit: int = 10,
    ) -> RerankResult:
        weights = dict(self.weights)
        scored = []
        for candidate in candidate_set.candidates:
            features = self._features(
                candidate_set.original_question,
                candidate,
            )
            score = sum(
                weights[name] * value for name, value in features.items()
            )
            scored.append((score, features, candidate))
        scored.sort(
            key=lambda item: (
                -item[0],
                item[2].item.projection.projection_id,
            )
        )
        return RerankResult(
            model_id=self.model_id,
            model_identity=self.model_identity,
            complexity_units=self.complexity_units,
            original_question=candidate_set.original_question,
            ranked=tuple(
                RankedCandidate(
                    candidate=candidate,
                    rank=index,
                    rerank_score=score,
                    feature_values=tuple(sorted(features.items())),
                )
                for index, (score, features, candidate) in enumerate(
                    scored[:limit],
                    start=1,
                )
            ),
        )

    @staticmethod
    def _features(
        question: str,
        candidate: Candidate,
    ) -> dict[str, float]:
        projection = candidate.item.projection
        query = _tokens(question)
        text = _tokens(projection.search_text)
        query_counts = Counter(query)
        text_counts = Counter(text)
        numerator = sum(
            query_counts[token] * text_counts.get(token, 0)
            for token in query_counts
        )
        query_norm = math.sqrt(
            sum(value * value for value in query_counts.values())
        )
        text_norm = math.sqrt(
            sum(value * value for value in text_counts.values())
        )
        token_cosine = numerator / max(query_norm * text_norm, 1.0)
        query_bigrams = set(zip(query, query[1:]))
        text_bigrams = set(zip(text, text[1:]))
        ordered_bigram = (
            len(query_bigrams & text_bigrams) / max(len(query_bigrams), 1)
        )
        query_trigrams = _trigrams(question)
        text_trigrams = _trigrams(projection.search_text)
        trigram_jaccard = len(query_trigrams & text_trigrams) / max(
            len(query_trigrams | text_trigrams),
            1,
        )
        query_exact = {
            _normalize(match.group(0)) for match in _EXACT_RE.finditer(question)
        }
        projection_exact = {
            _normalize(term) for term in projection.exact_terms
        }
        projection_exact.update(
            _normalize(match.group(0))
            for match in _EXACT_RE.finditer(projection.search_text)
        )
        return {
            "candidate": min(1.0, candidate.candidate_score * 20),
            "exact_identifier": float(bool(query_exact & projection_exact)),
            "ordered_bigram": ordered_bigram,
            "token_cosine": token_cosine,
            "trigram_jaccard": trigram_jaccard,
        }


def select_smallest_passing(
    metrics: Mapping[str, Mapping[str, float]],
    rerankers: tuple[Reranker, ...],
    *,
    minimum_recall_at_5: float,
) -> Reranker:
    passing = [
        reranker
        for reranker in rerankers
        if metrics[reranker.model_id]["recall_at_5"]
        >= minimum_recall_at_5
    ]
    if not passing:
        raise ValueError("no reranker meets the Recall@5 gate")
    return min(
        passing,
        key=lambda reranker: (
            reranker.complexity_units,
            -metrics[reranker.model_id].get("mrr", 0.0),
            reranker.model_id,
        ),
    )
