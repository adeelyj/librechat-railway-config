from __future__ import annotations

import math
import re
import time
from decimal import Decimal
from typing import Any

from .catalog import DISCLAIMER, searchable_text
from .embeddings import EmbeddingClient, deterministic_embedding
from .repository import CatalogRepository
from .terminology import TerminologyResolver

PROJECT_DIFFERENCE_FIELDS = [
    "application_sector",
    "medium",
    "pressure_bar",
    "capacity_l_min",
    "compressor_family",
    "compressor_model",
    "topology",
    "cooling",
    "control_package",
    "purification_package",
    "storage_filling_package",
    "installation",
    "environment",
    "standards",
]

TERMINOLOGY_FIELDS = ("medium", "topology", "compressor_family", "category")


def _tokens(value: str) -> set[str]:
    replacements = {
        "stickstoff": "nitrogen",
        "atemluft": "breathing air",
        "drucksensor": "pressure sensor",
        "filterpatrone": "filter cartridge",
        "verdichter": "compressor",
        "kompressor": "compressor",
        "steuerung": "controller",
        "speicher": "storage",
    }
    normalized = value.lower()
    for source, target in replacements.items():
        normalized = normalized.replace(source, target)
    return set(re.findall(r"[a-z0-9]+", normalized))


def _cosine(left: list[float], right: Any) -> float:
    if right is None:
        return 0.0
    if isinstance(right, str):
        right = [float(item) for item in right.strip("[]").split(",") if item]
    if not isinstance(right, (list, tuple)) or len(left) != len(right):
        return 0.0
    dot = sum(a * float(b) for a, b in zip(left, right))
    left_norm = math.sqrt(sum(a * a for a in left))
    right_norm = math.sqrt(sum(float(b) * float(b) for b in right))
    return dot / (left_norm * right_norm) if left_norm and right_norm else 0.0


def _public(record: dict[str, Any]) -> dict[str, Any]:
    def json_value(value: Any) -> Any:
        if isinstance(value, Decimal):
            return float(value)
        if isinstance(value, tuple):
            return [json_value(item) for item in value]
        if isinstance(value, list):
            return [json_value(item) for item in value]
        if isinstance(value, dict):
            return {key: json_value(item) for key, item in value.items()}
        return value

    return {
        key: json_value(value)
        for key, value in record.items()
        if key not in {"embedding", "search_text", "updated_at"}
    }


class SearchEngine:
    def __init__(
        self,
        repository: CatalogRepository | None = None,
        embedding_client: EmbeddingClient | None = None,
    ) -> None:
        self.repository = repository or CatalogRepository()
        self.embedding_client = embedding_client or EmbeddingClient()
        self.terminology = TerminologyResolver(self.repository.load_terminology())

    def _catalog(self) -> dict[str, list[dict[str, Any]]]:
        catalog = self.repository.load()
        for collection in catalog.values():
            for record in collection:
                record.setdefault("search_text", searchable_text(record))
                record.setdefault("embedding", deterministic_embedding(record["search_text"]))
        return catalog

    def _interpret_filters(
        self, query: str, supplied: dict[str, Any]
    ) -> tuple[dict[str, Any], dict[str, dict[str, Any]], list[dict[str, Any]]]:
        result = {
            key: value
            for key, value in supplied.items()
            if value is not None and key not in TERMINOLOGY_FIELDS
        }
        interpretation: dict[str, dict[str, Any]] = {}
        unknown_constraints: list[dict[str, Any]] = []

        for field in TERMINOLOGY_FIELDS:
            supplied_value = supplied.get(field)
            if supplied_value is not None and str(supplied_value).strip():
                resolution = self.terminology.resolve(field, str(supplied_value))
            else:
                resolution = self.terminology.infer(field, query)
            if resolution is None:
                continue
            interpretation[field] = resolution.as_dict()
            if resolution.status == "recognized":
                result[field] = resolution.normalized
            else:
                unknown_constraints.append({"field": field, **resolution.as_dict()})

        lower = query.lower()
        if "target_pressure_bar" not in result:
            match = re.search(r"(\d+(?:[.,]\d+)?)\s*bar(?:g|a)?\b", lower)
            if match:
                result["target_pressure_bar"] = float(match.group(1).replace(",", "."))
        if "capacity_l_min" not in result:
            match = re.search(
                r"(\d+(?:[.,]\d+)?)\s*(?:l\s*/\s*min|lpm|lit(?:er|re)s?\s*(?:per|/)\s*min)",
                lower,
            )
            if match:
                result["capacity_l_min"] = float(match.group(1).replace(",", "."))
        return result, interpretation, unknown_constraints

    def infer_filters(self, query: str, supplied: dict[str, Any]) -> dict[str, Any]:
        filters, _, _ = self._interpret_filters(query, supplied)
        return filters

    def _base(self, action: str, started: float) -> dict[str, Any]:
        return {
            "action": action,
            "dataset": "bauer_synthetic_demo_v1",
            "disclaimer": DISCLAIMER,
            "elapsed_ms": round((time.perf_counter() - started) * 1000, 2),
        }

    def _unknown_constraint_result(
        self,
        action: str,
        started: float,
        filters: dict[str, Any],
        interpretation: dict[str, dict[str, Any]],
        unknown_constraints: list[dict[str, Any]],
    ) -> dict[str, Any]:
        result = self._base(action, started)
        result.update(
            {
                "status": "unknown_constraint",
                "message": "One or more mandatory constraints could not be normalized safely.",
                "filters": filters,
                "interpretation": interpretation,
                "unknown_constraints": unknown_constraints,
                "hard_exclusions": [],
                "results": [],
            }
        )
        return result

    @staticmethod
    def _project_exclusion_reasons(project: dict[str, Any], filters: dict[str, Any]) -> list[str]:
        reasons: list[str] = []
        medium = filters.get("medium")
        pressure = filters.get("target_pressure_bar")
        topology = filters.get("topology")
        family = filters.get("compressor_family")
        if medium and project["medium"].lower() != str(medium).lower():
            reasons.append(f"medium mismatch: requires {medium}, project uses {project['medium']}")
        if pressure is not None and float(project["pressure_bar"]) < float(pressure):
            reasons.append(
                f"insufficient pressure: requires {pressure:g} bar, project is {project['pressure_bar']} bar"
            )
        if topology and project["topology"].lower() != str(topology).lower():
            reasons.append(f"topology mismatch: requires {topology}, project is {project['topology']}")
        if family and project["compressor_family"].lower() != str(family).lower():
            reasons.append(
                f"compressor family mismatch: requires {family}, project uses {project['compressor_family']}"
            )
        return reasons

    @staticmethod
    def _part_exclusion_reasons(part: dict[str, Any], filters: dict[str, Any]) -> list[str]:
        reasons: list[str] = []
        medium = filters.get("medium")
        pressure = filters.get("target_pressure_bar")
        category = filters.get("category")
        compatible_media = {str(item).lower() for item in part["compatible_media"]}
        if medium and str(medium).lower() not in compatible_media:
            reasons.append(f"medium mismatch: requires {medium}")
        if pressure is not None and float(part["max_pressure_bar"]) < float(pressure):
            reasons.append(
                f"insufficient pressure rating: requires {pressure:g} bar, part max is {part['max_pressure_bar']} bar"
            )
        if category and part["category"].lower() != str(category).lower():
            reasons.append(f"category mismatch: requires {category}")
        return reasons

    def search_similar_projects(self, query: str, limit: int = 5, **filters: Any) -> dict[str, Any]:
        started = time.perf_counter()
        catalog = self._catalog()
        inferred, interpretation, unknown_constraints = self._interpret_filters(query, filters)
        if unknown_constraints:
            return self._unknown_constraint_result(
                "search_similar_projects",
                started,
                inferred,
                interpretation,
                unknown_constraints,
            )
        query_upper = query.strip().upper()
        exact = [p for p in catalog["projects"] if query_upper in {str(p["project_id"]).upper(), str(p["compressor_model"]).upper()}]
        if exact:
            reasons = self._project_exclusion_reasons(exact[0], inferred)
            result = self._base("search_similar_projects", started)
            result.update(
                {
                    "status": "no_compatible_match" if reasons else "matches_found",
                    "filters": inferred,
                    "interpretation": interpretation,
                    "unknown_constraints": [],
                    "hard_exclusions": (
                        [{"project_id": exact[0]["project_id"], "reasons": reasons}] if reasons else []
                    ),
                    "results": (
                        []
                        if reasons
                        else [{**_public(exact[0]), "score": 1.0, "match_reasons": ["exact identifier match"]}]
                    ),
                }
            )
            return result

        eligible: list[dict[str, Any]] = []
        exclusions: list[dict[str, Any]] = []
        for project in catalog["projects"]:
            reasons = self._project_exclusion_reasons(project, inferred)
            if reasons:
                exclusions.append({"project_id": project["project_id"], "reasons": reasons})
            else:
                eligible.append(project)

        if not eligible:
            result = self._base("search_similar_projects", started)
            result.update(
                {
                    "status": "no_compatible_match",
                    "filters": inferred,
                    "interpretation": interpretation,
                    "unknown_constraints": [],
                    "hard_exclusions": exclusions,
                    "results": [],
                }
            )
            return result

        query_vector = self.embedding_client.embed(query)
        query_tokens = _tokens(query)
        included: list[dict[str, Any]] = []
        medium = inferred.get("medium")
        pressure = inferred.get("target_pressure_bar")
        topology = inferred.get("topology")
        family = inferred.get("compressor_family")
        for project in eligible:
            score = 0.0
            match_reasons: list[str] = []
            if medium:
                score += 0.25
                match_reasons.append(f"compatible medium: {medium}")
            if pressure is not None:
                pressure_delta = abs(float(project["pressure_bar"]) - float(pressure))
                score += 0.20 * max(0.0, 1.0 - pressure_delta / max(float(pressure), 1.0))
                match_reasons.append(f"pressure delta: {pressure_delta:g} bar")
            capacity = inferred.get("capacity_l_min")
            if capacity is not None:
                capacity_delta = abs(float(project["capacity_l_min"]) - float(capacity))
                score += 0.15 * max(0.0, 1.0 - capacity_delta / max(float(capacity), 1.0))
                match_reasons.append(f"capacity delta: {capacity_delta:g} l/min")
            if family and project["compressor_family"].lower() == str(family).lower():
                score += 0.10
                match_reasons.append(f"same compressor family: {family}")
            if topology:
                score += 0.10
                match_reasons.append(f"same topology: {topology}")
            overlap = len(query_tokens & _tokens(project["search_text"])) / max(len(query_tokens), 1)
            score += 0.10 * overlap
            semantic = max(0.0, _cosine(query_vector, project["embedding"]))
            score += 0.10 * semantic
            included.append({**_public(project), "score": round(score, 4), "match_reasons": match_reasons})

        included.sort(key=lambda item: (-item["score"], item["project_id"]))
        result = self._base("search_similar_projects", started)
        limited = included[: max(1, min(limit, 10))]
        result.update(
            {
                "status": "matches_found" if limited else "no_compatible_match",
                "filters": inferred,
                "interpretation": interpretation,
                "unknown_constraints": [],
                "hard_exclusions": exclusions,
                "results": limited,
            }
        )
        return result

    def compare_projects(self, project_id: str, compare_project_id: str) -> dict[str, Any]:
        started = time.perf_counter()
        projects = {p["project_id"].upper(): p for p in self._catalog()["projects"]}
        left = projects.get(project_id.upper())
        right = projects.get(compare_project_id.upper())
        result = self._base("compare_projects", started)
        if not left or not right:
            result.update({"status": "no_compatible_match", "found": False, "missing_ids": [identifier for identifier, item in ((project_id, left), (compare_project_id, right)) if item is None], "differences": []})
            return result
        differences = []
        matches = []
        for field in PROJECT_DIFFERENCE_FIELDS:
            if left.get(field) == right.get(field):
                matches.append({"field": field, "value": left.get(field)})
            else:
                differences.append({"field": field, project_id: left.get(field), compare_project_id: right.get(field)})
        result.update({"status": "matches_found", "found": True, "projects": [_public(left), _public(right)], "matches": matches, "differences": differences})
        return result

    def search_parts(self, query: str, limit: int = 10, **filters: Any) -> dict[str, Any]:
        started = time.perf_counter()
        catalog = self._catalog()
        inferred, interpretation, unknown_constraints = self._interpret_filters(query, filters)
        if unknown_constraints:
            return self._unknown_constraint_result(
                "search_parts",
                started,
                inferred,
                interpretation,
                unknown_constraints,
            )
        query_upper = query.strip().upper()
        exact = [p for p in catalog["parts"] if query_upper == str(p["part_id"]).upper()]
        if exact:
            reasons = self._part_exclusion_reasons(exact[0], inferred)
            result = self._base("search_parts", started)
            result.update(
                {
                    "status": "no_compatible_match" if reasons else "matches_found",
                    "filters": inferred,
                    "interpretation": interpretation,
                    "unknown_constraints": [],
                    "hard_exclusions": (
                        [{"part_id": exact[0]["part_id"], "reasons": reasons}] if reasons else []
                    ),
                    "results": (
                        []
                        if reasons
                        else [{**_public(exact[0]), "score": 1.0, "match_reasons": ["exact part identifier match"]}]
                    ),
                }
            )
            return result
        eligible = []
        exclusions = []
        for part in catalog["parts"]:
            reasons = self._part_exclusion_reasons(part, inferred)
            if reasons:
                exclusions.append({"part_id": part["part_id"], "reasons": reasons})
            else:
                eligible.append(part)

        if not eligible:
            result = self._base("search_parts", started)
            result.update(
                {
                    "status": "no_compatible_match",
                    "filters": inferred,
                    "interpretation": interpretation,
                    "unknown_constraints": [],
                    "hard_exclusions": exclusions,
                    "results": [],
                }
            )
            return result

        query_vector = self.embedding_client.embed(query)
        query_tokens = _tokens(query)
        included = []
        medium = inferred.get("medium")
        pressure = inferred.get("target_pressure_bar")
        for part in eligible:
            overlap = len(query_tokens & _tokens(part["search_text"])) / max(len(query_tokens), 1)
            semantic = max(0.0, _cosine(query_vector, part["embedding"]))
            score = 0.60 * overlap + 0.25 * semantic
            if medium:
                score += 0.10
            if pressure is not None:
                score += 0.05 * min(1.0, float(pressure) / max(float(part["max_pressure_bar"]), 1.0))
            included.append({**_public(part), "score": round(score, 4), "match_reasons": ["keyword/semantic description match", "passed compatibility filters"]})
        included.sort(key=lambda item: (-item["score"], item["part_id"]))
        result = self._base("search_parts", started)
        limited = included[: max(1, min(limit, 20))]
        result.update(
            {
                "status": "matches_found" if limited else "no_compatible_match",
                "filters": inferred,
                "interpretation": interpretation,
                "unknown_constraints": [],
                "hard_exclusions": exclusions,
                "results": limited,
            }
        )
        return result

    def search_documents(self, query: str, limit: int = 10) -> dict[str, Any]:
        started = time.perf_counter()
        query_upper = query.strip().upper()
        query_tokens = _tokens(query)
        query_vector = self.embedding_client.embed(query)
        results = []
        for document in self._catalog()["documents"]:
            exact = query_upper == str(document["document_id"]).upper()
            overlap = len(query_tokens & _tokens(document["search_text"])) / max(len(query_tokens), 1)
            semantic = max(0.0, _cosine(query_vector, document["embedding"]))
            score = 1.0 if exact else 0.75 * overlap + 0.25 * semantic
            if score > 0 or exact:
                results.append({**_public(document), "score": round(score, 4), "match_reasons": ["exact identifier match" if exact else "keyword/semantic match"]})
        results.sort(key=lambda item: (-item["score"], item["document_id"]))
        result = self._base("search_documents", started)
        limited = results[: max(1, min(limit, 20))]
        result.update({"status": "matches_found" if limited else "no_compatible_match", "results": limited})
        return result

    def get_details(self, kind: str, identifier: str) -> dict[str, Any]:
        started = time.perf_counter()
        catalog = self._catalog()
        collection = catalog["projects"] if kind == "project" else catalog["parts"]
        key = "project_id" if kind == "project" else "part_id"
        record = next((item for item in collection if str(item[key]).upper() == identifier.upper()), None)
        result = self._base(f"get_{kind}_details", started)
        if record is None:
            result.update({"status": "no_compatible_match", "found": False, key: identifier, "message": "No matching synthetic demo record was found."})
            return result
        document_ids = set(record.get("document_ids") or [])
        documents = [_public(item) for item in catalog["documents"] if item["document_id"] in document_ids]
        result.update({"status": "matches_found", "found": True, kind: _public(record), "documents": documents})
        if kind == "project":
            result["parts"] = [_public(item) for item in catalog["parts"] if identifier in (item.get("project_ids") or [])]
        return result

    def execute(self, action: str, **arguments: Any) -> dict[str, Any]:
        if action == "search_similar_projects":
            return self.search_similar_projects(**arguments)
        if action == "compare_projects":
            return self.compare_projects(arguments["project_id"], arguments["compare_project_id"])
        if action == "search_parts":
            return self.search_parts(**arguments)
        if action == "search_documents":
            return self.search_documents(arguments.get("query", ""), arguments.get("limit", 10))
        if action == "get_project_details":
            return self.get_details("project", arguments["project_id"])
        if action == "get_part_details":
            return self.get_details("part", arguments["part_id"])
        raise ValueError(f"Unsupported action: {action}")
