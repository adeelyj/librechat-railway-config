from __future__ import annotations

import contextlib
import io
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch


SERVICE_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SERVICE_ROOT))

from bauer_evidence_v3.evaluation import load_evaluation_manifest  # noqa: E402
from bauer_evidence_v3.gold_review_cli import main  # noqa: E402


TENANT_ID = "10000000-0000-4000-8000-000000000001"
KB_ID = "20000000-0000-4000-8000-000000000001"
RELEASE_ID = "30000000-0000-4000-8000-000000000001"
SOURCE_ID = "40000000-0000-4000-8000-000000000001"
PRINCIPAL_ID = "50000000-0000-4000-8000-000000000001"
ATTESTATION_ID = "60000000-0000-4000-8000-000000000001"


class GoldReviewCliTests(unittest.TestCase):
    def test_hashes_packet_and_records_confirmed_manifest(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            manifest_path = root / "reviewed.json"
            evidence_path = root / "signed-review.packet"
            manifest_path.write_text(
                json.dumps(
                    {
                        "schema_version": 1,
                        "suite_key": "bauer-v3-reviewed",
                        "suite_version": "2026-07-25.1",
                        "split": "development",
                        "gold_status": "independent_bauer_verified",
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
                    }
                ),
                encoding="utf-8",
            )
            evidence_path.write_bytes(b"signed review packet")
            manifest = load_evaluation_manifest(
                manifest_path,
                expected_split="development",
            )
            reviewer = Mock()
            reviewer.attest.return_value = ATTESTATION_ID
            output = io.StringIO()
            with (
                patch(
                    "bauer_evidence_v3.gold_review_cli."
                    "PostgresIndependentGoldReviewer.from_dsn",
                    return_value=reviewer,
                ),
                contextlib.redirect_stdout(output),
            ):
                result = main(
                    [
                        "--manifest",
                        str(manifest_path),
                        "--split",
                        "development",
                        "--review-evidence",
                        str(evidence_path),
                        "--decision",
                        "approve",
                        "--confirm-manifest-sha256",
                        manifest.manifest_sha256,
                    ],
                    environment={
                        "BAUER_V3_REVIEW_DATABASE_URL": "postgresql://review",
                        "BAUER_V3_REVIEW_TENANT_ID": TENANT_ID,
                        "BAUER_V3_REVIEW_PRINCIPAL_ID": PRINCIPAL_ID,
                    },
                )

        self.assertEqual(result, 0)
        payload = json.loads(output.getvalue())
        self.assertEqual(payload["attestation_id"], ATTESTATION_ID)
        self.assertEqual(payload["manifest_sha256"], manifest.manifest_sha256)
        reviewer.attest.assert_called_once()


if __name__ == "__main__":
    unittest.main()
