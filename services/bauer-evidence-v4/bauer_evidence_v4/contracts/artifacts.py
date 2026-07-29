"""Deterministically render checked-in JSON Schema and OpenAPI contracts."""

from __future__ import annotations

import copy
import json
from pathlib import Path
from typing import Any

from .models import (
    AuthorizationClaims,
    EvidenceUnitContract,
    ReleaseContract,
    V4AnswerRequest,
    V4AnswerResponse,
)


SCHEMA_MODELS = {
    "authorization-claims.schema.json": AuthorizationClaims,
    "evidence-unit.schema.json": EvidenceUnitContract,
    "release.schema.json": ReleaseContract,
    "request.schema.json": V4AnswerRequest,
    "response.schema.json": V4AnswerResponse,
}


def render_json_schemas() -> dict[str, dict[str, Any]]:
    rendered: dict[str, dict[str, Any]] = {}
    for filename, model in SCHEMA_MODELS.items():
        schema = model.model_json_schema(mode="validation")
        schema["$schema"] = "https://json-schema.org/draft/2020-12/schema"
        schema["$id"] = f"https://bauer.example/contracts/v4/{filename}"
        rendered[filename] = schema
    return rendered


def _rewrite_refs(value: Any) -> Any:
    if isinstance(value, dict):
        return {
            key: (
                item.replace("#/$defs/", "#/components/schemas/")
                if key == "$ref" and isinstance(item, str)
                else _rewrite_refs(item)
            )
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [_rewrite_refs(item) for item in value]
    return value


def render_openapi() -> dict[str, Any]:
    components: dict[str, Any] = {}
    for model in (
        AuthorizationClaims,
        EvidenceUnitContract,
        ReleaseContract,
        V4AnswerRequest,
        V4AnswerResponse,
    ):
        schema = copy.deepcopy(model.model_json_schema(mode="validation"))
        definitions = schema.pop("$defs", {})
        components.update(_rewrite_refs(definitions))
        components[model.__name__] = _rewrite_refs(schema)
    return {
        "openapi": "3.1.0",
        "info": {
            "title": "Bauer Evidence V4",
            "version": "4.0.0",
            "description": (
                "Host-neutral evidence and answer contract. The original question is "
                "authoritative; search_hint is optional derived retrieval input."
            ),
        },
        "paths": {
            "/v4/answer": {
                "post": {
                    "operationId": "answerBauerEvidenceV4",
                    "requestBody": {
                        "required": True,
                        "content": {
                            "application/json": {
                                "schema": {
                                    "$ref": "#/components/schemas/V4AnswerRequest"
                                }
                            }
                        },
                    },
                    "responses": {
                        "200": {
                            "description": "Validated complete, partial, not-found, or refused response",
                            "content": {
                                "application/json": {
                                    "schema": {
                                        "$ref": "#/components/schemas/V4AnswerResponse"
                                    }
                                }
                            },
                        },
                        "400": {"description": "Invalid host-neutral request"},
                        "401": {"description": "Invalid or expired signed scope"},
                        "403": {"description": "Scope does not authorize this knowledge base"},
                        "409": {"description": "Pinned candidate release is unavailable"},
                        "503": {"description": "Required local dependency is unavailable"},
                    },
                }
            }
        },
        "components": {"schemas": components},
    }


def write_contract_artifacts(destination: Path) -> list[Path]:
    destination.mkdir(parents=True, exist_ok=True)
    written: list[Path] = []
    for filename, payload in {
        **render_json_schemas(),
        "openapi.json": render_openapi(),
    }.items():
        path = destination / filename
        path.write_text(
            json.dumps(payload, indent=2, sort_keys=True, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )
        written.append(path)
    return written
