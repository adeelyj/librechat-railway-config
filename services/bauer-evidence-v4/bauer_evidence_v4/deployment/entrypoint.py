from __future__ import annotations

import logging
import signal
import sys

from bauer_evidence_v3.config import Settings as V3Settings
from bauer_evidence_v3.runtime import build_api_app
from bauer_evidence_v3.telemetry import configure_logging

from .api import create_router, load_release
from .config import V4Settings
from .migrations import run_migrations as run_v4_migrations
from .worker import V4CompilerWorker


LOGGER = logging.getLogger(__name__)


def _build_ocr_fallback(settings: V3Settings):
    if not settings.ocr_enabled:
        return None
    if (
        settings.ocr_detection_model is None
        or settings.ocr_recognition_model is None
        or settings.ocr_classification_model is None
    ):
        raise RuntimeError("enabled OCR is missing a pinned model path")
    from bauer_evidence_v3.ingest.ocr import RapidOcrEngine
    from bauer_evidence_v3.ingest.pipeline import Compiler
    from bauer_evidence_v3.ingest.render import PdfPlumberPageRenderer

    return Compiler(
        ocr_engine=RapidOcrEngine(
            detection_model=settings.ocr_detection_model,
            detection_model_sha256=settings.ocr_detection_model_sha256,
            recognition_model=settings.ocr_recognition_model,
            recognition_model_sha256=settings.ocr_recognition_model_sha256,
            classification_model=settings.ocr_classification_model,
            classification_model_sha256=(
                settings.ocr_classification_model_sha256
            ),
        ),
        page_renderer=PdfPlumberPageRenderer(),
        ocr_languages=settings.ocr_languages,
        ocr_minimum_confidence=settings.ocr_minimum_confidence,
        ocr_render_dpi=settings.ocr_render_dpi,
    )


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
        import psycopg

        with psycopg.connect(v3.database_url, autocommit=True) as connection:
            v3_version = connection.execute(
                """
                SELECT coalesce(max(version), 0)
                FROM bauer_rag_v3.schema_migrations
                """
            ).fetchone()[0]
        if int(v3_version) != v3.expected_migration_version:
            raise RuntimeError(
                "frozen V3 schema is not at its expected version"
            )
        v4_report = run_v4_migrations(v4.database_url)
        LOGGER.info(
            "combined migrations complete",
            extra={
                "stage": "migrate",
                "outcome": (
                    f"v3={v3_version},"
                    f"v4={v4_report.current_version}"
                ),
            },
        )
        return 0
    if command == "worker":
        worker = V4CompilerWorker(
            v4,
            ocr_compiler=_build_ocr_fallback(v3),
        )

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
