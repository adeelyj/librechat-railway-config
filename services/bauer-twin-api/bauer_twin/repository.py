from __future__ import annotations

import os
from typing import Any

from psycopg import connect
from psycopg.errors import UndefinedTable
from psycopg.rows import dict_row

from .catalog import build_catalog
from .terminology import terminology_rows


class CatalogRepository:
    def __init__(self) -> None:
        self.database_url = os.getenv("DATABASE_URL", "")
        self.require_database = os.getenv("BAUER_TWIN_REQUIRE_DATABASE", "false").lower() == "true"

    @property
    def mode(self) -> str:
        return "postgresql" if self.database_url else "in-memory"

    def load(self) -> dict[str, list[dict[str, Any]]]:
        if not self.database_url:
            if self.require_database:
                raise RuntimeError("DATABASE_URL is required")
            return build_catalog()

        with connect(self.database_url, row_factory=dict_row) as connection:
            projects = connection.execute(
                "SELECT * FROM bauer_twin.projects ORDER BY project_id"
            ).fetchall()
            parts = connection.execute(
                "SELECT * FROM bauer_twin.parts ORDER BY part_id"
            ).fetchall()
            documents = connection.execute(
                "SELECT * FROM bauer_twin.documents ORDER BY document_id"
            ).fetchall()
        if self.require_database and (len(projects) != 12 or len(parts) != 75):
            raise RuntimeError(
                f"Bauer Twin seed is incomplete: projects={len(projects)}, parts={len(parts)}"
            )
        return {
            "projects": [dict(item) for item in projects],
            "parts": [dict(item) for item in parts],
            "documents": [dict(item) for item in documents],
        }

    def load_terminology(self) -> list[dict[str, Any]]:
        if not self.database_url:
            return terminology_rows()

        try:
            with connect(self.database_url, row_factory=dict_row) as connection:
                rows = connection.execute(
                    """
                    SELECT domain, canonical_value, alias, language, query_safe
                    FROM bauer_twin.terminology_aliases
                    ORDER BY domain, alias
                    """
                ).fetchall()
        except UndefinedTable:
            # Supports a safe rolling deployment before the additive seed migration runs.
            return terminology_rows()
        return [dict(item) for item in rows] or terminology_rows()

    def health(self) -> dict[str, Any]:
        catalog = self.load()
        return {
            "database_mode": self.mode,
            "projects": len(catalog["projects"]),
            "parts": len(catalog["parts"]),
            "documents": len(catalog["documents"]),
            "terminology_aliases": len(self.load_terminology()),
        }

