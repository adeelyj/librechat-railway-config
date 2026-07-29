from __future__ import annotations

import argparse
import hashlib
import json
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path
from typing import Sequence

from bauer_evidence_v4.compilation import CanonicalCompiler
from bauer_evidence_v4.indexing import (
    EmbeddingCache,
    EmbeddingSpec,
    ProjectionBuilder,
)


class _DeterministicEmbedder:
    def __init__(self) -> None:
        self.embedded_text_count = 0

    def embed(
        self,
        texts: Sequence[str],
        *,
        spec: EmbeddingSpec,
    ) -> Sequence[Sequence[float]]:
        self.embedded_text_count += len(texts)
        return [
            [
                float(
                    hashlib.sha256(
                        f"{text}\0{dimension}".encode("utf-8")
                    ).digest()[0]
                )
                / 255
                for dimension in range(spec.dimensions)
            ]
            for text in texts
        ]


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser()
    parser.add_argument("--fixture", required=True, type=Path)
    parser.add_argument("--source-root", required=True, type=Path)
    parser.add_argument("--output", type=Path)
    return parser


def main() -> int:
    args = _parser().parse_args()
    bundle = json.loads(args.fixture.read_text(encoding="utf-8"))
    compiler = CanonicalCompiler()
    builder = ProjectionBuilder()
    projections = []
    for fixture in bundle["documents"]:
        source = fixture["source"]
        path = args.source_root.joinpath(*source["logical_path"].split("/"))
        result = compiler.compile(
            path.read_bytes(),
            source_path=source["logical_path"],
            declared_media_type=source["media_type"],
            fixture=fixture,
        )
        assert result.document is not None
        projections.extend(builder.build(result.document))

    serialized = "\n".join(item.to_json() for item in projections)
    spec = EmbeddingSpec(
        provider="verification",
        model="deterministic",
        revision="v1",
        dimensions=8,
        normalization="none",
    )
    cache = EmbeddingCache()
    embedder = _DeterministicEmbedder()
    cache.resolve(projections, spec=spec, embedder=embedder)
    first_embed_count = embedder.embedded_text_count
    cache.resolve(projections, spec=spec, embedder=embedder)
    exact_retry_embed_count = embedder.embedded_text_count - first_embed_count
    changed_spec = EmbeddingSpec(
        provider=spec.provider,
        model=spec.model,
        revision="v2",
        dimensions=spec.dimensions,
        normalization=spec.normalization,
    )
    cache.resolve(projections, spec=changed_spec, embedder=embedder)
    changed_identity_embed_count = (
        embedder.embedded_text_count
        - first_embed_count
        - exact_retry_embed_count
    )
    counts = Counter(item.projection_type for item in projections)
    report = {
        "schema_version": 1,
        "kind": "bauer-rag-v4-indexing-verification",
        "generated_at_utc": datetime.now(UTC).isoformat(),
        "passed": (
            bool(projections)
            and exact_retry_embed_count == 0
            and changed_identity_embed_count == first_embed_count
            and all(item.canonical_evidence_ids for item in projections)
        ),
        "locked_holdout_opened": False,
        "projection_count": len(projections),
        "projection_counts": dict(sorted(counts.items())),
        "projection_set_sha256": hashlib.sha256(
            serialized.encode("utf-8")
        ).hexdigest(),
        "unique_projection_ids": len(
            {item.projection_id for item in projections}
        ),
        "unique_search_text_identities": len(
            {item.search_text_sha256 for item in projections}
        ),
        "first_embed_count": first_embed_count,
        "exact_retry_embed_count": exact_retry_embed_count,
        "changed_identity_embed_count": changed_identity_embed_count,
    }
    encoded = json.dumps(
        report,
        ensure_ascii=False,
        allow_nan=False,
        indent=2,
        sort_keys=True,
    ) + "\n"
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(encoded, encoding="utf-8")
    else:
        print(encoded, end="")
    return 0 if report["passed"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
