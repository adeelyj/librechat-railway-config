from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path
from typing import Any


sys.path.insert(0, str(Path(__file__).resolve().parent))
from common import (  # noqa: E402
    EVAL_ROOT,
    create_run_directory,
    extract_text,
    load_json,
    request_json,
    require_secret,
    run_metadata,
    utc_now,
    write_json_exclusive,
    write_observation,
    write_run_metadata,
)


SYSTEM_PROMPT = """You answer only from the frozen evidence package below.
Do not search, use outside knowledge, or invent missing facts. Preserve qualifications, units,
source types, and explicit engineering constraints. If the evidence does not establish the answer,
say so. Cite supporting evidence with its exact [V2-N] identifier."""


def evidence_prompt(question: str, evidence: list[dict[str, Any]]) -> str:
    blocks = []
    for index, item in enumerate(evidence, start=1):
        citation = item.get("citation_id") or f"V2-{index}"
        location_parts = [
            *(str(part) for part in (item.get("section") or []) if part),
            *(str(item[key]) for key in ("table_title", "row_label") if item.get(key)),
        ]
        blocks.append(
            "\n".join(
                [
                    f"[{citation}]",
                    f"File ID: {item.get('file_id')}",
                    f"Filename: {item.get('filename')}",
                    f"Source type: {item.get('source_type')}",
                    f"Page: {item.get('page')}",
                    f"Location: {' / '.join(location_parts)}",
                    f"Evidence: {item.get('content', '')}",
                ]
            )
        )
    return f"Question:\n{question}\n\nFrozen evidence:\n\n" + "\n\n---\n\n".join(blocks)


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Answer from frozen V2 evidence through an OpenAI-compatible endpoint."
    )
    parser.add_argument("--retrieval-run", type=Path, required=True)
    parser.add_argument("--api-url", default=os.getenv("FROZEN_EVIDENCE_API_URL", ""))
    parser.add_argument("--token-env", default="FROZEN_EVIDENCE_API_TOKEN")
    parser.add_argument("--model", default=os.getenv("FROZEN_EVIDENCE_MODEL", ""))
    parser.add_argument("--system-id", required=True, help="For example qwen-local or codex-reference")
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--repetitions", type=int, default=5)
    parser.add_argument("--timeout", type=float, default=180)
    parser.add_argument(
        "--export-only",
        action="store_true",
        help="Write frozen packages for manual Codex execution without calling an API.",
    )
    args = parser.parse_args()

    source = load_json(args.retrieval_run)
    if source.get("kind") != "retrieval":
        parser.error("--retrieval-run must be a retrieval run.json")
    if not args.export_only and (not args.api_url or not args.model):
        parser.error("--api-url and --model are required unless --export-only is used")
    token = ""
    if not args.export_only:
        try:
            token = require_secret(args.token_env)
        except ValueError as error:
            parser.error(str(error))

    cases = {}
    for name in ("development", "holdout"):
        payload = load_json(EVAL_ROOT / "cases" / f"{name}.yaml")
        cases.update({item["id"]: item for item in payload["cases"]})
    latest_v2: dict[str, dict[str, Any]] = {}
    for observation in source["observations"]:
        if observation["system"] == "v2":
            latest_v2.setdefault(observation["case_id"], observation)

    run_dir = create_run_directory(args.run_id)
    run = run_metadata(
        "frozen_evidence",
        args.run_id,
        {
            "source_retrieval_run": str(args.retrieval_run.resolve()),
            "source_retrieval_run_id": source["run_id"],
            "system": args.system_id,
            "model": args.model or None,
            "export_only": args.export_only,
            "repetitions": args.repetitions,
            "observations": [],
        },
    )
    write_run_metadata(run_dir, run)
    for case_id, retrieval in latest_v2.items():
        prompt = evidence_prompt(cases[case_id]["prompt_en"], retrieval["results"])
        for repetition in range(1, args.repetitions + 1):
            item: dict[str, Any] = {
                "case_id": case_id,
                "system": args.system_id,
                "repetition": repetition,
                "evidence": retrieval["results"],
                "frozen_prompt": prompt,
            }
            if not args.export_only:
                status, response, elapsed_ms = request_json(
                    method="POST",
                    url=args.api_url,
                    token=token,
                    payload={
                        "model": args.model,
                        "temperature": 0,
                        "messages": [
                            {"role": "system", "content": SYSTEM_PROMPT},
                            {"role": "user", "content": prompt},
                        ],
                    },
                    timeout=args.timeout,
                )
                item.update(
                    {
                        "http_status": status,
                        "elapsed_ms": elapsed_ms,
                        "answer": extract_text(response),
                        "raw_response": response,
                    }
                )
            item["completed_at"] = utc_now()
            run["observations"].append(item)
            write_observation(run_dir, len(run["observations"]), item)
            print(f"{case_id} repetition={repetition} system={args.system_id}")
    run["completed_at"] = utc_now()
    write_json_exclusive(run_dir / "run.json", run)
    print(run_dir / "run.json")


if __name__ == "__main__":
    main()
