"""Deterministic identity for the source compiler that is actually running.

Release creation and compiler workers must derive this value independently
from the same executable toolchain.  An operator-supplied label is not enough:
artifact reuse is safe only when parser order and code, quality policy,
projection code, dependency versions, and embedding contract all match.
"""

from __future__ import annotations

import importlib
import importlib.metadata
import json
import platform
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from .ids import canonical_json, sha256_bytes, sha256_json
from .ingest import Compiler


TOOLCHAIN_FINGERPRINT_SCHEMA = 1
_CORE_MODULES = (
    "bauer_evidence_v3.ingest.canonical",
    "bauer_evidence_v3.ingest.ocr",
    "bauer_evidence_v3.ingest.pipeline",
    "bauer_evidence_v3.ingest.probe",
    "bauer_evidence_v3.ingest.quality",
    "bauer_evidence_v3.ingest.render",
)
_PROJECTION_MODULES = (
    "bauer_evidence_v3.projections",
)
_PARSER_DISTRIBUTIONS = {
    "pdfplumber_native": ("pdfplumber",),
    "pymupdf_native": ("PyMuPDF",),
}


@dataclass(frozen=True, slots=True)
class CompilerToolchainIdentity:
    """Immutable, inspectable compiler identity used by releases and workers."""

    fingerprint: str
    parser_version: str
    manifest_json: str

    @property
    def manifest(self) -> dict[str, Any]:
        """Return a fresh copy so callers cannot mutate the hashed manifest."""

        value = json.loads(self.manifest_json)
        if not isinstance(value, dict):  # pragma: no cover - construction invariant
            raise TypeError("toolchain manifest must be a JSON object")
        return value


def derive_compiler_toolchain(
    *,
    embedding_model_version: str,
    embedding_dimensions: int,
    compiler: Compiler | None = None,
    ocr_version: str | None = None,
    fact_model_version: str | None = None,
) -> CompilerToolchainIdentity:
    """Derive the release/compiler identity from the local runtime.

    This is the public release-control helper.  It must be executed in the same
    image (or a byte-for-byte equivalent compiler image) as the worker.
    """

    model = _required_text(embedding_model_version, "embedding_model_version")
    dimensions = _positive_int(embedding_dimensions, "embedding_dimensions")
    normalized_ocr = _optional_text(ocr_version)
    normalized_fact_model = _optional_text(fact_model_version)
    active_compiler = compiler or Compiler()
    active_ocr_engine = active_compiler.ocr_engine
    active_page_renderer = active_compiler.page_renderer
    if active_ocr_engine is None:
        if normalized_ocr is not None:
            raise ValueError(
                "ocr_version requires a compiler with an OCR engine"
            )
        ocr_component = None
    else:
        actual_ocr_version = _required_text(
            getattr(active_ocr_engine, "engine_version", None),
            "OCR engine_version",
        )
        if (
            normalized_ocr is not None
            and normalized_ocr != actual_ocr_version
        ):
            raise ValueError(
                "ocr_version does not match the configured OCR engine"
            )
        normalized_ocr = actual_ocr_version
        if active_page_renderer is None:  # pragma: no cover - Compiler invariant
            raise ValueError("OCR compiler requires a page renderer")
        ocr_component = {
            "version": normalized_ocr,
            "runtime_package": _distribution_version(
                "rapidocr-onnxruntime"
            ),
            "engine": _runtime_component(
                active_ocr_engine,
                id_attribute="engine_id",
                version_attribute="engine_version",
            ),
            "renderer": _runtime_component(
                active_page_renderer,
                id_attribute="renderer_id",
                version_attribute="renderer_version",
            ),
            "languages": active_compiler.ocr_languages,
            "minimum_confidence": (
                active_compiler.ocr_minimum_confidence
            ),
            "render_dpi": active_compiler.ocr_render_dpi,
        }

    parser_components = tuple(
        _parser_component(parser, ordinal=ordinal)
        for ordinal, parser in enumerate(active_compiler.parsers)
    )
    parser_version = "bauer-v3-parsers-" + sha256_json(
        {
            "schema": TOOLCHAIN_FINGERPRINT_SCHEMA,
            "parsers": parser_components,
        }
    )[:20]
    manifest: dict[str, Any] = {
        "schema": TOOLCHAIN_FINGERPRINT_SCHEMA,
        "runtime": {
            "implementation": platform.python_implementation(),
            "python_version": platform.python_version(),
        },
        "compiler": {
            "modules": tuple(_module_component(name) for name in _CORE_MODULES),
            "quality_policy": asdict(active_compiler.quality_policy),
        },
        "parsers": parser_components,
        "parser_version": parser_version,
        "projection": {
            "modules": tuple(
                _module_component(name) for name in _PROJECTION_MODULES
            ),
        },
        "embedding": {
            "model_version": model,
            "dimensions": dimensions,
        },
        "ocr": ocr_component,
        "fact_model": (
            {"version": normalized_fact_model}
            if normalized_fact_model is not None
            else None
        ),
    }
    manifest_bytes = canonical_json(manifest)
    return CompilerToolchainIdentity(
        fingerprint=sha256_bytes(manifest_bytes),
        parser_version=parser_version,
        manifest_json=manifest_bytes.decode("utf-8"),
    )


def compute_compiler_fingerprint(
    *,
    embedding_model_version: str,
    embedding_dimensions: int,
    compiler: Compiler | None = None,
    ocr_version: str | None = None,
    fact_model_version: str | None = None,
) -> str:
    """Return only the SHA-256 identity for concise release specifications."""

    return derive_compiler_toolchain(
        embedding_model_version=embedding_model_version,
        embedding_dimensions=embedding_dimensions,
        compiler=compiler,
        ocr_version=ocr_version,
        fact_model_version=fact_model_version,
    ).fingerprint


def _parser_component(parser: object, *, ordinal: int) -> dict[str, Any]:
    parser_id = _required_text(getattr(parser, "parser_id", None), "parser_id")
    parser_version = _required_text(
        getattr(parser, "parser_version", None),
        f"{parser_id} parser_version",
    )
    parser_type = type(parser)
    module_name = parser_type.__module__
    dependencies = {
        name: _distribution_version(name)
        for name in _PARSER_DISTRIBUTIONS.get(parser_id, ())
    }
    return {
        "ordinal": ordinal,
        "parser_id": parser_id,
        "parser_version": parser_version,
        "implementation": f"{module_name}.{parser_type.__qualname__}",
        "module_sha256": _module_sha256(module_name),
        "dependencies": dependencies,
    }


def _module_component(module_name: str) -> dict[str, str]:
    return {
        "module": module_name,
        "sha256": _module_sha256(module_name),
    }


def _runtime_component(
    value: object,
    *,
    id_attribute: str,
    version_attribute: str,
) -> dict[str, str]:
    runtime_type = type(value)
    module_name = runtime_type.__module__
    return {
        "id": _required_text(getattr(value, id_attribute, None), id_attribute),
        "version": _required_text(
            getattr(value, version_attribute, None),
            version_attribute,
        ),
        "implementation": (
            f"{module_name}.{runtime_type.__qualname__}"
        ),
        "module_sha256": _module_sha256(module_name),
    }


def _module_sha256(module_name: str) -> str:
    module = importlib.import_module(module_name)
    source_path = getattr(module, "__file__", None)
    content: bytes | None = None
    if source_path:
        path = Path(source_path)
        if path.suffix.casefold() in {".py", ".pyw"} and path.is_file():
            content = path.read_bytes()
    if content is None:
        loader = getattr(module, "__loader__", None)
        get_source = getattr(loader, "get_source", None)
        source = get_source(module_name) if callable(get_source) else None
        if source is not None:
            content = source.encode("utf-8")
    if content is None:
        raise RuntimeError(
            f"cannot derive a source fingerprint for module {module_name}"
        )
    # Git checkouts and container copies may use different native line endings;
    # line-ending normalization preserves identity for identical Python source.
    normalized = content.replace(b"\r\n", b"\n").replace(b"\r", b"\n")
    return sha256_bytes(normalized)


def _distribution_version(name: str) -> str | None:
    try:
        return importlib.metadata.version(name)
    except importlib.metadata.PackageNotFoundError:
        return None


def _required_text(value: object, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} is required")
    return value.strip()


def _optional_text(value: object) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise ValueError("optional toolchain versions must be text")
    normalized = value.strip()
    return normalized or None


def _positive_int(value: object, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise ValueError(f"{name} must be a positive integer")
    return value
