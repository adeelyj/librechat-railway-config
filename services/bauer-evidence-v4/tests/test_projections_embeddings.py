from __future__ import annotations

import dataclasses
import json
from pathlib import Path
from typing import Sequence

import pytest

from bauer_evidence_v4.compilation import CanonicalCompiler
from bauer_evidence_v4.indexing import (
    EmbeddingCache,
    EmbeddingSpec,
    ProjectionBuilder,
)


REPOSITORY_ROOT = Path(__file__).resolve().parents[3]
FIXTURE_PATH = (
    REPOSITORY_ROOT
    / "evals"
    / "bauer-rag-v4"
    / "fixtures"
    / "difficult-documents.json"
)
SOURCE_ROOT = Path(r"D:\02_Code\Bauer Kompressoren Demo")
REQUIRED_FIXTURES = {
    "verticus-i-technical-data",
    "english-en-iso-3834-2-certificate",
    "b-detection-sensor-calibration-n47183",
    "product-overview-k22-k28-range",
}


@pytest.fixture(scope="module")
def projections() -> dict[str, tuple]:
    if not SOURCE_ROOT.is_dir():
        pytest.skip("authoritative original corpus is not mounted")
    bundle = json.loads(FIXTURE_PATH.read_text(encoding="utf-8"))
    compiler = CanonicalCompiler()
    builder = ProjectionBuilder()
    result = {}
    for fixture in bundle["documents"]:
        if fixture["fixture_id"] not in REQUIRED_FIXTURES:
            continue
        source = fixture["source"]
        path = SOURCE_ROOT.joinpath(*source["logical_path"].split("/"))
        compiled = compiler.compile(
            path.read_bytes(),
            source_path=source["logical_path"],
            declared_media_type=source["media_type"],
            fixture=fixture,
        )
        assert compiled.document is not None
        result[fixture["fixture_id"]] = builder.build(compiled.document)
    return result


def test_projection_families_are_deterministic_and_traceable(
    projections: dict[str, tuple],
) -> None:
    for items in projections.values():
        assert items
        assert {item.projection_type for item in items} <= {
            "metadata",
            "passage",
            "table_row",
            "fact",
        }
        assert all(item.canonical_evidence_ids for item in items)
        assert all(item.search_text for item in items)
        assert len({item.projection_id for item in items}) == len(items)
        assert tuple(sorted(items, key=lambda item: item.projection_id)) == items


def test_exact_table_projection_contains_missing_context(
    projections: dict[str, tuple],
) -> None:
    rows = [
        item
        for item in projections["verticus-i-technical-data"]
        if item.projection_type == "table_row"
        and item.subject == "I 15.11-11-V"
    ]
    assert len(rows) == 2
    assert {
        dict(item.qualifiers)["group"]
        for item in rows
    } == {
        "VERTICUS I 350 - 420 bar",
        "VERTICUS I 420 - 525 bar",
    }
    for row in rows:
        assert "High-pressure compressor VERTICUS" in row.search_text
        assert "Effective free air delivery [l/min]: 420" in row.search_text
        assert "Maximum operating pressure [bar]:" in row.search_text
        assert "Motor power [kW]: 11" in row.search_text
        assert "Footnote 1: Free air delivery according to ISO 1217" in (
            row.search_text
        )
        assert "Footnote 2: Max. operating pressure" in row.search_text


def test_metadata_projections_preserve_certificate_and_document_identity(
    projections: dict[str, tuple],
) -> None:
    certificate = next(
        item
        for item in projections["english-en-iso-3834-2-certificate"]
        if item.projection_type == "metadata"
        and "File Number: 216." in item.search_text
    )
    assert "EN ISO 3834-2 Certificate" in certificate.search_text
    assert "Organization: BAUER KOMPRESSOREN GmbH." in certificate.search_text
    assert "Address: Stäblistr. 8, 81477 Munich." in certificate.search_text
    assert "Language: en." in certificate.search_text
    assert "0035_certificate-download_e42c3f318d.html" in (
        certificate.search_text
    )

    n47183 = next(
        item
        for item in projections["b-detection-sensor-calibration-n47183"]
        if item.projection_type == "metadata"
    )
    assert "Document number: N47183." in n47183.search_text
    assert "SENSOR CALIBRATION" in n47183.search_text
    assert "Subject: Sensor calibration for BAUER B-DETECTION" in (
        n47183.search_text
    )
    assert n47183.source_filename.endswith(".pdf")


def test_family_range_projection_does_not_merge_neighboring_family(
    projections: dict[str, tuple],
) -> None:
    k_facts = [
        item
        for item in projections["product-overview-k22-k28-range"]
        if item.projection_type == "fact"
        and item.subject == "K 22 – K 28 SERIES"
    ]
    assert {item.predicate for item in k_facts} >= {
        "pressure_range",
        "charging_rate",
        "motor_power",
    }
    assert all("PE-VE INDUSTRY" not in item.search_text for item in k_facts)
    assert all(dict(item.qualifiers)["scope"] == "family" for item in k_facts)


class _CountingEmbedder:
    def __init__(self) -> None:
        self.calls = 0
        self.text_count = 0

    def embed(
        self,
        texts: Sequence[str],
        *,
        spec: EmbeddingSpec,
    ) -> Sequence[Sequence[float]]:
        self.calls += 1
        self.text_count += len(texts)
        return [
            [
                float((len(text) + index + dimension) % 17) / 17
                for dimension in range(spec.dimensions)
            ]
            for index, text in enumerate(texts)
        ]


def test_embedding_reuse_requires_exact_identity(
    projections: dict[str, tuple],
) -> None:
    selected = projections["b-detection-sensor-calibration-n47183"][:3]
    cache = EmbeddingCache()
    embedder = _CountingEmbedder()
    spec = EmbeddingSpec(
        provider="local",
        model="multilingual-e5-small",
        revision="sha256:reviewed-v1",
        dimensions=8,
        normalization="l2",
    )

    first = cache.resolve(selected, spec=spec, embedder=embedder)
    first_count = embedder.text_count
    assert first_count == len(selected)
    assert len(first) == len(selected)

    second = cache.resolve(selected, spec=spec, embedder=embedder)
    assert embedder.text_count == first_count
    assert {
        key: value.identity.identity_sha256 for key, value in first.items()
    } == {
        key: value.identity.identity_sha256 for key, value in second.items()
    }

    changed_spec = dataclasses.replace(spec, revision="sha256:reviewed-v2")
    cache.resolve(selected, spec=changed_spec, embedder=embedder)
    assert embedder.text_count == first_count + len(selected)

    changed_projection = dataclasses.replace(
        selected[0],
        projection_id=f"{selected[0].projection_id}-changed",
        search_text=f"{selected[0].search_text} changed",
        search_text_sha256=__import__("hashlib").sha256(
            f"{selected[0].search_text} changed".encode()
        ).hexdigest(),
    )
    cache.resolve(
        (changed_projection,),
        spec=spec,
        embedder=embedder,
    )
    assert embedder.text_count == first_count + len(selected) + 1


def test_projection_regeneration_is_identical(
    projections: dict[str, tuple],
) -> None:
    items = projections["verticus-i-technical-data"]
    serialized = tuple(item.to_json() for item in items)
    assert serialized == tuple(item.to_json() for item in items)
