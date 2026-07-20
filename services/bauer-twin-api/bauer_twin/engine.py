from __future__ import annotations

import math
import re
import time
from decimal import Decimal
from typing import Any

from .catalog import DISCLAIMER, searchable_text
from .embeddings import EmbeddingClient, deterministic_embedding
from .repository import CatalogRepository

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

    def _catalog(self) -> dict[str, list[dict[str, Any]]]:
        catalog = self.repository.load()
        for collection in catalog.values():
            for record in collection:
                record.setdefault("search_text", searchable_text(record))
                record.setdefault("embedding", deterministic_embedding(record["search_text"]))
        return catalog

    @staticmethod
    def infer_filters(query: str, supplied: dict[str, Any]) -> dict[str, Any]:
        result = {key: value for key, value in supplied.items() if value is not None}
        lower = query.lower()
        if "medium" not in result:
            if re.search(r"\b(nitrogen|stickstoff|n2)\b", lower):
                result["medium"] = "nitrogen"
            elif "breathing air" in lower or "atemluft" in lower:
                result["medium"] = "breathing air"
            elif re.search(r"\b(air|luft)\b", lower):
                result["medium"] = "air"
        if "target_pressure_bar" not in result:
            match = re.search(r"(\d{2,3})\s*bar\b", lower)
            if match:
                result["target_pressure_bar"] = float(match.group(1))
        if "capacity_l_min" not in result:
            match = re.search(r"(\d{2,4})\s*(?:l\s*/\s*min|lpm|lit(?:er|re)s?\s*(?:per|/)\s*min)", lower)
            if match:
                result["capacity_l_min"] = float(match.group(1))
        if "compressor_family" not in result:
            match = re.search(r"\bbm\s*(40|100)\b", lower)
            if match:
                result["compressor_family"] = f"BM {match.group(1)}"
        if "topology" not in result and "booster" in lower:
            result["topology"] = "booster"
        if "category" not in result:
            if re.search(r"\b(sensor|transmitter|drucksensor|temperatursensor|taupunktsensor)\b", lower):
                result["category"] = "SNS"
            elif re.search(r"\b(filter|cartridge|filterpatrone|purification)\b", lower):
                result["category"] = "PUR"
            elif re.search(r"\b(valve|ventil)\b", lower):
                result["category"] = "VLV"
        return result

    def _base(self, action: str, started: float) -> dict[str, Any]:
        return {
            "action": action,
            "dataset": "bauer_synthetic_demo_v1",
            "disclaimer": DISCLAIMER,
            "elapsed_ms": round((time.perf_counter() - started) * 1000, 2),
        }

    def search_similar_projects(self, query: str, limit: int = 5, **filters: Any) -> dict[str, Any]:
        started = time.perf_counter()
        catalog = self._catalog()
        inferred = self.infer_filters(query, filters)
        query_upper = query.strip().upper()
        exact = [p for p in catalog["projects"] if query_upper in {str(p["project_id"]).upper(), str(p["compressor_model"]).upper()}]
        if exact:
            result = self._base("search_similar_projects", started)
            result.update({"filters": inferred, "hard_exclusions": [], "results": [{**_public(exact[0]), "score": 1.0, "match_reasons": ["exact identifier match"]}]})
            return result

        query_vector = self.embedding_client.embed(query)
        query_tokens = _tokens(query)
        included: list[dict[str, Any]] = []
        exclusions: list[dict[str, Any]] = []
        for project in catalog["projects"]:
            reasons: list[str] = []
            medium = inferred.get("medium")
            pressure = inferred.get("target_pressure_bar")
            topology = inferred.get("topology")
            family = inferred.get("compressor_family")
            if medium and project["medium"].lower() != str(medium).lower():
                reasons.append(f"medium mismatch: requires {medium}, project uses {project['medium']}")
            if pressure is not None and float(project["pressure_bar"]) < float(pressure):
                reasons.append(f"insufficient pressure: requires {pressure:g} bar, project is {project['pressure_bar']} bar")
            if topology and project["topology"].lower() != str(topology).lower():
                reasons.append(f"topology mismatch: requires {topology}, project is {project['topology']}")
            if family and project["compressor_family"].lower() != str(family).lower():
                reasons.append(
                    f"compressor family mismatch: requires {family}, project uses {project['compressor_family']}"
                )
            if reasons:
                exclusions.append({"project_id": project["project_id"], "reasons": reasons})
                continue

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
        result.update({"filters": inferred, "hard_exclusions": exclusions, "results": included[: max(1, min(limit, 10))]})
        return result

    def compare_projects(self, project_id: str, compare_project_id: str) -> dict[str, Any]:
        started = time.perf_counter()
        projects = {p["project_id"].upper(): p for p in self._catalog()["projects"]}
        left = projects.get(project_id.upper())
        right = projects.get(compare_project_id.upper())
        result = self._base("compare_projects", started)
        if not left or not right:
            result.update({"found": False, "missing_ids": [identifier for identifier, item in ((project_id, left), (compare_project_id, right)) if item is None], "differences": []})
            return result
        differences = []
        matches = []
        for field in PROJECT_DIFFERENCE_FIELDS:
            if left.get(field) == right.get(field):
                matches.append({"field": field, "value": left.get(field)})
            else:
                differences.append({"field": field, project_id: left.get(field), compare_project_id: right.get(field)})
        result.update({"found": True, "projects": [_public(left), _public(right)], "matches": matches, "differences": differences})
        return result

    def search_parts(self, query: str, limit: int = 10, **filters: Any) -> dict[str, Any]:
        started = time.perf_counter()
        catalog = self._catalog()
        inferred = self.infer_filters(query, filters)
        query_upper = query.strip().upper()
        exact = [p for p in catalog["parts"] if query_upper == str(p["part_id"]).upper()]
        if exact:
            result = self._base("search_parts", started)
            result.update({"filters": inferred, "hard_exclusions": [], "results": [{**_public(exact[0]), "score": 1.0, "match_reasons": ["exact part identifier match"]}]})
            return result
        query_vector = self.embedding_client.embed(query)
        query_tokens = _tokens(query)
        included = []
        exclusions = []
        for part in catalog["parts"]:
            reasons = []
            medium = inferred.get("medium")
            pressure = inferred.get("target_pressure_bar")
            if medium and medium not in part["compatible_media"]:
                reasons.append(f"medium mismatch: requires {medium}")
            if pressure is not None and float(part["max_pressure_bar"]) < float(pressure):
                reasons.append(f"insufficient pressure rating: requires {pressure:g} bar, part max is {part['max_pressure_bar']} bar")
            category = inferred.get("category")
            if category and part["category"].lower() != str(category).lower():
                reasons.append(f"category mismatch: requires {category}")
            if reasons:
                exclusions.append({"part_id": part["part_id"], "reasons": reasons})
                continue
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
        result.update({"filters": inferred, "hard_exclusions": exclusions, "results": included[: max(1, min(limit, 20))]})
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
        result.update({"results": results[: max(1, min(limit, 20))]})
        return result

    def get_details(self, kind: str, identifier: str) -> dict[str, Any]:
        started = time.perf_counter()
        catalog = self._catalog()
        collection = catalog["projects"] if kind == "project" else catalog["parts"]
        key = "project_id" if kind == "project" else "part_id"
        record = next((item for item in collection if str(item[key]).upper() == identifier.upper()), None)
        result = self._base(f"get_{kind}_details", started)
        if record is None:
            result.update({"found": False, key: identifier, "message": "No matching synthetic demo record was found."})
            return result
        document_ids = set(record.get("document_ids") or [])
        documents = [_public(item) for item in catalog["documents"] if item["document_id"] in document_ids]
        result.update({"found": True, kind: _public(record), "documents": documents})
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
