from __future__ import annotations

import json
import os
import secrets
from typing import Literal

from mcp.server.fastmcp import FastMCP
from starlette.requests import Request
from starlette.responses import JSONResponse, Response

from bauer_twin.engine import SearchEngine
from bauer_twin.repository import CatalogRepository

SERVER_INSTRUCTIONS = """
This server exposes a synthetic Bauer Technical Twin demo. Use search_bauer_twin for
historical-project similarity, exact/semantic part lookup, document lookup, project
comparison, and details. Treat medium and pressure exclusions as hard constraints.
Pass mandatory business constraints explicitly. Medium, topology, family, and category
accept open text and are normalized by the service. A no_compatible_match or
unknown_constraint result is final; do not retry by removing a mandatory constraint.
Always preserve the returned synthetic-data disclaimer. Use LibreChat file_search
separately when a user needs quotations or page-level evidence from the uploaded corpus.
""".strip()

mcp = FastMCP(
    "Bauer Technical Twin",
    instructions=SERVER_INSTRUCTIONS,
    host="0.0.0.0",
    streamable_http_path="/mcp",
    json_response=True,
    stateless_http=True,
)
repository = CatalogRepository()
engine = SearchEngine(repository=repository)


@mcp.tool()
def search_bauer_twin(
    action: Literal[
        "search_similar_projects",
        "compare_projects",
        "search_parts",
        "search_documents",
        "get_project_details",
        "get_part_details",
    ],
    query: str = "",
    project_id: str = "",
    compare_project_id: str = "",
    part_id: str = "",
    medium: str | None = None,
    target_pressure_bar: float | None = None,
    capacity_l_min: float | None = None,
    compressor_family: str | None = None,
    topology: str | None = None,
    category: str | None = None,
    limit: int = 5,
) -> dict:
    """Search the synthetic Bauer Technical Twin demo.

    Choose one action. For similar projects or parts, provide a natural-language
    query and any known technical filters. Business vocabulary accepts open text;
    known English, German, abbreviation, and chemical-symbol aliases are normalized
    deterministically. Unknown values return unknown_constraint instead of a schema
    error. Medium, target pressure, and topology are hard compatibility constraints;
    a part/project below the required pressure is excluded. A no_compatible_match or
    unknown_constraint result is final and must not be retried with weaker constraints.
    Exact SYN project/part IDs bypass fuzzy ranking when compatible. To compare two
    projects, provide project_id and compare_project_id. Detail actions require the
    corresponding ID. Returned public URLs are evidence links, while all project,
    part, and compatibility records remain explicitly synthetic demo data.
    """
    arguments = {
        "query": query,
        "project_id": project_id,
        "compare_project_id": compare_project_id,
        "part_id": part_id,
        "medium": medium,
        "target_pressure_bar": target_pressure_bar,
        "capacity_l_min": capacity_l_min,
        "compressor_family": compressor_family,
        "topology": topology,
        "category": category,
        "limit": limit,
    }
    if action in {"compare_projects", "get_project_details"} and not project_id:
        raise ValueError(f"project_id is required for {action}")
    if action == "compare_projects" and not compare_project_id:
        raise ValueError("compare_project_id is required for compare_projects")
    if action == "get_part_details" and not part_id:
        raise ValueError("part_id is required for get_part_details")
    if action.startswith("search_") and action != "search_documents" and not query:
        raise ValueError(f"query is required for {action}")
    if action == "search_documents" and not query:
        raise ValueError("query is required for search_documents")
    allowed = {
        "search_similar_projects": {"query", "medium", "target_pressure_bar", "capacity_l_min", "compressor_family", "topology", "limit"},
        "compare_projects": {"project_id", "compare_project_id"},
        "search_parts": {"query", "medium", "target_pressure_bar", "compressor_family", "category", "limit"},
        "search_documents": {"query", "limit"},
        "get_project_details": {"project_id"},
        "get_part_details": {"part_id"},
    }[action]
    return engine.execute(action, **{key: value for key, value in arguments.items() if key in allowed and value not in (None, "")})


@mcp.custom_route("/health", methods=["GET"])
async def health(_: Request) -> Response:
    try:
        details = repository.health()
        return JSONResponse({"status": "ok", "service": "bauer-twin-api", **details})
    except Exception as exc:
        return JSONResponse({"status": "error", "error": str(exc)}, status_code=503)


@mcp.custom_route("/v1/search", methods=["POST"])
async def http_search(request: Request) -> Response:
    try:
        payload = await request.json()
        action = payload.pop("action")
        return JSONResponse(engine.execute(action, **payload))
    except Exception as exc:
        return JSONResponse({"error": str(exc)}, status_code=400)


class BearerAuthMiddleware:
    def __init__(self, app) -> None:
        self.app = app
        self.token = os.getenv("BAUER_TWIN_API_KEY", "")
        self.allow_unauthenticated = os.getenv("BAUER_TWIN_ALLOW_UNAUTHENTICATED", "false").lower() == "true"
        if not self.token and not self.allow_unauthenticated:
            raise RuntimeError("BAUER_TWIN_API_KEY is required")

    async def __call__(self, scope, receive, send) -> None:
        path = scope.get("path", "")
        if scope.get("type") == "http" and path != "/health" and not self.allow_unauthenticated:
            headers = {key.decode("latin-1").lower(): value.decode("latin-1") for key, value in scope.get("headers", [])}
            expected = f"Bearer {self.token}"
            if not secrets.compare_digest(headers.get("authorization", ""), expected):
                response = JSONResponse({"error": "unauthorized"}, status_code=401)
                await response(scope, receive, send)
                return
        await self.app(scope, receive, send)


def create_app():
    return BearerAuthMiddleware(mcp.streamable_http_app())


app = create_app()
