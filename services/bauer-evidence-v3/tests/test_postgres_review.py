from __future__ import annotations

import sys
import unittest
from contextlib import nullcontext
from pathlib import Path
from unittest.mock import patch


SERVICE_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SERVICE_ROOT))

from bauer_evidence_v3.evaluation import parse_evaluation_manifest  # noqa: E402
from bauer_evidence_v3.postgres_review import (  # noqa: E402
    PostgresIndependentGoldReviewer,
)


TENANT_ID = "10000000-0000-4000-8000-000000000001"
KB_ID = "20000000-0000-4000-8000-000000000001"
RELEASE_ID = "30000000-0000-4000-8000-000000000001"
SOURCE_ID = "40000000-0000-4000-8000-000000000001"
PRINCIPAL_ID = "50000000-0000-4000-8000-000000000001"
ATTESTATION_ID = "60000000-0000-4000-8000-000000000001"


def _manifest(*, status: str = "independent_bauer_verified"):
    return parse_evaluation_manifest(
        {
            "schema_version": 1,
            "suite_key": "bauer-v3-reviewed",
            "suite_version": "2026-07-25.1",
            "split": "development",
            "gold_status": status,
            "verified_at": "2026-07-25T10:00:00Z",
            "verified_by": "independent-bauer-reviewer",
            "authorization_scope": {
                "tenant_id": TENANT_ID,
                "knowledge_base_id": KB_ID,
                "allowed_release_ids": [RELEASE_ID],
                "allowed_source_ids": [SOURCE_ID],
            },
            "queries": [
                {
                    "case_id": "reviewed-query",
                    "category": "retrieval",
                    "prompt": {
                        "mode": "query",
                        "query": "reviewed query",
                        "top_k": 5,
                    },
                    "retrieval": {
                        "required_evidence_ids": [
                            "70000000-0000-4000-8000-000000000001"
                        ]
                    },
                }
            ],
        },
        expected_split="development",
    )


class _Cursor:
    def fetchone(self):
        return {"attestation_id": ATTESTATION_ID}


class _Connection:
    def __init__(self):
        self.calls = []

    def transaction(self):
        return nullcontext()

    def execute(self, sql, params=()):
        self.calls.append((" ".join(str(sql).split()), params))
        return _Cursor()


class PostgresIndependentGoldReviewerTests(unittest.TestCase):
    def setUp(self):
        self.connection = _Connection()
        self.reviewer = PostgresIndependentGoldReviewer(
            lambda: nullcontext(self.connection),
            tenant_id=TENANT_ID,
            reviewer_principal_id=PRINCIPAL_ID,
        )

    @patch(
        "bauer_evidence_v3.postgres_review.verify_runtime_database_role",
        return_value="reviewer_login",
    )
    def test_attestation_requires_exact_reviewer_and_binds_both_hashes(
        self,
        verify_role,
    ):
        manifest = _manifest()
        result = self.reviewer.attest(
            manifest=manifest,
            review_evidence_sha256="b" * 64,
            decision="approve",
        )

        self.assertEqual(result, ATTESTATION_ID)
        verify_role.assert_called_once_with(
            self.connection,
            required_group_role="bauer_rag_v3_reviewer",
        )
        attestation_calls = [
            call
            for call in self.connection.calls
            if "attest_independent_gold" in call[0]
        ]
        self.assertEqual(len(attestation_calls), 1)
        self.assertEqual(
            attestation_calls[0][1],
            (
                manifest.eval_suite_id,
                manifest.manifest_sha256,
                "b" * 64,
                "approve",
            ),
        )

    def test_rejects_interim_manifest_before_opening_database(self):
        with self.assertRaisesRegex(
            ValueError,
            "independent_bauer_verified",
        ):
            self.reviewer.attest(
                manifest=_manifest(status="interim_reviewed"),
                review_evidence_sha256="b" * 64,
                decision="approve",
            )
        self.assertEqual(self.connection.calls, [])

    def test_rejects_invalid_evidence_hash_and_decision(self):
        with self.assertRaisesRegex(ValueError, "lowercase SHA-256"):
            self.reviewer.attest(
                manifest=_manifest(),
                review_evidence_sha256="not-a-hash",
                decision="approve",
            )
        with self.assertRaisesRegex(ValueError, "approve or reject"):
            self.reviewer.attest(
                manifest=_manifest(),
                review_evidence_sha256="b" * 64,
                decision="waive",
            )


if __name__ == "__main__":
    unittest.main()
