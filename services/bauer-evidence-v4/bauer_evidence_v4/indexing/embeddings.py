from __future__ import annotations

import hashlib
import json
import math
from dataclasses import dataclass, field
from typing import Protocol, Sequence

from .models import SearchProjection


@dataclass(frozen=True, slots=True)
class EmbeddingSpec:
    provider: str
    model: str
    revision: str
    dimensions: int
    normalization: str

    def __post_init__(self) -> None:
        for field_name in ("provider", "model", "revision", "normalization"):
            if not getattr(self, field_name).strip():
                raise ValueError(f"{field_name} must be non-empty")
        if self.dimensions <= 0:
            raise ValueError("dimensions must be positive")


@dataclass(frozen=True, slots=True)
class EmbeddingIdentity:
    spec: EmbeddingSpec
    input_sha256: str
    identity_sha256: str

    @classmethod
    def from_projection(
        cls,
        projection: SearchProjection,
        spec: EmbeddingSpec,
    ) -> "EmbeddingIdentity":
        if projection.search_text_sha256 != hashlib.sha256(
            projection.search_text.encode("utf-8")
        ).hexdigest():
            raise ValueError("projection search-text identity is inconsistent")
        material = json.dumps(
            {
                "dimensions": spec.dimensions,
                "input_sha256": projection.search_text_sha256,
                "model": spec.model,
                "normalization": spec.normalization,
                "provider": spec.provider,
                "revision": spec.revision,
            },
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        return cls(
            spec=spec,
            input_sha256=projection.search_text_sha256,
            identity_sha256=hashlib.sha256(material).hexdigest(),
        )


@dataclass(frozen=True, slots=True)
class EmbeddingRecord:
    identity: EmbeddingIdentity
    vector: tuple[float, ...]

    def __post_init__(self) -> None:
        if len(self.vector) != self.identity.spec.dimensions:
            raise ValueError("embedding vector dimension mismatch")
        if not all(math.isfinite(value) for value in self.vector):
            raise ValueError("embedding vector contains non-finite values")


class BatchEmbedder(Protocol):
    def embed(
        self,
        texts: Sequence[str],
        *,
        spec: EmbeddingSpec,
    ) -> Sequence[Sequence[float]]: ...


@dataclass(slots=True)
class EmbeddingCache:
    records: dict[str, EmbeddingRecord] = field(default_factory=dict)

    def resolve(
        self,
        projections: Sequence[SearchProjection],
        *,
        spec: EmbeddingSpec,
        embedder: BatchEmbedder,
    ) -> dict[str, EmbeddingRecord]:
        by_identity: dict[str, tuple[EmbeddingIdentity, SearchProjection]] = {}
        projection_identity: dict[str, str] = {}
        for projection in projections:
            identity = EmbeddingIdentity.from_projection(projection, spec)
            by_identity.setdefault(
                identity.identity_sha256,
                (identity, projection),
            )
            projection_identity[projection.projection_id] = (
                identity.identity_sha256
            )
        missing = [
            pair
            for key, pair in sorted(by_identity.items())
            if key not in self.records
        ]
        if missing:
            vectors = embedder.embed(
                [projection.search_text for _, projection in missing],
                spec=spec,
            )
            if len(vectors) != len(missing):
                raise ValueError("embedder returned the wrong vector count")
            staged: list[EmbeddingRecord] = []
            for (identity, _), vector in zip(missing, vectors, strict=True):
                staged.append(
                    EmbeddingRecord(
                        identity=identity,
                        vector=tuple(float(value) for value in vector),
                    )
                )
            for record in staged:
                self.records[record.identity.identity_sha256] = record
        return {
            projection_id: self.records[identity_sha]
            for projection_id, identity_sha in projection_identity.items()
        }
