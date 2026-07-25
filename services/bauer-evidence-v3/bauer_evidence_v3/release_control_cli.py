"""Command-line release control for Bauer Evidence V3.

Run this module from the compiler image:

    python -m bauer_evidence_v3.release_control_cli --help

Commands are deliberately split so offline manifest and toolchain operations
do not initialize a database connection.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from collections.abc import Callable, Mapping, Sequence
from dataclasses import asdict, dataclass, is_dataclass
from pathlib import Path
from typing import Any, TextIO

from .release_control import (
    CONFIRM_ACTIVE_POINTER_CHANGE,
    RELEASE_EMBEDDING_DIMENSIONS,
    DatabaseControlSettings,
    ReleaseControlError,
    ReleaseController,
    build_object_store_from_environment,
    create_manifest_from_spec,
    default_admin,
    default_bootstrap_admin,
    default_status_reader,
    derive_toolchain_from_environment,
    load_json_file,
    load_verified_manifest,
    toolchain_to_dict,
    write_manifest_once,
)
from .source_manifest import load_source_manifest
from .toolchain import CompilerToolchainIdentity


@dataclass(frozen=True, slots=True)
class CliDependencies:
    """Injectable construction boundary used by tests and operator tooling."""

    admin_factory: Callable[[DatabaseControlSettings], Any] = default_admin
    bootstrap_admin_factory: Callable[[DatabaseControlSettings], Any] = (
        default_bootstrap_admin
    )
    status_reader_factory: Callable[
        [DatabaseControlSettings],
        Any,
    ] = default_status_reader
    object_store_factory: Callable[
        [Mapping[str, str] | None],
        Any,
    ] = build_object_store_from_environment
    toolchain_factory: Callable[..., CompilerToolchainIdentity] = (
        derive_toolchain_from_environment
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="bauer-v3-release",
        description=(
            "Immutable source-manifest and gated Bauer Evidence V3 "
            "release control"
        ),
    )
    commands = parser.add_subparsers(dest="command", required=True)

    toolchain = commands.add_parser(
        "toolchain",
        help="derive and print the running compiler identity",
    )
    _add_toolchain_arguments(toolchain, release=False)

    manifest_create = commands.add_parser(
        "manifest-create",
        help="create an immutable manifest from a JSON source specification",
    )
    manifest_create.add_argument("--spec", required=True, type=Path)
    manifest_create.add_argument("--source-root", required=True, type=Path)
    manifest_create.add_argument("--output", required=True, type=Path)

    manifest_verify = commands.add_parser(
        "manifest-verify",
        help="verify a manifest checksum and every original source byte",
    )
    manifest_verify.add_argument("--manifest", required=True, type=Path)
    manifest_verify.add_argument("--source-root", required=True, type=Path)

    bootstrap = commands.add_parser(
        "bootstrap",
        help="bootstrap the configured tenant, KB, principals, and grants",
    )
    bootstrap.add_argument("--spec", required=True, type=Path)

    release_build = commands.add_parser(
        "release-build",
        help="verify, stage, create, and enqueue one immutable release",
    )
    release_build.add_argument("--manifest", required=True, type=Path)
    release_build.add_argument("--source-root", required=True, type=Path)
    release_build.add_argument("--created-by", required=True)
    release_build.add_argument("--based-on-release-id")
    release_build.add_argument("--prompt-version")
    release_build.add_argument("--priority", type=int, default=0)
    release_build.add_argument("--max-attempts", type=int, default=5)
    release_build.add_argument(
        "--expected-toolchain-fingerprint",
        help=(
            "optional SHA-256 assertion; the fingerprint is still derived "
            "inside this compiler image"
        ),
    )
    _add_toolchain_arguments(release_build, release=True)

    begin_validation = commands.add_parser(
        "release-begin-validation",
        help="run database evidence gates and enter validation",
    )
    _add_release_id(begin_validation)

    mark_ready = commands.add_parser(
        "release-mark-ready",
        help="run database evidence, QA, and verified-gold gates",
    )
    _add_release_id(mark_ready)

    status = commands.add_parser(
        "release-status",
        help="inspect release, active pointer, jobs, QA, and latest eval",
    )
    status.add_argument("--release-id")

    mark_failed = commands.add_parser(
        "release-mark-failed",
        help="mark a non-ready release failed with a terminal error",
    )
    _add_release_id(mark_failed)
    mark_failed.add_argument("--error", required=True)

    replay_dead_letter = commands.add_parser(
        "job-replay-dead-letter",
        help="create one audited idempotent replay of a dead job",
    )
    replay_dead_letter.add_argument("--job-id", required=True)
    replay_dead_letter.add_argument("--idempotency-key", required=True)
    replay_dead_letter.add_argument("--actor-principal-id", required=True)

    retire = commands.add_parser(
        "release-retire",
        help="retire an inactive ready release",
    )
    _add_release_id(retire)

    activate = commands.add_parser(
        "release-activate",
        help="activate a ready release through the production database gate",
    )
    _add_release_id(activate)
    _add_active_pointer_arguments(activate)

    rollback = commands.add_parser(
        "release-rollback",
        help="repoint serving to an earlier ready release",
    )
    rollback.add_argument("--target-release-id", required=True)
    _add_active_pointer_arguments(rollback)
    return parser


def main(
    argv: Sequence[str] | None = None,
    *,
    environment: Mapping[str, str] | None = None,
    stdout: TextIO | None = None,
    dependencies: CliDependencies | None = None,
) -> int:
    arguments = build_parser().parse_args(
        list(sys.argv[1:] if argv is None else argv)
    )
    values = os.environ if environment is None else environment
    output = sys.stdout if stdout is None else stdout
    deps = dependencies or CliDependencies()

    if arguments.command == "toolchain":
        identity = _derive_identity(arguments, values, deps)
        _print_json(toolchain_to_dict(identity), output)
        return 0

    if arguments.command == "manifest-create":
        manifest = create_manifest_from_spec(
            arguments.spec,
            source_root=arguments.source_root,
        )
        target = write_manifest_once(manifest, arguments.output)
        _print_json(
            {
                "manifest_path": str(target),
                "manifest_sha256": manifest.manifest_sha256,
                "release_id": manifest.release_id,
                "source_count": manifest.source_count,
            },
            output,
        )
        return 0

    if arguments.command == "manifest-verify":
        manifest = load_verified_manifest(
            arguments.manifest,
            source_root=arguments.source_root,
        )
        _print_json(
            {
                "verified": True,
                "manifest_sha256": manifest.manifest_sha256,
                "release_id": manifest.release_id,
                "source_count": manifest.source_count,
            },
            output,
        )
        return 0

    settings = DatabaseControlSettings.from_environment(values)
    if arguments.command == "release-status":
        result = deps.status_reader_factory(settings).inspect(
            kb_id=settings.scope.knowledge_base_id,
            release_id=arguments.release_id,
        )
        _print_json(_jsonable(result), output)
        return 0

    admin = (
        deps.bootstrap_admin_factory(settings)
        if arguments.command == "bootstrap"
        else deps.admin_factory(settings)
    )
    controller = ReleaseController(
        scope=settings.scope,
        admin=admin,
    )

    if arguments.command == "bootstrap":
        result = controller.bootstrap(load_json_file(arguments.spec))
    elif arguments.command == "release-build":
        manifest = load_source_manifest(arguments.manifest.read_bytes())
        identity = _derive_identity(arguments, values, deps)
        expected = arguments.expected_toolchain_fingerprint
        if expected is not None and expected.casefold() != identity.fingerprint:
            raise ReleaseControlError(
                "derived toolchain fingerprint does not match "
                "--expected-toolchain-fingerprint"
            )
        object_store = deps.object_store_factory(values)
        result = controller.build_release(
            manifest=manifest,
            source_root=arguments.source_root,
            object_store=object_store,
            toolchain=identity,
            embedding_model_version=_embedding_model(arguments, values),
            created_by=arguments.created_by,
            based_on_release_id=arguments.based_on_release_id,
            fact_model_version=arguments.fact_model_version,
            prompt_version=arguments.prompt_version,
            priority=arguments.priority,
            max_attempts=arguments.max_attempts,
        )
    elif arguments.command == "release-begin-validation":
        result = controller.begin_validation(arguments.release_id)
    elif arguments.command == "release-mark-ready":
        result = controller.mark_ready(arguments.release_id)
    elif arguments.command == "release-mark-failed":
        result = controller.mark_failed(
            arguments.release_id,
            error=arguments.error,
        )
    elif arguments.command == "job-replay-dead-letter":
        result = controller.replay_dead_letter(
            arguments.job_id,
            idempotency_key=arguments.idempotency_key,
            actor_principal_id=arguments.actor_principal_id,
        )
    elif arguments.command == "release-retire":
        result = controller.retire(arguments.release_id)
    elif arguments.command == "release-activate":
        result = controller.activate(
            release_id=arguments.release_id,
            actor_principal_id=arguments.actor_principal_id,
            reason=arguments.reason,
            confirmation=arguments.confirm_active_pointer_change,
        )
    elif arguments.command == "release-rollback":
        result = controller.rollback(
            target_release_id=arguments.target_release_id,
            actor_principal_id=arguments.actor_principal_id,
            reason=arguments.reason,
            confirmation=arguments.confirm_active_pointer_change,
        )
    else:  # pragma: no cover - argparse owns the command vocabulary
        raise ReleaseControlError(
            f"unsupported release-control command: {arguments.command}"
        )

    _print_json(_jsonable(result), output)
    return 0


def _add_toolchain_arguments(
    parser: argparse.ArgumentParser,
    *,
    release: bool,
) -> None:
    parser.add_argument(
        "--embedding-model-version",
        help="defaults to BAUER_V3_EMBEDDING_MODEL",
    )
    parser.add_argument(
        "--embedding-dimensions",
        type=int,
        help="defaults to BAUER_V3_EMBEDDING_DIMENSIONS",
    )
    parser.add_argument("--fact-model-version")
    if release:
        parser.set_defaults(
            embedding_dimensions=RELEASE_EMBEDDING_DIMENSIONS
        )


def _add_release_id(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--release-id", required=True)


def _add_active_pointer_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--actor-principal-id", required=True)
    parser.add_argument("--reason", required=True)
    parser.add_argument(
        "--confirm-active-pointer-change",
        action="store_const",
        const=CONFIRM_ACTIVE_POINTER_CHANGE,
        required=True,
        help=(
            "required acknowledgement that this command changes the "
            "production serving pointer"
        ),
    )


def _derive_identity(
    arguments: argparse.Namespace,
    environment: Mapping[str, str],
    dependencies: CliDependencies,
) -> CompilerToolchainIdentity:
    return dependencies.toolchain_factory(
        embedding_model_version=_embedding_model(arguments, environment),
        embedding_dimensions=_embedding_dimensions(arguments, environment),
        fact_model_version=arguments.fact_model_version,
        environment=environment,
    )


def _embedding_model(
    arguments: argparse.Namespace,
    environment: Mapping[str, str],
) -> str:
    value = arguments.embedding_model_version or environment.get(
        "BAUER_V3_EMBEDDING_MODEL",
        "",
    )
    if not isinstance(value, str) or not value.strip():
        raise ReleaseControlError(
            "--embedding-model-version or BAUER_V3_EMBEDDING_MODEL is required"
        )
    return value.strip()


def _embedding_dimensions(
    arguments: argparse.Namespace,
    environment: Mapping[str, str],
) -> int:
    value = arguments.embedding_dimensions
    if value is None:
        value = environment.get("BAUER_V3_EMBEDDING_DIMENSIONS", "")
    try:
        result = int(value)
    except (TypeError, ValueError) as exc:
        raise ReleaseControlError(
            "--embedding-dimensions or "
            "BAUER_V3_EMBEDDING_DIMENSIONS must be an integer"
        ) from exc
    if result < 1:
        raise ReleaseControlError("embedding dimensions must be positive")
    return result


def _jsonable(value: Any) -> Any:
    to_dict = getattr(value, "to_dict", None)
    if callable(to_dict):
        return _jsonable(to_dict())
    if is_dataclass(value):
        return _jsonable(asdict(value))
    if isinstance(value, Mapping):
        return {str(key): _jsonable(item) for key, item in value.items()}
    if isinstance(value, (tuple, list)):
        return [_jsonable(item) for item in value]
    if isinstance(value, Path):
        return str(value)
    return value


def _print_json(value: Any, output: TextIO) -> None:
    print(
        json.dumps(
            _jsonable(value),
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
        ),
        file=output,
    )


def run() -> int:
    try:
        return main()
    except (ReleaseControlError, ValueError, OSError) as exc:
        print(f"release-control error: {exc}", file=sys.stderr)
        return 2
    except Exception as exc:  # pragma: no cover - deployment dependency failures
        # Do not echo arbitrary driver/client exception text: some libraries
        # include connection options in failures.  The exception type is
        # enough for an operator to correlate with the private execution log.
        print(
            f"release-control operation failed: {type(exc).__name__}",
            file=sys.stderr,
        )
        return 1


if __name__ == "__main__":
    raise SystemExit(run())
