"""Record an independent Bauer gold-review attestation.

The review packet is produced and signed outside this program.  This command
hashes that immutable packet and records the decision through the dedicated
reviewer database tier; it cannot execute evaluations or activate releases.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
from typing import Mapping, Sequence

from .db_security import DatabaseRoleSecurityError
from .evaluation import EvaluationError, load_evaluation_manifest
from .postgres_review import (
    PostgresIndependentGoldReviewer,
    PostgresReviewError,
)


DEFAULT_DATABASE_ENV = "BAUER_V3_REVIEW_DATABASE_URL"
DEFAULT_TENANT_ENV = "BAUER_V3_REVIEW_TENANT_ID"
DEFAULT_PRINCIPAL_ENV = "BAUER_V3_REVIEW_PRINCIPAL_ID"


def main(
    argv: Sequence[str] | None = None,
    *,
    environment: Mapping[str, str] | None = None,
) -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Bind an independent Bauer review decision to one immutable V3 "
            "gold manifest and signed review packet."
        )
    )
    parser.add_argument("--manifest", required=True, type=Path)
    parser.add_argument(
        "--split",
        required=True,
        choices=("development", "holdout"),
    )
    parser.add_argument("--acknowledge-locked-holdout", action="store_true")
    parser.add_argument(
        "--review-evidence",
        required=True,
        type=Path,
        help="Immutable independently signed review packet to hash and bind.",
    )
    parser.add_argument(
        "--decision",
        required=True,
        choices=("approve", "reject"),
    )
    parser.add_argument(
        "--confirm-manifest-sha256",
        required=True,
        help="Exact manifest SHA-256 independently confirmed by the reviewer.",
    )
    parser.add_argument(
        "--database-url-env",
        default=DEFAULT_DATABASE_ENV,
    )
    parser.add_argument(
        "--tenant-id-env",
        default=DEFAULT_TENANT_ENV,
    )
    parser.add_argument(
        "--reviewer-principal-id-env",
        default=DEFAULT_PRINCIPAL_ENV,
    )
    args = parser.parse_args(list(argv) if argv is not None else None)
    env = os.environ if environment is None else environment

    try:
        manifest = load_evaluation_manifest(
            args.manifest,
            expected_split=args.split,
            acknowledge_locked_holdout=args.acknowledge_locked_holdout,
        )
        confirmed_hash = args.confirm_manifest_sha256.strip().lower()
        if confirmed_hash != manifest.manifest_sha256:
            raise EvaluationError(
                "confirmed manifest SHA-256 does not match the loaded manifest"
            )
        if manifest.gold_status != "independent_bauer_verified":
            raise EvaluationError(
                "independent attestation requires an "
                "independent_bauer_verified manifest"
            )
        evidence_hash = _hash_review_packet(args.review_evidence)
        reviewer = PostgresIndependentGoldReviewer.from_dsn(
            _required_environment(env, args.database_url_env),
            tenant_id=_required_environment(env, args.tenant_id_env),
            reviewer_principal_id=_required_environment(
                env,
                args.reviewer_principal_id_env,
            ),
        )
        attestation_id = reviewer.attest(
            manifest=manifest,
            review_evidence_sha256=evidence_hash,
            decision=args.decision,
        )
    except (
        DatabaseRoleSecurityError,
        EvaluationError,
        OSError,
        PostgresReviewError,
        ValueError,
    ) as error:
        parser.error(str(error))

    print(
        json.dumps(
            {
                "attestation_id": attestation_id,
                "decision": args.decision,
                "eval_suite_id": manifest.eval_suite_id,
                "manifest_sha256": manifest.manifest_sha256,
                "review_evidence_sha256": evidence_hash,
            },
            sort_keys=True,
            separators=(",", ":"),
        )
    )
    return 0


def _required_environment(
    environment: Mapping[str, str],
    name: str,
) -> str:
    value = environment.get(name)
    if value is None or not value.strip():
        raise EvaluationError(f"required environment variable {name} is missing")
    return value.strip()


def _hash_review_packet(path: Path) -> str:
    if not path.is_file():
        raise EvaluationError(f"review evidence file does not exist: {path}")
    digest = hashlib.sha256()
    byte_count = 0
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            byte_count += len(chunk)
            digest.update(chunk)
    if byte_count < 1:
        raise EvaluationError("review evidence file must not be empty")
    return digest.hexdigest()


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
