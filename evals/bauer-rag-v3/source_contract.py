"""Build and verify the immutable Bauer V3 original-source corpus contract.

The contract is intentionally release-independent.  It freezes the reviewed
V2 dedup selection and its existing external file IDs, but points only at
original PDF/HTML bytes.  A later release manifest may bind these records to a
tenant, knowledge base, and release.

This module only reads the source root.  The CLI refuses to place generated
output anywhere under that root.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
import uuid
from dataclasses import asdict, dataclass
from pathlib import Path, PurePosixPath
from typing import Any, Mapping, Sequence


SCHEMA_VERSION = 1
CONTRACT_KIND = "bauer-v3-original-source-corpus"
EXPECTED_SOURCE_COUNT = 373
DEFAULT_CORPUS_KEY = "bauer-kompressoren"
TAXONOMY_ID = "bauer-source-path-navigation-v1"
TAXONOMY_SEMANTICS = "navigation-only-non-citable"
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_SOURCE_LAYOUT = {
    "pdf": ("application/pdf", ".pdf", "bauer_index/raw_docs"),
    "html": ("text/html", ".html", "bauer_index/raw_html"),
}

# These rules intentionally inspect only the immutable source type and path.
# They must never inspect, summarize, or make claims about source content.
# Ordering is part of TAXONOMY_ID: the first matching rule wins.
_CATEGORY_RULES: tuple[tuple[tuple[str, str], tuple[str, ...]], ...] = (
    (
        ("Corporate & Compliance", "Legal, policy & certification"),
        (
            "general-terms",
            "conditions-generales",
            "contrataci",
            "code-of-conduct",
            "human-rights",
            "quality-policy",
            "beschaffungswesen",
            "certificate",
            "zert-",
            "zert_",
            "gtc",
            "agb",
        ),
    ),
    (
        ("News & References", "Newsletters"),
        ("b-news",),
    ),
    (
        ("News & References", "References"),
        ("reference", "references", "sponsorship"),
    ),
    (
        ("Digital, Control & Service", "Digital & controls"),
        (
            "b-cloud",
            "b-app",
            "b-select",
            "smart-solutions",
            "b-control",
            "control",
            "fernbedienung",
            "display",
        ),
    ),
    (
        ("Applications & Industries", "Energy & gas"),
        (
            "hydrogen",
            "helium",
            "cng",
            "biogas",
            "biomethane",
            "fuel-gas",
            "gas-injection",
            "oil-and-gas",
            "oil-gas",
            "petrochemical",
        ),
    ),
    (
        ("Applications & Industries", "Industry & specialist uses"),
        (
            "industry",
            "sector",
            "aerospace",
            "automotive",
            "chemical",
            "food",
            "mining",
            "research",
            "shipbuilding",
            "shipping",
            "offshore",
            "motor-racing",
            "medical-technology",
            "paintball",
            "shooting-sport",
            "sports-diving",
            "professional-diving",
            "breathing-air-sport",
            "firefighting",
            "production",
        ),
    ),
    (
        ("Products & Systems", "Air & gas treatment"),
        (
            "purification",
            "treatment",
            "filter",
            "membrane",
            "detection",
            "aeroguard",
            "virus-free",
            "seccant",
            "kool",
            "nitrox",
            "blending",
            "pureair",
            "cartouch",
            "kaltetrockner",
        ),
    ),
    (
        ("Products & Systems", "Storage, filling & distribution"),
        (
            "storage",
            "speicher",
            "distribution",
            "measurement",
            "filling",
            "dispenser",
            "terminal",
            "reducing",
            "reduzierstation",
            "zapf",
            "betankung",
        ),
    ),
    (
        ("Accessories & Consumables", "Accessories & maintenance"),
        (
            "accessor",
            "zubehoer",
            "zubehör",
            "hose",
            "schlauch",
            "fuellvent",
            "füllvent",
            "safety-valve",
            "sicherheitsvent",
            "soupape",
            "oel",
            "öl",
        ),
    ),
    (
        ("Corporate & Resources", "Downloads & catalogues"),
        (
            "documents",
            "download",
            "merchandising",
            "product-overview",
            "product_overview",
            "catalogue",
            "catalogues",
        ),
    ),
    (
        ("Products & Systems", "Compressors & boosters"),
        (
            "compressor",
            "kompressor",
            "booster",
            "compact-line",
            "compact_line",
            "verticus",
            "mini-verticus",
            "junior-ii",
            "mariner",
            "oceanus",
            "capitano",
            "premium-line",
            "profi-line",
        ),
    ),
)


class SourceContractError(ValueError):
    """The corpus selection or contract is structurally invalid."""


class SourceContractIntegrityError(SourceContractError):
    """Original bytes do not match the frozen corpus identity."""


@dataclass(frozen=True, slots=True)
class VerificationReport:
    source_count: int
    pdf_count: int
    html_count: int
    prior_duplicate_alias_count: int
    accounted_original_file_count: int
    selected_byte_count: int
    contract_sha256: str


def build_contract(
    *,
    v2_dedup_manifest: str | Path,
    provision_state: str | Path,
    source_root: str | Path,
    corpus_key: str = DEFAULT_CORPUS_KEY,
    expected_source_count: int = EXPECTED_SOURCE_COUNT,
) -> dict[str, Any]:
    """Create a deterministic contract after verifying the complete corpus.

    ``v2_dedup_manifest`` is the reviewed V2 export manifest.  Its Markdown
    filenames are used only to join the existing external file IDs from
    ``provision_state``; derivatives never become V3 sources.
    """

    root = _source_root(source_root)
    records = _load_json(v2_dedup_manifest, expected_type=list)
    state = _load_json(provision_state, expected_type=dict)
    if len(records) != expected_source_count:
        raise SourceContractError(
            f"V2 dedup manifest must contain exactly {expected_source_count} "
            f"records; found {len(records)}"
        )

    uploaded = _uploaded_mapping(state, corpus_key)
    records_by_filename = _records_by_filename(records)
    record_names = set(records_by_filename)
    upload_names = set(uploaded)
    if record_names != upload_names:
        missing = sorted(record_names - upload_names)
        unexpected = sorted(upload_names - record_names)
        raise SourceContractError(
            "external file-ID mapping does not exactly match the V2 selection "
            f"(missing={missing}, unexpected={unexpected})"
        )

    selected_paths: dict[str, str] = {}
    selected_hashes: dict[str, str] = {}
    external_ids: dict[str, str] = {}
    alias_paths: dict[str, str] = {}
    normalized_records: list[dict[str, Any]] = []
    normalized_id_mapping: list[dict[str, Any]] = []
    sources: list[dict[str, Any]] = []

    for derivative_filename in sorted(records_by_filename, key=_sort_key):
        record = records_by_filename[derivative_filename]
        source_type = _source_type(record.get("kind"))
        media_type, required_suffix, required_parent = _SOURCE_LAYOUT[source_type]
        source_path = _relative_path(record.get("sourcePath"), "sourcePath")
        if Path(source_path).suffix.casefold() != required_suffix:
            raise SourceContractError(
                f"{source_path}: source type {source_type!r} requires "
                f"a {required_suffix} file"
            )
        if not (
            source_path == required_parent
            or source_path.startswith(f"{required_parent}/")
        ):
            raise SourceContractError(
                f"{source_path}: {source_type} source must be under "
                f"{required_parent}/"
            )

        expected_sha256 = _sha256(record.get("sourceChecksum"), "sourceChecksum")
        _claim_unique(
            selected_paths,
            source_path,
            derivative_filename,
            "selected source path",
        )
        _claim_unique(
            selected_hashes,
            expected_sha256,
            source_path,
            "selected source SHA-256",
        )

        upload = uploaded[derivative_filename]
        external_file_id = _required_text(
            upload.get("fileId"),
            f"{derivative_filename}.fileId",
        )
        _claim_unique(
            external_ids,
            external_file_id,
            source_path,
            "external file ID",
        )
        exported_sha256 = _sha256(
            record.get("exportedChecksum"),
            f"{derivative_filename}.exportedChecksum",
        )
        uploaded_sha256 = _sha256(
            upload.get("sha256"),
            f"{derivative_filename}.uploaded.sha256",
        )
        if exported_sha256 != uploaded_sha256:
            raise SourceContractIntegrityError(
                f"{derivative_filename}: uploaded derivative checksum does not "
                "match the reviewed V2 export"
            )
        if upload.get("embedded") is not True:
            raise SourceContractError(
                f"{derivative_filename}: prior external file mapping is not "
                "recorded as embedded"
            )

        duplicate_source_paths = tuple(
            sorted(
                (
                    _relative_path(value, f"{derivative_filename}.duplicatePaths")
                    for value in _require_list(
                        record.get("duplicatePaths"),
                        f"{derivative_filename}.duplicatePaths",
                    )
                ),
                key=_sort_key,
            )
        )
        for duplicate_path in duplicate_source_paths:
            if Path(duplicate_path).suffix.casefold() != required_suffix:
                raise SourceContractError(
                    f"{duplicate_path}: duplicate alias does not match selected "
                    f"source type {source_type}"
                )
            _claim_unique(
                alias_paths,
                duplicate_path,
                source_path,
                "prior duplicate alias path",
            )

        page_count = record.get("pageCount")
        if page_count is not None:
            page_count = _non_negative_int(
                page_count,
                f"{derivative_filename}.pageCount",
                minimum=1,
            )
        ocr_used = record.get("ocrUsed")
        if not isinstance(ocr_used, bool):
            raise SourceContractError(
                f"{derivative_filename}.ocrUsed must be a boolean"
            )
        character_count = _non_negative_int(
            record.get("characterCount"),
            f"{derivative_filename}.characterCount",
        )

        actual_sha256, byte_size = _inspect_file(root, source_path)
        if actual_sha256 != expected_sha256:
            raise SourceContractIntegrityError(
                f"{source_path}: expected SHA-256 {expected_sha256}, "
                f"found {actual_sha256}"
            )

        normalized_record = {
            "derivative_filename": derivative_filename,
            "source_type": source_type,
            "source_path": source_path,
            "source_sha256": expected_sha256,
            "exported_sha256": exported_sha256,
            "duplicate_source_paths": list(duplicate_source_paths),
            "page_count": page_count,
            "ocr_used": ocr_used,
            "character_count": character_count,
        }
        normalized_records.append(normalized_record)
        normalized_id_mapping.append(
            {
                "derivative_filename": derivative_filename,
                "external_file_id": external_file_id,
                "uploaded_sha256": uploaded_sha256,
                "embedded": True,
            }
        )
        sources.append(
            {
                "external_file_id": external_file_id,
                "logical_path": source_path,
                "source_path": source_path,
                "source_type": source_type,
                "declared_media_type": media_type,
                "expected_sha256": expected_sha256,
                "expected_byte_size": byte_size,
                "visibility": "inherited",
                "category_path": list(
                    navigation_category_path(
                        source_type=source_type,
                        source_path=source_path,
                    )
                ),
                "is_original": True,
                "is_derivative": False,
                "prior_metadata": {
                    "v2_derivative_filename": derivative_filename,
                    "v2_derivative_sha256": exported_sha256,
                    "v2_page_count": page_count,
                    "v2_ocr_used": ocr_used,
                    "v2_character_count": character_count,
                    "v2_duplicate_source_paths": list(duplicate_source_paths),
                },
            }
        )

    selected_casefold = {value.casefold() for value in selected_paths}
    alias_casefold = {value.casefold() for value in alias_paths}
    overlap = selected_casefold & alias_casefold
    if overlap:
        raise SourceContractError(
            "a prior duplicate alias is also selected as an original: "
            f"{sorted(overlap)}"
        )

    # Recheck every alias against its selected source.  This validates the V2
    # dedup declaration without making an alias a V3 source.
    selected_digest_by_path = {
        source["source_path"]: source["expected_sha256"] for source in sources
    }
    for alias_path, selected_path in sorted(
        alias_paths.items(), key=lambda item: _sort_key(item[0])
    ):
        alias_sha256, _ = _inspect_file(root, alias_path)
        if alias_sha256 != selected_digest_by_path[selected_path]:
            raise SourceContractIntegrityError(
                f"{alias_path}: prior duplicate alias no longer matches "
                f"{selected_path}"
            )

    actual_paths = _enumerate_original_paths(root)
    accounted_paths = set(selected_paths) | set(alias_paths)
    if _casefold_set(actual_paths) != _casefold_set(accounted_paths):
        missing = _casefold_difference(accounted_paths, actual_paths)
        unexpected = _casefold_difference(actual_paths, accounted_paths)
        raise SourceContractError(
            "V2 selection plus duplicate aliases does not exactly account for "
            f"the original PDF/HTML corpus (missing={missing}, "
            f"unexpected={unexpected})"
        )

    sources.sort(key=lambda item: _sort_key(item["logical_path"]))
    selected_counts = {
        source_type: sum(
            1 for source in sources if source["source_type"] == source_type
        )
        for source_type in sorted(_SOURCE_LAYOUT)
    }
    payload: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "contract_kind": CONTRACT_KIND,
        "selection_basis": {
            "strategy": "reviewed-v2-content-sha256-dedup",
            "corpus_key": corpus_key,
            "v2_dedup_records_sha256": _digest_json(normalized_records),
            "external_id_mapping_sha256": _digest_json(normalized_id_mapping),
            "category_taxonomy": {
                "taxonomy_id": TAXONOMY_ID,
                "semantics": TAXONOMY_SEMANTICS,
                "citable": False,
                "inputs": ["source_type", "source_path"],
            },
        },
        "source_count": len(sources),
        "selected_counts": selected_counts,
        "prior_duplicate_alias_count": len(alias_paths),
        "accounted_original_file_count": len(actual_paths),
        "sources": sources,
    }
    contract = _seal(payload)
    validate_contract(contract, expected_source_count=expected_source_count)
    return contract


def load_contract(
    path: str | Path,
    *,
    expected_source_count: int = EXPECTED_SOURCE_COUNT,
) -> dict[str, Any]:
    contract = _load_json(path, expected_type=dict)
    validate_contract(contract, expected_source_count=expected_source_count)
    return contract


def validate_contract(
    contract: Mapping[str, Any],
    *,
    expected_source_count: int = EXPECTED_SOURCE_COUNT,
) -> None:
    """Validate structure, canonical ordering, uniqueness, and self-digest."""

    if not isinstance(contract, Mapping):
        raise SourceContractError("contract must be a JSON object")
    required_top = {
        "schema_version",
        "contract_kind",
        "selection_basis",
        "source_count",
        "selected_counts",
        "prior_duplicate_alias_count",
        "accounted_original_file_count",
        "sources",
        "contract_sha256",
    }
    if set(contract) != required_top:
        raise SourceContractError(
            "contract fields do not match schema "
            f"(missing={sorted(required_top - set(contract))}, "
            f"unexpected={sorted(set(contract) - required_top)})"
        )
    if contract["schema_version"] != SCHEMA_VERSION:
        raise SourceContractError(
            f"unsupported schema_version: {contract['schema_version']!r}"
        )
    if contract["contract_kind"] != CONTRACT_KIND:
        raise SourceContractError(
            f"unexpected contract_kind: {contract['contract_kind']!r}"
        )
    source_count = _non_negative_int(contract["source_count"], "source_count")
    if source_count != expected_source_count:
        raise SourceContractError(
            f"source_count must be exactly {expected_source_count}; "
            f"found {source_count}"
        )
    sources = _require_list(contract["sources"], "sources")
    if len(sources) != source_count:
        raise SourceContractError(
            "source_count does not equal the number of source records"
        )
    selection_basis = contract["selection_basis"]
    if not isinstance(selection_basis, Mapping):
        raise SourceContractError("selection_basis must be a JSON object")
    expected_selection_fields = {
        "strategy",
        "corpus_key",
        "v2_dedup_records_sha256",
        "external_id_mapping_sha256",
        "category_taxonomy",
    }
    if set(selection_basis) != expected_selection_fields:
        raise SourceContractError(
            "selection_basis fields do not match schema"
        )
    if selection_basis["strategy"] != "reviewed-v2-content-sha256-dedup":
        raise SourceContractError("selection_basis.strategy is not supported")
    _required_text(selection_basis["corpus_key"], "selection_basis.corpus_key")
    _sha256(
        selection_basis["v2_dedup_records_sha256"],
        "selection_basis.v2_dedup_records_sha256",
    )
    _sha256(
        selection_basis["external_id_mapping_sha256"],
        "selection_basis.external_id_mapping_sha256",
    )
    expected_taxonomy = {
        "taxonomy_id": TAXONOMY_ID,
        "semantics": TAXONOMY_SEMANTICS,
        "citable": False,
        "inputs": ["source_type", "source_path"],
    }
    if selection_basis["category_taxonomy"] != expected_taxonomy:
        raise SourceContractError(
            "selection_basis.category_taxonomy must declare the deterministic "
            "navigation-only, non-citable taxonomy"
        )

    expected_source_fields = {
        "external_file_id",
        "logical_path",
        "source_path",
        "source_type",
        "declared_media_type",
        "expected_sha256",
        "expected_byte_size",
        "visibility",
        "category_path",
        "is_original",
        "is_derivative",
        "prior_metadata",
    }
    paths: dict[str, str] = {}
    logical_paths: dict[str, str] = {}
    digests: dict[str, str] = {}
    external_ids: dict[str, str] = {}
    alias_paths: dict[str, str] = {}
    observed_counts = {source_type: 0 for source_type in _SOURCE_LAYOUT}
    previous_sort_key: tuple[str, str] | None = None

    for ordinal, raw_source in enumerate(sources):
        if not isinstance(raw_source, Mapping):
            raise SourceContractError(f"sources[{ordinal}] must be an object")
        if set(raw_source) != expected_source_fields:
            raise SourceContractError(
                f"sources[{ordinal}] fields do not match schema"
            )
        source_path = _relative_path(
            raw_source["source_path"], f"sources[{ordinal}].source_path"
        )
        logical_path = _relative_path(
            raw_source["logical_path"], f"sources[{ordinal}].logical_path"
        )
        if logical_path != source_path:
            raise SourceContractError(
                f"sources[{ordinal}]: logical_path must map exactly to source_path"
            )
        sort_key = _sort_key(logical_path)
        if previous_sort_key is not None and sort_key <= previous_sort_key:
            raise SourceContractError("sources must use canonical logical-path order")
        previous_sort_key = sort_key

        source_type = _source_type(raw_source["source_type"])
        media_type, suffix, parent = _SOURCE_LAYOUT[source_type]
        if raw_source["declared_media_type"] != media_type:
            raise SourceContractError(
                f"{source_path}: declared media type does not match source type"
            )
        if (
            Path(source_path).suffix.casefold() != suffix
            or not source_path.startswith(f"{parent}/")
        ):
            raise SourceContractError(
                f"{source_path}: path does not match source type {source_type}"
            )
        observed_counts[source_type] += 1

        expected_sha256 = _sha256(
            raw_source["expected_sha256"],
            f"sources[{ordinal}].expected_sha256",
        )
        external_file_id = _required_text(
            raw_source["external_file_id"],
            f"sources[{ordinal}].external_file_id",
        )
        _non_negative_int(
            raw_source["expected_byte_size"],
            f"sources[{ordinal}].expected_byte_size",
        )
        if raw_source["visibility"] != "inherited":
            raise SourceContractError(
                f"sources[{ordinal}].visibility must be 'inherited'"
            )
        category_path = _validated_category_path(
            raw_source["category_path"],
            f"sources[{ordinal}].category_path",
        )
        expected_category_path = navigation_category_path(
            source_type=source_type,
            source_path=source_path,
        )
        if category_path != expected_category_path:
            raise SourceContractError(
                f"sources[{ordinal}].category_path does not match "
                f"{TAXONOMY_ID}"
            )
        if (
            raw_source["is_original"] is not True
            or raw_source["is_derivative"] is not False
        ):
            raise SourceContractError(
                f"sources[{ordinal}] must identify original, non-derivative bytes"
            )
        metadata = raw_source["prior_metadata"]
        if not isinstance(metadata, Mapping):
            raise SourceContractError(
                f"sources[{ordinal}].prior_metadata must be an object"
            )
        duplicates = _require_list(
            metadata.get("v2_duplicate_source_paths"),
            f"sources[{ordinal}].prior_metadata.v2_duplicate_source_paths",
        )
        for raw_alias in duplicates:
            alias = _relative_path(
                raw_alias,
                f"sources[{ordinal}].prior_metadata.v2_duplicate_source_paths",
            )
            _claim_unique(
                alias_paths,
                alias,
                source_path,
                "prior duplicate alias path",
            )

        _claim_unique(paths, source_path, str(ordinal), "selected source path")
        _claim_unique(
            logical_paths,
            logical_path,
            str(ordinal),
            "logical path",
        )
        _claim_unique(
            digests,
            expected_sha256,
            source_path,
            "selected source SHA-256",
        )
        _claim_unique(
            external_ids,
            external_file_id,
            source_path,
            "external file ID",
        )

    selected_counts = contract["selected_counts"]
    if not isinstance(selected_counts, Mapping) or dict(selected_counts) != observed_counts:
        raise SourceContractError(
            f"selected_counts does not match sources: {observed_counts}"
        )
    alias_count = _non_negative_int(
        contract["prior_duplicate_alias_count"],
        "prior_duplicate_alias_count",
    )
    if alias_count != len(alias_paths):
        raise SourceContractError(
            "prior_duplicate_alias_count does not match source metadata"
        )
    accounted_count = _non_negative_int(
        contract["accounted_original_file_count"],
        "accounted_original_file_count",
    )
    if accounted_count != source_count + alias_count:
        raise SourceContractError(
            "accounted_original_file_count must equal selected sources plus "
            "prior duplicate aliases"
        )
    overlap = _casefold_set(paths) & _casefold_set(alias_paths)
    if overlap:
        raise SourceContractError(
            f"selected source paths overlap duplicate aliases: {sorted(overlap)}"
        )

    supplied_digest = _sha256(contract["contract_sha256"], "contract_sha256")
    expected_digest = contract_sha256(contract)
    if supplied_digest != expected_digest:
        raise SourceContractIntegrityError(
            "contract_sha256 does not match the contract contents"
        )


def verify_contract(
    contract: Mapping[str, Any],
    *,
    source_root: str | Path,
    expected_source_count: int = EXPECTED_SOURCE_COUNT,
) -> VerificationReport:
    """Read-only verification of all selected originals and duplicate aliases."""

    validate_contract(contract, expected_source_count=expected_source_count)
    root = _source_root(source_root)
    selected_paths: set[str] = set()
    alias_paths: set[str] = set()
    selected_byte_count = 0
    counts = {source_type: 0 for source_type in _SOURCE_LAYOUT}

    for source in contract["sources"]:
        source_path = source["source_path"]
        selected_paths.add(source_path)
        actual_sha256, actual_size = _inspect_file(root, source_path)
        if actual_sha256 != source["expected_sha256"]:
            raise SourceContractIntegrityError(
                f"{source_path}: expected SHA-256 {source['expected_sha256']}, "
                f"found {actual_sha256}"
            )
        if actual_size != source["expected_byte_size"]:
            raise SourceContractIntegrityError(
                f"{source_path}: expected {source['expected_byte_size']} bytes, "
                f"found {actual_size}"
            )
        selected_byte_count += actual_size
        counts[source["source_type"]] += 1

        for alias in source["prior_metadata"]["v2_duplicate_source_paths"]:
            alias_paths.add(alias)
            alias_sha256, _ = _inspect_file(root, alias)
            if alias_sha256 != source["expected_sha256"]:
                raise SourceContractIntegrityError(
                    f"{alias}: prior duplicate alias no longer matches {source_path}"
                )

    actual_paths = _enumerate_original_paths(root)
    accounted_paths = selected_paths | alias_paths
    if _casefold_set(actual_paths) != _casefold_set(accounted_paths):
        missing = _casefold_difference(accounted_paths, actual_paths)
        unexpected = _casefold_difference(actual_paths, accounted_paths)
        raise SourceContractIntegrityError(
            "contract does not exactly account for the current original corpus "
            f"(missing={missing}, unexpected={unexpected})"
        )

    return VerificationReport(
        source_count=len(selected_paths),
        pdf_count=counts["pdf"],
        html_count=counts["html"],
        prior_duplicate_alias_count=len(alias_paths),
        accounted_original_file_count=len(actual_paths),
        selected_byte_count=selected_byte_count,
        contract_sha256=contract["contract_sha256"],
    )


def bind_release_spec(
    contract: Mapping[str, Any],
    *,
    tenant_id: str,
    knowledge_base_id: str,
    release_id: str,
    expected_source_count: int = EXPECTED_SOURCE_COUNT,
) -> dict[str, Any]:
    """Bind the frozen corpus to one strict release-control source spec."""

    validate_contract(
        contract,
        expected_source_count=expected_source_count,
    )
    contract_digest = str(contract["contract_sha256"])
    sources = []
    for source in contract["sources"]:
        sources.append(
            {
                "external_file_id": source["external_file_id"],
                "logical_path": source["logical_path"],
                "source_type": source["source_type"],
                "declared_media_type": source["declared_media_type"],
                "visibility": source["visibility"],
                "category_path": list(source["category_path"]),
                "metadata": {
                    "source_contract_sha256": contract_digest,
                    "expected_source_sha256": source["expected_sha256"],
                    "expected_source_byte_size": source["expected_byte_size"],
                    "prior_v2": dict(source["prior_metadata"]),
                },
                "is_original": True,
                "is_derivative": False,
            }
        )
    return {
        "tenant_id": _uuid_text(tenant_id, "tenant_id"),
        "knowledge_base_id": _uuid_text(
            knowledge_base_id,
            "knowledge_base_id",
        ),
        "release_id": _uuid_text(release_id, "release_id"),
        "sources": sources,
    }


def release_spec_bytes(specification: Mapping[str, Any]) -> bytes:
    return (
        json.dumps(
            specification,
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
            allow_nan=False,
        )
        + "\n"
    ).encode("utf-8")


def write_release_spec_once(
    specification: Mapping[str, Any],
    *,
    output: str | Path,
    source_root: str | Path,
) -> None:
    """Write a bound source spec once, accepting only byte-identical retries."""

    root = _source_root(source_root)
    destination = Path(output).expanduser().resolve()
    try:
        destination.relative_to(root)
    except ValueError:
        pass
    else:
        raise SourceContractError(
            "release spec output must not be written inside the source root"
        )
    encoded = release_spec_bytes(specification)
    if destination.exists():
        try:
            existing = destination.read_bytes()
        except OSError as exc:
            raise SourceContractError(
                f"cannot read existing release spec {destination}: {exc}"
            ) from exc
        if existing != encoded:
            raise SourceContractIntegrityError(
                f"refusing to overwrite different release spec: {destination}"
            )
        return
    destination.parent.mkdir(parents=True, exist_ok=True)
    try:
        with destination.open("xb") as handle:
            handle.write(encoded)
    except FileExistsError:
        if destination.read_bytes() != encoded:
            raise SourceContractIntegrityError(
                f"refusing to overwrite different release spec: {destination}"
            )


def contract_sha256(contract: Mapping[str, Any]) -> str:
    payload = dict(contract)
    payload.pop("contract_sha256", None)
    return _digest_json(payload)


def contract_bytes(contract: Mapping[str, Any]) -> bytes:
    validate_contract(
        contract,
        expected_source_count=int(contract.get("source_count", -1)),
    )
    return (
        json.dumps(
            contract,
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
            allow_nan=False,
        )
        + "\n"
    ).encode("utf-8")


def write_contract(
    contract: Mapping[str, Any],
    *,
    output: str | Path,
    source_root: str | Path,
) -> None:
    """Atomically write outside the immutable original-source tree."""

    root = _source_root(source_root)
    destination = Path(output).expanduser().resolve()
    try:
        destination.relative_to(root)
    except ValueError:
        pass
    else:
        raise SourceContractError(
            "contract output must not be written inside the source root"
        )
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_name(f".{destination.name}.tmp")
    temporary.write_bytes(contract_bytes(contract))
    os.replace(temporary, destination)


def _seal(payload: Mapping[str, Any]) -> dict[str, Any]:
    contract = dict(payload)
    contract["contract_sha256"] = _digest_json(contract)
    return contract


def _load_json(path: str | Path, *, expected_type: type) -> Any:
    source = Path(path)
    try:
        raw = source.read_bytes()
    except OSError as exc:
        raise SourceContractError(f"cannot read JSON file {source}: {exc}") from exc
    try:
        value = json.loads(raw)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise SourceContractError(f"{source} is not valid UTF-8 JSON") from exc
    if not isinstance(value, expected_type):
        raise SourceContractError(
            f"{source} must contain a JSON {expected_type.__name__}"
        )
    return value


def _uploaded_mapping(state: Mapping[str, Any], corpus_key: str) -> dict[str, Any]:
    uploaded = state.get("uploaded")
    if not isinstance(uploaded, Mapping):
        raise SourceContractError("provision state uploaded field must be an object")
    mapping = uploaded.get(corpus_key)
    if not isinstance(mapping, Mapping):
        raise SourceContractError(
            f"provision state has no uploaded mapping for {corpus_key!r}"
        )
    result: dict[str, Any] = {}
    for raw_name, raw_value in mapping.items():
        name = _required_text(raw_name, "uploaded filename")
        if not isinstance(raw_value, Mapping):
            raise SourceContractError(f"uploaded mapping {name!r} must be an object")
        result[name] = dict(raw_value)
    return result


def _records_by_filename(records: Sequence[Any]) -> dict[str, Mapping[str, Any]]:
    result: dict[str, Mapping[str, Any]] = {}
    seen: dict[str, str] = {}
    for ordinal, raw in enumerate(records):
        if not isinstance(raw, Mapping):
            raise SourceContractError(f"V2 record {ordinal} must be an object")
        filename = _required_text(raw.get("filename"), f"record {ordinal}.filename")
        _claim_unique(seen, filename, str(ordinal), "V2 derivative filename")
        result[filename] = raw
    return result


def navigation_category_path(
    *,
    source_type: str,
    source_path: str,
) -> tuple[str, str]:
    """Return a deterministic, non-citable navigation category.

    The category is derived exclusively from the source type and immutable
    source-path basename.  It is a routing aid, not evidence about the file,
    and must never be cited or used to substantiate an answer.
    """

    normalized_type = _source_type(source_type)
    normalized_path = _relative_path(source_path, "source_path")
    stem = PurePosixPath(normalized_path).stem.casefold()
    if normalized_type == "html":
        # HTML filenames carry deterministic crawl ordinals and content-hash
        # suffixes.  Removing them keeps taxonomy routing tied to the page slug.
        stem = re.sub(r"^\d+_", "", stem)
        stem = re.sub(r"_[0-9a-f]{10}$", "", stem)
    stem = re.sub(r"[\s_]+", "-", stem)
    stem = re.sub(r"-+", "-", stem)

    if normalized_type == "pdf" and "bauer-amfile" in stem:
        return "Archive", "Legacy document IDs"
    for category_path, needles in _CATEGORY_RULES:
        if any(needle in stem for needle in needles):
            return category_path
    if normalized_type == "pdf":
        return "Documents", "Product & technical literature"
    return "Products & Systems", "Product pages"


def _source_root(path: str | Path) -> Path:
    root = Path(path).expanduser().resolve()
    if not root.is_dir():
        raise SourceContractError(f"source root is not a directory: {root}")
    return root


def _resolve_source(root: Path, relative_path: str) -> Path:
    candidate = root.joinpath(*PurePosixPath(relative_path).parts)
    try:
        resolved = candidate.resolve(strict=True)
    except FileNotFoundError as exc:
        raise SourceContractIntegrityError(
            f"source file is missing: {relative_path}"
        ) from exc
    try:
        resolved.relative_to(root)
    except ValueError as exc:
        raise SourceContractError(
            f"source path escapes the source root: {relative_path}"
        ) from exc
    if not resolved.is_file():
        raise SourceContractIntegrityError(
            f"source path is not a regular file: {relative_path}"
        )
    return resolved


def _inspect_file(root: Path, relative_path: str) -> tuple[str, int]:
    source = _resolve_source(root, relative_path)
    digest = hashlib.sha256()
    size = 0
    try:
        with source.open("rb") as handle:
            while block := handle.read(1024 * 1024):
                digest.update(block)
                size += len(block)
    except OSError as exc:
        raise SourceContractIntegrityError(
            f"cannot read source file {relative_path}: {exc}"
        ) from exc
    return digest.hexdigest(), size


def _enumerate_original_paths(root: Path) -> set[str]:
    paths: set[str] = set()
    for source_type, (_, suffix, parent) in _SOURCE_LAYOUT.items():
        directory = root.joinpath(*PurePosixPath(parent).parts)
        if not directory.is_dir():
            raise SourceContractIntegrityError(
                f"required {source_type} source directory is missing: {parent}"
            )
        for candidate in directory.rglob("*"):
            if candidate.is_file() and candidate.suffix.casefold() == suffix:
                relative = candidate.relative_to(root).as_posix()
                if relative.casefold() in _casefold_set(paths):
                    raise SourceContractError(
                        f"case-insensitive duplicate source path: {relative}"
                    )
                paths.add(relative)
    return paths


def _relative_path(value: Any, field: str) -> str:
    text = _required_text(value, field).replace("\\", "/")
    if text.startswith("/") or re.match(r"^[A-Za-z]:", text):
        raise SourceContractError(f"{field} must be relative: {text!r}")
    raw_parts = text.split("/")
    if any(part in {"", ".", ".."} for part in raw_parts):
        raise SourceContractError(
            f"{field} must be a canonical relative path: {text!r}"
        )
    path = PurePosixPath(text)
    normalized = path.as_posix()
    if normalized != text:
        raise SourceContractError(
            f"{field} must be a canonical POSIX path: {text!r}"
        )
    return normalized


def _source_type(value: Any) -> str:
    source_type = _required_text(value, "source type").casefold()
    if source_type not in _SOURCE_LAYOUT:
        raise SourceContractError(
            f"source type must be one of {sorted(_SOURCE_LAYOUT)}; "
            f"found {source_type!r}"
        )
    return source_type


def _sha256(value: Any, field: str) -> str:
    text = _required_text(value, field).casefold()
    if not _SHA256_RE.fullmatch(text):
        raise SourceContractError(
            f"{field} must be a 64-character lowercase SHA-256 digest"
        )
    return text


def _required_text(value: Any, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise SourceContractError(f"{field} must be a non-empty string")
    if value != value.strip():
        raise SourceContractError(f"{field} must not have surrounding whitespace")
    return value


def _uuid_text(value: Any, field: str) -> str:
    text = _required_text(value, field)
    try:
        return str(uuid.UUID(text))
    except ValueError as exc:
        raise SourceContractError(f"{field} must be a UUID") from exc


def _require_list(value: Any, field: str) -> list[Any]:
    if not isinstance(value, list):
        raise SourceContractError(f"{field} must be a JSON array")
    return value


def _validated_category_path(value: Any, field: str) -> tuple[str, str]:
    raw_path = _require_list(value, field)
    if len(raw_path) != 2:
        raise SourceContractError(
            f"{field} must contain exactly two non-empty navigation levels"
        )
    return (
        _required_text(raw_path[0], f"{field}[0]"),
        _required_text(raw_path[1], f"{field}[1]"),
    )


def _non_negative_int(value: Any, field: str, *, minimum: int = 0) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
        raise SourceContractError(f"{field} must be an integer >= {minimum}")
    return value


def _claim_unique(
    seen: dict[str, str],
    value: str,
    owner: str,
    field: str,
) -> None:
    key = value.casefold()
    previous = seen.get(key)
    if previous is not None:
        raise SourceContractError(
            f"duplicate {field} {value!r} claimed by {previous!r} and {owner!r}"
        )
    seen[key] = owner


def _sort_key(value: str) -> tuple[str, str]:
    return value.casefold(), value


def _casefold_set(values: Any) -> set[str]:
    return {str(value).casefold() for value in values}


def _casefold_difference(left: set[str], right: set[str]) -> list[str]:
    right_folded = _casefold_set(right)
    return sorted(
        (value for value in left if value.casefold() not in right_folded),
        key=_sort_key,
    )


def _digest_json(value: Any) -> str:
    encoded = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Build or verify the Bauer V3 original-source contract."
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    generate = subparsers.add_parser(
        "generate",
        help="rebuild the deterministic contract from reviewed V2 records",
    )
    generate.add_argument("--v2-dedup-manifest", required=True, type=Path)
    generate.add_argument("--provision-state", required=True, type=Path)
    generate.add_argument("--source-root", required=True, type=Path)
    generate.add_argument("--output", required=True, type=Path)
    generate.add_argument("--corpus-key", default=DEFAULT_CORPUS_KEY)
    generate.add_argument(
        "--check",
        action="store_true",
        help="compare generated bytes with --output without writing",
    )

    verify = subparsers.add_parser(
        "verify",
        help="verify the frozen contract against original source bytes",
    )
    verify.add_argument("--contract", required=True, type=Path)
    verify.add_argument("--source-root", required=True, type=Path)

    bind = subparsers.add_parser(
        "bind-release",
        help="verify and bind the frozen corpus to one release source spec",
    )
    bind.add_argument("--contract", required=True, type=Path)
    bind.add_argument("--source-root", required=True, type=Path)
    bind.add_argument("--tenant-id", required=True)
    bind.add_argument("--knowledge-base-id", required=True)
    bind.add_argument("--release-id", required=True)
    bind.add_argument("--output", required=True, type=Path)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        if args.command == "generate":
            contract = build_contract(
                v2_dedup_manifest=args.v2_dedup_manifest,
                provision_state=args.provision_state,
                source_root=args.source_root,
                corpus_key=args.corpus_key,
            )
            if args.check:
                expected = contract_bytes(contract)
                try:
                    actual = args.output.read_bytes()
                except OSError as exc:
                    raise SourceContractError(
                        f"cannot read checked contract {args.output}: {exc}"
                    ) from exc
                if actual != expected:
                    raise SourceContractIntegrityError(
                        f"generated contract differs from {args.output}"
                    )
            else:
                write_contract(
                    contract,
                    output=args.output,
                    source_root=args.source_root,
                )
            report = verify_contract(contract, source_root=args.source_root)
        elif args.command == "verify":
            contract = load_contract(args.contract)
            report = verify_contract(contract, source_root=args.source_root)
        else:
            contract = load_contract(args.contract)
            report = verify_contract(contract, source_root=args.source_root)
            specification = bind_release_spec(
                contract,
                tenant_id=args.tenant_id,
                knowledge_base_id=args.knowledge_base_id,
                release_id=args.release_id,
            )
            write_release_spec_once(
                specification,
                output=args.output,
                source_root=args.source_root,
            )
    except SourceContractError as exc:
        print(f"source contract error: {exc}", file=sys.stderr)
        return 2
    print(json.dumps(asdict(report), sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
