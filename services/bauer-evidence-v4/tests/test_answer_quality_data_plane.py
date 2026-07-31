from __future__ import annotations

import importlib.util
import hashlib
import json
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[3]
MODULE_PATH = ROOT / "scripts" / "deployment" / "v4_data_plane.py"
SPEC = importlib.util.spec_from_file_location("v4_data_plane", MODULE_PATH)
assert SPEC is not None and SPEC.loader is not None
DATA_PLANE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(DATA_PLANE)


def _manifest() -> dict[str, object]:
    digest = DATA_PLANE.SYNTHETIC_SHA256
    return {
        "content_sha256": digest,
        "byte_size": DATA_PLANE.SYNTHETIC_BYTE_SIZE,
        "object_key": (
            "bauer-rag-v3/sha256/"
            f"{digest[:2]}/{digest[2:4]}/{digest}.html"
        ),
        "read_after_write_verified": True,
    }


def test_answer_quality_release_extends_but_does_not_redefine_v3() -> None:
    assert DATA_PLANE.V3_SOURCE_COUNT == 373
    assert DATA_PLANE.EXPECTED_SOURCE_COUNT == 374
    assert DATA_PLANE.EXPECTED_SOURCE_CONTRACT == (
        "ad10b44f9dbac8ba6ee7ec62f558fa28518465044b31b7bdad02f8dcfa908c80"
    )


def test_synthetic_source_manifest_is_exact_verified_and_deterministic() -> None:
    first = DATA_PLANE._synthetic_source(_manifest())
    second = DATA_PLANE._synthetic_source(_manifest())
    assert first == second
    assert first["external_source_id"] == "synthetic-demo-v1"
    assert first["media_type"] == "text/html"
    assert first["source_id"] != first["source_version_id"]


@pytest.mark.parametrize(
    ("field", "value"),
    (
        ("content_sha256", "0" * 64),
        ("byte_size", 1),
        ("object_key", "../synthetic.html"),
        ("read_after_write_verified", False),
    ),
)
def test_synthetic_source_manifest_fails_closed(field: str, value: object) -> None:
    manifest = _manifest()
    manifest[field] = value
    with pytest.raises(DATA_PLANE.DataPlaneError):
        DATA_PLANE._synthetic_source(manifest)


def test_data_plane_targets_complete_migration_and_v4_only_source() -> None:
    source = MODULE_PATH.read_text(encoding="utf-8")
    assert "V4 schema is not at migration 007" in source
    assert "'derived_verified'" in source
    assert "V3_SOURCE_COUNT" in source
    assert "active_release_pointer_count\"] != 0" in source


def test_carried_forward_fedora_rehearsal_matches_exact_migration_set() -> None:
    expected = {
        "001_foundation.sql": "7334e643fe30f04774508a3c84e5aa7414c886e5d771b1a3681c28d72a21f651",
        "002_registry_releases.sql": "7952f9684e19d54b8baf6418b792e766e6f8c57a7d1b6fd941fc5871fc8d4fd3",
        "003_canonical_projections.sql": "5e33213c7f2e86087afc29f4a9c8df2be465a708f2a80820a88cf4927379db25",
        "004_jobs_audit_validation.sql": "f49279ada844879001760c9bc8ae73aaebd3a238e345d0bcf81da6a3015b6df0",
        "005_roles_rls.sql": "80f61ed0a85c10afc98795fa9437396fbe22524971f37ac5f65bf9a3e446ecb0",
        "006_runtime_artifacts.sql": "092e37fe655dac3f9efeb553091f85a8b94498c13083f9aa43a704b1c48d6c67",
        "007_pinned_release_sources.sql": "aee8430ebfc52e76809ef8c6e01e7cf5a787400a6947908899305776ac3b86b0",
    }
    migration_root = ROOT / "services" / "bauer-evidence-v4" / "migrations"
    actual = {
        path.name: hashlib.sha256(
            path.read_text(encoding="utf-8").replace("\r\n", "\n").encode("utf-8")
        ).hexdigest()
        for path in sorted(migration_root.glob("*.sql"))
    }
    assert actual == expected
    carry_forward = json.loads(
        (
            ROOT
            / "evidence"
            / "bauer-rag-v4-answer-quality"
            / "fedora-operational-rehearsal-carry-forward.json"
        ).read_text(encoding="utf-8")
    )
    assert carry_forward["passed"] is True
    assert carry_forward["basis"][
        "exact_lf_normalized_migration_checksums_match"
    ] is True
    assert carry_forward["fresh_rehearsal_attempt"]["result"] == (
        "transport_blocked"
    )
