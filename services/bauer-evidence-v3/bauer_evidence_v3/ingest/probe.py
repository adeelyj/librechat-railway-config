from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from pathlib import PurePath

from .canonical import Attributes, attribute_items


_HTML_START_RE = re.compile(
    rb"^\s*(?:<!doctype\s+html\b|<html\b|<head\b|<body\b)",
    re.IGNORECASE,
)
_HTML_STRUCTURE_RE = re.compile(
    rb"<(?:html|head|body|main|article|table)\b",
    re.IGNORECASE,
)
_KNOWN_MEDIA_TYPES = {
    "application/pdf": ("pdf", "application/pdf"),
    "text/html": ("html", "text/html"),
    "application/xhtml+xml": ("html", "text/html"),
    "text/markdown": ("markdown", "text/markdown"),
    "text/x-markdown": ("markdown", "text/markdown"),
    "text/plain": ("text", "text/plain"),
}


@dataclass(frozen=True, slots=True)
class SourceProbe:
    source_sha256: str
    source_name: str
    media_type: str
    format: str
    signature: str
    byte_count: int
    attributes: Attributes = ()


def probe_source(
    payload: bytes,
    *,
    source_name: str,
    declared_media_type: str | None = None,
) -> SourceProbe:
    if not isinstance(payload, bytes):
        raise TypeError("payload must be bytes")
    if not source_name or "\x00" in source_name:
        raise ValueError("source_name must be non-empty and contain no NUL")

    source_sha256 = hashlib.sha256(payload).hexdigest()
    suffix = PurePath(source_name).suffix.casefold()
    declared = (declared_media_type or "").split(";", 1)[0].strip().casefold()
    sample = payload[:65_536]
    text_sample = sample[3:] if sample.startswith(b"\xef\xbb\xbf") else sample

    signature = "unknown"
    detected_format = "unknown"
    media_type = "application/octet-stream"

    if sample.startswith(b"%PDF-"):
        signature = "pdf_magic"
        detected_format = "pdf"
        media_type = "application/pdf"
    elif _HTML_START_RE.search(text_sample) or (
        suffix in {".html", ".htm", ".xhtml"} and _HTML_STRUCTURE_RE.search(text_sample)
    ):
        signature = "html_markup"
        detected_format = "html"
        media_type = "text/html"
    elif b"\x00" not in sample:
        try:
            text_sample.decode("utf-8-sig")
        except UnicodeDecodeError:
            signature = "non_utf8_text"
        else:
            signature = "utf8_text"
            if suffix in {".md", ".markdown", ".mdown", ".mkd"}:
                detected_format = "markdown"
                media_type = "text/markdown"
            elif suffix in {".html", ".htm", ".xhtml"}:
                detected_format = "html"
                media_type = "text/html"
            else:
                detected_format = "text"
                media_type = "text/plain"
    else:
        signature = "binary_nul"

    if detected_format == "unknown" and declared in _KNOWN_MEDIA_TYPES:
        detected_format, media_type = _KNOWN_MEDIA_TYPES[declared]
        signature = "declared_media_type"

    attributes = {
        "declared_media_type": declared,
        "extension": suffix,
    }
    if declared and declared in _KNOWN_MEDIA_TYPES:
        declared_format = _KNOWN_MEDIA_TYPES[declared][0]
        if detected_format != "unknown" and declared_format != detected_format:
            attributes["declared_type_mismatch"] = "true"

    return SourceProbe(
        source_sha256=source_sha256,
        source_name=source_name,
        media_type=media_type,
        format=detected_format,
        signature=signature,
        byte_count=len(payload),
        attributes=attribute_items(attributes),
    )
