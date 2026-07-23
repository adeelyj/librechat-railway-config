from __future__ import annotations

import argparse
import sys
from pathlib import Path


sys.path.insert(0, str(Path(__file__).resolve().parent))
from common import (  # noqa: E402
    case_prompt,
    create_run_directory,
    load_cases,
    load_manifest,
    run_metadata,
    utc_now,
    write_json_exclusive,
    write_observation,
    write_run_metadata,
)


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Export immutable, corpus-restricted packages for the Codex reference."
    )
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--split", choices=("development", "holdout", "all"), default="development")
    parser.add_argument("--acknowledge-locked-holdout", action="store_true")
    parser.add_argument("--repetitions", type=int, default=5)
    parser.add_argument(
        "--corpus",
        type=Path,
        default=Path(r"D:\02_Code\LibreChat_Setup\tmp\corpora\bauer-kompressoren"),
    )
    args = parser.parse_args()
    try:
        cases = load_cases(args.split, args.acknowledge_locked_holdout)
    except ValueError as error:
        parser.error(str(error))
    if not args.corpus.is_dir():
        parser.error(f"Frozen corpus does not exist: {args.corpus}")

    manifest = load_manifest()
    run_dir = create_run_directory(args.run_id)
    run = run_metadata(
        "reference_packages",
        args.run_id,
        {
            "split": args.split,
            "system": "codex-reference",
            "repetitions": args.repetitions,
            "external_web_allowed": False,
            "outside_knowledge_allowed": False,
            "judge": "verified_gold",
            "corpus": str(args.corpus.resolve()),
            "authorization_namespace": manifest["authorization_namespace"],
            "authorized_file_count": manifest["file_count"],
            "instructions": (
                "Use only files in the frozen corpus manifest. Do not browse the web or use "
                "outside knowledge. Preserve source type and source locations; qualify or refuse "
                "anything not established. Codex is a competitor, never the judge."
            ),
            "observations": [],
        },
    )
    write_run_metadata(run_dir, run)
    for case in cases:
        for repetition in range(1, args.repetitions + 1):
            observation = {
                "case_id": case["id"],
                "split": case["split"],
                "category": case["category"],
                "system": "codex-reference",
                "repetition": repetition,
                "prompt": case_prompt(case),
                "answer": None,
                "evidence": [],
            }
            run["observations"].append(observation)
            write_observation(run_dir, len(run["observations"]), observation)
    run["completed_at"] = utc_now()
    write_json_exclusive(run_dir / "run.json", run)
    print(run_dir / "run.json")


if __name__ == "__main__":
    main()
