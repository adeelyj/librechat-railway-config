"""Versioned request, response, evidence, release, and authorization contracts.

This module deliberately contains no LibreChat, ONIX, persistence, or model-client
imports. Host adapters construct these values; V4 core services consume them.
"""

from __future__ import annotations

from datetime import datetime
from enum import Enum
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class ContractModel(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        str_strip_whitespace=True,
        use_enum_values=True,
    )


class ConversationTurn(ContractModel):
    role: Literal["user", "assistant"]
    content: str = Field(min_length=1, max_length=4000)


class ClientContext(ContractModel):
    type: Literal["librechat", "onix", "reference"]
    instance: str = Field(min_length=1, max_length=128, pattern=r"^[A-Za-z0-9._:-]+$")


class TypedConstraint(ContractModel):
    field: str = Field(min_length=1, max_length=128, pattern=r"^[a-z][a-z0-9_]*$")
    operator: Literal["eq", "neq", "in", "not_in", "gte", "lte"]
    value: str | int | float | bool | list[str | int | float | bool]
    mandatory: bool = True


class AuthorizationEnvelope(ContractModel):
    signed_scope: str = Field(min_length=32, max_length=16384)


class V4AnswerRequest(ContractModel):
    schema_version: Literal["4.0"] = "4.0"
    request_id: str = Field(min_length=8, max_length=128, pattern=r"^[A-Za-z0-9._:-]+$")
    question: str = Field(min_length=1, max_length=4000)
    search_hint: str | None = Field(default=None, max_length=2000)
    conversation_context: list[ConversationTurn] = Field(default_factory=list, max_length=12)
    locale: str = Field(default="de-DE", pattern=r"^[A-Za-z]{2}(?:-[A-Za-z0-9]{2,8})?$")
    client: ClientContext
    authorization: AuthorizationEnvelope
    typed_constraints: list[TypedConstraint] = Field(default_factory=list, max_length=32)

    @field_validator("question")
    @classmethod
    def question_must_contain_text(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("question must contain non-whitespace text")
        return value

    @field_validator("search_hint")
    @classmethod
    def empty_hint_is_none(cls, value: str | None) -> str | None:
        if value is None:
            return None
        return value if value.strip() else None

    @property
    def immutable_original_question(self) -> str:
        """Return the only text allowed to define answer requirements."""

        return self.question


class CoverageState(str, Enum):
    SUPPORTED = "supported"
    AMBIGUOUS = "ambiguous"
    CONTRADICTED = "contradicted"
    ABSENT = "absent"
    REFUSED = "refused"


class AnswerStatus(str, Enum):
    COMPLETE = "complete"
    PARTIAL = "partial"
    NOT_FOUND = "not_found"
    REFUSED = "refused"


class SourceCoordinate(ContractModel):
    source_id: str = Field(min_length=1, max_length=256)
    source_version_id: str = Field(min_length=1, max_length=256)
    page: int | None = Field(default=None, ge=1)
    printed_page: str | None = Field(default=None, max_length=64)
    section_path: list[str] = Field(default_factory=list, max_length=32)
    table_id: str | None = Field(default=None, max_length=256)
    row_index: int | None = Field(default=None, ge=0)
    column_index: int | None = Field(default=None, ge=0)
    source_span: str | None = Field(default=None, max_length=512)


class DocumentMetadataContract(ContractModel):
    original_filename: str = Field(min_length=1, max_length=1024)
    external_file_id: str | None = Field(default=None, max_length=256)
    document_number: str | None = Field(default=None, max_length=256)
    title: str = Field(min_length=1, max_length=2048)
    language: str | None = Field(default=None, max_length=32)
    subject: str | None = Field(default=None, max_length=1024)
    revision: str | None = Field(default=None, max_length=256)
    publication_date: str | None = Field(default=None, max_length=64)
    source_type: Literal["pdf", "html", "text", "markdown", "other"]
    page_count: int | None = Field(default=None, ge=1)
    source_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    authority: Literal["original", "derived_verified"]


class CitationContract(ContractModel):
    citation_id: str = Field(min_length=1, max_length=256)
    evidence_id: str = Field(min_length=1, max_length=256)
    source_title: str = Field(min_length=1, max_length=2048)
    original_filename: str = Field(min_length=1, max_length=1024)
    document_number: str | None = Field(default=None, max_length=256)
    coordinate: SourceCoordinate
    table_title: str | None = Field(default=None, max_length=2048)
    header_path: list[str] = Field(default_factory=list, max_length=32)
    raw_value: str | None = Field(default=None, max_length=2048)
    normalized_value: float | int | None = None
    raw_unit: str | None = Field(default=None, max_length=128)
    normalized_unit: str | None = Field(default=None, max_length=128)
    qualifier: str | None = Field(default=None, max_length=256)
    footnotes: list[str] = Field(default_factory=list, max_length=32)
    excerpt: str = Field(min_length=1, max_length=8000)


class EvidenceUnitContract(ContractModel):
    evidence_id: str = Field(min_length=1, max_length=256)
    evidence_type: Literal["document_metadata", "passage", "table_row", "typed_fact"]
    release_public_id: str = Field(min_length=1, max_length=256)
    coordinate: SourceCoordinate
    search_text: str = Field(min_length=1, max_length=16000)
    metadata: DocumentMetadataContract
    citation: CitationContract
    canonical_record_ids: list[str] = Field(min_length=1, max_length=128)
    authorization_source_id: str = Field(min_length=1, max_length=256)

    @model_validator(mode="after")
    def citation_must_address_this_evidence(self) -> "EvidenceUnitContract":
        if self.citation.evidence_id != self.evidence_id:
            raise ValueError("citation evidence_id must match evidence unit")
        if self.citation.coordinate.source_version_id != self.coordinate.source_version_id:
            raise ValueError("citation and evidence must address one source version")
        return self


class CoverageItem(ContractModel):
    field: str = Field(min_length=1, max_length=256)
    state: CoverageState
    required: bool = True
    citation_ids: list[str] = Field(default_factory=list, max_length=64)
    detail: str | None = Field(default=None, max_length=1000)

    @model_validator(mode="after")
    def supported_fields_require_citations(self) -> "CoverageItem":
        if self.state == CoverageState.SUPPORTED.value and not self.citation_ids:
            raise ValueError("supported coverage requires at least one citation")
        if self.state in {CoverageState.ABSENT.value, CoverageState.REFUSED.value} and self.citation_ids:
            raise ValueError("absent/refused coverage cannot cite supporting evidence")
        return self


class ReleaseContract(ContractModel):
    public_id: str = Field(min_length=1, max_length=256)
    source_contract_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    canonical_schema_version: str = Field(min_length=1, max_length=64)
    compiler_identity: str = Field(pattern=r"^[0-9a-f]{64}$")
    projection_identity: str = Field(pattern=r"^[0-9a-f]{64}$")
    embedding_identity: str = Field(pattern=r"^[0-9a-f]{64}$")
    reranker_identity: str = Field(pattern=r"^[0-9a-f]{64}$")
    gate_manifest_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")


class V4AnswerResponse(ContractModel):
    schema_version: Literal["4.0"] = "4.0"
    request_id: str = Field(min_length=8, max_length=128, pattern=r"^[A-Za-z0-9._:-]+$")
    status: AnswerStatus
    answer: str = Field(min_length=1, max_length=32000)
    coverage: list[CoverageItem] = Field(default_factory=list, max_length=128)
    not_found: list[str] = Field(default_factory=list, max_length=128)
    citations: list[CitationContract] = Field(default_factory=list, max_length=128)
    release: ReleaseContract
    trace_id: str = Field(min_length=8, max_length=128, pattern=r"^[A-Za-z0-9._:-]+$")
    validation: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="after")
    def status_matches_coverage(self) -> "V4AnswerResponse":
        missing = {
            item.field
            for item in self.coverage
            if item.required and item.state != CoverageState.SUPPORTED.value
        }
        cited = {citation.citation_id for citation in self.citations}
        unknown = {
            citation_id
            for item in self.coverage
            for citation_id in item.citation_ids
            if citation_id not in cited
        }
        if unknown:
            raise ValueError("coverage references unknown citation IDs")
        if self.status == AnswerStatus.COMPLETE.value and (missing or self.not_found):
            raise ValueError("complete response cannot have missing required fields")
        if self.status == AnswerStatus.PARTIAL.value and not missing:
            raise ValueError("partial response requires a missing, ambiguous, or contradicted field")
        if self.status == AnswerStatus.NOT_FOUND.value and any(
            item.state == CoverageState.SUPPORTED.value for item in self.coverage
        ):
            raise ValueError("not_found response cannot contain supported fields")
        if set(self.not_found) - {
            item.field for item in self.coverage if item.state == CoverageState.ABSENT.value
        }:
            raise ValueError("not_found entries must correspond to absent coverage")
        return self


class AuthorizationClaims(ContractModel):
    schema_version: Literal["4.0"] = "4.0"
    issuer: str = Field(min_length=1, max_length=256)
    audience: str = Field(min_length=1, max_length=256)
    client: ClientContext
    principal_id: str = Field(min_length=1, max_length=256)
    tenant_id: str = Field(min_length=1, max_length=256)
    knowledge_base_id: str = Field(min_length=1, max_length=256)
    authorized_external_source_ids: list[str] = Field(min_length=1, max_length=1000)
    issued_at: datetime
    expires_at: datetime
    nonce: str = Field(min_length=16, max_length=256)

    @field_validator("authorized_external_source_ids")
    @classmethod
    def source_scope_is_unique(cls, value: list[str]) -> list[str]:
        if any(not item.strip() for item in value):
            raise ValueError("authorized source IDs cannot be blank")
        if len(value) != len(set(value)):
            raise ValueError("authorized source IDs must be unique")
        return value

    @model_validator(mode="after")
    def expiry_follows_issue(self) -> "AuthorizationClaims":
        if self.expires_at <= self.issued_at:
            raise ValueError("authorization expiry must follow issue time")
        return self
