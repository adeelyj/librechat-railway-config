from __future__ import annotations

import asyncio
import json
import math
import os
import re
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from typing import Any, Iterable

from .extraction import normalize_for_search


IDENTIFIER_RE = re.compile(
    r"\b(?:(?:BM|K|GIB|GI|PE)\s+[A-Z0-9][A-Z0-9./_-]*|"
    r"(?=[A-Z0-9./_-]{2,40}\b)(?=[A-Z0-9./_-]*[A-Z])(?=[A-Z0-9./_-]*\d)"
    r"[A-Z0-9][A-Z0-9./_-]*)\b",
    re.IGNORECASE,
)
NUMBER_UNIT_RE = re.compile(
    r"\b\d+(?:[.,]\d+)?\s*(?:bar|barg|psi|psig|mpa|kpa|l/min|m[³3]/h|cfm|kw|rpm|ppm|%)\b",
    re.IGNORECASE,
)
QUOTED_RE = re.compile(r'["“„](.+?)["”]', re.DOTALL)
TABLE_INTENT_RE = re.compile(
    r"\b(?:table|technical data|model|row|maximum|minimum|pressure|capacity|"
    r"tabelle|technische daten|modell|zeile|maximal|mindestens|druck|leistung)\b",
    re.IGNORECASE,
)
QUERY_ALIASES = {
    "stickstoff": "nitrogen",
    "atemluft": "breathing air",
    "druckluft": "compressed air",
    "nachverdichter": "booster",
    "kompressor": "compressor",
    "trockner": "dryer",
    "ventil": "valve",
    "steuerung": "control",
}


@dataclass(frozen=True)
class QueryAnalysis:
    original: str
    normalized: str
    tokens: tuple[str, ...]
    identifiers: tuple[str, ...]
    number_units: tuple[str, ...]
    quoted_phrases: tuple[str, ...]
    table_intent: bool


@dataclass
class Candidate:
    chunk_id: str
    file_id: str
    filename: str
    title: str | None
    language: str | None
    publication_date: str | None
    certificates: list[str]
    product_families: list[str]
    media: list[str]
    component_categories: list[str]
    standards: list[str]
    content: str
    page: int | None
    section_path: list[str]
    chunk_kind: str
    table_title: str | None = None
    row_label: str | None = None
    headers: list[str] = field(default_factory=list)
    row_values: list[str] = field(default_factory=list)
    units: list[str] = field(default_factory=list)
    footnotes: str | None = None
    source_type: str = "public_document"
    metadata: dict[str, Any] = field(default_factory=dict)
    channels: dict[str, dict[str, Any]] = field(default_factory=dict)
    fusion_score: float = 0.0
    rerank_score: float = 0.0
    final_score: float = 0.0

    @property
    def location_key(self) -> str:
        section = "/".join(self.section_path)
        return "\0".join(
            (
                self.file_id,
                str(self.page or ""),
                self.table_title or "",
                self.row_label or "",
                section,
                normalize_for_search(self.content[:240]),
            )
        )


def analyze_query(query: str) -> QueryAnalysis:
    normalized = normalize_for_search(query)
    base_tokens = [token for token in normalized.split() if len(token) > 1]
    aliased_tokens = [
        alias_token
        for token in base_tokens
        for alias_token in QUERY_ALIASES.get(token, "").split()
    ]
    tokens = tuple(dict.fromkeys([*base_tokens, *aliased_tokens]))
    identifiers = tuple(
        dict.fromkeys(normalize_for_search(match.group(0)) for match in IDENTIFIER_RE.finditer(query))
    )
    number_units = tuple(
        dict.fromkeys(normalize_for_search(match.group(0)) for match in NUMBER_UNIT_RE.finditer(query))
    )
    quoted = tuple(
        dict.fromkeys(normalize_for_search(match.group(1)) for match in QUOTED_RE.finditer(query))
    )
    return QueryAnalysis(
        original=query,
        normalized=normalized,
        tokens=tokens,
        identifiers=identifiers,
        number_units=number_units,
        quoted_phrases=quoted,
        table_intent=bool(TABLE_INTENT_RE.search(query)),
    )


def reciprocal_rank_fusion(
    channel_results: dict[str, list[Candidate]],
    *,
    rrf_k: int = 60,
    exact_boost: float = 0.035,
) -> list[Candidate]:
    by_location: dict[str, Candidate] = {}
    for channel, candidates in channel_results.items():
        for rank, candidate in enumerate(candidates, start=1):
            existing = by_location.get(candidate.location_key)
            if existing is None:
                existing = candidate
                by_location[candidate.location_key] = existing
            channel_info = dict(candidate.channels.get(channel, {}))
            channel_info["rank"] = rank
            existing.channels[channel] = channel_info
            existing.fusion_score += 1.0 / (rrf_k + rank)
            if channel == "exact":
                existing.fusion_score += exact_boost
    return sorted(
        by_location.values(),
        key=lambda item: (-item.fusion_score, item.file_id, item.page or 0, item.chunk_id),
    )


def limit_candidates_per_file(
    candidates: Iterable[Candidate],
    *,
    top_n: int,
    max_per_file: int = 2,
) -> list[Candidate]:
    selected: list[Candidate] = []
    per_file: dict[str, int] = {}
    for candidate in candidates:
        if per_file.get(candidate.file_id, 0) >= max_per_file:
            continue
        selected.append(candidate)
        per_file[candidate.file_id] = per_file.get(candidate.file_id, 0) + 1
        if len(selected) == top_n:
            break
    return selected


def filter_candidates_by_score(
    candidates: Iterable[Candidate],
    *,
    minimum_score: float,
) -> list[Candidate]:
    return [
        candidate
        for candidate in candidates
        if math.isfinite(candidate.final_score)
        and candidate.final_score >= minimum_score
    ]


def _token_overlap(analysis: QueryAnalysis, candidate: Candidate) -> float:
    haystack = normalize_for_search(
        "\n".join(
            (
                candidate.filename,
                candidate.title or "",
                candidate.table_title or "",
                candidate.row_label or "",
                candidate.content,
            )
        )
    )
    if not analysis.tokens:
        return 0.0
    matched = sum(1 for token in analysis.tokens if token in haystack)
    return matched / len(analysis.tokens)


def deterministic_rerank(
    analysis: QueryAnalysis,
    candidates: Iterable[Candidate],
    *,
    top_n: int = 8,
) -> list[Candidate]:
    reranked: list[Candidate] = []
    for candidate in candidates:
        searchable = normalize_for_search(
            f"{candidate.filename}\n{candidate.title or ''}\n{candidate.content}"
        )
        overlap = _token_overlap(analysis, candidate)
        identifier_hits = sum(1 for value in analysis.identifiers if value in searchable)
        number_hits = sum(1 for value in analysis.number_units if value in searchable)
        quoted_hits = sum(1 for value in analysis.quoted_phrases if value in searchable)
        table_bonus = 0.09 if analysis.table_intent and candidate.chunk_kind == "table_row" else 0.0
        candidate.rerank_score = (
            overlap * 0.55
            + min(identifier_hits, 2) * 0.12
            + min(number_hits, 2) * 0.08
            + min(quoted_hits, 1) * 0.12
            + table_bonus
        )
        candidate.final_score = candidate.fusion_score + candidate.rerank_score
        candidate.metadata["reranker"] = "deterministic-multilingual-fallback-v1"
        reranked.append(candidate)
    reranked.sort(
        key=lambda item: (-item.final_score, -item.fusion_score, item.file_id, item.chunk_id)
    )
    return reranked[:top_n]


def _remote_rerank_sync(
    url: str,
    model: str,
    analysis: QueryAnalysis,
    candidates: list[Candidate],
    timeout_seconds: float,
) -> dict[int, float]:
    payload = {
        "model": model,
        "query": analysis.original,
        "documents": [candidate.content for candidate in candidates],
        "top_n": len(candidates),
    }
    request = urllib.request.Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    api_key = os.getenv("BAUER_RAG_V2_RERANK_API_KEY", "")
    if api_key:
        request.add_header("Authorization", f"Bearer {api_key}")
    with urllib.request.urlopen(request, timeout=timeout_seconds) as response:
        parsed = json.loads(response.read().decode("utf-8"))
    scores: dict[int, float] = {}
    for item in parsed.get("results", []):
        index = item.get("index")
        score = item.get("relevance_score", item.get("score"))
        if isinstance(index, int) and isinstance(score, (int, float)) and 0 <= index < len(candidates):
            scores[index] = float(score)
    if len(scores) != len(candidates):
        raise ValueError("Reranker did not return one score for every candidate")
    return scores


async def rerank_candidates(
    analysis: QueryAnalysis,
    candidates: list[Candidate],
    *,
    top_n: int = 8,
) -> tuple[list[Candidate], dict[str, Any]]:
    bounded = candidates[: min(max(1, int(os.getenv("BAUER_RAG_V2_RERANK_CANDIDATES", "25"))), 30)]
    url = os.getenv("BAUER_RAG_V2_RERANK_URL", "").strip()
    model = os.getenv("BAUER_RAG_V2_RERANK_MODEL", "").strip()
    timeout_seconds = min(
        max(float(os.getenv("BAUER_RAG_V2_RERANK_TIMEOUT_SECONDS", "1.5")), 0.1),
        10.0,
    )
    if not url or not model:
        return deterministic_rerank(analysis, bounded, top_n=top_n), {
            "mode": "fusion_plus_deterministic_fallback",
            "fallback_reason": "remote_reranker_not_configured",
        }

    try:
        scores = await asyncio.to_thread(
            _remote_rerank_sync,
            url,
            model,
            analysis,
            bounded,
            timeout_seconds,
        )
        for index, candidate in enumerate(bounded):
            score = scores[index]
            if not math.isfinite(score):
                raise ValueError("Reranker returned a non-finite score")
            candidate.rerank_score = score
            candidate.final_score = candidate.fusion_score + score
            candidate.metadata["reranker"] = model
        bounded.sort(
            key=lambda item: (-item.final_score, -item.fusion_score, item.file_id, item.chunk_id)
        )
        return bounded[:top_n], {"mode": "remote", "model": model}
    except (OSError, ValueError, json.JSONDecodeError, urllib.error.URLError) as error:
        return deterministic_rerank(analysis, bounded, top_n=top_n), {
            "mode": "fusion_plus_deterministic_fallback",
            "fallback_reason": type(error).__name__,
        }
