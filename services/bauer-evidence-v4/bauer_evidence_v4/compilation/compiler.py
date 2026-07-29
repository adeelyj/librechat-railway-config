from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from typing import Any, Protocol

from ..canonical.models import (
    CompilationCandidate,
    CompilationResult,
    QualityIssue,
    QualityReport,
)
from ..canonical.normalize import sorted_attributes
from .html import HtmlDomParser, HtmlTextParser
from .pdf import PdfLayoutParser, PypdfTextParser
from .quality import evaluate_document


class SourceParser(Protocol):
    parser_id: str
    parser_version: str

    def parse(
        self,
        payload: bytes,
        *,
        source_path: str,
        source_sha256: str | None = None,
    ): ...


class CompilationQuarantined(ValueError):
    def __init__(self, result: CompilationResult):
        super().__init__(
            f"source {result.source_path!r} did not pass canonical compiler gates"
        )
        self.result = result


def _default_parsers() -> dict[str, tuple[SourceParser, ...]]:
    return {
        "text/html": (HtmlDomParser(), HtmlTextParser()),
        "application/pdf": (PdfLayoutParser(), PypdfTextParser()),
    }


@dataclass(frozen=True, slots=True)
class CanonicalCompiler:
    parsers: dict[str, tuple[SourceParser, ...]] = field(
        default_factory=_default_parsers
    )
    max_source_bytes: int = 512 * 1024 * 1024

    def compile(
        self,
        payload: bytes,
        *,
        source_path: str,
        declared_media_type: str | None = None,
        fixture: dict[str, Any] | None = None,
        enforce_gate: bool = True,
    ) -> CompilationResult:
        if not payload:
            raise ValueError("payload must not be empty")
        source_sha256 = hashlib.sha256(payload).hexdigest()
        if len(payload) > self.max_source_bytes:
            result = CompilationResult(
                status="quarantined",
                document=None,
                selected_parser_id=None,
                candidates=(
                    CompilationCandidate(
                        parser_id="source_probe",
                        document=None,
                        quality=QualityReport(
                            status="fatal",
                            semantic_score=0,
                            metrics=sorted_attributes(
                                {"source_bytes": len(payload)}
                            ),
                            issues=(
                                QualityIssue(
                                    code="source_size_limit",
                                    severity="fatal",
                                    message="source exceeds compiler byte limit",
                                ),
                            ),
                        ),
                        error="source exceeds compiler byte limit",
                    ),
                ),
                source_sha256=source_sha256,
                source_path=source_path,
            )
            if enforce_gate:
                raise CompilationQuarantined(result)
            return result

        media_type = self._media_type(payload, declared_media_type)
        configured = self.parsers.get(media_type, ())
        if not configured:
            raise ValueError(f"unsupported source media type: {media_type}")

        candidates: list[CompilationCandidate] = []
        for parser in configured:
            try:
                document = parser.parse(
                    payload,
                    source_path=source_path,
                    source_sha256=source_sha256,
                )
                if (
                    document.source_sha256 != source_sha256
                    or document.source_path != source_path
                    or document.media_type != media_type
                ):
                    raise ValueError(
                        "parser returned provenance inconsistent with source probe"
                    )
                report = evaluate_document(document, fixture=fixture)
                candidates.append(
                    CompilationCandidate(
                        parser_id=parser.parser_id,
                        document=document,
                        quality=report,
                    )
                )
            except Exception as error:
                candidates.append(
                    CompilationCandidate(
                        parser_id=parser.parser_id,
                        document=None,
                        quality=QualityReport(
                            status="fatal",
                            semantic_score=0,
                            metrics=(),
                            issues=(
                                QualityIssue(
                                    code="parser_failed",
                                    severity="fatal",
                                    message=type(error).__name__,
                                ),
                            ),
                        ),
                        error=f"{type(error).__name__}: {error}",
                    )
                )

        status_rank = {
            "pass": 0,
            "warning": 1,
            "quarantine": 2,
            "fatal": 3,
        }
        selected = min(
            candidates,
            key=lambda candidate: (
                status_rank[candidate.quality.status],
                -candidate.quality.semantic_score,
                len(candidate.quality.issues),
                candidate.parser_id,
            ),
        )
        published = (
            selected.document is not None
            and selected.quality.status in {"pass", "warning"}
        )
        result = CompilationResult(
            status="published" if published else "quarantined",
            document=selected.document if published else None,
            selected_parser_id=selected.parser_id if published else None,
            candidates=tuple(candidates),
            source_sha256=source_sha256,
            source_path=source_path,
        )
        if not published and enforce_gate:
            raise CompilationQuarantined(result)
        return result

    @staticmethod
    def _media_type(payload: bytes, declared: str | None) -> str:
        declared = (declared or "").split(";", 1)[0].strip().casefold()
        if payload.startswith(b"%PDF-"):
            detected = "application/pdf"
        else:
            sample = payload[:65_536].lstrip().lower()
            detected = (
                "text/html"
                if sample.startswith((b"<!doctype html", b"<html", b"<head"))
                or b"<body" in sample
                else "application/octet-stream"
            )
        if declared and declared != detected:
            raise ValueError(
                f"declared media type {declared!r} conflicts with {detected!r}"
            )
        return detected
