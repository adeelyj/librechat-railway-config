from __future__ import annotations

import math
from collections import Counter, defaultdict
from dataclasses import dataclass, replace
from decimal import Decimal
from typing import Protocol

from .models import EvidenceItem
from .planner import QueryPlan, RetrievalChannel, normalize_text


class EmbeddingProvider(Protocol):
    def embed(self, text: str) -> tuple[float, ...]: ...


class HashingEmbeddingProvider:
    """Deterministic local fallback for tests, not a production semantic model."""

    def __init__(self, dimensions: int = 64) -> None:
        if dimensions < 8:
            raise ValueError("dimensions must be at least eight")
        self.dimensions = dimensions

    def embed(self, text: str) -> tuple[float, ...]:
        vector = [0.0] * self.dimensions
        for token in normalize_text(text).split():
            bucket = hash_token(token) % self.dimensions
            sign = -1.0 if hash_token(f"sign:{token}") & 1 else 1.0
            vector[bucket] += sign
        norm = math.sqrt(sum(value * value for value in vector))
        if norm:
            vector = [value / norm for value in vector]
        return tuple(vector)


def hash_token(token: str) -> int:
    value = 2166136261
    for byte in token.encode("utf-8"):
        value ^= byte
        value = (value * 16777619) & 0xFFFFFFFF
    return value


@dataclass(frozen=True, slots=True)
class SearchUnit:
    search_unit_id: str
    evidence: EvidenceItem
    unit_type: str
    search_text: str
    exact_terms: tuple[str, ...] = ()
    subject: str | None = None
    predicate: str | None = None
    numeric_value: Decimal | None = None
    unit: str | None = None
    embedding: tuple[float, ...] = ()
    navigation_path: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if self.evidence.release_id == "":
            raise ValueError("search units must belong to a release")
        if self.unit_type not in {
            "block",
            "table_row",
            "table_cell",
            "fact",
            "document",
            "navigation_summary",
        }:
            raise ValueError(f"unsupported search unit type: {self.unit_type}")


@dataclass(frozen=True, slots=True)
class NavigationNode:
    node_id: str
    release_id: str
    label: str
    description: str
    linked_evidence_ids: tuple[str, ...]
    aliases: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class RetrievalResult:
    evidence: EvidenceItem
    score: float
    channels: tuple[str, ...]
    reasons: tuple[str, ...]


_CHANNEL_WEIGHT = {
    RetrievalChannel.EXACT: 3.0,
    RetrievalChannel.FACT: 2.8,
    RetrievalChannel.TABLE: 2.5,
    RetrievalChannel.LEXICAL: 1.2,
    RetrievalChannel.SEMANTIC: 0.8,
    RetrievalChannel.NAVIGATION: 0.7,
}
_STRUCTURED_TYPES = {"fact", "table_row", "table_cell"}


class InMemoryEvidenceIndex:
    """Release-pinned reference index for the local V3 vertical slice."""

    def __init__(self, embedding_provider: EmbeddingProvider | None = None) -> None:
        self.embedding_provider = embedding_provider or HashingEmbeddingProvider()
        self._units: dict[str, list[SearchUnit]] = defaultdict(list)
        self._navigation: dict[str, list[NavigationNode]] = defaultdict(list)

    def add(self, unit: SearchUnit) -> None:
        if not unit.embedding:
            unit = replace(unit, embedding=self.embedding_provider.embed(unit.search_text))
        if any(
            existing.search_unit_id == unit.search_unit_id
            for existing in self._units[unit.evidence.release_id]
        ):
            raise ValueError(f"duplicate search unit: {unit.search_unit_id}")
        self._units[unit.evidence.release_id].append(unit)

    def add_navigation(self, node: NavigationNode) -> None:
        if any(existing.node_id == node.node_id for existing in self._navigation[node.release_id]):
            raise ValueError(f"duplicate navigation node: {node.node_id}")
        self._navigation[node.release_id].append(node)

    def retrieve(
        self,
        plan: QueryPlan,
        *,
        release_id: str,
        authorized_source_ids: set[str] | frozenset[str],
    ) -> tuple[RetrievalResult, ...]:
        if not authorized_source_ids:
            return ()
        units = [
            unit
            for unit in self._units.get(release_id, ())
            if self._authorized(unit.evidence, authorized_source_ids)
        ]
        if not units:
            return ()
        by_evidence_id = {unit.evidence.evidence_id: unit for unit in units}
        channel_rankings: dict[RetrievalChannel, list[tuple[SearchUnit, float, str]]] = {}
        for channel in plan.channels:
            if channel is RetrievalChannel.EXACT:
                ranking = self._exact(plan, units)
            elif channel is RetrievalChannel.FACT:
                ranking = self._facts(plan, units)
            elif channel is RetrievalChannel.TABLE:
                ranking = self._tables(plan, units)
            elif channel is RetrievalChannel.LEXICAL:
                ranking = self._lexical(plan, units)
            elif channel is RetrievalChannel.SEMANTIC:
                ranking = self._semantic(plan, units)
            elif channel is RetrievalChannel.NAVIGATION:
                ranking = self._navigation_expand(plan, release_id, by_evidence_id)
            else:  # pragma: no cover - exhaustive enum guard
                ranking = []
            if ranking:
                channel_rankings[channel] = ranking

        fused: dict[str, float] = defaultdict(float)
        channels: dict[str, list[str]] = defaultdict(list)
        reasons: dict[str, list[str]] = defaultdict(list)
        evidence_by_id: dict[str, EvidenceItem] = {}
        unit_type_by_id: dict[str, str] = {}
        for channel, ranking in channel_rankings.items():
            weight = _CHANNEL_WEIGHT[channel]
            for rank, (unit, raw_score, reason) in enumerate(ranking, start=1):
                evidence_id = unit.evidence.evidence_id
                evidence_by_id[evidence_id] = unit.evidence
                unit_type_by_id[evidence_id] = unit.unit_type
                fused[evidence_id] += weight / (20.0 + rank)
                fused[evidence_id] += min(max(raw_score, 0.0), 1.0) * weight * 0.002
                channels[evidence_id].append(channel.value)
                reasons[evidence_id].append(reason)

        for evidence_id, unit_type in unit_type_by_id.items():
            if unit_type in _STRUCTURED_TYPES and (
                plan.table_intent or RetrievalChannel.FACT in plan.channels
            ):
                fused[evidence_id] += 0.05

        ordered = sorted(
            fused,
            key=lambda evidence_id: (
                -fused[evidence_id],
                evidence_by_id[evidence_id].source_document_id,
                evidence_by_id[evidence_id].coordinate.page_number,
                evidence_id,
            ),
        )
        results: list[RetrievalResult] = []
        seen_locations: set[tuple[object, ...]] = set()
        for evidence_id in ordered:
            evidence = evidence_by_id[evidence_id]
            location = (
                evidence.source_version_id,
                evidence.coordinate.page_number,
                evidence.coordinate.block_id,
                evidence.coordinate.table_id,
                evidence.coordinate.row_index,
                evidence.coordinate.cell_id,
                normalize_text(evidence.content),
            )
            if location in seen_locations:
                continue
            seen_locations.add(location)
            channel_values = tuple(dict.fromkeys(channels[evidence_id]))
            projected = replace(
                evidence,
                retrieval_channels=channel_values,
                score=fused[evidence_id],
            )
            if not projected.is_citable or projected.generated_summary:
                continue
            results.append(
                RetrievalResult(
                    evidence=projected,
                    score=fused[evidence_id],
                    channels=channel_values,
                    reasons=tuple(dict.fromkeys(reasons[evidence_id])),
                )
            )
            if len(results) >= plan.top_k:
                break
        return tuple(results)

    @staticmethod
    def _authorized(
        evidence: EvidenceItem,
        authorized_source_ids: set[str] | frozenset[str],
    ) -> bool:
        external_file_id = str(evidence.metadata.get("external_file_id", ""))
        return (
            evidence.source_document_id in authorized_source_ids
            or (external_file_id and external_file_id in authorized_source_ids)
        )

    @staticmethod
    def _exact(
        plan: QueryPlan,
        units: list[SearchUnit],
    ) -> list[tuple[SearchUnit, float, str]]:
        needles = {
            normalize_text(value)
            for value in (*plan.identifiers, *plan.quoted_phrases)
            if normalize_text(value)
        }
        if not needles:
            return []
        ranked: list[tuple[SearchUnit, float, str]] = []
        for unit in units:
            haystack = {normalize_text(term) for term in unit.exact_terms}
            haystack.add(normalize_text(unit.search_text))
            matches = [
                needle
                for needle in needles
                if any(needle == value or needle in value for value in haystack)
            ]
            if matches:
                ranked.append(
                    (unit, len(matches) / len(needles), f"exact:{','.join(sorted(matches))}")
                )
        return sorted(
            ranked,
            key=lambda item: (-item[1], item[0].search_unit_id),
        )

    @staticmethod
    def _facts(
        plan: QueryPlan,
        units: list[SearchUnit],
    ) -> list[tuple[SearchUnit, float, str]]:
        facts = [unit for unit in units if unit.unit_type == "fact"]
        if not facts:
            return []
        query_tokens = set(plan.tokens)
        ranked: list[tuple[SearchUnit, float, str]] = []
        for unit in facts:
            subject_tokens = set(normalize_text(unit.subject or "").split())
            predicate_tokens = set(normalize_text(unit.predicate or "").split("_"))
            lexical = len(query_tokens & (subject_tokens | predicate_tokens)) / max(
                len(query_tokens), 1
            )
            numeric = 0.0
            if plan.numeric_mentions and unit.numeric_value is not None:
                numeric = max(
                    1.0 if mention.value == unit.numeric_value else 0.0
                    for mention in plan.numeric_mentions
                )
                requested_units = {
                    mention.unit for mention in plan.numeric_mentions if mention.unit is not None
                }
                if requested_units and normalize_text(unit.unit or "") not in requested_units:
                    numeric = 0.0
            score = max(lexical, numeric)
            if score or plan.superlative:
                ranked.append((unit, score + 0.1, f"fact:{unit.predicate or 'typed'}"))
        if plan.superlative and any(unit.numeric_value is not None for unit, _, _ in ranked):
            reverse = plan.superlative == "maximum"
            ranked.sort(
                key=lambda item: (
                    item[0].numeric_value is None,
                    -(item[0].numeric_value or Decimal(0))
                    if reverse
                    else (item[0].numeric_value or Decimal(0)),
                    item[0].search_unit_id,
                )
            )
            return ranked
        return sorted(ranked, key=lambda item: (-item[1], item[0].search_unit_id))

    @staticmethod
    def _tables(
        plan: QueryPlan,
        units: list[SearchUnit],
    ) -> list[tuple[SearchUnit, float, str]]:
        query_tokens = set(plan.tokens)
        ranked: list[tuple[SearchUnit, float, str]] = []
        for unit in units:
            if unit.unit_type not in {"table_row", "table_cell"}:
                continue
            tokens = set(normalize_text(unit.search_text).split())
            overlap = len(query_tokens & tokens) / max(len(query_tokens), 1)
            exact_bonus = 0.2 if any(
                normalize_text(identifier) in normalize_text(unit.search_text)
                for identifier in plan.identifiers
            ) else 0.0
            if overlap or exact_bonus:
                ranked.append((unit, min(1.0, overlap + exact_bonus), "structured_table"))
        return sorted(ranked, key=lambda item: (-item[1], item[0].search_unit_id))

    @staticmethod
    def _lexical(
        plan: QueryPlan,
        units: list[SearchUnit],
    ) -> list[tuple[SearchUnit, float, str]]:
        query_counts = Counter(plan.tokens)
        if not query_counts:
            return []
        document_frequency: Counter[str] = Counter()
        unit_tokens: dict[str, Counter[str]] = {}
        for unit in units:
            counts = Counter(normalize_text(unit.search_text).split())
            unit_tokens[unit.search_unit_id] = counts
            document_frequency.update(counts.keys())
        ranked: list[tuple[SearchUnit, float, str]] = []
        for unit in units:
            counts = unit_tokens[unit.search_unit_id]
            score = 0.0
            matches: list[str] = []
            for token, query_frequency in query_counts.items():
                if token not in counts:
                    continue
                inverse = math.log((len(units) + 1) / (document_frequency[token] + 0.5)) + 1
                score += min(counts[token], query_frequency) * inverse
                matches.append(token)
            if matches:
                normalized_score = score / max(len(query_counts), 1)
                ranked.append(
                    (unit, min(normalized_score, 1.0), f"lexical:{','.join(matches[:8])}")
                )
        return sorted(ranked, key=lambda item: (-item[1], item[0].search_unit_id))

    def _semantic(
        self,
        plan: QueryPlan,
        units: list[SearchUnit],
    ) -> list[tuple[SearchUnit, float, str]]:
        query_vector = self.embedding_provider.embed(plan.query)
        ranked: list[tuple[SearchUnit, float, str]] = []
        for unit in units:
            if not unit.embedding or len(unit.embedding) != len(query_vector):
                continue
            similarity = sum(left * right for left, right in zip(query_vector, unit.embedding))
            if similarity > 0:
                ranked.append((unit, min(similarity, 1.0), "semantic"))
        return sorted(ranked, key=lambda item: (-item[1], item[0].search_unit_id))

    def _navigation_expand(
        self,
        plan: QueryPlan,
        release_id: str,
        by_evidence_id: dict[str, SearchUnit],
    ) -> list[tuple[SearchUnit, float, str]]:
        query_tokens = set(plan.tokens)
        expanded: dict[str, tuple[SearchUnit, float, str]] = {}
        for node in self._navigation.get(release_id, ()):
            node_tokens = set(normalize_text(" ".join((node.label, node.description, *node.aliases))).split())
            overlap = len(query_tokens & node_tokens) / max(len(query_tokens), 1)
            if not overlap:
                continue
            for evidence_id in node.linked_evidence_ids:
                unit = by_evidence_id.get(evidence_id)
                if unit and (
                    evidence_id not in expanded or overlap > expanded[evidence_id][1]
                ):
                    expanded[evidence_id] = (unit, overlap, f"navigation:{node.node_id}")
        return sorted(expanded.values(), key=lambda item: (-item[1], item[0].search_unit_id))
