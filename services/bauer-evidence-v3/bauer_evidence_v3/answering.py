from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Protocol

from .auth import AuthorizationContext
from .evidence import EvidencePackage, build_evidence_package
from .generation import GenerationRequest, ModelGateway
from .planner import QueryPlan, analyze_query
from .retrieval import RetrievalResult
from .validation import ValidationResult, validate_answer


@dataclass(frozen=True, slots=True)
class AnswerResult:
    status: str
    answer: str
    release_id: str
    plan: QueryPlan
    evidence: EvidencePackage
    validation: ValidationResult | None
    repair_attempted: bool


@dataclass(frozen=True, slots=True)
class RetrievalRun:
    release_id: str
    plan: QueryPlan
    results: tuple[RetrievalResult, ...]


class PinnedReleaseLike(Protocol):
    release_id: str
    tenant_id: str


class ReleaseRegistry(Protocol):
    def pin_active(self, knowledge_base_id: str) -> PinnedReleaseLike: ...


class EvidenceIndex(Protocol):
    def retrieve(
        self,
        plan: QueryPlan,
        *,
        release_id: str,
        authorized_source_ids: set[str] | frozenset[str],
    ) -> tuple[RetrievalResult, ...]: ...


class AnswerService:
    def __init__(
        self,
        *,
        releases: ReleaseRegistry,
        index: EvidenceIndex,
        model: ModelGateway,
    ) -> None:
        self.releases = releases
        self.index = index
        self.model = model

    async def answer(
        self,
        *,
        authorization: AuthorizationContext,
        question: str,
        mandatory_constraints: dict[str, object] | None = None,
        forbidden_claim_values: tuple[str, ...] = (),
        top_k: int = 8,
    ) -> AnswerResult:
        plan = analyze_query(
            question,
            mandatory_constraints=mandatory_constraints,
            forbidden_claim_values=forbidden_claim_values,
            top_k=top_k,
        )
        release_id, retrieved = await asyncio.to_thread(
            self._retrieve_plan,
            authorization,
            plan,
        )
        authorized_sources = frozenset(authorization.authorized_source_ids)
        package = build_evidence_package(
            question=question,
            release_id=release_id,
            tenant_id=authorization.tenant_id,
            knowledge_base_id=authorization.knowledge_base_id,
            results=retrieved,
            authorized_source_ids=authorized_sources,
        )
        if not package.citations:
            return AnswerResult(
                status="refused_no_evidence",
                answer=(
                    "I cannot confirm the answer because no authorized evidence was found in "
                    f"knowledge release {release_id}."
                ),
                release_id=release_id,
                plan=plan,
                evidence=package,
                validation=None,
                repair_attempted=False,
            )

        first_answer = await self.model.generate(
            GenerationRequest(question=question, plan=plan, evidence=package)
        )
        first_validation = validate_answer(answer=first_answer, package=package, plan=plan)
        if first_validation.valid:
            return AnswerResult(
                status="answered",
                answer=first_answer,
                release_id=release_id,
                plan=plan,
                evidence=package,
                validation=first_validation,
                repair_attempted=False,
            )

        repaired_answer = await self.model.generate(
            GenerationRequest(
                question=question,
                plan=plan,
                evidence=package,
                previous_answer=first_answer,
                violations=first_validation.violations,
            )
        )
        repaired_validation = validate_answer(
            answer=repaired_answer,
            package=package,
            plan=plan,
        )
        if repaired_validation.valid:
            return AnswerResult(
                status="answered_after_repair",
                answer=repaired_answer,
                release_id=release_id,
                plan=plan,
                evidence=package,
                validation=repaired_validation,
                repair_attempted=True,
            )
        return AnswerResult(
            status="refused_after_validation",
            answer=(
                "I cannot confirm a supported answer from the authorized evidence. "
                "The draft failed deterministic claim validation, so no engineering claim was returned."
            ),
            release_id=release_id,
            plan=plan,
            evidence=package,
            validation=repaired_validation,
            repair_attempted=True,
        )

    def retrieve(
        self,
        *,
        authorization: AuthorizationContext,
        question: str,
        top_k: int = 8,
    ) -> tuple[QueryPlan, tuple[RetrievalResult, ...]]:
        run = self.retrieve_pinned(
            authorization=authorization,
            question=question,
            top_k=top_k,
        )
        return run.plan, run.results

    def retrieve_pinned(
        self,
        *,
        authorization: AuthorizationContext,
        question: str,
        top_k: int = 8,
    ) -> RetrievalRun:
        plan = analyze_query(question, top_k=top_k)
        release_id, results = self._retrieve_plan(authorization, plan)
        return RetrievalRun(
            release_id=release_id,
            plan=plan,
            results=results,
        )

    def _retrieve_plan(
        self,
        authorization: AuthorizationContext,
        plan: QueryPlan,
    ) -> tuple[str, tuple[RetrievalResult, ...]]:
        release = self.releases.pin_active(authorization.knowledge_base_id)
        if release.tenant_id != authorization.tenant_id:
            raise PermissionError("active release tenant does not match authorization context")
        results = self.index.retrieve(
            plan,
            release_id=release.release_id,
            authorized_source_ids=frozenset(authorization.authorized_source_ids),
        )
        return release.release_id, results
