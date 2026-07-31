from __future__ import annotations

import argparse
import importlib.util
from html import escape
from pathlib import Path


REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
CATALOG_PATH = (
    REPOSITORY_ROOT
    / "services"
    / "bauer-twin-api"
    / "bauer_twin"
    / "catalog.py"
)
DEFAULT_OUTPUT = (
    REPOSITORY_ROOT
    / "services"
    / "bauer-evidence-v4"
    / "resources"
    / "bauer-synthetic-demo-v1.html"
)


def _catalog() -> tuple[str, dict]:
    spec = importlib.util.spec_from_file_location("bauer_demo_catalog", CATALOG_PATH)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot load synthetic catalog: {CATALOG_PATH}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.DISCLAIMER, module.build_catalog()


def _cell(value: object) -> str:
    if isinstance(value, list):
        value = "; ".join(str(item) for item in value)
    return escape(str(value))


def build_html() -> str:
    disclaimer, catalog = _catalog()
    parts_by_project: dict[str, list[str]] = {}
    for part in catalog["parts"]:
        for project_id in part.get("project_ids", []):
            parts_by_project.setdefault(project_id, []).append(part["part_id"])
    headers = (
        "project_id",
        "cluster",
        "application_sector",
        "medium",
        "pressure_bar",
        "capacity_l_min",
        "compressor_family",
        "compressor_model",
        "topology",
        "cooling",
        "control_package",
        "purification_package",
        "storage_filling_package",
        "installation",
        "environment",
        "standards",
        "status",
        "document_ids",
        "linked_part_ids",
        "authority_notice",
    )
    rows = []
    for project in sorted(catalog["projects"], key=lambda item: item["project_id"]):
        values = dict(project)
        values["linked_part_ids"] = sorted(parts_by_project[project["project_id"]])
        values["authority_notice"] = disclaimer
        rows.append(
            "<tr>" + "".join(f"<td>{_cell(values.get(key, ''))}</td>" for key in headers) + "</tr>"
        )
    return "\n".join(
        (
            "<!doctype html>",
            '<html lang="en"><head><meta charset="utf-8">',
            "<title>Bauer synthetic demo evidence v1</title></head><body><main>",
            "<h1>Bauer synthetic demo evidence v1</h1>",
            f"<p>{escape(disclaimer)}</p>",
            "<p>This immutable projection is generated from bauer_synthetic_demo_v1. "
            "It records demo relationships only and contains no confirmed Bauer engineering rules.</p>",
            "<table><thead><tr>",
            "".join(f"<th>{escape(key)}</th>" for key in headers),
            "</tr></thead><tbody>",
            *rows,
            "</tbody></table></main></body></html>",
            "",
        )
    )


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    args = parser.parse_args()
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(build_html(), encoding="utf-8", newline="\n")
    print(args.output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
