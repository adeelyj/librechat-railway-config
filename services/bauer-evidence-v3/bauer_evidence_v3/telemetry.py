from __future__ import annotations

import json
import logging
import os
import re
import sys
from dataclasses import dataclass, field
from typing import Any, Callable


_SENSITIVE_KEY_RE = re.compile(
    r"(?:authorization|api[_-]?key|password|secret|token|document[_-]?body|content)",
    re.IGNORECASE,
)


def redact(value: Any, *, key: str = "") -> Any:
    if key and _SENSITIVE_KEY_RE.search(key):
        return "[REDACTED]"
    if isinstance(value, dict):
        return {str(name): redact(item, key=str(name)) for name, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [redact(item) for item in value]
    return value


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        body = {
            "timestamp": self.formatTime(record, "%Y-%m-%dT%H:%M:%S%z"),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        for name in (
            "event_type",
            "request_id",
            "trace_id",
            "route",
            "tenant_id",
            "knowledge_base_id",
            "user_id",
            "agent_id",
            "authorized_source_count",
            "job_id",
            "job_type",
            "queue_name",
            "release_id",
            "previous_release_id",
            "target_status",
            "source_version_id",
            "stage",
            "duration_ms",
            "outcome",
            "reason_code",
            "evidence_count",
            "repair_attempted",
            "validation_disposition",
        ):
            if hasattr(record, name):
                body[name] = getattr(record, name)
        if record.exc_info:
            body["exception_type"] = record.exc_info[0].__name__
        return json.dumps(redact(body), ensure_ascii=False, separators=(",", ":"))


def configure_logging(level: str | None = None) -> None:
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(JsonFormatter())
    root = logging.getLogger()
    root.handlers.clear()
    root.addHandler(handler)
    root.setLevel((level or os.getenv("LOG_LEVEL", "INFO")).upper())


@dataclass(frozen=True, slots=True)
class TelemetryState:
    enabled: bool
    service_name: str
    exporter_endpoint: str | None


@dataclass(frozen=True, slots=True)
class _DomainMetrics:
    authorization_decisions: Any
    answer_decisions: Any
    jobs_completed: Any
    job_duration_ms: Any
    release_transitions: Any


_DOMAIN_METRICS: _DomainMetrics | None = None


@dataclass(frozen=True, slots=True)
class AuthorizationAuditEvent:
    """Body-free authorization decision suitable for logs or a durable sink."""

    request_id: str
    route: str
    outcome: str
    reason_code: str
    tenant_id: str | None = None
    knowledge_base_id: str | None = None
    user_id: str | None = None
    agent_id: str | None = None
    authorized_source_count: int | None = None
    authorized_source_ids: tuple[str, ...] = field(
        default=(),
        repr=False,
    )
    release_id: str | None = None


AuthorizationAuditSink = Callable[[AuthorizationAuditEvent], None]


def record_authorization_audit(
    event: AuthorizationAuditEvent,
    *,
    sink: AuthorizationAuditSink | None = None,
) -> None:
    """Record an authorization decision without a token, query, or source body."""

    if sink is not None:
        sink(event)
    logging.getLogger("bauer_evidence_v3.authorization").info(
        "authorization decision",
        extra={
            "event_type": "authorization_decision",
            "request_id": event.request_id,
            "route": event.route,
            "tenant_id": event.tenant_id,
            "knowledge_base_id": event.knowledge_base_id,
            "user_id": event.user_id,
            "agent_id": event.agent_id,
            "authorized_source_count": event.authorized_source_count,
            "outcome": event.outcome,
            "reason_code": event.reason_code,
        },
    )
    instruments = _DOMAIN_METRICS
    if instruments is not None:
        instruments.authorization_decisions.add(
            1,
            {
                "route": event.route,
                "outcome": event.outcome,
                "reason_code": event.reason_code,
            },
        )


def record_answer_decision(
    *,
    request_id: str,
    route: str,
    release_id: str,
    outcome: str,
    evidence_count: int,
    repair_attempted: bool,
    validation_disposition: str | None,
) -> None:
    """Record the final validation disposition without answer or evidence text."""

    logging.getLogger("bauer_evidence_v3.answer").info(
        "answer boundary decision",
        extra={
            "event_type": "answer_boundary_decision",
            "request_id": request_id,
            "route": route,
            "release_id": release_id,
            "outcome": outcome,
            "evidence_count": evidence_count,
            "repair_attempted": repair_attempted,
            "validation_disposition": validation_disposition,
        },
    )
    instruments = _DOMAIN_METRICS
    if instruments is not None:
        instruments.answer_decisions.add(
            1,
            {
                "route": route,
                "outcome": outcome,
                "validation_disposition": validation_disposition or "none",
            },
        )


def record_job_outcome(
    *,
    job_id: str,
    job_type: str,
    queue_name: str,
    outcome: str,
    duration_ms: int | None = None,
) -> None:
    logging.getLogger("bauer_evidence_v3.queue").info(
        "compiler job outcome",
        extra={
            "event_type": "compiler_job_outcome",
            "job_id": job_id,
            "job_type": job_type,
            "queue_name": queue_name,
            "outcome": outcome,
            "duration_ms": duration_ms,
        },
    )
    instruments = _DOMAIN_METRICS
    if instruments is not None:
        attributes = {
            "job_type": job_type,
            "queue_name": queue_name,
            "outcome": outcome,
        }
        instruments.jobs_completed.add(1, attributes)
        if duration_ms is not None:
            instruments.job_duration_ms.record(duration_ms, attributes)


def record_release_transition(
    *,
    release_id: str,
    previous_status: str,
    target_status: str,
    operation: str = "status_transition",
    previous_release_id: str | None = None,
) -> None:
    logging.getLogger("bauer_evidence_v3.release").info(
        "knowledge release transition",
        extra={
            "event_type": "knowledge_release_transition",
            "release_id": release_id,
            "previous_release_id": previous_release_id,
            "stage": operation,
            "outcome": previous_status,
            "target_status": target_status,
        },
    )
    instruments = _DOMAIN_METRICS
    if instruments is not None:
        instruments.release_transitions.add(
            1,
            {
                "operation": operation,
                "previous_status": previous_status,
                "target_status": target_status,
            },
        )


def configure_telemetry(
    *,
    service_name: str,
    service_version: str,
    environment: str,
    fastapi_app=None,
) -> TelemetryState:
    """Configure OTLP when installed and requested; otherwise remain an explicit no-op."""

    endpoint = os.getenv("OTEL_EXPORTER_OTLP_ENDPOINT", "").strip()
    if not endpoint:
        return TelemetryState(False, service_name, None)
    try:
        from opentelemetry import metrics, trace
        from opentelemetry.exporter.otlp.proto.http.metric_exporter import (
            OTLPMetricExporter,
        )
        from opentelemetry.exporter.otlp.proto.http.trace_exporter import (
            OTLPSpanExporter,
        )
        from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor
        from opentelemetry.instrumentation.httpx import HTTPXClientInstrumentor
        from opentelemetry.sdk.metrics import MeterProvider
        from opentelemetry.sdk.metrics.export import PeriodicExportingMetricReader
        from opentelemetry.sdk.resources import Resource
        from opentelemetry.sdk.trace import TracerProvider
        from opentelemetry.sdk.trace.export import BatchSpanProcessor
    except ModuleNotFoundError as exc:
        raise RuntimeError(
            "OTLP endpoint is configured but observability dependencies are missing"
        ) from exc

    resource = Resource.create(
        {
            "service.name": service_name,
            "service.version": service_version,
            "deployment.environment.name": environment,
        }
    )
    tracer_provider = TracerProvider(resource=resource)
    tracer_provider.add_span_processor(
        BatchSpanProcessor(OTLPSpanExporter(endpoint=f"{endpoint.rstrip('/')}/v1/traces"))
    )
    trace.set_tracer_provider(tracer_provider)
    metric_reader = PeriodicExportingMetricReader(
        OTLPMetricExporter(endpoint=f"{endpoint.rstrip('/')}/v1/metrics")
    )
    metrics.set_meter_provider(
        MeterProvider(resource=resource, metric_readers=[metric_reader])
    )
    meter = metrics.get_meter("bauer_evidence_v3.domain", service_version)
    global _DOMAIN_METRICS
    _DOMAIN_METRICS = _DomainMetrics(
        authorization_decisions=meter.create_counter(
            "bauer.v3.authorization.decisions",
            unit="{decision}",
            description="V3 authorization decisions by bounded reason code",
        ),
        answer_decisions=meter.create_counter(
            "bauer.v3.answer.decisions",
            unit="{decision}",
            description="V3 final answer validation outcomes",
        ),
        jobs_completed=meter.create_counter(
            "bauer.v3.queue.job.outcomes",
            unit="{job}",
            description="Durable compiler job outcomes",
        ),
        job_duration_ms=meter.create_histogram(
            "bauer.v3.queue.job.duration",
            unit="ms",
            description="Durable compiler job handler duration",
        ),
        release_transitions=meter.create_counter(
            "bauer.v3.release.transitions",
            unit="{transition}",
            description="Knowledge-release lifecycle and pointer transitions",
        ),
    )
    HTTPXClientInstrumentor().instrument()
    if fastapi_app is not None:
        FastAPIInstrumentor.instrument_app(
            fastapi_app,
            excluded_urls="health,ready",
        )
    return TelemetryState(True, service_name, endpoint)
