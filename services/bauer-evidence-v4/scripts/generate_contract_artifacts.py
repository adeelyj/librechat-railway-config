"""Generate the checked-in V4 OpenAPI and JSON Schema artifacts."""

from __future__ import annotations

from pathlib import Path

from bauer_evidence_v4.contracts.artifacts import write_contract_artifacts


def main() -> int:
    destination = Path(__file__).resolve().parents[1] / "contracts" / "schemas"
    for path in write_contract_artifacts(destination):
        print(path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
