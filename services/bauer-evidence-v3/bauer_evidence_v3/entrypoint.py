from __future__ import annotations

import logging
import signal
import sys
from collections.abc import Sequence

from .config import ConfigurationError, Settings
from .migrations import run_migrations
from .runtime import build_api_app, build_compiler_worker
from .telemetry import configure_logging


LOGGER = logging.getLogger(__name__)
SUPPORTED_COMMANDS = frozenset({"api", "worker", "migrate"})


def main(argv: Sequence[str] | None = None) -> int:
    arguments = list(sys.argv[1:] if argv is None else argv)
    command = arguments.pop(0).strip().casefold() if arguments else None
    if arguments:
        raise ConfigurationError("unexpected command arguments")
    if command is not None and command not in SUPPORTED_COMMANDS:
        raise ConfigurationError(f"unsupported V3 service command: {command}")

    settings = Settings.from_environment(service_role_override=command)
    configure_logging()
    selected = command or settings.service_role
    LOGGER.info(
        "starting Bauer Evidence V3",
        extra={"stage": selected, "outcome": settings.public_summary()},
    )

    if selected == "migrate":
        report = run_migrations(settings.database_url)
        LOGGER.info(
            "database migrations complete",
            extra={
                "stage": "migrate",
                "outcome": (
                    f"version={report.current_version},"
                    f"applied={','.join(str(value) for value in report.applied_versions) or 'none'}"
                ),
            },
        )
        return 0
    if selected == "worker":
        return _run_worker(settings)

    import uvicorn

    uvicorn.run(
        build_api_app(settings),
        host="0.0.0.0",
        port=settings.port,
        server_header=False,
        proxy_headers=True,
    )
    return 0


def _run_worker(settings: Settings) -> int:
    worker = build_compiler_worker(settings)

    def stop_worker(signum, _frame) -> None:
        LOGGER.info(
            "compiler worker stop requested",
            extra={"stage": "worker", "outcome": f"signal={signum}"},
        )
        worker.stop()

    signal.signal(signal.SIGTERM, stop_worker)
    if hasattr(signal, "SIGINT"):
        signal.signal(signal.SIGINT, stop_worker)
    try:
        worker.run_forever()
    except KeyboardInterrupt:
        worker.stop()
    LOGGER.info(
        "compiler worker stopped",
        extra={"stage": "worker", "outcome": "stopped"},
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
