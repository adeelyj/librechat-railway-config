from __future__ import annotations

import argparse
import sys
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
RUNNERS = REPO_ROOT / "evals" / "bauer-rag-v2" / "runners"
sys.path.insert(0, str(RUNNERS))
from common import (  # noqa: E402
    EVAL_ROOT,
    collect_evidence,
    create_run_directory,
    load_json,
    run_metadata,
    sha256_file,
    write_json_exclusive,
)


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Import the preserved 2026-07-21 V1 Agent run as an immutable baseline."
    )
    parser.add_argument(
        "--source",
        type=Path,
        default=Path(
            r"D:\02_Code\LibreChat_Setup\tmp\librechat-benchmark-outputs.json"
        ),
    )
    parser.add_argument("--run-id", default="v1-historical-20260721")
    args = parser.parse_args()
    source = load_json(args.source)
    baseline = load_json(EVAL_ROOT / "baselines" / "agents.json")
    expected_agent = baseline["bauer_v1"]["id"]
    if source.get("agent_id") != expected_agent:
        parser.error(
            f"historical Agent {source.get('agent_id')} does not match {expected_agent}"
        )
    cases = {}
    for split in ("development", "holdout"):
        payload = load_json(EVAL_ROOT / "cases" / f"{split}.yaml")
        cases.update(
            {item["id"]: {**item, "split": split} for item in payload["cases"]}
        )
    unknown = [item["id"] for item in source["cases"] if item["id"] not in cases]
    if unknown:
        parser.error(f"historical source has unknown case IDs: {unknown}")

    run_dir = create_run_directory(args.run_id)
    run = run_metadata(
        "end_to_end",
        args.run_id,
        {
            "baseline_kind": "historical_preserved_v1",
            "source_path": str(args.source.resolve()),
            "source_sha256": sha256_file(args.source),
            "source_generated_at": source.get("generated_at"),
            "source_updated_at": source.get("updated_at"),
            "base_url": source.get("base_url"),
            "systems": {"v1": {"agent_id": source["agent_id"]}},
            "repetitions": 1,
            "limitations": [
                "Historical run predates the V2 implementation.",
                "It contains one end-to-end repetition of B01-B14.",
                "It is not a replacement for the required five-run retrieval and end-to-end baseline.",
            ],
            "observations": [],
        },
    )
    for item in source["cases"]:
        case = cases[item["id"]]
        run["observations"].append(
            {
                "case_id": item["id"],
                "split": case["split"],
                "category": case["category"],
                "system": "v1",
                "agent_id": source["agent_id"],
                "repetition": 1,
                "status": item.get("status"),
                "elapsed_ms": round(float(item.get("elapsed_seconds") or 0) * 1000, 2),
                "answer": item.get("answer") or "",
                "evidence": collect_evidence(item.get("message")),
                "conversation_id": item.get("conversation_id"),
                "stream_id": item.get("stream_id"),
                "started_at": item.get("started_at"),
                "completed_at": item.get("completed_at"),
                "assistant_message": item.get("message"),
            }
        )
    run["completed_at"] = source.get("updated_at")
    write_json_exclusive(run_dir / "run.json", run)
    print(run_dir / "run.json")


if __name__ == "__main__":
    main()
