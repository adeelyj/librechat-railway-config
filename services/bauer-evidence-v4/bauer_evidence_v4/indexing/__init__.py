"""Deterministic search projections and exact-identity embedding reuse."""

from .embeddings import (
    EmbeddingCache,
    EmbeddingIdentity,
    EmbeddingRecord,
    EmbeddingSpec,
)
from .models import SearchProjection
from .projections import ProjectionBuilder

__all__ = [
    "EmbeddingCache",
    "EmbeddingIdentity",
    "EmbeddingRecord",
    "EmbeddingSpec",
    "ProjectionBuilder",
    "SearchProjection",
]
