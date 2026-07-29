from __future__ import annotations

import json
import tempfile
import unittest
from datetime import UTC, datetime, timedelta
from pathlib import Path

from pydantic import ValidationError

from bauer_evidence_v4.contracts.artifacts import (
    render_json_schemas,
    render_openapi,
    write_contract_artifacts,
)
from bauer_evidence_v4.contracts.models import (
    AuthorizationClaims,
    AuthorizationEnvelope,
    CitationContract,
    ClientContext,
    CoverageItem,
    DocumentMetadataContract,
    ReleaseContract,
    SourceCoordinate,
    V4AnswerRequest,
    V4AnswerResponse,
)


DIGEST = "a" * 64


def release() -> ReleaseContract:
    return ReleaseContract(
        public_id="candidate-v4-local",
        source_contract_sha256=DIGEST,
        canonical_schema_version="4.0.0",
        compiler_identity=DIGEST,
        projection_identity=DIGEST,
        embedding_identity=DIGEST,
        reranker_identity=DIGEST,
        gate_manifest_sha256=DIGEST,
    )


def citation() -> CitationContract:
    coordinate = SourceCoordinate(
        source_id="source-1",
        source_version_id="version-1",
        page=4,
        section_path=["Technical data"],
    )
    return CitationContract(
        citation_id="C-1",
        evidence_id="E-1",
        source_title="Technical brochure",
        original_filename="brochure.pdf",
        coordinate=coordinate,
        excerpt="Maximum operating pressure: 420 bar.",
    )


class ContractTests(unittest.TestCase):
    def test_original_question_is_not_replaced_by_search_hint(self) -> None:
        request = V4AnswerRequest(
            request_id="request-123",
            question="Give title, language, subject, and filename for N47183.",
            search_hint="N47183",
            client=ClientContext(type="librechat", instance="testing"),
            authorization=AuthorizationEnvelope(signed_scope="x" * 32),
        )
        self.assertEqual(
            request.immutable_original_question,
            "Give title, language, subject, and filename for N47183.",
        )
        self.assertNotEqual(request.immutable_original_question, request.search_hint)

    def test_signed_scope_accepts_the_sealed_373_source_envelope_size(self) -> None:
        request = V4AnswerRequest(
            request_id="request-373",
            question="Find the exact Bauer evidence.",
            client=ClientContext(type="librechat", instance="testing"),
            authorization=AuthorizationEnvelope(signed_scope="x" * 16422),
        )
        self.assertEqual(len(request.authorization.signed_scope), 16422)
        with self.assertRaises(ValidationError):
            AuthorizationEnvelope(signed_scope="x" * 65537)

    def test_contracts_reject_unknown_fields(self) -> None:
        with self.assertRaises(ValidationError):
            V4AnswerRequest.model_validate(
                {
                    "request_id": "request-123",
                    "question": "Question",
                    "client": {"type": "reference", "instance": "test"},
                    "authorization": {"signed_scope": "x" * 32},
                    "librechat_agent_id": "must-not-be-core-data",
                }
            )

    def test_complete_response_cannot_hide_missing_fields(self) -> None:
        with self.assertRaises(ValidationError):
            V4AnswerResponse(
                request_id="request-123",
                status="complete",
                answer="Only one field was found.",
                coverage=[
                    CoverageItem(field="title", state="supported", citation_ids=["C-1"]),
                    CoverageItem(field="language", state="absent"),
                ],
                not_found=["language"],
                citations=[citation()],
                release=release(),
                trace_id="trace-1234",
            )

    def test_partial_response_has_explicit_absence(self) -> None:
        response = V4AnswerResponse(
            request_id="request-123",
            status="partial",
            answer="The title is supported; language was not found.",
            coverage=[
                CoverageItem(field="title", state="supported", citation_ids=["C-1"]),
                CoverageItem(field="language", state="absent"),
            ],
            not_found=["language"],
            citations=[citation()],
            release=release(),
            trace_id="trace-1234",
        )
        self.assertEqual(response.status, "partial")

    def test_authorization_claims_require_unique_scope_and_bounded_expiry(self) -> None:
        now = datetime.now(UTC)
        claims = AuthorizationClaims(
            issuer="librechat-adapter",
            audience="bauer-evidence-v4",
            client=ClientContext(type="librechat", instance="testing"),
            principal_id="principal-1",
            tenant_id="tenant-1",
            knowledge_base_id="kb-1",
            authorized_external_source_ids=["file-1", "file-2"],
            issued_at=now,
            expires_at=now + timedelta(minutes=5),
            nonce="n" * 16,
        )
        self.assertEqual(len(claims.authorized_external_source_ids), 2)
        with self.assertRaises(ValidationError):
            AuthorizationClaims.model_validate(
                {
                    **claims.model_dump(),
                    "authorized_external_source_ids": ["file-1", "file-1"],
                }
            )

    def test_checked_in_artifacts_match_deterministic_renderer(self) -> None:
        checked_in = Path(__file__).resolve().parents[1] / "contracts" / "schemas"
        expected = {**render_json_schemas(), "openapi.json": render_openapi()}
        for filename, payload in expected.items():
            self.assertEqual(
                json.loads((checked_in / filename).read_text(encoding="utf-8")),
                payload,
            )
        with tempfile.TemporaryDirectory() as directory:
            paths = write_contract_artifacts(Path(directory))
            self.assertEqual(len(paths), len(expected))


if __name__ == "__main__":
    unittest.main()
