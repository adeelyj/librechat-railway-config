from __future__ import annotations

from dataclasses import dataclass

from ..retrieval.models import Candidate


@dataclass(frozen=True, slots=True)
class RankedCandidate:
    candidate: Candidate
    rank: int
    rerank_score: float
    feature_values: tuple[tuple[str, float], ...]


@dataclass(frozen=True, slots=True)
class RerankResult:
    model_id: str
    model_identity: str
    complexity_units: int
    original_question: str
    ranked: tuple[RankedCandidate, ...]

    def __post_init__(self) -> None:
        if not self.original_question.strip():
            raise ValueError("reranker must retain the original question")
        if tuple(item.rank for item in self.ranked) != tuple(
            range(1, len(self.ranked) + 1)
        ):
            raise ValueError("ranked candidates must have consecutive ranks")
