"""Executable, release-pinned evaluation contracts for Bauer Evidence V3.

The checked-in evaluation harness can score an already captured run.  This
module owns the preceding production operation: execute reviewed cases against
either the answer service or its HTTP API, enforce the declared authorization
scope, and produce normalized per-case records that can be persisted without
storing retrieved document content.

The target and persistence boundaries are protocols so tests and offline
validation can inject deterministic implementations.  Production callers use
``HttpEvaluationTarget`` together with ``PostgresEvaluationStore`` from
``postgres_eval``.  Reviewed extraction/table targets additionally use the
reader-only ``PostgresEvaluationObservationSource`` from that module.
"""

from __future__ import annotations

import asyncio
import json
import time
import uuid
from collections import Counter
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Protocol

from .answering import AnswerService
from .auth import AuthorizationContext
from .ids import canonicalize_mapping, sha256_json
from .observation_metrics import score_canonical_observations


EVALUATION_SCHEMA_VERSION = 1
ALLOWED_SPLITS = frozenset({"development", "holdout"})
ALLOWED_GOLD_STATUSES = frozenset(
    {
        "draft",
        "interim_reviewed",
        "verified",
        "independent_bauer_verified",
        "retired",
    }
)
ALLOWED_MODES = frozenset({"answer", "query"})
_VERIFIED_GOLD_STATUSES = frozenset(
    {"verified", "independent_bauer_verified"}
)
_EVALUATION_NAMESPACE = uuid.UUID("5b937f52-fbf7-5db2-b79d-e43d46f292b7")
_ALWAYS_HARD_FAILURES = frozenset(
    {
        "authorization_leakage",
        "invalid_answer_accepted",
        "malformed_evidence_id",
        "release_mismatch",
        "target_error",
    }
)
_ANSWERED_STATUSES = frozenset({"answered", "answered_after_repair"})


class EvaluationError(RuntimeError):
    """Base class for executable evaluation failures."""


class EvaluationManifestError(EvaluationError, ValueError):
    """The evaluation manifest is unsafe or malformed."""


class LockedHoldoutError(EvaluationManifestError):
    """A holdout was requested without the explicit locked-set acknowledgement."""


class EvaluationTargetError(EvaluationError):
    """The answer/query target returned an invalid response."""


@dataclass(frozen=True, slots=True)
class EvaluationScope:
    tenant_id: str
    knowledge_base_id: str
    allowed_release_ids: tuple[str, ...]
    allowed_source_ids: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class EvaluationCase:
    eval_case_id: str
    case_key: str
    split: str
    category: str
    mode: str
    question: str
    mandatory_constraints: Mapping[str, str | tuple[str, ...]]
    forbidden_claim_values: tuple[str, ...]
    top_k: int
    required_evidence_ids: tuple[str, ...]
    forbidden_evidence_ids: tuple[str, ...]
    exact_lookup: bool
    refusal_expected: bool | None
    accepted_statuses: tuple[str, ...]
    required_answer_substrings: tuple[str, ...]
    forbidden_answer_substrings: tuple[str, ...]
    hard_failure_codes: tuple[str, ...]
    metadata: Mapping[str, Any] = field(default_factory=dict)

    @property
    def prompt_payload(self) -> dict[str, Any]:
        return {
            "mode": self.mode,
            "query": self.question,
            "mandatory_constraints": {
                key: list(value) if isinstance(value, tuple) else value
                for key, value in self.mandatory_constraints.items()
            },
            "forbidden_claim_values": list(self.forbidden_claim_values),
            "top_k": self.top_k,
        }

    @property
    def expected_payload(self) -> dict[str, Any]:
        return {
            "retrieval": {
                "required_evidence_ids": list(self.required_evidence_ids),
                "forbidden_evidence_ids": list(self.forbidden_evidence_ids),
                "exact_lookup": self.exact_lookup,
            },
            "answer": {
                "refusal_expected": self.refusal_expected,
                "accepted_statuses": list(self.accepted_statuses),
                "required_substrings": list(self.required_answer_substrings),
                "forbidden_substrings": list(self.forbidden_answer_substrings),
            },
        }


@dataclass(frozen=True, slots=True)
class EvaluationManifest:
    eval_suite_id: str
    suite_key: str
    suite_version: str
    split: str
    gold_status: str
    manifest_sha256: str
    scope: EvaluationScope
    extraction_targets: tuple[Mapping[str, Any], ...]
    table_targets: tuple[Mapping[str, Any], ...]
    cases: tuple[EvaluationCase, ...]
    metadata: Mapping[str, Any]
    verified_at: datetime | None
    verified_by: str | None


@dataclass(frozen=True, slots=True)
class EvaluationRequest:
    case_key: str
    mode: str
    question: str
    mandatory_constraints: Mapping[str, str | tuple[str, ...]]
    forbidden_claim_values: tuple[str, ...]
    top_k: int


@dataclass(frozen=True, slots=True)
class EvaluationTargetResult:
    status: str
    release_id: str
    evidence: tuple[Mapping[str, Any], ...]
    answer: str = ""
    validation: Mapping[str, Any] | None = None


class EvaluationTarget(Protocol):
    async def execute(
        self,
        request: EvaluationRequest,
    ) -> EvaluationTargetResult: ...


@dataclass(frozen=True, slots=True)
class EvaluationObservations:
    """Canonical compiler observations captured under one database snapshot."""

    extraction: tuple[Mapping[str, Any], ...] = ()
    tables: tuple[Mapping[str, Any], ...] = ()
    latency_ms: int | None = None


class EvaluationObservationSource(Protocol):
    def capture(
        self,
        *,
        manifest: EvaluationManifest,
        release_id: str,
    ) -> EvaluationObservations: ...


@dataclass(frozen=True, slots=True)
class EvaluationCaseResult:
    eval_result_id: str
    eval_case_id: str
    case_key: str
    repetition: int
    passed: bool
    scores: Mapping[str, Any]
    hard_failures: tuple[str, ...]
    evidence_search_unit_ids: tuple[str, ...]
    latency_ms: int
    details: Mapping[str, Any]


@dataclass(frozen=True, slots=True)
class EvaluationRunSpec:
    eval_run_id: str
    tenant_id: str
    knowledge_base_id: str
    release_id: str
    eval_suite_id: str
    code_version: str
    model_version: str | None
    prompt_version: str | None
    repetitions: int
    started_at: datetime
    split: str = "development"


@dataclass(frozen=True, slots=True)
class EvaluationRunReport:
    spec: EvaluationRunSpec
    status: str
    passed: bool
    aggregate_metrics: Mapping[str, Any]
    hard_failures: tuple[str, ...]
    results: tuple[EvaluationCaseResult, ...]
    completed_at: datetime
    observations: EvaluationObservations = field(
        default_factory=EvaluationObservations
    )
    observation_hard_failures: tuple[str, ...] = ()

    def to_json(self) -> dict[str, Any]:
        return {
            "schema_version": EVALUATION_SCHEMA_VERSION,
            "run_id": self.spec.eval_run_id,
            "eval_run_id": self.spec.eval_run_id,
            "eval_suite_id": self.spec.eval_suite_id,
            "tenant_id": self.spec.tenant_id,
            "knowledge_base_id": self.spec.knowledge_base_id,
            "release_id": self.spec.release_id,
            "split": self.spec.split,
            "status": self.status,
            "passed": self.passed,
            "code_version": self.spec.code_version,
            "model_version": self.spec.model_version,
            "prompt_version": self.spec.prompt_version,
            "repetitions": self.spec.repetitions,
            "started_at": self.spec.started_at.isoformat(),
            "completed_at": self.completed_at.isoformat(),
            "aggregate_metrics": dict(self.aggregate_metrics),
            "hard_failures": list(self.hard_failures),
            "results": [
                {
                    "eval_result_id": item.eval_result_id,
                    "eval_case_id": item.eval_case_id,
                    "case_key": item.case_key,
                    "repetition": item.repetition,
                    "passed": item.passed,
                    "scores": dict(item.scores),
                    "hard_failures": list(item.hard_failures),
                    "evidence_search_unit_ids": list(
                        item.evidence_search_unit_ids
                    ),
                    "latency_ms": item.latency_ms,
                    "details": dict(item.details),
                }
                for item in self.results
            ],
            "observations": {
                "extraction": [
                    dict(item) for item in self.observations.extraction
                ],
                "tables": [dict(item) for item in self.observations.tables],
                "retrieval": [],
                "answers": [],
                "latency": (
                    [
                        {
                            "stage": "evaluation",
                            "route": "canonical_postgres_capture",
                            "elapsed_ms": self.observations.latency_ms,
                            "success": True,
                        }
                    ]
                    if self.observations.latency_ms is not None
                    else []
                ),
                "authorization": [],
            },
            "observation_hard_failures": list(
                self.observation_hard_failures
            ),
        }


class EvaluationStore(Protocol):
    def prepare_suite(self, manifest: EvaluationManifest) -> None: ...

    def begin_run(self, spec: EvaluationRunSpec) -> None: ...

    def complete_run(self, report: EvaluationRunReport) -> None: ...

    def fail_run(
        self,
        spec: EvaluationRunSpec,
        *,
        completed_at: datetime,
        error: str,
    ) -> None: ...


def load_evaluation_manifest(
    path: Path,
    *,
    expected_split: str,
    acknowledge_locked_holdout: bool = False,
) -> EvaluationManifest:
    """Load one explicit split, refusing locked holdout before file access."""

    split = _validate_split(
        expected_split,
        acknowledge_locked_holdout=acknowledge_locked_holdout,
    )
    try:
        payload = json.loads(Path(path).read_text(encoding="utf-8-sig"))
    except FileNotFoundError as error:
        raise EvaluationManifestError(
            f"evaluation manifest does not exist: {path}"
        ) from error
    except json.JSONDecodeError as error:
        raise EvaluationManifestError(
            f"evaluation manifest must be JSON-compatible YAML: {error}"
        ) from error
    return parse_evaluation_manifest(payload, expected_split=split)


def parse_evaluation_manifest(
    payload: Any,
    *,
    expected_split: str,
) -> EvaluationManifest:
    split = _validate_split(
        expected_split,
        acknowledge_locked_holdout=expected_split == "holdout",
    )
    root = _mapping(payload, "manifest")
    if root.get("schema_version") != EVALUATION_SCHEMA_VERSION:
        raise EvaluationManifestError(
            f"manifest schema_version must be {EVALUATION_SCHEMA_VERSION}"
        )
    if root.get("split") != split:
        raise EvaluationManifestError(
            f"manifest split {root.get('split')!r} does not match {split!r}"
        )

    suite_key = _text(root.get("suite_key"), "suite_key")
    suite_version = _text(root.get("suite_version"), "suite_version")
    gold_status = _text(root.get("gold_status"), "gold_status")
    if gold_status not in ALLOWED_GOLD_STATUSES:
        raise EvaluationManifestError(
            "gold_status must be one of: "
            + ", ".join(sorted(ALLOWED_GOLD_STATUSES))
        )

    scope_payload = _mapping(
        root.get("authorization_scope"),
        "authorization_scope",
    )
    tenant_id = _uuid_text(scope_payload.get("tenant_id"), "tenant_id")
    knowledge_base_id = _uuid_text(
        scope_payload.get("knowledge_base_id"),
        "knowledge_base_id",
    )
    allowed_release_ids = tuple(
        _uuid_text(value, "allowed_release_ids item")
        for value in _text_list(
            scope_payload.get("allowed_release_ids"),
            "allowed_release_ids",
            require_nonempty=True,
        )
    )
    if len(set(allowed_release_ids)) != len(allowed_release_ids):
        raise EvaluationManifestError("allowed_release_ids contains duplicates")
    allowed_source_ids = tuple(
        sorted(
            set(
                _text_list(
                    scope_payload.get("allowed_source_ids"),
                    "allowed_source_ids",
                )
            )
        )
    )
    scope = EvaluationScope(
        tenant_id=tenant_id,
        knowledge_base_id=knowledge_base_id,
        allowed_release_ids=allowed_release_ids,
        allowed_source_ids=allowed_source_ids,
    )
    extraction_targets = _parse_extraction_targets(
        root.get("extraction_targets", []),
        allowed_source_ids=set(scope.allowed_source_ids),
    )
    table_targets = _parse_table_targets(
        root.get("table_targets", []),
        allowed_source_ids=set(scope.allowed_source_ids),
    )

    eval_suite_id = str(
        uuid.uuid5(
            _EVALUATION_NAMESPACE,
            f"{tenant_id}\x1f{suite_key}\x1f{suite_version}",
        )
    )
    raw_cases = root.get("queries")
    if not isinstance(raw_cases, list):
        raise EvaluationManifestError(
            "manifest queries must be an array"
        )
    if not raw_cases and not (extraction_targets or table_targets):
        raise EvaluationManifestError(
            "manifest must contain at least one query or structured target"
        )
    cases = tuple(
        _parse_case(
            raw_case,
            eval_suite_id=eval_suite_id,
            split=split,
            position=index,
        )
        for index, raw_case in enumerate(raw_cases)
    )
    case_keys = [case.case_key for case in cases]
    if len(case_keys) != len(set(case_keys)):
        raise EvaluationManifestError("manifest case keys must be unique")

    verified_at = _optional_datetime(root.get("verified_at"), "verified_at")
    verified_by = _optional_text(root.get("verified_by"), "verified_by")
    if gold_status in _VERIFIED_GOLD_STATUSES:
        if verified_at is None or verified_by is None:
            raise EvaluationManifestError(
                "verified gold requires verified_at and verified_by"
            )

    metadata = canonicalize_mapping(
        _mapping(root.get("metadata", {}), "metadata")
    )
    detached_payload = json.loads(
        json.dumps(root, ensure_ascii=False, allow_nan=False)
    )
    return EvaluationManifest(
        eval_suite_id=eval_suite_id,
        suite_key=suite_key,
        suite_version=suite_version,
        split=split,
        gold_status=gold_status,
        manifest_sha256=sha256_json(detached_payload),
        scope=scope,
        extraction_targets=extraction_targets,
        table_targets=table_targets,
        cases=cases,
        metadata=metadata,
        verified_at=verified_at,
        verified_by=verified_by,
    )


class AnswerServiceEvaluationTarget:
    """Direct adapter for deterministic/in-process release validation."""

    def __init__(
        self,
        *,
        answer_service: AnswerService,
        authorization: AuthorizationContext,
    ) -> None:
        self._answer_service = answer_service
        self._authorization = authorization

    async def execute(
        self,
        request: EvaluationRequest,
    ) -> EvaluationTargetResult:
        if request.mode == "query":
            run = await asyncio.to_thread(
                self._answer_service.retrieve_pinned,
                authorization=self._authorization,
                question=request.question,
                top_k=request.top_k,
            )
            return EvaluationTargetResult(
                status="retrieved",
                release_id=run.release_id,
                evidence=tuple(
                    {
                        "evidence_id": item.evidence.evidence_id,
                        "source_document_id": (
                            item.evidence.source_document_id
                        ),
                        "external_file_id": item.evidence.metadata.get(
                            "external_file_id"
                        ),
                        "tenant_id": item.evidence.tenant_id,
                        "knowledge_base_id": item.evidence.knowledge_base_id,
                        "release_id": item.evidence.release_id,
                    }
                    for item in run.results
                ),
            )

        result = await self._answer_service.answer(
            authorization=self._authorization,
            question=request.question,
            mandatory_constraints=dict(request.mandatory_constraints),
            forbidden_claim_values=request.forbidden_claim_values,
            top_k=request.top_k,
        )
        validation = (
            {
                "valid": result.validation.valid,
                "disposition": result.validation.disposition.value,
                "safe_refusal_detected": (
                    result.validation.safe_refusal_detected
                ),
                "violations": [
                    asdict(violation)
                    for violation in result.validation.violations
                ],
            }
            if result.validation is not None
            else None
        )
        return EvaluationTargetResult(
            status=result.status,
            answer=result.answer,
            release_id=result.release_id,
            evidence=tuple(
                {
                    "evidence_id": item.evidence_id,
                    "source_document_id": item.source_document_id,
                    "external_file_id": item.external_file_id,
                    "tenant_id": result.evidence.tenant_id,
                    "knowledge_base_id": result.evidence.knowledge_base_id,
                    "release_id": result.evidence.release_id,
                }
                for item in result.evidence.citations
            ),
            validation=validation,
        )


class HttpEvaluationTarget:
    """Reusable HTTP adapter for a deployed private V3 API."""

    def __init__(
        self,
        *,
        api_url: str,
        authorization_token: str | Callable[[], str | Awaitable[str]],
        timeout_seconds: float = 60.0,
        client: Any | None = None,
    ) -> None:
        normalized_url = api_url.strip().rstrip("/")
        if not normalized_url.startswith(("https://", "http://")):
            raise ValueError("api_url must be an absolute HTTP(S) URL")
        if timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be positive")
        self._api_url = normalized_url
        self._authorization_token = authorization_token
        self._timeout_seconds = timeout_seconds
        self._client: Any | None = client
        self._owns_client = client is None

    async def __aenter__(self) -> "HttpEvaluationTarget":
        await self._ensure_client()
        return self

    async def __aexit__(self, _exc_type, _exc, _traceback) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        if self._client is not None and self._owns_client:
            await self._client.aclose()
            self._client = None

    async def execute(
        self,
        request: EvaluationRequest,
    ) -> EvaluationTargetResult:
        client = await self._ensure_client()
        token_value = self._authorization_token
        token = token_value() if callable(token_value) else token_value
        if isinstance(token, Awaitable):
            token = await token
        if not isinstance(token, str) or not token.strip():
            raise EvaluationTargetError(
                "authorization token provider returned no token"
            )
        body: dict[str, Any] = {
            "query": request.question,
            "top_k": request.top_k,
        }
        if request.mode == "answer":
            body["mandatory_constraints"] = {
                key: list(value) if isinstance(value, tuple) else value
                for key, value in request.mandatory_constraints.items()
            }
            body["forbidden_claim_values"] = list(
                request.forbidden_claim_values
            )
        response = await client.post(
            f"{self._api_url}/v3/{request.mode}",
            headers={
                "authorization": f"Bearer {token.strip()}",
                "content-type": "application/json",
            },
            json=body,
        )
        if response.status_code != 200:
            raise EvaluationTargetError(
                f"V3 {request.mode} endpoint returned HTTP "
                f"{response.status_code}"
            )
        try:
            payload = response.json()
        except ValueError as error:
            raise EvaluationTargetError(
                "V3 endpoint returned a non-JSON response"
            ) from error
        return _target_result_from_api(payload, mode=request.mode)

    async def _ensure_client(self) -> Any:
        if self._client is None:
            try:
                import httpx
            except ImportError as error:  # pragma: no cover - deployment guard
                raise EvaluationTargetError(
                    "httpx is required for HTTP evaluation"
                ) from error
            self._client = httpx.AsyncClient(
                timeout=self._timeout_seconds,
                follow_redirects=False,
            )
        return self._client


class EvaluationRunner:
    """Execute all manifest cases and finalize one immutable database run."""

    def __init__(
        self,
        *,
        target: EvaluationTarget | None,
        store: EvaluationStore,
        observation_source: EvaluationObservationSource | None = None,
    ) -> None:
        self._target = target
        self._store = store
        self._observation_source = observation_source

    async def run(
        self,
        manifest: EvaluationManifest,
        *,
        release_id: str,
        code_version: str,
        model_version: str | None = None,
        prompt_version: str | None = None,
        repetitions: int = 1,
        eval_run_id: str | None = None,
    ) -> EvaluationRunReport:
        normalized_release_id = _uuid_text(release_id, "release_id")
        if normalized_release_id not in manifest.scope.allowed_release_ids:
            raise EvaluationManifestError(
                "selected release_id is outside manifest allowed_release_ids"
            )
        normalized_code_version = _text(code_version, "code_version")
        if isinstance(repetitions, bool) or not 1 <= repetitions <= 100:
            raise EvaluationManifestError(
                "repetitions must be an integer between 1 and 100"
            )
        observation_targets_present = bool(
            manifest.extraction_targets or manifest.table_targets
        )
        if manifest.cases and self._target is None:
            raise EvaluationManifestError(
                "query cases require an answer/query evaluation target"
            )
        if observation_targets_present and self._observation_source is None:
            raise EvaluationManifestError(
                "extraction/table targets require a canonical observation "
                "source; they cannot be silently skipped"
            )
        run_id = (
            _uuid_text(eval_run_id, "eval_run_id")
            if eval_run_id is not None
            else str(uuid.uuid4())
        )
        started_at = datetime.now(UTC)
        spec = EvaluationRunSpec(
            eval_run_id=run_id,
            tenant_id=manifest.scope.tenant_id,
            knowledge_base_id=manifest.scope.knowledge_base_id,
            release_id=normalized_release_id,
            eval_suite_id=manifest.eval_suite_id,
            code_version=normalized_code_version,
            model_version=_optional_text(model_version, "model_version"),
            prompt_version=_optional_text(prompt_version, "prompt_version"),
            repetitions=repetitions,
            started_at=started_at,
            split=manifest.split,
        )
        self._store.prepare_suite(manifest)
        self._store.begin_run(spec)
        try:
            observations = EvaluationObservations()
            observation_metrics: dict[str, Any] = {}
            observation_hard_failures: tuple[str, ...] = ()
            if observation_targets_present:
                capture_started = time.perf_counter()
                captured = await asyncio.to_thread(
                    self._observation_source.capture,
                    manifest=manifest,
                    release_id=normalized_release_id,
                )
                capture_latency_ms = max(
                    0,
                    round(
                        (time.perf_counter() - capture_started) * 1_000
                    ),
                )
                observations = EvaluationObservations(
                    extraction=tuple(captured.extraction),
                    tables=tuple(captured.tables),
                    latency_ms=capture_latency_ms,
                )
                _validate_captured_observation_scope(
                    manifest,
                    spec,
                    observations,
                )
                (
                    observation_metrics,
                    observation_hard_failures,
                ) = score_canonical_observations(
                    extraction_targets=manifest.extraction_targets,
                    extraction_observations=observations.extraction,
                    table_targets=manifest.table_targets,
                    table_observations=observations.tables,
                )
            results: list[EvaluationCaseResult] = []
            for repetition in range(1, repetitions + 1):
                for case in manifest.cases:
                    results.append(
                        await self._execute_case(
                            manifest,
                            spec,
                            case,
                            repetition=repetition,
                        )
                    )
            aggregate_metrics = _aggregate_results(results)
            aggregate_metrics.update(observation_metrics)
            hard_failures = tuple(
                sorted(
                    {
                        code
                        for result in results
                        for code in result.hard_failures
                    }.union(observation_hard_failures)
                )
            )
            passed = (
                (bool(results) or observation_targets_present)
                and all(item.passed for item in results)
                and not observation_hard_failures
            )
            report = EvaluationRunReport(
                spec=spec,
                status="succeeded",
                passed=passed,
                aggregate_metrics=aggregate_metrics,
                hard_failures=hard_failures,
                results=tuple(results),
                completed_at=datetime.now(UTC),
                observations=observations,
                observation_hard_failures=observation_hard_failures,
            )
            self._store.complete_run(report)
            return report
        except Exception as error:
            completed_at = datetime.now(UTC)
            try:
                self._store.fail_run(
                    spec,
                    completed_at=completed_at,
                    error=f"{type(error).__name__}: {error}"[:2_000],
                )
            except Exception:
                # Preserve the causal evaluator/storage exception.
                pass
            raise

    async def _execute_case(
        self,
        manifest: EvaluationManifest,
        spec: EvaluationRunSpec,
        case: EvaluationCase,
        *,
        repetition: int,
    ) -> EvaluationCaseResult:
        request = EvaluationRequest(
            case_key=case.case_key,
            mode=case.mode,
            question=case.question,
            mandatory_constraints=case.mandatory_constraints,
            forbidden_claim_values=case.forbidden_claim_values,
            top_k=case.top_k,
        )
        start = time.perf_counter()
        target_error: str | None = None
        try:
            if self._target is None:
                raise EvaluationTargetError(
                    "query evaluation target is not configured"
                )
            response = await self._target.execute(request)
        except Exception as error:
            target_error = f"{type(error).__name__}: {error}"[:1_000]
            response = EvaluationTargetResult(
                status="target_error",
                release_id=spec.release_id,
                evidence=(),
            )
        elapsed_ms = max(0, round((time.perf_counter() - start) * 1_000))
        return _score_case(
            manifest,
            spec,
            case,
            response,
            repetition=repetition,
            latency_ms=elapsed_ms,
            target_error=target_error,
        )


def _validate_captured_observation_scope(
    manifest: EvaluationManifest,
    spec: EvaluationRunSpec,
    observations: EvaluationObservations,
) -> None:
    expected_sources = {
        str(target["source_id"])
        for target in manifest.extraction_targets
    }
    expected_tables = {
        str(target["table_id"]): str(target["source_id"])
        for target in manifest.table_targets
    }
    allowed_sources = set(manifest.scope.allowed_source_ids)
    seen_sources: set[str] = set()
    seen_tables: set[str] = set()

    def validate_scope(
        observation: Mapping[str, Any],
        *,
        label: str,
        expected_source_id: str | None = None,
    ) -> str:
        source_id = str(observation.get("source_id") or "").strip()
        if (
            not source_id
            or source_id not in allowed_sources
            or (
                expected_source_id is not None
                and source_id != expected_source_id
            )
        ):
            raise EvaluationTargetError(
                f"{label} escaped its authorized source scope"
            )
        expected_coordinates = (
            ("tenant_id", manifest.scope.tenant_id),
            ("knowledge_base_id", manifest.scope.knowledge_base_id),
            ("release_id", spec.release_id),
        )
        for field_name, expected in expected_coordinates:
            actual = str(observation.get(field_name) or "").strip()
            if actual != expected:
                raise EvaluationTargetError(
                    f"{label}.{field_name} does not match the pinned scope"
                )
        for required in (
            "source_version_id",
            "artifact_set_id",
            "source_sha256",
        ):
            if not str(observation.get(required) or "").strip():
                raise EvaluationTargetError(
                    f"{label}.{required} is required"
                )
        return source_id

    for position, raw in enumerate(observations.extraction):
        observation = _mapping(raw, f"extraction observation {position}")
        source_id = validate_scope(
            observation,
            label=f"extraction observation {position}",
        )
        if source_id not in expected_sources:
            raise EvaluationTargetError(
                "canonical capture returned an unrequested extraction source"
            )
        if source_id in seen_sources:
            raise EvaluationTargetError(
                "canonical capture returned a duplicate extraction source"
            )
        seen_sources.add(source_id)

    for position, raw in enumerate(observations.tables):
        observation = _mapping(raw, f"table observation {position}")
        table_id = str(observation.get("table_id") or "").strip()
        if not table_id or table_id not in expected_tables:
            raise EvaluationTargetError(
                "canonical capture returned an unrequested table"
            )
        validate_scope(
            observation,
            label=f"table observation {position}",
            expected_source_id=expected_tables[table_id],
        )
        if table_id in seen_tables:
            raise EvaluationTargetError(
                "canonical capture returned a duplicate table"
            )
        seen_tables.add(table_id)


def _score_case(
    manifest: EvaluationManifest,
    spec: EvaluationRunSpec,
    case: EvaluationCase,
    response: EvaluationTargetResult,
    *,
    repetition: int,
    latency_ms: int,
    target_error: str | None,
) -> EvaluationCaseResult:
    failures: list[str] = []
    failure_details: dict[str, Any] = {}
    if target_error is not None:
        failures.append("target_error")
        failure_details["target_error"] = target_error
    if response.release_id != spec.release_id:
        failures.append("release_mismatch")
        failure_details["release_mismatch"] = {
            "expected": spec.release_id,
            "actual": response.release_id,
        }

    evidence_ids: list[str] = []
    persisted_evidence_ids: list[str] = []
    authorization_violations: list[dict[str, str]] = []
    malformed_evidence_ids: list[str] = []
    allowed_sources = set(manifest.scope.allowed_source_ids)
    for position, raw_evidence in enumerate(response.evidence, start=1):
        evidence = _mapping(raw_evidence, f"target evidence {position}")
        evidence_id = str(evidence.get("evidence_id") or "").strip()
        evidence_ids.append(evidence_id)
        try:
            persisted_evidence_ids.append(
                _uuid_text(evidence_id, "evidence_id")
            )
        except EvaluationManifestError:
            malformed_evidence_ids.append(evidence_id)

        source_candidates = {
            str(evidence.get(name) or "").strip()
            for name in (
                "source_document_id",
                "source_id",
                "external_file_id",
            )
            if str(evidence.get(name) or "").strip()
        }
        if not source_candidates.intersection(allowed_sources):
            authorization_violations.append(
                {
                    "kind": "source_scope_mismatch",
                    "evidence_id": evidence_id,
                }
            )
        for field_name, expected in (
            ("tenant_id", manifest.scope.tenant_id),
            ("knowledge_base_id", manifest.scope.knowledge_base_id),
            ("release_id", spec.release_id),
        ):
            actual = str(evidence.get(field_name) or "").strip()
            if actual and actual != expected:
                authorization_violations.append(
                    {
                        "kind": f"{field_name}_mismatch",
                        "evidence_id": evidence_id,
                    }
                )

    if malformed_evidence_ids:
        failures.append("malformed_evidence_id")
        failure_details["malformed_evidence_ids"] = malformed_evidence_ids
    if authorization_violations:
        failures.append("authorization_leakage")
        failure_details["authorization_violations"] = (
            authorization_violations
        )
    if len(evidence_ids) != len(set(evidence_ids)):
        failures.append("duplicate_evidence")

    required = set(case.required_evidence_ids)
    retrieved = set(evidence_ids)
    missing_required = sorted(required - retrieved)
    forbidden_retrieved = sorted(
        set(case.forbidden_evidence_ids).intersection(retrieved)
    )
    if missing_required:
        failures.append("required_evidence_missing")
        failure_details["missing_required_evidence_ids"] = missing_required
    if forbidden_retrieved:
        failures.append("forbidden_evidence_retrieved")
        failure_details["forbidden_evidence_ids"] = forbidden_retrieved
    if (
        case.exact_lookup
        and case.required_evidence_ids
        and (
            not evidence_ids
            or evidence_ids[0] not in set(case.required_evidence_ids)
        )
    ):
        failures.append("exact_lookup_miss")

    if case.mode == "answer":
        refused = response.status.startswith("refused")
        if case.refusal_expected is True and not refused:
            failures.append("expected_refusal_missing")
        elif case.refusal_expected is False and refused:
            failures.append("unexpected_refusal")
        if (
            case.accepted_statuses
            and response.status not in case.accepted_statuses
        ):
            failures.append("unexpected_status")
        elif not case.accepted_statuses:
            if case.refusal_expected is True and not refused:
                failures.append("unexpected_status")
            elif (
                case.refusal_expected is not True
                and response.status not in _ANSWERED_STATUSES
            ):
                failures.append("unexpected_status")

        validation = response.validation
        if response.status in _ANSWERED_STATUSES and (
            not isinstance(validation, Mapping)
            or validation.get("valid") is not True
        ):
            failures.append("invalid_answer_accepted")

        normalized_answer = _normalize_text(response.answer)
        missing_answer_terms = [
            value
            for value in case.required_answer_substrings
            if _normalize_text(value) not in normalized_answer
        ]
        forbidden_answer_terms = [
            value
            for value in case.forbidden_answer_substrings
            if _normalize_text(value) in normalized_answer
        ]
        if missing_answer_terms:
            failures.append("required_answer_text_missing")
            failure_details["missing_answer_substrings"] = (
                missing_answer_terms
            )
        if forbidden_answer_terms:
            failures.append("forbidden_answer_text_present")
            failure_details["forbidden_answer_substrings"] = (
                forbidden_answer_terms
            )

    unique_failures = tuple(sorted(set(failures)))
    hard_failures = tuple(
        code
        for code in unique_failures
        if code in _ALWAYS_HARD_FAILURES
        or code in set(case.hard_failure_codes)
    )
    recall = (
        len(required.intersection(retrieved)) / len(required)
        if required
        else None
    )
    scores: dict[str, Any] = {
        "evidence_count": len(evidence_ids),
        "required_evidence_recall": (
            round(recall, 6) if recall is not None else None
        ),
        "exact_lookup_passed": (
            (
                bool(evidence_ids)
                and evidence_ids[0] in set(case.required_evidence_ids)
            )
            if case.exact_lookup and case.required_evidence_ids
            else None
        ),
        "authorization_passed": not authorization_violations,
        "release_pin_passed": response.release_id == spec.release_id,
        "latency_ms": latency_ms,
    }
    result_id = str(
        uuid.uuid5(
            uuid.UUID(spec.eval_run_id),
            f"{case.eval_case_id}\x1f{repetition}",
        )
    )
    validation_details = (
        canonicalize_mapping(response.validation)
        if isinstance(response.validation, Mapping)
        else None
    )
    details = {
        "case_key": case.case_key,
        "mode": case.mode,
        "status": response.status,
        "answer": response.answer,
        "evidence_ids": evidence_ids,
        "validation": validation_details,
        "failure_codes": list(unique_failures),
        "failure_details": failure_details,
    }
    return EvaluationCaseResult(
        eval_result_id=result_id,
        eval_case_id=case.eval_case_id,
        case_key=case.case_key,
        repetition=repetition,
        passed=not unique_failures,
        scores=scores,
        hard_failures=hard_failures,
        evidence_search_unit_ids=tuple(dict.fromkeys(persisted_evidence_ids)),
        latency_ms=latency_ms,
        details=details,
    )


def _aggregate_results(
    results: Sequence[EvaluationCaseResult],
) -> dict[str, Any]:
    total = len(results)
    passed = sum(item.passed for item in results)
    latencies = sorted(item.latency_ms for item in results)
    failure_counts = Counter(
        code
        for item in results
        for code in item.details.get("failure_codes", [])
    )
    hard_failure_counts = Counter(
        code for item in results for code in item.hard_failures
    )
    return {
        "case_result_count": total,
        "passed_case_result_count": passed,
        "failed_case_result_count": total - passed,
        "pass_rate": round(passed / total, 6) if total else None,
        "latency_p50_ms": _percentile(latencies, 0.50),
        "latency_p95_ms": _percentile(latencies, 0.95),
        "failure_counts": dict(sorted(failure_counts.items())),
        "hard_failure_counts": dict(sorted(hard_failure_counts.items())),
    }


def _percentile(values: Sequence[int], fraction: float) -> int | None:
    if not values:
        return None
    position = round((len(values) - 1) * fraction)
    return int(values[position])


def _target_result_from_api(
    payload: Any,
    *,
    mode: str,
) -> EvaluationTargetResult:
    root = _mapping(payload, "V3 API response")
    release_id = _text(root.get("release_id"), "response.release_id")
    if mode == "query":
        evidence_raw = root.get("results")
        status = "retrieved"
        answer = ""
        validation = None
    else:
        evidence_raw = root.get("evidence")
        status = _text(root.get("status"), "response.status")
        answer = str(root.get("answer") or "")
        validation_value = root.get("validation")
        validation = (
            canonicalize_mapping(
                _mapping(validation_value, "response.validation")
            )
            if validation_value is not None
            else None
        )
    if not isinstance(evidence_raw, list):
        raise EvaluationTargetError("V3 API evidence/results must be an array")
    evidence = tuple(
        canonicalize_mapping(_mapping(item, "response evidence item"))
        for item in evidence_raw
    )
    return EvaluationTargetResult(
        status=status,
        release_id=release_id,
        evidence=evidence,
        answer=answer,
        validation=validation,
    )


def _parse_extraction_targets(
    value: Any,
    *,
    allowed_source_ids: set[str],
) -> tuple[Mapping[str, Any], ...]:
    if not isinstance(value, list):
        raise EvaluationManifestError(
            "manifest extraction_targets must be an array"
        )
    targets: list[Mapping[str, Any]] = []
    source_ids: list[str] = []
    for position, raw_target in enumerate(value):
        label = f"extraction_targets[{position}]"
        target = dict(_mapping(raw_target, label))
        source_id = _uuid_text(target.get("source_id"), f"{label}.source_id")
        if source_id not in allowed_source_ids:
            raise EvaluationManifestError(
                f"{label}.source_id is outside allowed_source_ids"
            )
        target["source_id"] = source_id
        target["expected_page_count"] = _nonnegative_integer(
            target.get("expected_page_count"),
            f"{label}.expected_page_count",
        )
        facts = target.get("facts", [])
        if not isinstance(facts, list):
            raise EvaluationManifestError(f"{label}.facts must be an array")
        normalized_facts: list[Mapping[str, Any]] = []
        fact_ids: list[str] = []
        for fact_position, raw_fact in enumerate(facts):
            fact_label = f"{label}.facts[{fact_position}]"
            fact = dict(_mapping(raw_fact, fact_label))
            fact_id = _text(fact.get("fact_id"), f"{fact_label}.fact_id")
            if "value" not in fact:
                raise EvaluationManifestError(
                    f"{fact_label}.value is required"
                )
            fact["fact_id"] = fact_id
            normalized_facts.append(canonicalize_mapping(fact))
            fact_ids.append(fact_id)
        if len(fact_ids) != len(set(fact_ids)):
            raise EvaluationManifestError(
                f"{label}.facts contains duplicate fact IDs"
            )
        target["facts"] = normalized_facts
        targets.append(canonicalize_mapping(target))
        source_ids.append(source_id)
    if len(source_ids) != len(set(source_ids)):
        raise EvaluationManifestError(
            "extraction_targets contains duplicate source IDs"
        )
    return tuple(targets)


def _parse_table_targets(
    value: Any,
    *,
    allowed_source_ids: set[str],
) -> tuple[Mapping[str, Any], ...]:
    if not isinstance(value, list):
        raise EvaluationManifestError(
            "manifest table_targets must be an array"
        )
    targets: list[Mapping[str, Any]] = []
    table_ids: list[str] = []
    for position, raw_target in enumerate(value):
        label = f"table_targets[{position}]"
        target = dict(_mapping(raw_target, label))
        source_id = _uuid_text(target.get("source_id"), f"{label}.source_id")
        if source_id not in allowed_source_ids:
            raise EvaluationManifestError(
                f"{label}.source_id is outside allowed_source_ids"
            )
        table_id = _text(target.get("table_id"), f"{label}.table_id")
        target["source_id"] = source_id
        target["table_id"] = table_id
        target["row_count"] = _nonnegative_integer(
            target.get("row_count"),
            f"{label}.row_count",
        )
        target["column_count"] = _positive_integer(
            target.get("column_count"),
            f"{label}.column_count",
        )
        cells = target.get("cells")
        if not isinstance(cells, list) or not cells:
            raise EvaluationManifestError(
                f"{label}.cells must be a non-empty array"
            )
        normalized_cells: list[Mapping[str, Any]] = []
        cell_ids: list[str] = []
        for cell_position, raw_cell in enumerate(cells):
            cell_label = f"{label}.cells[{cell_position}]"
            cell = dict(_mapping(raw_cell, cell_label))
            cell_id = _text(cell.get("cell_id"), f"{cell_label}.cell_id")
            if "value" not in cell:
                raise EvaluationManifestError(
                    f"{cell_label}.value is required"
                )
            cell["cell_id"] = cell_id
            cell["row_span"] = _positive_integer(
                cell.get("row_span", 1),
                f"{cell_label}.row_span",
            )
            cell["column_span"] = _positive_integer(
                cell.get("column_span", 1),
                f"{cell_label}.column_span",
            )
            normalized_cells.append(canonicalize_mapping(cell))
            cell_ids.append(cell_id)
        if len(cell_ids) != len(set(cell_ids)):
            raise EvaluationManifestError(
                f"{label}.cells contains duplicate cell IDs"
            )
        target["cells"] = normalized_cells
        targets.append(canonicalize_mapping(target))
        table_ids.append(table_id)
    if len(table_ids) != len(set(table_ids)):
        raise EvaluationManifestError(
            "table_targets contains duplicate table IDs"
        )
    return tuple(targets)


def _parse_case(
    raw_case: Any,
    *,
    eval_suite_id: str,
    split: str,
    position: int,
) -> EvaluationCase:
    case = _mapping(raw_case, f"queries[{position}]")
    case_key = _text(
        case.get("case_key") or case.get("case_id"),
        f"queries[{position}].case_key",
    )
    category = _text(
        case.get("category", "general"),
        f"queries[{position}].category",
    )
    prompt = _mapping(case.get("prompt", {}), f"{case_key}.prompt")
    question = _text(
        prompt.get("query")
        or prompt.get("question")
        or case.get("question")
        or case.get("query"),
        f"{case_key}.prompt.query",
    )
    retrieval = _mapping(
        case.get("retrieval", {}),
        f"{case_key}.retrieval",
    )
    answer = _mapping(case.get("answer", {}), f"{case_key}.answer")
    expected = _mapping(case.get("expected", {}), f"{case_key}.expected")
    if expected:
        retrieval = _mapping(
            expected.get("retrieval", retrieval),
            f"{case_key}.expected.retrieval",
        )
        answer = _mapping(
            expected.get("answer", answer),
            f"{case_key}.expected.answer",
        )
    mode_value = (
        prompt.get("mode")
        or case.get("mode")
        or ("answer" if answer or case.get("answer") is not None else "query")
    )
    mode = _text(mode_value, f"{case_key}.mode").casefold()
    if mode not in ALLOWED_MODES:
        raise EvaluationManifestError(
            f"{case_key}.mode must be answer or query"
        )

    top_k = prompt.get("top_k", case.get("top_k", 8))
    if isinstance(top_k, bool) or not isinstance(top_k, int) or not 1 <= top_k <= 20:
        raise EvaluationManifestError(
            f"{case_key}.top_k must be an integer between 1 and 20"
        )
    constraints_raw = _mapping(
        prompt.get(
            "mandatory_constraints",
            case.get("mandatory_constraints", {}),
        ),
        f"{case_key}.mandatory_constraints",
    )
    constraints: dict[str, str | tuple[str, ...]] = {}
    for raw_key, raw_value in constraints_raw.items():
        key = _text(raw_key, f"{case_key}.constraint key")
        if isinstance(raw_value, str):
            constraints[key] = _text(
                raw_value,
                f"{case_key}.mandatory_constraints.{key}",
            )
        elif isinstance(raw_value, list):
            values = tuple(
                _text(
                    value,
                    f"{case_key}.mandatory_constraints.{key} item",
                )
                for value in raw_value
            )
            if not values:
                raise EvaluationManifestError(
                    f"{case_key}.mandatory_constraints.{key} must not be empty"
                )
            constraints[key] = values
        else:
            raise EvaluationManifestError(
                f"{case_key}.mandatory_constraints.{key} must be a string or array"
            )

    refusal_value = answer.get("refusal_expected")
    if refusal_value is not None and not isinstance(refusal_value, bool):
        raise EvaluationManifestError(
            f"{case_key}.answer.refusal_expected must be boolean or null"
        )
    accepted_statuses = tuple(
        _text_list(
            answer.get(
                "accepted_statuses",
                answer.get("allowed_statuses", []),
            ),
            f"{case_key}.answer.accepted_statuses",
        )
    )
    hard_failure_codes = tuple(
        sorted(
            set(
                _text_list(
                    case.get(
                        "hard_failure_codes",
                        expected.get("hard_failure_codes", []),
                    ),
                    f"{case_key}.hard_failure_codes",
                )
            )
        )
    )
    metadata = canonicalize_mapping(
        _mapping(case.get("metadata", {}), f"{case_key}.metadata")
    )
    required_evidence_ids = tuple(
        _text_list(
            retrieval.get("required_evidence_ids", []),
            f"{case_key}.retrieval.required_evidence_ids",
        )
    )
    forbidden_evidence_ids = tuple(
        _text_list(
            retrieval.get("forbidden_evidence_ids", []),
            f"{case_key}.retrieval.forbidden_evidence_ids",
        )
    )
    overlap = sorted(
        set(required_evidence_ids).intersection(forbidden_evidence_ids)
    )
    if overlap:
        raise EvaluationManifestError(
            f"{case_key} requires and forbids the same evidence IDs"
        )
    exact_lookup = retrieval.get("exact_lookup", False)
    if not isinstance(exact_lookup, bool):
        raise EvaluationManifestError(
            f"{case_key}.retrieval.exact_lookup must be boolean"
        )
    return EvaluationCase(
        eval_case_id=str(uuid.uuid5(uuid.UUID(eval_suite_id), case_key)),
        case_key=case_key,
        split=split,
        category=category,
        mode=mode,
        question=question,
        mandatory_constraints=constraints,
        forbidden_claim_values=tuple(
            _text_list(
                prompt.get(
                    "forbidden_claim_values",
                    case.get("forbidden_claim_values", []),
                ),
                f"{case_key}.forbidden_claim_values",
            )
        ),
        top_k=top_k,
        required_evidence_ids=required_evidence_ids,
        forbidden_evidence_ids=forbidden_evidence_ids,
        exact_lookup=exact_lookup,
        refusal_expected=refusal_value,
        accepted_statuses=accepted_statuses,
        required_answer_substrings=tuple(
            _text_list(
                answer.get(
                    "required_substrings",
                    answer.get("required_text", []),
                ),
                f"{case_key}.answer.required_substrings",
            )
        ),
        forbidden_answer_substrings=tuple(
            _text_list(
                answer.get(
                    "forbidden_substrings",
                    answer.get("forbidden_text", []),
                ),
                f"{case_key}.answer.forbidden_substrings",
            )
        ),
        hard_failure_codes=hard_failure_codes,
        metadata=metadata,
    )


def _validate_split(
    split: str,
    *,
    acknowledge_locked_holdout: bool,
) -> str:
    if split not in ALLOWED_SPLITS:
        raise EvaluationManifestError(
            "split must be exactly development or holdout"
        )
    if split == "holdout" and not acknowledge_locked_holdout:
        raise LockedHoldoutError(
            "locked holdout requires explicit acknowledgement before file access"
        )
    return split


def _mapping(value: Any, label: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise EvaluationManifestError(f"{label} must be an object")
    return value


def _text(value: Any, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise EvaluationManifestError(f"{label} must be a non-empty string")
    normalized = value.strip()
    if len(normalized) > 4_000:
        raise EvaluationManifestError(f"{label} exceeds its length limit")
    return normalized


def _optional_text(value: Any, label: str) -> str | None:
    if value is None:
        return None
    return _text(value, label)


def _uuid_text(value: Any, label: str) -> str:
    normalized = _text(value, label)
    try:
        return str(uuid.UUID(normalized))
    except ValueError as error:
        raise EvaluationManifestError(f"{label} must be a UUID") from error


def _text_list(
    value: Any,
    label: str,
    *,
    require_nonempty: bool = False,
) -> list[str]:
    if not isinstance(value, list):
        raise EvaluationManifestError(f"{label} must be an array")
    values = [_text(item, f"{label} item") for item in value]
    if require_nonempty and not values:
        raise EvaluationManifestError(f"{label} must not be empty")
    return values


def _nonnegative_integer(value: Any, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise EvaluationManifestError(
            f"{label} must be a non-negative integer"
        )
    return value


def _positive_integer(value: Any, label: str) -> int:
    result = _nonnegative_integer(value, label)
    if result < 1:
        raise EvaluationManifestError(f"{label} must be a positive integer")
    return result


def _optional_datetime(value: Any, label: str) -> datetime | None:
    if value is None:
        return None
    normalized = _text(value, label)
    try:
        parsed = datetime.fromisoformat(normalized.replace("Z", "+00:00"))
    except ValueError as error:
        raise EvaluationManifestError(
            f"{label} must be an ISO-8601 timestamp"
        ) from error
    if parsed.tzinfo is None:
        raise EvaluationManifestError(f"{label} must include a timezone")
    return parsed.astimezone(UTC)


def _normalize_text(value: str) -> str:
    return " ".join(value.casefold().split())
