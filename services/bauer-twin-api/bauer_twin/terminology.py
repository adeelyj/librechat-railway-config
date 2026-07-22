from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from difflib import get_close_matches
from typing import Any, Iterable


_SUBSCRIPT_TRANSLATION = str.maketrans("₀₁₂₃₄₅₆₇₈₉", "0123456789")


def normalize_text(value: str) -> str:
    """Normalize user terminology without guessing its meaning."""
    normalized = unicodedata.normalize("NFKC", str(value)).translate(_SUBSCRIPT_TRANSLATION).casefold()
    normalized = normalized.replace("_", " ").replace("-", " ")
    return re.sub(r"\s+", " ", normalized).strip()


_TERMS: dict[str, tuple[dict[str, Any], ...]] = {
    "medium": (
        {
            "canonical_value": "nitrogen",
            "aliases": (("nitrogen", "en", True), ("stickstoff", "de", True), ("n2", "symbol", True)),
        },
        {
            "canonical_value": "breathing air",
            "aliases": (("breathing air", "en", True), ("atemluft", "de", True)),
        },
        {
            "canonical_value": "air",
            "aliases": (
                ("air", "en", True),
                ("compressed air", "en", True),
                ("luft", "de", True),
                ("druckluft", "de", True),
            ),
        },
        {
            "canonical_value": "helium",
            "aliases": (("helium", "en/de", True), ("heliumgas", "de", True), ("he", "symbol", False)),
        },
        {
            "canonical_value": "argon",
            "aliases": (("argon", "en/de", True), ("argongas", "de", True), ("ar", "symbol", False)),
        },
        {
            "canonical_value": "hydrogen",
            "aliases": (
                ("hydrogen", "en", True),
                ("wasserstoff", "de", True),
                ("wasserstoffgas", "de", True),
                ("h2", "symbol", True),
            ),
        },
        {
            "canonical_value": "oxygen",
            "aliases": (("oxygen", "en", True), ("sauerstoff", "de", True), ("o2", "symbol", True)),
        },
        {
            "canonical_value": "carbon dioxide",
            "aliases": (
                ("carbon dioxide", "en", True),
                ("kohlendioxid", "de", True),
                ("co2", "symbol", True),
            ),
        },
        {
            "canonical_value": "natural gas",
            "aliases": (("natural gas", "en", True), ("erdgas", "de", True)),
        },
    ),
    "topology": (
        {
            "canonical_value": "booster",
            "aliases": (
                ("booster", "en/de", True),
                ("booster compressor", "en", True),
                ("pressure booster", "en", True),
                ("nachverdichter", "de", True),
                ("druckerhöher", "de", True),
            ),
        },
        {
            "canonical_value": "compressor",
            "aliases": (
                ("compressor", "en", False),
                ("kompressor", "de", False),
                ("verdichter", "de", False),
            ),
        },
        {
            "canonical_value": "compressor + filling station",
            "aliases": (
                ("compressor and filling station", "en", False),
                ("compressor filling station", "en", False),
                ("füllstation", "de", False),
                ("fuellstation", "de", False),
            ),
        },
    ),
    "compressor_family": (
        {
            "canonical_value": "N2 Booster",
            "aliases": (("n2 booster", "product", False), ("nitrogen booster", "en", False)),
        },
        {
            "canonical_value": "BM 40",
            "aliases": (("bm 40", "product", True), ("bm40", "product", True)),
        },
        {
            "canonical_value": "BM 100",
            "aliases": (("bm 100", "product", True), ("bm100", "product", True)),
        },
        {
            "canonical_value": "Breathing Air",
            "aliases": (("breathing air", "en", False), ("atemluft", "de", False)),
        },
    ),
    "category": (
        {
            "canonical_value": "CMP",
            "aliases": (("cmp", "code", False), ("compressor", "en", False), ("kompressor", "de", False)),
        },
        {
            "canonical_value": "CTL",
            "aliases": (("ctl", "code", False), ("controller", "en", False), ("steuerung", "de", False)),
        },
        {
            "canonical_value": "SNS",
            "aliases": (
                ("sns", "code", False),
                ("sensor", "en", True),
                ("transmitter", "en", True),
                ("drucksensor", "de", True),
                ("temperatursensor", "de", True),
                ("taupunktsensor", "de", True),
            ),
        },
        {
            "canonical_value": "PUR",
            "aliases": (
                ("pur", "code", False),
                ("filter", "en/de", True),
                ("cartridge", "en", True),
                ("purification", "en", True),
                ("filterpatrone", "de", True),
            ),
        },
        {
            "canonical_value": "VLV",
            "aliases": (("vlv", "code", False), ("valve", "en", True), ("ventil", "de", True)),
        },
        {
            "canonical_value": "CLG",
            "aliases": (("clg", "code", False), ("cooling", "en", False), ("kühlung", "de", False)),
        },
        {
            "canonical_value": "STG",
            "aliases": (("stg", "code", False), ("storage", "en", False), ("speicher", "de", False)),
        },
    ),
}


def terminology_rows() -> list[dict[str, Any]]:
    """Return version-controlled seed rows for local and PostgreSQL-backed use."""
    rows: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()
    for domain, entries in _TERMS.items():
        for entry in entries:
            canonical = str(entry["canonical_value"])
            aliases = list(entry["aliases"])
            aliases.append((canonical, "canonical", True))
            for alias, language, query_safe in aliases:
                normalized_alias = normalize_text(alias)
                identity = (domain, normalized_alias)
                if identity in seen:
                    continue
                seen.add(identity)
                rows.append(
                    {
                        "domain": domain,
                        "canonical_value": canonical,
                        "alias": normalized_alias,
                        "language": language,
                        "query_safe": bool(query_safe),
                    }
                )
    return rows


@dataclass(frozen=True)
class Resolution:
    domain: str
    raw: str
    normalized: str | None
    status: str
    matched_alias: str | None = None
    suggestions: tuple[str, ...] = ()

    def as_dict(self) -> dict[str, Any]:
        return {
            "raw": self.raw,
            "normalized": self.normalized,
            "status": self.status,
            "matched_alias": self.matched_alias,
            "suggestions": list(self.suggestions),
        }


class TerminologyResolver:
    def __init__(self, rows: Iterable[dict[str, Any]] | None = None) -> None:
        self._aliases: dict[str, dict[str, dict[str, Any]]] = {}
        for row in rows or terminology_rows():
            domain = str(row["domain"])
            alias = normalize_text(str(row["alias"]))
            canonical = str(row["canonical_value"])
            existing = self._aliases.setdefault(domain, {}).get(alias)
            if existing and existing["canonical_value"] != canonical:
                raise ValueError(f"Ambiguous {domain} alias: {alias}")
            self._aliases[domain][alias] = {
                "canonical_value": canonical,
                "query_safe": bool(row.get("query_safe", True)),
            }

    def resolve(self, domain: str, raw: str) -> Resolution:
        normalized_raw = normalize_text(raw)
        match = self._aliases.get(domain, {}).get(normalized_raw)
        if match:
            return Resolution(
                domain=domain,
                raw=str(raw),
                normalized=str(match["canonical_value"]),
                status="recognized",
                matched_alias=normalized_raw,
            )

        aliases = list(self._aliases.get(domain, {}))
        close_aliases = get_close_matches(normalized_raw, aliases, n=3, cutoff=0.82)
        suggestions: list[str] = []
        for alias in close_aliases:
            canonical = str(self._aliases[domain][alias]["canonical_value"])
            if canonical not in suggestions:
                suggestions.append(canonical)
        return Resolution(
            domain=domain,
            raw=str(raw),
            normalized=None,
            status="unknown",
            suggestions=tuple(suggestions),
        )

    def infer(self, domain: str, query: str) -> Resolution | None:
        normalized_query = normalize_text(query)
        candidates = [
            (alias, details)
            for alias, details in self._aliases.get(domain, {}).items()
            if details["query_safe"]
        ]
        candidates.sort(key=lambda item: (-len(item[0]), item[0]))
        for alias, details in candidates:
            pattern = rf"(?<![a-z0-9]){re.escape(alias)}(?![a-z0-9])"
            if re.search(pattern, normalized_query):
                return Resolution(
                    domain=domain,
                    raw=alias,
                    normalized=str(details["canonical_value"]),
                    status="recognized",
                    matched_alias=alias,
                )
        return None
