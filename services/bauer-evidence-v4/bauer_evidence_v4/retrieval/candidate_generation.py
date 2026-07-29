from __future__ import annotations

import hashlib
import math
import re
import unicodedata
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from typing import Iterable, Protocol

from .models import (
    AuthorizedScope,
    Candidate,
    CandidateSet,
    ChannelHit,
    IndexedProjection,
    RetrievalRequest,
    StructuredConstraint,
    Subquestion,
)


_TOKEN_RE = re.compile(r"[A-Za-zÀ-ÖØ-öø-ÿ0-9]+(?:[./-][A-Za-z0-9.]+)*")
_EXACT_RE = re.compile(
    r"\b(?:"
    r"N\d{4,}|"
    r"[A-Z]{1,6}(?:[\s.-]?\d)+(?:[./-][A-Z0-9.]+)*|"
    r"(?:ISO|EN)\s+\d{3,5}(?:-\d+)?"
    r")\b",
    re.IGNORECASE,
)
_SYNONYMS = {
    "förderleistung": ("effective", "free", "air", "delivery"),
    "foerderleistung": ("effective", "free", "air", "delivery"),
    "abschalt": ("shutdown",),
    "abschaltdruck": ("shutdown", "pressure"),
    "stufenzahl": ("number", "stages"),
    "stufen": ("stages",),
    "motorleistung": ("motor", "power"),
    "quelle": ("source",),
    "zertifikat": ("certificate",),
    "sprache": ("language",),
    "gewicht": ("weight",),
    "drehzahl": ("rotational", "speed"),
}


def _normalize(value: str) -> str:
    folded = unicodedata.normalize("NFKD", value).casefold()
    folded = "".join(
        character for character in folded if not unicodedata.combining(character)
    )
    return " ".join(_TOKEN_RE.findall(folded))


def _tokens(value: str, *, expand: bool = True) -> tuple[str, ...]:
    values = list(_TOKEN_RE.findall(_normalize(value)))
    if expand:
        for token in tuple(values):
            values.extend(_SYNONYMS.get(token, ()))
    return tuple(values)


def _trigrams(value: str) -> frozenset[str]:
    normalized = f"  {_normalize(value)}  "
    return frozenset(
        normalized[index : index + 3]
        for index in range(max(0, len(normalized) - 2))
    )


def _authorized(item: IndexedProjection, scope: AuthorizedScope) -> bool:
    """The one release/tenant/KB/source predicate used by every channel."""

    return (
        item.tenant_id == scope.tenant_id
        and item.knowledge_base_id == scope.knowledge_base_id
        and item.release_id == scope.release_id
        and item.authorization_source_id
        in scope.authorized_external_source_ids
    )


def _constraint_value(item: IndexedProjection, field_name: str) -> str | None:
    projection = item.projection
    if field_name == "document_number":
        return projection.document_number
    if field_name == "source_filename":
        return projection.source_filename
    if field_name == "subject":
        return projection.subject
    if field_name == "predicate":
        return projection.predicate
    if field_name == "unit":
        return projection.unit_ucum or projection.unit_raw
    raise ValueError(f"unsupported structured field: {field_name}")


def _matches_constraints(
    item: IndexedProjection,
    constraints: tuple[StructuredConstraint, ...],
) -> bool:
    for constraint in constraints:
        actual = _constraint_value(item, constraint.field)
        values = (
            constraint.value
            if isinstance(constraint.value, tuple)
            else (constraint.value,)
        )
        if actual is None:
            return False
        normalized_actual = _normalize(actual)
        normalized_values = {_normalize(value) for value in values}
        if constraint.operator == "eq" and normalized_actual not in normalized_values:
            return False
        if constraint.operator == "in" and normalized_actual not in normalized_values:
            return False
    return True


class CandidateChannel(Protocol):
    channel: str

    def search(
        self,
        query: str,
        *,
        subquestion_id: str,
        scope: AuthorizedScope,
        constraints: tuple[StructuredConstraint, ...],
        limit: int,
    ) -> list[ChannelHit]: ...


@dataclass(frozen=True, slots=True)
class ExactStructuredChannel:
    items: tuple[IndexedProjection, ...]
    channel: str = "exact"

    def search(
        self,
        query: str,
        *,
        subquestion_id: str,
        scope: AuthorizedScope,
        constraints: tuple[StructuredConstraint, ...],
        limit: int,
    ) -> list[ChannelHit]:
        query_normalized = _normalize(query)
        query_identifiers = {
            _normalize(match.group(0)) for match in _EXACT_RE.finditer(query)
        }
        scored: list[tuple[float, IndexedProjection]] = []
        for item in self.items:
            if not _authorized(item, scope):
                continue
            if constraints and not _matches_constraints(item, constraints):
                continue
            projection = item.projection
            terms = {_normalize(term) for term in projection.exact_terms}
            matched = query_identifiers & terms
            score = 20.0 * len(matched)
            for term in terms:
                if len(term) >= 4 and term in query_normalized:
                    score += 3.0
            if constraints:
                score += 10.0
            if score > 0:
                scored.append((score, item))
        scored.sort(
            key=lambda pair: (-pair[0], pair[1].projection.projection_id)
        )
        return [
            ChannelHit(
                item=item,
                channel="exact",
                score=score,
                rank=index + 1,
                subquestion_id=subquestion_id,
            )
            for index, (score, item) in enumerate(scored[:limit])
        ]


@dataclass(frozen=True, slots=True)
class LexicalTrigramChannel:
    items: tuple[IndexedProjection, ...]
    channel: str = "lexical"
    document_frequency: Counter[str] = field(init=False, repr=False)

    def __post_init__(self) -> None:
        frequency: Counter[str] = Counter()
        for item in self.items:
            frequency.update(set(_tokens(item.projection.search_text)))
        object.__setattr__(self, "document_frequency", frequency)

    def search(
        self,
        query: str,
        *,
        subquestion_id: str,
        scope: AuthorizedScope,
        constraints: tuple[StructuredConstraint, ...],
        limit: int,
    ) -> list[ChannelHit]:
        query_tokens = _tokens(query)
        query_counter = Counter(query_tokens)
        query_trigrams = _trigrams(query)
        corpus_size = max(len(self.items), 1)
        scored: list[tuple[float, IndexedProjection]] = []
        for item in self.items:
            if not _authorized(item, scope):
                continue
            if constraints and not _matches_constraints(item, constraints):
                continue
            text_tokens = _tokens(item.projection.search_text)
            text_counter = Counter(text_tokens)
            lexical = 0.0
            for token, query_tf in query_counter.items():
                if token not in text_counter:
                    continue
                df = self.document_frequency.get(token, 0)
                idf = math.log(1 + (corpus_size + 1) / (df + 1))
                lexical += min(query_tf, text_counter[token]) * idf
            text_trigrams = _trigrams(item.projection.search_text)
            trigram = (
                len(query_trigrams & text_trigrams)
                / max(len(query_trigrams | text_trigrams), 1)
            )
            score = lexical + 8.0 * trigram
            if score > 0:
                scored.append((score, item))
        scored.sort(
            key=lambda pair: (-pair[0], pair[1].projection.projection_id)
        )
        return [
            ChannelHit(
                item=item,
                channel="lexical",
                score=score,
                rank=index + 1,
                subquestion_id=subquestion_id,
            )
            for index, (score, item) in enumerate(scored[:limit])
        ]


@dataclass(frozen=True, slots=True)
class HashingDenseEncoder:
    dimensions: int = 256

    def encode(self, value: str) -> tuple[float, ...]:
        vector = [0.0] * self.dimensions
        tokens = _tokens(value)
        features = [*tokens, *(f"{left}_{right}" for left, right in zip(tokens, tokens[1:]))]
        for feature in features:
            digest = hashlib.sha256(feature.encode("utf-8")).digest()
            index = int.from_bytes(digest[:4], "big") % self.dimensions
            sign = 1.0 if digest[4] & 1 else -1.0
            vector[index] += sign
        norm = math.sqrt(sum(value * value for value in vector))
        return tuple(value / norm for value in vector) if norm else tuple(vector)


@dataclass(frozen=True, slots=True)
class DenseChannel:
    items: tuple[IndexedProjection, ...]
    encoder: HashingDenseEncoder = field(default_factory=HashingDenseEncoder)
    channel: str = "dense"
    vectors: tuple[tuple[float, ...], ...] = field(init=False, repr=False)

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "vectors",
            tuple(
                self.encoder.encode(item.projection.search_text)
                for item in self.items
            ),
        )

    def search(
        self,
        query: str,
        *,
        subquestion_id: str,
        scope: AuthorizedScope,
        constraints: tuple[StructuredConstraint, ...],
        limit: int,
    ) -> list[ChannelHit]:
        query_vector = self.encoder.encode(query)
        scored: list[tuple[float, IndexedProjection]] = []
        for item, vector in zip(self.items, self.vectors, strict=True):
            if not _authorized(item, scope):
                continue
            if constraints and not _matches_constraints(item, constraints):
                continue
            score = sum(
                left * right
                for left, right in zip(query_vector, vector, strict=True)
            )
            if score > 0:
                scored.append((score, item))
        scored.sort(
            key=lambda pair: (-pair[0], pair[1].projection.projection_id)
        )
        return [
            ChannelHit(
                item=item,
                channel="dense",
                score=score,
                rank=index + 1,
                subquestion_id=subquestion_id,
            )
            for index, (score, item) in enumerate(scored[:limit])
        ]


@dataclass(frozen=True, slots=True)
class CandidateGenerator:
    items: tuple[IndexedProjection, ...]
    channel_limit: int = 40
    exact: ExactStructuredChannel = field(init=False)
    lexical: LexicalTrigramChannel = field(init=False)
    dense: DenseChannel = field(init=False)

    def __post_init__(self) -> None:
        object.__setattr__(self, "exact", ExactStructuredChannel(self.items))
        object.__setattr__(self, "lexical", LexicalTrigramChannel(self.items))
        object.__setattr__(self, "dense", DenseChannel(self.items))

    def retrieve(
        self,
        request: RetrievalRequest,
        *,
        limit: int = 50,
    ) -> CandidateSet:
        subquestions = request.subquestions or (
            Subquestion("whole_question", request.question),
        )
        all_hits: list[ChannelHit] = []
        channels: tuple[CandidateChannel, ...] = (
            self.exact,
            self.lexical,
            self.dense,
        )
        for subquestion in subquestions:
            query = subquestion.text
            if request.search_hint:
                query = f"{query} {request.search_hint}"
            for channel in channels:
                all_hits.extend(
                    channel.search(
                        query,
                        subquestion_id=subquestion.subquestion_id,
                        scope=request.scope,
                        constraints=request.constraints,
                        limit=self.channel_limit,
                    )
                )
        merged = self._union(all_hits)
        candidates = tuple(
            sorted(
                merged,
                key=lambda item: (
                    -item.candidate_score,
                    item.item.projection.projection_id,
                ),
            )[:limit]
        )
        if any(
            not _authorized(candidate.item, request.scope)
            for candidate in candidates
        ):
            raise AssertionError("candidate union contains unauthorized evidence")
        authorized_count = sum(
            _authorized(item, request.scope) for item in self.items
        )
        hit_counts = Counter(hit.channel for hit in all_hits)
        return CandidateSet(
            original_question=request.question,
            search_hint=request.search_hint,
            candidates=candidates,
            authorized_projection_count=authorized_count,
            channel_hit_counts=tuple(sorted(hit_counts.items())),
        )

    @staticmethod
    def _union(hits: Iterable[ChannelHit]) -> list[Candidate]:
        by_canonical: dict[
            tuple[str, ...],
            dict[str, object],
        ] = {}
        channel_weights = {"exact": 3.0, "lexical": 1.0, "dense": 1.0}
        for hit in hits:
            key = hit.item.canonical_key
            state = by_canonical.setdefault(
                key,
                {
                    "item": hit.item,
                    "rrf": 0.0,
                    "channels": set(),
                    "subquestions": set(),
                    "scores": defaultdict(float),
                },
            )
            state["rrf"] = float(state["rrf"]) + (
                channel_weights[hit.channel] / (60 + hit.rank)
            )
            state["channels"].add(hit.channel)  # type: ignore[union-attr]
            state["subquestions"].add(hit.subquestion_id)  # type: ignore[union-attr]
            scores = state["scores"]
            scores[hit.channel] = max(scores[hit.channel], hit.score)  # type: ignore[index]
            current = state["item"]
            if hit.item.projection.projection_id < current.projection.projection_id:  # type: ignore[union-attr]
                state["item"] = hit.item
        return [
            Candidate(
                item=state["item"],  # type: ignore[arg-type]
                candidate_score=float(state["rrf"]),
                channels=tuple(sorted(state["channels"])),  # type: ignore[arg-type]
                subquestion_ids=tuple(sorted(state["subquestions"])),  # type: ignore[arg-type]
                channel_scores=tuple(
                    sorted(
                        (
                            channel,
                            float(score),
                        )
                        for channel, score in state["scores"].items()  # type: ignore[union-attr]
                    )
                ),
            )
            for state in by_canonical.values()
        ]
