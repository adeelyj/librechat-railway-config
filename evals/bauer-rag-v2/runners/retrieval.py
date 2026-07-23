from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path
from typing import Any


sys.path.insert(0, str(Path(__file__).resolve().parent))
from common import (  # noqa: E402
    case_prompt,
    create_run_directory,
    load_cases,
    load_manifest,
    normalize_v1,
    normalize_v2,
    request_json,
    require_secret,
    run_metadata,
    utc_now,
    write_json_exclusive,
    write_observation,
    write_run_metadata,
)


def execute(
    *,
    system: str,
    base_url: str,
    token: str,
    query: str,
    file_ids: list[str],
    entity_id: str,
    timeout: float,
    v2_debug: bool,
) -> dict[str, Any]:
    path = "/query_v2" if system == "v2" else "/query_multiple"
    payload = {
        "query": query,
        "file_ids": file_ids,
        "entity_id": entity_id,
        "k": 10,
    }
    if system == "v2":
        payload["debug"] = v2_debug
    status, raw, elapsed_ms = request_json(
        method="POST",
        url=f"{base_url.rstrip('/')}{path}",
        token=token,
        payload=payload,
        timeout=timeout,
    )
    allowed = set(file_ids)
    results, unauthorized = (
        normalize_v2(raw, allowed) if system == "v2" else normalize_v1(raw, allowed)
    )
    return {
        "system": system,
        "path": path,
        "http_status": status,
        "elapsed_ms": elapsed_ms,
        "unauthorized_result_count": unauthorized,
        "results": results,
        "raw_response": raw,
        "completed_at": utc_now(),
    }


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Run serial, alternating V1/V2 retrieval-only Bauer evaluations."
    )
    parser.add_argument("--base-url", default=os.getenv("RAG_API_URL", ""))
    parser.add_argument("--token-env", default="RAG_API_TOKEN")
    parser.add_argument("--v1-entity-id", required=True)
    parser.add_argument("--v2-entity-id", required=True)
    parser.add_argument("--split", choices=("development", "holdout", "all"), default="development")
    parser.add_argument("--acknowledge-locked-holdout", action="store_true")
    parser.add_argument("--repetitions", type=int, default=5)
    parser.add_argument("--timeout", type=float, default=120)
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--fail-fast", action="store_true")
    parser.add_argument(
        "--v2-debug",
        action="store_true",
        help="Request admin-only V2 diagnostics; they stay in raw retrieval output.",
    )
    args = parser.parse_args()

    if not args.base_url:
        parser.error("--base-url or RAG_API_URL is required")
    if not 1 <= args.repetitions <= 20:
        parser.error("--repetitions must be between 1 and 20")
    try:
        token = require_secret(args.token_env)
        cases = load_cases(args.split, args.acknowledge_locked_holdout)
    except ValueError as error:
        parser.error(str(error))

    manifest = load_manifest()
    file_ids = [record["file_id"] for record in manifest["records"]]
    run_dir = create_run_directory(args.run_id)
    run = run_metadata(
        "retrieval",
        args.run_id,
        {
            "split": args.split,
            "repetitions": args.repetitions,
            "serial_execution": True,
            "alternating_order": True,
            "v2_debug": args.v2_debug,
            "base_url": args.base_url,
            "systems": {
                "v1": {"entity_id": args.v1_entity_id, "path": "/query_multiple"},
                "v2": {"entity_id": args.v2_entity_id, "path": "/query_v2"},
            },
            "file_count": len(file_ids),
            "observations": [],
        },
    )
    write_run_metadata(run_dir, run)

    for case_index, case in enumerate(cases):
        for repetition in range(1, args.repetitions + 1):
            order = ("v1", "v2") if (case_index + repetition) % 2 else ("v2", "v1")
            for sequence, system in enumerate(order, start=1):
                entity_id = (
                    args.v1_entity_id if system == "v1" else args.v2_entity_id
                )
                try:
                    observation = execute(
                        system=system,
                        base_url=args.base_url,
                        token=token,
                        query=case_prompt(case),
                        file_ids=file_ids,
                        entity_id=entity_id,
                        timeout=args.timeout,
                        v2_debug=args.v2_debug,
                    )
                except Exception as error:  # preserve a complete timed-run record
                    observation = {
                        "system": system,
                        "error": f"{type(error).__name__}: {error}",
                        "results": [],
                        "completed_at": utc_now(),
                    }
                    if args.fail_fast:
                        raise
                observation.update(
                    {
                        "case_id": case["id"],
                        "split": case["split"],
                        "category": case["category"],
                        "repetition": repetition,
                        "sequence_in_pair": sequence,
                        "pair_order": list(order),
                    }
                )
                run["observations"].append(observation)
                write_observation(run_dir, len(run["observations"]), observation)
                print(
                    f"{case['id']} repetition={repetition} system={system} "
                    f"status={observation.get('http_status', 'error')} "
                    f"results={len(observation['results'])}"
                )

    run["completed_at"] = utc_now()
    write_json_exclusive(run_dir / "run.json", run)
    print(run_dir / "run.json")


if __name__ == "__main__":
    main()
