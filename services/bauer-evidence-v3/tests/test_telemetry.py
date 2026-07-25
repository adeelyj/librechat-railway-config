from __future__ import annotations

import json
import logging
import os
import sys
import unittest
from io import StringIO
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
from unittest.mock import Mock


SERVICE_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SERVICE_ROOT))

from bauer_evidence_v3.telemetry import (  # noqa: E402
    AuthorizationAuditEvent,
    JsonFormatter,
    configure_telemetry,
    record_answer_decision,
    record_authorization_audit,
    record_job_outcome,
    record_release_transition,
    redact,
)


class TelemetryTests(unittest.TestCase):
    def test_nested_secrets_and_document_content_are_redacted(self) -> None:
        value = redact(
            {
                "release_id": "release",
                "authorization": "Bearer secret",
                "nested": {"api_key": "key", "content": "document body"},
            }
        )
        self.assertEqual(value["release_id"], "release")
        self.assertEqual(value["authorization"], "[REDACTED]")
        self.assertEqual(value["nested"]["api_key"], "[REDACTED]")
        self.assertEqual(value["nested"]["content"], "[REDACTED]")

    def test_json_formatter_emits_allowlisted_context_only(self) -> None:
        stream = StringIO()
        handler = logging.StreamHandler(stream)
        handler.setFormatter(JsonFormatter())
        logger = logging.getLogger("v3-test-json")
        logger.handlers = [handler]
        logger.propagate = False
        logger.setLevel(logging.INFO)
        logger.info(
            "compiled",
            extra={"release_id": "release", "password": "must-not-appear"},
        )
        body = json.loads(stream.getvalue())
        self.assertEqual(body["release_id"], "release")
        self.assertNotIn("password", body)

    def test_authorization_and_answer_audit_never_emit_bodies_or_tokens(self) -> None:
        stream = StringIO()
        handler = logging.StreamHandler(stream)
        handler.setFormatter(JsonFormatter())
        authorization_logger = logging.getLogger(
            "bauer_evidence_v3.authorization"
        )
        answer_logger = logging.getLogger("bauer_evidence_v3.answer")
        for logger in (authorization_logger, answer_logger):
            logger.handlers = [handler]
            logger.propagate = False
            logger.setLevel(logging.INFO)

        captured: list[AuthorizationAuditEvent] = []
        record_authorization_audit(
            AuthorizationAuditEvent(
                request_id="request",
                route="/v3/answer",
                outcome="allowed",
                reason_code="signed_scope_verified",
                tenant_id="tenant",
                knowledge_base_id="kb",
                user_id="user",
                agent_id="agent",
                authorized_source_count=3,
            ),
            sink=captured.append,
        )
        record_answer_decision(
            request_id="request",
            route="/v3/answer",
            release_id="release",
            outcome="refused_after_validation",
            evidence_count=2,
            repair_attempted=True,
            validation_disposition="refuse",
        )

        records = [json.loads(line) for line in stream.getvalue().splitlines()]
        self.assertEqual(captured[0].authorized_source_count, 3)
        self.assertEqual(records[0]["event_type"], "authorization_decision")
        self.assertEqual(records[1]["event_type"], "answer_boundary_decision")
        serialized = json.dumps(records)
        self.assertNotIn("authorization", records[0])
        self.assertNotIn("Bearer", serialized)
        self.assertNotIn("query", serialized)
        self.assertNotIn("document_body", serialized)

    def test_no_endpoint_is_an_explicit_noop_without_optional_dependencies(self) -> None:
        with patch.dict(os.environ, {}, clear=True):
            state = configure_telemetry(
                service_name="bauer-v3",
                service_version="test",
                environment="development",
            )
        self.assertFalse(state.enabled)

    def test_queue_and_release_domain_metrics_use_bounded_attributes(self) -> None:
        instruments = SimpleNamespace(
            authorization_decisions=Mock(),
            answer_decisions=Mock(),
            jobs_completed=Mock(),
            job_duration_ms=Mock(),
            release_transitions=Mock(),
        )
        with patch(
            "bauer_evidence_v3.telemetry._DOMAIN_METRICS",
            instruments,
        ):
            record_job_outcome(
                job_id="high-cardinality-job-id",
                job_type="compile",
                queue_name="compiler",
                outcome="succeeded",
                duration_ms=42,
            )
            record_release_transition(
                release_id="high-cardinality-release-id",
                previous_status="validating",
                target_status="ready",
            )

        job_attributes = instruments.jobs_completed.add.call_args.args[1]
        release_attributes = (
            instruments.release_transitions.add.call_args.args[1]
        )
        self.assertEqual(
            job_attributes,
            {
                "job_type": "compile",
                "queue_name": "compiler",
                "outcome": "succeeded",
            },
        )
        self.assertNotIn("job_id", job_attributes)
        self.assertNotIn("release_id", release_attributes)
        instruments.job_duration_ms.record.assert_called_once_with(
            42,
            job_attributes,
        )


if __name__ == "__main__":
    unittest.main()
