from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


EVAL_ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(EVAL_ROOT))

from v3eval import (  # noqa: E402
    EvaluationContractError,
    load_run,
    load_suite,
    score_run,
    write_json_exclusive,
)


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Score one immutable Bauer RAG V3 evaluation run."
    )
    parser.add_argument("--run", type=Path, required=True)
    parser.add_argument(
        "--suite-root",
        type=Path,
        default=EVAL_ROOT,
        help="Evaluation root containing cases/<split>.yaml.",
    )
    parser.add_argument(
        "--split",
        choices=("development", "holdout"),
        default="development",
        help="One explicit split; combined loading is intentionally unsupported.",
    )
    parser.add_argument("--acknowledge-locked-holdout", action="store_true")
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()

    try:
        suite = load_suite(
            args.suite_root,
            split=args.split,
            acknowledge_locked_holdout=args.acknowledge_locked_holdout,
        )
        run = load_run(args.run, expected_split=args.split)
        report = score_run(suite, run)
        if args.output:
            write_json_exclusive(args.output, report)
    except (EvaluationContractError, FileExistsError) as error:
        parser.error(str(error))

    print(json.dumps(report, indent=2, ensure_ascii=False, sort_keys=True))
    if args.output:
        print(args.output)


if __name__ == "__main__":
    main()
