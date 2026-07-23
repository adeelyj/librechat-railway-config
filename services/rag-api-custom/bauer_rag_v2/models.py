from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field


class QueryV2Body(BaseModel):
    query: str = Field(min_length=1, max_length=4000)
    file_ids: list[str]
    entity_id: str | None = None
    k: int = 8
    index_version: str | None = None
    debug: bool = False


class StartIndexRunBody(BaseModel):
    namespace: str = Field(min_length=1, max_length=200)
    index_version: str = Field(min_length=1, max_length=120)
    expected_file_count: int = Field(gt=0, le=1000)
    extractor_version: str | None = None
    embedding_version: str = Field(min_length=1, max_length=200)
    metadata: dict[str, Any] = Field(default_factory=dict)


class ValidateV2Body(BaseModel):
    answer: str = Field(min_length=1, max_length=100000)
    evidence: list[dict[str, Any]]
    mandatory_constraints: dict[str, Any] = Field(default_factory=dict)
    safe_refusal_expected: bool = False
