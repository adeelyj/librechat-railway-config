from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Any


sys.path.insert(0, str(Path(__file__).resolve().parent))
from common import (  # noqa: E402
    create_run_directory,
    load_json,
    load_manifest,
    run_metadata,
    utc_now,
    write_json_exclusive,
)


def key(item: dict[str, Any]) -> tuple[str, int]:
    return str(item["case_id"]), int(item["repetition"])


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Import independently produced Codex answers into a new immutable raw run."
    )
    parser.add_argument("--packages", type=Path, required=True)
    parser.add_argument("--answers", type=Path, required=True)
    parser.add_argument("--run-id", required=True)
    args = parser.parse_args()
    packages = load_json(args.packages)
    supplied = load_json(args.answers)
    if packages.get("kind") != "reference_packages":
        parser.error("--packages must point to a reference_packages run")
    if supplied.get("source_run_id") != packages.get("run_id"):
        parser.error("answer source_run_id does not match the package run")
    expected = {key(item): item for item in packages["observations"]}
    answers = {key(item): item for item in supplied.get("answers", [])}
    if set(expected) != set(answers):
        missing = sorted(set(expected) - set(answers))
        extra = sorted(set(answers) - set(expected))
        parser.error(f"answer keys differ; missing={missing[:5]} extra={extra[:5]}")

    allowed = {
        str(record["file_id"]) for record in load_manifest()["records"]
    }
    output_observations = []
    for observation_key, package in expected.items():
        answer = answers[observation_key]
        if not str(answer.get("answer") or "").strip():
            parser.error(f"empty answer for {observation_key}")
        evidence = answer.get("evidence") or []
        unauthorized = [
            item.get("file_id")
            for item in evidence
            if str(item.get("file_id") or "") not in allowed
        ]
        if unauthorized:
            parser.error(
                f"answer {observation_key} cites unauthorized file IDs: {unauthorized[:5]}"
            )
        output_observations.append(
            {
                **package,
                "answer": answer["answer"],
                "evidence": evidence,
                "elapsed_ms": answer.get("elapsed_ms"),
                "completed_at": answer.get("completed_at") or utc_now(),
            }
        )

    run_dir = create_run_directory(args.run_id)
    run = run_metadata(
        "end_to_end",
        args.run_id,
        {
            "split": packages["split"],
            "system": "codex-reference",
            "source_package_run_id": packages["run_id"],
            "source_answers": str(args.answers.resolve()),
            "external_web_allowed": False,
            "outside_knowledge_allowed": False,
            "observations": output_observations,
            "completed_at": utc_now(),
        },
    )
    write_json_exclusive(run_dir / "run.json", run)
    print(run_dir / "run.json")


if __name__ == "__main__":
    main()
