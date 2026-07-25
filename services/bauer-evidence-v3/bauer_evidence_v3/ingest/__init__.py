"""Dependency-light, source-native evidence compiler primitives."""

from .canonical import (
    Block,
    Cell,
    Document,
    Page,
    Table,
    canonical_json,
    stable_id,
)
from .pipeline import CompilationResult, Compiler, compile_source
from .probe import SourceProbe, probe_source
from .render import PageRender, PageRenderer, PdfPlumberPageRenderer
from .quality import (
    QualityGateError,
    QualityIssue,
    QualityPolicy,
    QualityReport,
    evaluate_document,
)

__all__ = [
    "Block",
    "Cell",
    "CompilationResult",
    "Compiler",
    "Document",
    "Page",
    "PageRender",
    "PageRenderer",
    "PdfPlumberPageRenderer",
    "QualityGateError",
    "QualityIssue",
    "QualityPolicy",
    "QualityReport",
    "SourceProbe",
    "Table",
    "canonical_json",
    "compile_source",
    "evaluate_document",
    "probe_source",
    "stable_id",
]
