from __future__ import annotations

import copy
import io
import json
import sys
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path


EVAL_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(EVAL_ROOT))

import verify_difficult_sources as verifier  # noqa: E402


class DifficultSourceFixtureTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.fixture = verifier.load_json(verifier.DEFAULT_FIXTURE)
        cls.contract = verifier.load_json(
            verifier.DEFAULT_SOURCE_CONTRACT
        )

    def test_checked_in_bundle_is_valid_and_source_contract_bound(self):
        result = verifier.verify_bundle(
            self.fixture,
            source_contract=self.contract,
        )
        self.assertTrue(result["valid"])
        self.assertFalse(result["benchmark_eligible"])
        self.assertEqual(
            result["case_ids"],
            ["B08", "B11", "B20", "B21"],
        )
        self.assertEqual(result["source_contract_identity_count"], 3)
        self.assertEqual(
            result["review_status"],
            "interim_codex_reviewed_requires_bauer_signoff",
        )
        self.assertEqual(
            result["fixture_sha256"],
            "c9d958cd007ba694b3cb549cf99c87d2c0b9b0bccffb87c5d7e858ee5e2583d7",
        )

    def test_canonical_digest_is_stable_across_mapping_order(self):
        reversed_fixture = {
            key: self.fixture[key]
            for key in reversed(tuple(self.fixture))
        }
        self.assertEqual(
            verifier.canonical_sha256(self.fixture),
            verifier.canonical_sha256(reversed_fixture),
        )

    def test_case_semantics_keep_cross_source_and_contradiction_boundaries(self):
        cases = {
            case["case_id"]: case for case in self.fixture["cases"]
        }
        self.assertEqual(
            len(cases["B08"]["additional_source_expectations"]),
            1,
        )
        self.assertEqual(
            cases["B08"]["additional_source_expectations"][0][
                "provenance"
            ]["physical_page"],
            41,
        )
        self.assertEqual(
            cases["B20"]["expectations"]["structural_checks"][0][
                "expected_count"
            ],
            3,
        )
        self.assertEqual(
            cases["B11"]["source_identity"]["external_file_id"],
            "8f7af4a4-94b1-42a8-89ce-ba54960eb2b0",
        )
        self.assertEqual(
            cases["B11"]["expectations"]["decision_checks"][0][
                "required_outcome"
            ],
            "not_supported_by_documented_limit",
        )
        self.assertEqual(
            cases["B21"]["expectations"]["contradiction_checks"][0][
                "kind"
            ],
            "headline_vs_later_technical_data",
        )
        self.assertEqual(
            cases["B21"]["source_identity"]["external_file_id"],
            "8f7af4a4-94b1-42a8-89ce-ba54960eb2b0",
        )

    def test_fixture_rejects_benchmark_claims_and_runtime_ids(self):
        benchmark = copy.deepcopy(self.fixture)
        benchmark["benchmark_eligible"] = True
        with self.assertRaisesRegex(
            verifier.DifficultSourceFixtureError,
            "ineligible as a benchmark",
        ):
            verifier.verify_bundle(
                benchmark,
                source_contract=self.contract,
            )

        runtime_bound = copy.deepcopy(self.fixture)
        runtime_bound["cases"][0]["release_id"] = (
            "00000000-0000-4000-8000-000000000001"
        )
        with self.assertRaisesRegex(
            verifier.DifficultSourceFixtureError,
            "forbidden production/runtime binding",
        ):
            verifier.verify_bundle(
                runtime_bound,
                source_contract=self.contract,
            )

    def test_fixture_rejects_source_contract_hash_drift(self):
        changed_contract = copy.deepcopy(self.contract)
        external_id = self.fixture["cases"][0]["source_identity"][
            "external_file_id"
        ]
        source = next(
            item
            for item in changed_contract["sources"]
            if item["external_file_id"] == external_id
        )
        source["expected_sha256"] = "0" * 64
        with self.assertRaisesRegex(
            verifier.DifficultSourceFixtureError,
            "expected_sha256 disagrees",
        ):
            verifier.verify_bundle(
                self.fixture,
                source_contract=changed_contract,
            )

    def test_cli_emits_machine_readable_verification(self):
        stdout = io.StringIO()
        stderr = io.StringIO()
        with redirect_stdout(stdout), redirect_stderr(stderr):
            result = verifier.main([])
        self.assertEqual(result, 0, stderr.getvalue())
        payload = json.loads(stdout.getvalue())
        self.assertTrue(payload["valid"])
        self.assertEqual(payload["source_contract_identity_count"], 3)


if __name__ == "__main__":
    unittest.main()
