"""Deterministic synthetic vertical slice for the Bauer demo.

Nothing in this module represents confirmed Bauer internal project or part data.
Public Bauer URLs are evidence links only; the compatibility relationships are
synthetic and exist solely to demonstrate the proposed search workflow.
"""

from __future__ import annotations

from typing import Any

DISCLAIMER = (
    "DEMO DATA: project, part, and compatibility records are synthetic and are not "
    "confirmed Bauer Kompressoren master data. Linked documents are public evidence sources."
)

DOCUMENTS: list[dict[str, Any]] = [
    {
        "document_id": "DOC-BM-40",
        "title": "BM series 40 product page",
        "document_type": "product_page",
        "source_url": "https://www.bauer-kompressoren.de/en_en/bm-series-40",
        "language": "en",
        "local_filename": "bm-series-40 public product page",
        "summary": "Public product information for the BM 40 series.",
    },
    {
        "document_id": "DOC-BM-100",
        "title": "BM series 100 product page",
        "document_type": "product_page",
        "source_url": "https://www.bauer-kompressoren.de/en_en/bm-series-100",
        "language": "en",
        "local_filename": "bm-series-100 public product page",
        "summary": "Public product information for the BM 100 series.",
    },
    {
        "document_id": "DOC-INDUSTRY",
        "title": "Compressors for Industry brochure",
        "document_type": "brochure",
        "source_url": "https://www.bauer-kompressoren.de/en_en/amfile/file/download/file/62/product/54/",
        "language": "en",
        "local_filename": "2026-04_Compressors_for_Industry_EN_N39771_sc.pdf",
        "summary": "Public overview of industrial compressor products and applications.",
    },
    {
        "document_id": "DOC-B-NITROX",
        "title": "B-NITROX brochure",
        "document_type": "brochure",
        "source_url": "https://www.bauer-kompressoren.de/en_en/amfile/file/download/file/27/product/14/",
        "language": "en",
        "local_filename": "2026-04_B-Nitrox_EN_N34460_sc.pdf",
        "summary": "Public B-NITROX product brochure used as nitrogen-related evidence.",
    },
    {
        "document_id": "DOC-PE-NITROX",
        "title": "PE-NITROX product page",
        "document_type": "product_page",
        "source_url": "https://www.bauer-kompressoren.de/en_en/pe-nitrox-membrane",
        "language": "en",
        "local_filename": "pe-nitrox-membrane public product page",
        "summary": "Public product page covering a membrane-based Nitrox system.",
    },
    {
        "document_id": "DOC-BREATHING-AIR",
        "title": "Breathing air sport product overview",
        "document_type": "product_page",
        "source_url": "https://www.bauer-kompressoren.de/en_en/products/breathing-air-sport",
        "language": "en",
        "local_filename": "breathing-air-sport public category page",
        "summary": "Public overview of breathing-air compressor products.",
    },
    {
        "document_id": "DOC-COMPACT-LINE",
        "title": "Compact Line brochure",
        "document_type": "brochure",
        "source_url": "https://www.bauer-kompressoren.de/en_en/amfile/file/download/file/272/product/7/",
        "language": "en",
        "local_filename": "2026-04_Compact_Line_EN_N40564_sc_1.pdf",
        "summary": "Public brochure for compact compressor systems.",
    },
    {
        "document_id": "DOC-PRODUCT-OVERVIEW",
        "title": "BAUER product overview",
        "document_type": "catalogue",
        "source_url": "https://www.bauer-kompressoren.de/en_en/products",
        "language": "en",
        "local_filename": "2025-06_Product_overview_EN_N37488_sc.pdf",
        "summary": "Public cross-product overview used for general product evidence.",
    },
]


def _project(
    project_id: str,
    cluster: str,
    medium: str,
    pressure_bar: int,
    capacity_l_min: int,
    family: str,
    model: str,
    sector: str,
    topology: str,
    cooling: str,
    purification: str,
    documents: list[str],
) -> dict[str, Any]:
    return {
        "project_id": project_id,
        "name": f"Synthetic {cluster} {pressure_bar} bar / {capacity_l_min} l/min",
        "synthetic": True,
        "cluster": cluster,
        "application_sector": sector,
        "medium": medium,
        "pressure_bar": pressure_bar,
        "capacity_l_min": capacity_l_min,
        "compressor_family": family,
        "compressor_model": model,
        "topology": topology,
        "cooling": cooling,
        "control_package": "B-CONTROL MICRO synthetic configuration",
        "purification_package": purification,
        "storage_filling_package": f"Synthetic {pressure_bar}-bar storage and filling package",
        "installation": "indoor skid, service access on three sides",
        "environment": "temperate industrial environment, 5-40 C",
        "standards": ["CE demo assumption", "four-eyes engineering review required"],
        "status": "reference",
        "summary": (
            f"Synthetic historical {sector} project using {model} for {medium} at "
            f"{pressure_bar} bar and {capacity_l_min} l/min, {topology}, {cooling}."
        ),
        "document_ids": documents,
    }


def build_catalog() -> dict[str, list[dict[str, Any]]]:
    projects: list[dict[str, Any]] = []
    for pressure, capacity in ((365, 400), (365, 500), (420, 450), (420, 500)):
        projects.append(
            _project(
                f"SYN-BK-N2-{pressure}-{capacity}",
                "nitrogen booster package",
                "nitrogen",
                pressure,
                capacity,
                "N2 Booster",
                f"SYN-N2B-{pressure}",
                "industrial gas",
                "booster",
                "water-cooled" if capacity >= 500 else "air-cooled",
                "synthetic nitrogen-compatible filtration package",
                ["DOC-B-NITROX", "DOC-INDUSTRY", "DOC-PRODUCT-OVERVIEW"],
            )
        )
    for pressure, capacity in ((40, 500), (40, 800), (100, 500), (100, 800)):
        family = "BM 40" if pressure == 40 else "BM 100"
        projects.append(
            _project(
                f"SYN-BK-AIR-BM{pressure}-{capacity}",
                f"{family} industrial air package",
                "air",
                pressure,
                capacity,
                family,
                f"SYN-{family.replace(' ', '-')}-{capacity}",
                "industrial compressed air",
                "compressor",
                "air-cooled",
                "synthetic industrial air filtration package",
                [f"DOC-BM-{pressure}", "DOC-INDUSTRY", "DOC-PRODUCT-OVERVIEW"],
            )
        )
    for pressure, capacity in ((300, 320), (300, 500), (420, 320), (420, 500)):
        projects.append(
            _project(
                f"SYN-BK-BA-{pressure}-{capacity}",
                "breathing-air filling package",
                "breathing air",
                pressure,
                capacity,
                "Breathing Air Demo",
                f"SYN-BA-{pressure}-{capacity}",
                "breathing air / diving",
                "compressor and filling station",
                "air-cooled",
                "synthetic breathing-air purification and monitoring package",
                ["DOC-BREATHING-AIR", "DOC-COMPACT-LINE", "DOC-PRODUCT-OVERVIEW"],
            )
        )

    category_specs = [
        ("CMP", "compressor block", "compressor, Verdichter, Kompressor"),
        ("CTL", "control package", "controller, Steuerung, control unit"),
        ("PUR", "purification cartridge", "filter cartridge, Filterpatrone, purification"),
        ("VLV", "pressure valve set", "valve, Ventil, safety valve"),
        ("CLG", "cooling kit", "cooler, Kuehlung, cooling system"),
        ("STG", "storage and filling kit", "storage bank, Speicher, filling panel"),
    ]
    parts: list[dict[str, Any]] = []
    for project in projects:
        for code, description, synonyms in category_specs:
            part_id = f"SYN-P-{code}-{project['project_id'][7:]}"
            parts.append(
                {
                    "part_id": part_id,
                    "description": f"Synthetic {description} for {project['name']}",
                    "synthetic": True,
                    "category": code,
                    "lifecycle_status": "demo-active",
                    "synonyms": synonyms.split(", "),
                    "compatible_models": [project["compressor_model"]],
                    "compatible_media": [project["medium"]],
                    "min_pressure_bar": 0,
                    "max_pressure_bar": project["pressure_bar"],
                    "project_ids": [project["project_id"]],
                    "document_ids": project["document_ids"],
                    "summary": f"Synthetic {description}; compatibility is a demo assumption only.",
                }
            )

    common_parts = [
        {
            "part_id": "SYN-P-SNS-PRESSURE-500",
            "description": "Synthetic 0-500 bar pressure transmitter",
            "category": "SNS",
            "synonyms": ["pressure sensor", "Drucksensor", "pressure transmitter"],
            "compatible_media": ["nitrogen", "air", "breathing air"],
            "max_pressure_bar": 500,
        },
        {
            "part_id": "SYN-P-SNS-TEMP-IND",
            "description": "Synthetic industrial discharge temperature sensor",
            "category": "SNS",
            "synonyms": ["temperature sensor", "Temperatursensor", "discharge temperature"],
            "compatible_media": ["nitrogen", "air"],
            "max_pressure_bar": 500,
        },
        {
            "part_id": "SYN-P-SNS-DEWPOINT-BA",
            "description": "Synthetic breathing-air dew-point monitor",
            "category": "SNS",
            "synonyms": ["dew point sensor", "Taupunktsensor", "breathing air monitor"],
            "compatible_media": ["breathing air"],
            "max_pressure_bar": 420,
        },
    ]
    for part in common_parts:
        matching = [
            project["project_id"]
            for project in projects
            if project["medium"] in part["compatible_media"]
            and project["pressure_bar"] <= part["max_pressure_bar"]
        ]
        part.update(
            {
                "synthetic": True,
                "lifecycle_status": "demo-active",
                "compatible_models": [],
                "min_pressure_bar": 0,
                "project_ids": matching,
                "document_ids": ["DOC-PRODUCT-OVERVIEW"],
                "summary": "Synthetic common sensor; compatibility is a demo assumption only.",
            }
        )
        parts.append(part)

    assert len(projects) == 12
    assert len(parts) == 75
    return {"projects": projects, "parts": parts, "documents": DOCUMENTS}


def searchable_text(record: dict[str, Any]) -> str:
    values: list[str] = []
    for key, value in record.items():
        if key in {"synthetic", "document_ids", "project_ids"}:
            continue
        if isinstance(value, list):
            values.extend(str(item) for item in value)
        elif value is not None:
            values.append(str(value))
    return " ".join(values)

