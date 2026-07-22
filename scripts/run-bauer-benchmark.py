from __future__ import annotations

import argparse
import json
import os
import statistics
import sys
import time
from pathlib import Path
from typing import Any

import httpx

REPO_ROOT = Path(__file__).resolve().parents[1]
SERVICE_ROOT = REPO_ROOT / "services" / "bauer-twin-api"
sys.path.insert(0, str(SERVICE_ROOT))

from bauer_twin.engine import SearchEngine  # noqa: E402


CASES: list[dict[str, Any]] = [
    {"name": "exact-project", "action": "search_similar_projects", "query": "SYN-BK-N2-365-400", "expected": "SYN-BK-N2-365-400"},
    {"name": "exact-part", "action": "search_parts", "query": "SYN-P-SNS-PRESSURE-500", "expected": "SYN-P-SNS-PRESSURE-500"},
    {"name": "exact-document", "action": "search_documents", "query": "DOC-BM-40", "expected": "DOC-BM-40"},
    {"name": "n2-420-500", "action": "search_similar_projects", "query": "nitrogen booster 420 bar 500 l/min", "expected": "SYN-BK-N2-420-500", "medium": "nitrogen", "minimum_pressure": 420},
    {"name": "n2-420-near", "action": "search_similar_projects", "query": "N2 booster 420 bar about 480 l/min", "expected": "SYN-BK-N2-420-500", "medium": "nitrogen", "minimum_pressure": 420},
    {"name": "n2-365", "action": "search_similar_projects", "query": "nitrogen booster 365 bar 400 l/min", "expected": "SYN-BK-N2-365-400", "medium": "nitrogen", "minimum_pressure": 365},
    {"name": "bm40", "action": "search_similar_projects", "query": "BM 40 air compressor 500 l/min", "expected": "SYN-BK-AIR-BM40-500", "medium": "air", "minimum_pressure": 40},
    {"name": "bm100", "action": "search_similar_projects", "query": "BM 100 air 800 l/min", "expected": "SYN-BK-AIR-BM100-800", "medium": "air", "minimum_pressure": 100},
    {"name": "breathing-300", "action": "search_similar_projects", "query": "breathing air station 300 bar 320 l/min", "expected": "SYN-BK-BA-300-320", "medium": "breathing air", "minimum_pressure": 300},
    {"name": "breathing-420", "action": "search_similar_projects", "query": "breathing air station 420 bar 500 l/min", "expected": "SYN-BK-BA-420-500", "medium": "breathing air", "minimum_pressure": 420},
    {"name": "german-n2", "action": "search_similar_projects", "query": "Stickstoff Booster 420 bar 500 l/min", "expected": "SYN-BK-N2-420-500", "medium": "nitrogen", "minimum_pressure": 420},
    {"name": "german-breathing", "action": "search_similar_projects", "query": "Atemluft Kompressor 420 bar 320 l/min", "expected": "SYN-BK-BA-420-320", "medium": "breathing air", "minimum_pressure": 420},
    {"name": "helium-no-match-en", "action": "search_similar_projects", "query": "helium booster 420 bar 500 l/min", "expected": None, "expected_status": "no_compatible_match", "expected_filter_medium": "helium"},
    {"name": "helium-no-match-de", "action": "search_similar_projects", "query": "Heliumgas-Nachverdichter 420 bar 500 l/min", "expected": None, "expected_status": "no_compatible_match", "expected_filter_medium": "helium"},
    {"name": "unknown-medium", "action": "search_similar_projects", "query": "SpecialGas-X booster 420 bar 500 l/min", "parameters": {"medium": "SpecialGas-X"}, "expected": None, "expected_status": "unknown_constraint"},
    {"name": "n2-filter", "action": "search_parts", "query": "Filterpatrone fuer Stickstoff 420 bar", "expected_prefix": "SYN-P-PUR-N2-420", "medium": "nitrogen", "minimum_pressure": 420},
    {"name": "pressure-sensor", "action": "search_parts", "query": "pressure sensor suitable for nitrogen at 420 bar", "expected": "SYN-P-SNS-PRESSURE-500", "medium": "nitrogen", "minimum_pressure": 420},
    {"name": "bm40-document", "action": "search_documents", "query": "BM 40 product information", "expected": "DOC-BM-40"},
]


def execute_local(engine: SearchEngine, case: dict[str, Any]) -> dict[str, Any]:
    return engine.execute(case["action"], query=case["query"], limit=5, **case.get("parameters", {}))


def execute_http(client: httpx.Client, url: str, case: dict[str, Any]) -> dict[str, Any]:
    payload = {"action": case["action"], "query": case["query"], "limit": 5, **case.get("parameters", {})}
    response = client.post(f"{url.rstrip('/')}/v1/search", json=payload)
    response.raise_for_status()
    return response.json()


def result_id(action: str, result: dict[str, Any]) -> str | None:
    rows = result.get("results") or []
    if not rows:
        return None
    key = {"search_similar_projects": "project_id", "search_parts": "part_id", "search_documents": "document_id"}[action]
    return rows[0].get(key)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--url", help="Live Bauer Twin API base URL; omit for local engine")
    parser.add_argument("--token", default=os.getenv("BAUER_TWIN_API_KEY", ""))
    parser.add_argument("--output")
    args = parser.parse_args()
    engine = SearchEngine() if not args.url else None
    client = httpx.Client(timeout=60, headers={"Authorization": f"Bearer {args.token}"}) if args.url else None
    outcomes = []
    latencies = []
    hard_filter_violations = 0
    status_violations = 0
    for case in CASES:
        started = time.perf_counter()
        result = execute_http(client, args.url, case) if client else execute_local(engine, case)
        elapsed = (time.perf_counter() - started) * 1000
        latencies.append(elapsed)
        top = result_id(case["action"], result)
        passed = top == case.get("expected") if "expected" in case else bool(top and top.startswith(case["expected_prefix"]))
        if case.get("expected_status") and result.get("status") != case["expected_status"]:
            passed = False
            status_violations += 1
        if case.get("expected_filter_medium") and result.get("filters", {}).get("medium") != case["expected_filter_medium"]:
            passed = False
            status_violations += 1
        for row in result.get("results") or []:
            if case.get("medium") and case["action"] == "search_similar_projects" and row.get("medium") != case["medium"]:
                hard_filter_violations += 1
            pressure = row.get("pressure_bar") if case["action"] == "search_similar_projects" else row.get("max_pressure_bar")
            if case.get("minimum_pressure") and pressure is not None and float(pressure) < case["minimum_pressure"]:
                hard_filter_violations += 1
        outcomes.append({"name": case["name"], "passed": passed, "top_result": top, "elapsed_ms": round(elapsed, 2)})

    english = next(item for item in outcomes if item["name"] == "n2-420-500")["top_result"]
    german = next(item for item in outcomes if item["name"] == "german-n2")["top_result"]
    summary = {
        "cases": len(outcomes),
        "passed": sum(1 for item in outcomes if item["passed"]),
        "top1_accuracy": round(sum(1 for item in outcomes if item["passed"]) / len(outcomes), 4),
        "hard_filter_violations": hard_filter_violations,
        "status_violations": status_violations,
        "german_english_consistent": english == german,
        "median_latency_ms": round(statistics.median(latencies), 2),
        "max_latency_ms": round(max(latencies), 2),
        "outcomes": outcomes,
    }
    rendered = json.dumps(summary, indent=2)
    print(rendered)
    if args.output:
        output = Path(args.output)
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(rendered + "\n", encoding="utf-8")
    if summary["passed"] != summary["cases"] or hard_filter_violations or status_violations or not summary["german_english_consistent"]:
        raise SystemExit(1)


if __name__ == "__main__":
    main()

