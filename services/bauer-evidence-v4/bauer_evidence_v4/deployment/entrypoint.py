from __future__ import annotations

import logging
import signal
import sys

from bauer_evidence_v3.config import Settings as V3Settings
from bauer_evidence_v3.migrations import run_migrations as run_v3_migrations
from bauer_evidence_v3.runtime import build_api_app
from bauer_evidence_v3.telemetry import configure_logging

from .api import create_router, load_release
from .config import V4Settings
from .migrations import run_migrations as run_v4_migrations
from .worker import V4CompilerWorker


LOGGER = logging.getLogger(__name__)


def main() -> int:
    if len(sys.argv) != 2 or sys.argv[1] not in {
        "api",
        "worker",
        "migrate",
    }:
        raise SystemExit("usage: entrypoint.py api|worker|migrate")
    command = sys.argv[1]
    configure_logging()
    v3 = V3Settings.from_environment(service_role_override=command)
    v4 = V4Settings.from_environment(service_role=command)
    if command == "migrate":
        v3_report = run_v3_migrations(v3.database_url)
        v4_report = run_v4_migrations(v4.database_url)
        LOGGER.info(
            "combined migrations complete",
            extra={
                "stage": "migrate",
                "outcome": (
                    f"v3={v3_report.current_version},"
                    f"v4={v4_report.current_version}"
                ),
            },
        )
        return 0
    if command == "worker":
        worker = V4CompilerWorker(v4)

        def stop(signum, _frame):
            LOGGER.info(
                "V4 worker stop requested",
                extra={"stage": "worker", "outcome": f"signal={signum}"},
            )
            worker.stop()

        signal.signal(signal.SIGTERM, stop)
        if hasattr(signal, "SIGINT"):
            signal.signal(signal.SIGINT, stop)
        worker.run_forever()
        return 0

    app = build_api_app(v3)
    loaded = load_release(v4)
    app.include_router(create_router(v4, loaded))
    import uvicorn

    uvicorn.run(
        app,
        host="0.0.0.0",
        port=v3.port,
        server_header=False,
        proxy_headers=True,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
