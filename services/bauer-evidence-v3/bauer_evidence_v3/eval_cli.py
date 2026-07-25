"""Command-line release validation for Bauer Evidence V3.

There are deliberately three target modes:

* a private HTTP API serving an exact ``ready`` candidate;
* a local, in-process answer service pinned to an exact ``validating`` release;
  and
* a reader-only canonical extraction/table capture with no answer target.

The second mode closes the validation/readiness cycle without exposing an
incomplete release through a network route.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import math
import os
import time
import uuid
from collections.abc import Mapping
from pathlib import Path
from typing import Any, Callable, Sequence

from .answering import AnswerService
from .auth import AuthorizationContext
from .db_security import DatabaseRoleSecurityError
from .embeddings import OpenAICompatibleEmbeddingProvider
from .evaluation import (
    AnswerServiceEvaluationTarget,
    EvaluationError,
    EvaluationRunner,
    HttpEvaluationTarget,
    load_evaluation_manifest,
)
from .generation import OpenAICompatibleModelGateway
from .models import ReleaseStatus
from .postgres_eval import (
    PostgresEvaluationError,
    PostgresEvaluationObservationSource,
    PostgresEvaluationStore,
)
from .postgres_runtime import (
    PostgresEvidenceIndex,
    PostgresReleaseRegistry,
    PostgresRuntimeError,
    PostgresValidationReleaseRegistry,
)
from .releases import ReleaseError


DEFAULT_DATABASE_ENV = "BAUER_V3_EVAL_DATABASE_URL"
DEFAULT_TOKEN_ENV = "BAUER_V3_EVAL_AUTHORIZATION_TOKEN"
DEFAULT_PRINCIPALS_ENV = "BAUER_V3_EVAL_PRINCIPAL_IDS"
DEFAULT_QUERY_DATABASE_ENV = "BAUER_V3_EVAL_QUERY_DATABASE_URL"
DEFAULT_QUERY_PRINCIPALS_ENV = "BAUER_V3_EVAL_QUERY_PRINCIPAL_IDS"
DEFAULT_EVAL_USER_ENV = "BAUER_V3_EVAL_USER_ID"
DEFAULT_EVAL_AGENT_ENV = "BAUER_V3_EVAL_AGENT_ID"
DEFAULT_EVAL_AUDIENCE_ENV = "BAUER_V3_EVAL_AUDIENCE"
DEFAULT_EMBEDDING_DIMENSIONS = 1_024
DIRECT_AUTHORIZATION_TTL_SECONDS = 4 * 60 * 60


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Execute and persist one release-pinned Bauer Evidence V3 "
            "evaluation suite."
        )
    )
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument(
        "--split",
        choices=("development", "holdout"),
        required=True,
        help="An explicit split is mandatory; there is no combined mode.",
    )
    parser.add_argument("--acknowledge-locked-holdout", action="store_true")
    parser.add_argument("--release-id", required=True)
    target_group = parser.add_mutually_exclusive_group(required=True)
    target_group.add_argument(
        "--api-url",
        help=(
            "Private HTTP API fixed to the selected ready release. "
            "This mode requires an authorization token."
        ),
    )
    target_group.add_argument(
        "--direct-validating",
        action="store_true",
        help=(
            "Evaluate an exact validating release in-process with a "
            "least-privilege reader connection; no API route is created."
        ),
    )
    target_group.add_argument(
        "--canonical-only",
        action="store_true",
        help=(
            "Capture and score extraction/table targets from canonical "
            "PostgreSQL state without calling an answer model or API."
        ),
    )
    parser.add_argument("--code-version", required=True)
    parser.add_argument("--model-version")
    parser.add_argument("--prompt-version")
    parser.add_argument("--repetitions", type=int, default=1)
    parser.add_argument("--timeout-seconds", type=float, default=60.0)
    parser.add_argument(
        "--database-url-env",
        default=DEFAULT_DATABASE_ENV,
        help="Environment variable containing the evaluator-role PostgreSQL DSN.",
    )
    parser.add_argument(
        "--query-database-url-env",
        default=DEFAULT_QUERY_DATABASE_ENV,
        help=(
            "Environment variable containing the separate non-owner "
            "reader-role PostgreSQL DSN. It is always required for direct "
            "mode and for canonical extraction/table capture."
        ),
    )
    token_group = parser.add_mutually_exclusive_group()
    token_group.add_argument(
        "--authorization-token-file",
        type=Path,
        help=(
            "File containing a short-lived token. It is re-read for every "
            "case so a sidecar may rotate it."
        ),
    )
    token_group.add_argument(
        "--authorization-token-env",
        default=DEFAULT_TOKEN_ENV,
        help="Environment variable containing the short-lived API token.",
    )
    parser.add_argument(
        "--principal-id",
        action="append",
        default=[],
        help=(
            "Evaluation principal UUID with the app-level admin grant; repeat for "
            "multiple principals. "
            f"Defaults to JSON array in {DEFAULT_PRINCIPALS_ENV}."
        ),
    )
    parser.add_argument(
        "--query-principal-id",
        action="append",
        default=[],
        help=(
            "Query/capture reader principal UUID; repeat for multiple "
            "principals. Defaults to JSON array in "
            f"{DEFAULT_QUERY_PRINCIPALS_ENV}."
        ),
    )
    parser.add_argument(
        "--output",
        type=Path,
        help="Optional exclusive local JSON audit copy; existing files are refused.",
    )
    args = parser.parse_args(list(argv) if argv is not None else None)

    try:
        manifest = load_evaluation_manifest(
            args.manifest,
            expected_split=args.split,
            acknowledge_locked_holdout=args.acknowledge_locked_holdout,
        )
        if (
            not math.isfinite(args.timeout_seconds)
            or args.timeout_seconds <= 0
        ):
            raise EvaluationError("timeout_seconds must be positive and finite")
        database_url = _required_environment(args.database_url_env)
        principal_ids = tuple(args.principal_id) or _principal_ids_from_env(
            DEFAULT_PRINCIPALS_ENV
        )
        query_database_url: str | None = None
        query_principal_ids: tuple[str, ...] = ()
        if args.direct_validating:
            query_database_url = _required_environment(
                args.query_database_url_env
            )
            if query_database_url == database_url:
                raise EvaluationError(
                    "direct evaluation requires a query DSN separate from "
                    "the evaluator persistence DSN"
                )
            query_principal_ids = (
                tuple(args.query_principal_id)
                or _principal_ids_from_env(DEFAULT_QUERY_PRINCIPALS_ENV)
            )
            target = _build_direct_validating_target(
                manifest=manifest,
                release_id=args.release_id,
                query_database_url=query_database_url,
                query_principal_ids=query_principal_ids,
                timeout_seconds=args.timeout_seconds,
            )
        elif args.canonical_only:
            if getattr(manifest, "cases", ()):
                raise EvaluationError(
                    "canonical-only mode cannot execute query cases"
                )
            if not (
                getattr(manifest, "extraction_targets", ())
                or getattr(manifest, "table_targets", ())
            ):
                raise EvaluationError(
                    "canonical-only mode requires extraction/table targets"
                )
            target = None
        else:
            token_provider = _token_provider(
                token_file=args.authorization_token_file,
                token_environment=args.authorization_token_env,
            )
            # Preflight before creating a database run. A rotating file is
            # still read again for every request by the HTTP target.
            if not token_provider().strip():
                raise EvaluationError("authorization token is empty")
            target = HttpEvaluationTarget(
                api_url=args.api_url,
                authorization_token=token_provider,
                timeout_seconds=args.timeout_seconds,
            )

        observation_source = None
        if (
            getattr(manifest, "extraction_targets", ())
            or getattr(manifest, "table_targets", ())
        ):
            query_database_url = query_database_url or _required_environment(
                args.query_database_url_env
            )
            if query_database_url == database_url:
                raise EvaluationError(
                    "canonical observation capture requires a reader DSN "
                    "separate from the evaluator persistence DSN"
                )
            query_principal_ids = (
                query_principal_ids
                or tuple(args.query_principal_id)
                or _principal_ids_from_env(DEFAULT_QUERY_PRINCIPALS_ENV)
            )
            observation_source = (
                PostgresEvaluationObservationSource.from_dsn(
                    query_database_url,
                    tenant_id=manifest.scope.tenant_id,
                    knowledge_base_id=(
                        manifest.scope.knowledge_base_id
                    ),
                    principal_ids=query_principal_ids,
                    enforce_least_privilege=True,
                    statement_timeout_ms=max(
                        1,
                        round(args.timeout_seconds * 1_000),
                    ),
                )
            )

        store = PostgresEvaluationStore.from_dsn(
            database_url,
            tenant_id=manifest.scope.tenant_id,
            principal_ids=principal_ids,
        )
        store.verify_database_role()
        report = asyncio.run(
            _execute(
                manifest=manifest,
                store=store,
                target=target,
                observation_source=observation_source,
                release_id=args.release_id,
                code_version=args.code_version,
                model_version=args.model_version,
                prompt_version=args.prompt_version,
                repetitions=args.repetitions,
            )
        )
        if args.output is not None:
            _write_json_exclusive(args.output, report.to_json())
    except (
        EvaluationError,
        DatabaseRoleSecurityError,
        PostgresEvaluationError,
        PostgresRuntimeError,
        ReleaseError,
        FileExistsError,
        OSError,
        ValueError,
    ) as error:
        parser.error(str(error))

    print(
        json.dumps(
            {
                "eval_run_id": report.spec.eval_run_id,
                "release_id": report.spec.release_id,
                "passed": report.passed,
                "hard_failures": list(report.hard_failures),
                "case_result_count": len(report.results),
                "database_persisted": True,
                "output": str(args.output) if args.output else None,
            },
            ensure_ascii=False,
            sort_keys=True,
        )
    )
    return 0 if report.passed else 1


async def _execute(
    *,
    manifest,
    store,
    target,
    observation_source,
    release_id: str,
    code_version: str,
    model_version: str | None,
    prompt_version: str | None,
    repetitions: int,
):
    if isinstance(target, HttpEvaluationTarget):
        async with target:
            runner = EvaluationRunner(
                target=target,
                store=store,
                observation_source=observation_source,
            )
            return await runner.run(
                manifest,
                release_id=release_id,
                code_version=code_version,
                model_version=model_version,
                prompt_version=prompt_version,
                repetitions=repetitions,
            )

    runner = EvaluationRunner(
        target=target,
        store=store,
        observation_source=observation_source,
    )
    return await runner.run(
        manifest,
        release_id=release_id,
        code_version=code_version,
        model_version=model_version,
        prompt_version=prompt_version,
        repetitions=repetitions,
    )


def _build_direct_validating_target(
    *,
    manifest,
    release_id: str,
    query_database_url: str,
    query_principal_ids: tuple[str, ...],
    timeout_seconds: float,
    environment: Mapping[str, str] | None = None,
    release_registry_factory: Callable[..., Any] = PostgresReleaseRegistry,
    validation_registry_factory: Callable[..., Any] = (
        PostgresValidationReleaseRegistry
    ),
    evidence_index_factory: Callable[..., Any] = PostgresEvidenceIndex,
    embedding_provider_factory: Callable[..., Any] = (
        OpenAICompatibleEmbeddingProvider
    ),
    model_gateway_factory: Callable[..., Any] = OpenAICompatibleModelGateway,
    answer_service_factory: Callable[..., Any] = AnswerService,
    target_factory: Callable[..., Any] = AnswerServiceEvaluationTarget,
    clock: Callable[[], float] = time.time,
):
    """Build the evaluator-only target and preflight its exact release pin."""

    values = os.environ if environment is None else environment
    normalized_release_id = str(uuid.UUID(release_id))
    if normalized_release_id not in manifest.scope.allowed_release_ids:
        raise EvaluationError(
            "selected release_id is outside manifest allowed_release_ids"
        )
    if timeout_seconds <= 0:
        raise EvaluationError("timeout_seconds must be positive")
    if not query_database_url.strip():
        raise EvaluationError("direct evaluation query database DSN is required")
    if not query_principal_ids:
        raise EvaluationError(
            "direct evaluation requires at least one query principal"
        )

    model_base_url = _required_environment(
        "BAUER_V3_MODEL_BASE_URL",
        environment=values,
    )
    model_api_key = _required_environment(
        "BAUER_V3_MODEL_API_KEY",
        environment=values,
    )
    model_name = _required_environment(
        "BAUER_V3_MODEL_NAME",
        environment=values,
    )
    embedding_base_url = _required_environment(
        "BAUER_V3_EMBEDDING_BASE_URL",
        environment=values,
    )
    embedding_api_key = _required_environment(
        "BAUER_V3_EMBEDDING_API_KEY",
        environment=values,
    )
    embedding_model = _required_environment(
        "BAUER_V3_EMBEDDING_MODEL",
        environment=values,
    )
    dimensions = _embedding_dimensions(values)

    embedding = embedding_provider_factory(
        base_url=embedding_base_url,
        api_key=embedding_api_key,
        model=embedding_model,
        dimensions=dimensions,
        timeout_seconds=timeout_seconds,
    )
    base_registry = release_registry_factory(
        query_database_url,
        tenant_id=manifest.scope.tenant_id,
        knowledge_base_id=manifest.scope.knowledge_base_id,
        principal_ids=query_principal_ids,
        enforce_least_privilege=True,
    )
    validating_registry = validation_registry_factory(
        base_registry,
        release_id=normalized_release_id,
    )
    pinned = validating_registry.pin_active(
        manifest.scope.knowledge_base_id
    )
    if (
        str(pinned.release_id) != normalized_release_id
        or str(pinned.tenant_id) != manifest.scope.tenant_id
        or str(pinned.knowledge_base_id)
        != manifest.scope.knowledge_base_id
        or pinned.status != ReleaseStatus.VALIDATING
    ):
        raise EvaluationError(
            "direct evaluator release preflight did not return the exact "
            "validating manifest scope"
        )

    index = evidence_index_factory(
        query_database_url,
        tenant_id=manifest.scope.tenant_id,
        knowledge_base_id=manifest.scope.knowledge_base_id,
        principal_ids=query_principal_ids,
        embedding_provider=embedding,
        embedding_dimensions=dimensions,
        release_status=ReleaseStatus.VALIDATING,
        enforce_least_privilege=True,
    )
    model = model_gateway_factory(
        base_url=model_base_url,
        api_key=model_api_key,
        model=model_name,
        timeout_seconds=timeout_seconds,
    )
    service = answer_service_factory(
        releases=validating_registry,
        index=index,
        model=model,
    )
    issued_at = int(clock())
    authorization = AuthorizationContext(
        tenant_id=manifest.scope.tenant_id,
        knowledge_base_id=manifest.scope.knowledge_base_id,
        user_id=_required_environment(
            DEFAULT_EVAL_USER_ENV,
            environment=values,
        ),
        agent_id=_required_environment(
            DEFAULT_EVAL_AGENT_ENV,
            environment=values,
        ),
        audience=_required_environment(
            DEFAULT_EVAL_AUDIENCE_ENV,
            environment=values,
        ),
        issued_at=issued_at,
        expires_at=issued_at + DIRECT_AUTHORIZATION_TTL_SECONDS,
        authorized_source_ids=manifest.scope.allowed_source_ids,
    )
    return target_factory(
        answer_service=service,
        authorization=authorization,
    )


def _embedding_dimensions(environment: Mapping[str, str]) -> int:
    raw = environment.get(
        "BAUER_V3_EMBEDDING_DIMENSIONS",
        str(DEFAULT_EMBEDDING_DIMENSIONS),
    )
    try:
        dimensions = int(raw)
    except (TypeError, ValueError) as error:
        raise EvaluationError(
            "BAUER_V3_EMBEDDING_DIMENSIONS must be an integer"
        ) from error
    if dimensions != DEFAULT_EMBEDDING_DIMENSIONS:
        raise EvaluationError(
            "BAUER_V3_EMBEDDING_DIMENSIONS must be 1024 for the V3 schema"
        )
    return dimensions


def _required_environment(
    name: str,
    *,
    environment: Mapping[str, str] | None = None,
) -> str:
    if not isinstance(name, str) or not name.strip():
        raise EvaluationError("environment variable name is required")
    values = os.environ if environment is None else environment
    value = values.get(name.strip(), "").strip()
    if not value:
        raise EvaluationError(
            f"required environment variable is not set: {name.strip()}"
        )
    return value


def _principal_ids_from_env(
    environment_name: str,
    *,
    environment: Mapping[str, str] | None = None,
) -> tuple[str, ...]:
    raw = _required_environment(
        environment_name,
        environment=environment,
    )
    try:
        values = json.loads(raw)
    except json.JSONDecodeError as error:
        raise EvaluationError(
            f"{environment_name} must be a JSON UUID array"
        ) from error
    if not isinstance(values, list) or not values:
        raise EvaluationError(
            f"{environment_name} must be a non-empty JSON array"
        )
    if any(not isinstance(item, str) or not item.strip() for item in values):
        raise EvaluationError(
            f"{environment_name} must contain UUID strings"
        )
    return tuple(item.strip() for item in values)


def _token_provider(
    *,
    token_file: Path | None,
    token_environment: str,
) -> Callable[[], str]:
    if token_file is not None:
        resolved = token_file.resolve()

        def from_file() -> str:
            return resolved.read_text(encoding="utf-8-sig").strip()

        return from_file

    environment_name = str(token_environment or "").strip()
    if not environment_name:
        raise EvaluationError(
            "authorization token environment variable name is required"
        )

    def from_environment() -> str:
        return os.environ.get(environment_name, "").strip()

    return from_environment


def _write_json_exclusive(path: Path, payload: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("x", encoding="utf-8", newline="\n") as handle:
        json.dump(
            payload,
            handle,
            ensure_ascii=False,
            allow_nan=False,
            indent=2,
            sort_keys=True,
        )
        handle.write("\n")


if __name__ == "__main__":
    raise SystemExit(main())
