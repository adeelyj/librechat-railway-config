from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import sys

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPOSITORY_ROOT / "services" / "bauer-evidence-v3"))
sys.path.insert(0, str(REPOSITORY_ROOT / "services" / "bauer-evidence-v4"))

from bauer_evidence_v3.object_store import S3ObjectStore  # noqa: E402
from bauer_evidence_v4.deployment.config import V4Settings  # noqa: E402
from bauer_evidence_v4.deployment.worker import _s3_client  # noqa: E402


DEFAULT_SOURCE = (
    REPOSITORY_ROOT
    / "services"
    / "bauer-evidence-v4"
    / "resources"
    / "bauer-synthetic-demo-v1.html"
)
EXPECTED_SHA256 = (
    "d1653edcab0b6dc501230f8d2f077880a3066f087fa5ca937394293d9cbde1ea"
)
EXPECTED_BYTE_SIZE = 12972


def stage(source: Path) -> dict[str, object]:
    payload = source.read_bytes()
    digest = hashlib.sha256(payload).hexdigest()
    if digest != EXPECTED_SHA256 or len(payload) != EXPECTED_BYTE_SIZE:
        raise RuntimeError("synthetic source is not the reviewed immutable fixture")
    if b"not confirmed Bauer Kompressoren master data" not in payload:
        raise RuntimeError("synthetic authority boundary is absent")

    settings = V4Settings.from_environment(service_role="worker")
    store = S3ObjectStore(
        bucket=settings.source_s3_bucket,
        client=_s3_client(settings),
        prefix=settings.source_s3_prefix,
    )
    stored = store.put(payload, media_type="text/html", suffix=".html")
    verified = store.get(stored.object_key)
    if verified != payload:
        raise RuntimeError("synthetic source read-after-write verification failed")
    return {
        "schema_version": "bauer-rag-v4-synthetic-source-stage-v1",
        "external_source_id": "synthetic-demo-v1",
        "original_filename": "bauer-synthetic-demo-v1.html",
        "content_sha256": stored.sha256,
        "byte_size": stored.byte_size,
        "media_type": stored.media_type,
        "object_key": stored.object_key,
        "read_after_write_verified": True,
        "v3_release_membership_mutated": False,
        "locked_holdout_opened": False,
        "credentials_or_connection_details_emitted": False,
    }


def main() -> int:
    try:
        parser = argparse.ArgumentParser()
        parser.add_argument("--source", type=Path, default=DEFAULT_SOURCE)
        parser.add_argument("--output", type=Path, required=True)
        args = parser.parse_args()
        result = stage(args.source.resolve())
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(
            json.dumps(result, sort_keys=True, indent=2) + "\n",
            encoding="utf-8",
            newline="\n",
        )
        print(
            json.dumps(
                {
                    "output_path": str(args.output),
                    "content_sha256": result["content_sha256"],
                    "read_after_write_verified": True,
                    "credentials_or_connection_details_emitted": False,
                },
                sort_keys=True,
                separators=(",", ":"),
            )
        )
        return 0
    except Exception as error:
        print(
            json.dumps(
                {
                    "error_type": type(error).__name__,
                    "error_fingerprint": hashlib.sha256(
                        f"{type(error).__name__}:{error}".encode(
                            "utf-8", errors="replace"
                        )
                    ).hexdigest(),
                    "credentials_or_connection_details_emitted": False,
                },
                sort_keys=True,
                separators=(",", ":"),
            )
        )
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
