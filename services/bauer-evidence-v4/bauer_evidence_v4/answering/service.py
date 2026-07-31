from __future__ import annotations

from dataclasses import dataclass, field, replace
import hashlib
import json

from ..contracts.models import (
    CoverageItem,
    ReleaseContract,
    V4AnswerResponse,
)
from ..reranking import RerankResult, TransparentFeatureReranker
from ..retrieval import (
    AuthorizedScope,
    CandidateGenerator,
    RetrievalRequest,
)
from .analysis import TaskAnalyzer
from .coverage import CoverageEngine
from .evidence import EvidenceMaterializer, SourceRegistry
from .models import AnswerDraft, FieldCoverage, TaskPlan
from .render import GroundedAnswerBuilder
from .validation import AnswerValidator, TargetedRepair


@dataclass(frozen=True, slots=True)
class AnswerService:
    candidate_generator: CandidateGenerator
    source_registry: SourceRegistry
    release: ReleaseContract
    analyzer: TaskAnalyzer = field(default_factory=TaskAnalyzer)
    reranker: TransparentFeatureReranker = field(
        default_factory=TransparentFeatureReranker
    )
    coverage_engine: CoverageEngine = field(default_factory=CoverageEngine)
    builder: GroundedAnswerBuilder = field(
        default_factory=GroundedAnswerBuilder
    )
    validator: AnswerValidator = field(default_factory=AnswerValidator)

    def answer(
        self,
        *,
        request_id: str,
        trace_id: str,
        question: str,
        search_hint: str | None,
        locale: str,
        scope: AuthorizedScope,
    ) -> V4AnswerResponse:
        plan = self.analyzer.analyze(
            question,
            locale=locale,
            search_hints=(search_hint,) if search_hint else (),
        )
        candidate_set = self.candidate_generator.retrieve(
            RetrievalRequest(
                question=question,
                search_hint=search_hint,
                subquestions=plan.subquestions,
                constraints=(),
                scope=scope,
            ),
            limit=80,
        )
        if candidate_set.original_question != question:
            raise AssertionError("candidate generation changed the original question")
        # Preserve independent evidence needs in broad comparison questions.
        # Coverage remains bounded to at most two claim-sized supports per
        # field, so this wider rerank window cannot become answer material by
        # itself.
        reranked = self._rerank_requirements_first(candidate_set, limit=60)
        if reranked.original_question != question:
            raise AssertionError("reranking changed the original question")
        evidence = EvidenceMaterializer(self.source_registry).materialize(
            reranked
        )
        coverage = self.coverage_engine.evaluate(plan, evidence)
        citations_by_evidence = {
            context.unit.evidence_id: context.citation
            for context in evidence
        }
        draft = self.builder.build(
            plan,
            coverage,
            citations_by_evidence,
        )
        validation = self.validator.validate(plan, draft, evidence)
        if not validation.passed:
            draft = TargetedRepair(self.builder).repair(
                plan,
                draft,
                citations_by_evidence,
            )
            validation = self.validator.validate(
                plan,
                draft,
                evidence,
                repair_attempted=True,
            )
        if not validation.passed:
            draft = self._refused(
                plan.fields,
                "The available evidence could not be validated safely.",
            )

        response_coverage = [
            CoverageItem(
                field=item.field.field,
                state=item.state,
                required=item.field.required,
                citation_ids=[
                    citations_by_evidence[evidence_id].citation_id
                    for evidence_id in item.evidence_ids
                    if evidence_id in citations_by_evidence
                ],
                detail=item.detail,
            )
            for item in draft.coverage
        ]
        return V4AnswerResponse(
            request_id=request_id,
            status=draft.status,
            answer=draft.answer,
            coverage=response_coverage,
            not_found=[
                item.field.field
                for item in draft.coverage
                if item.state == "absent"
            ],
            citations=list(draft.citations),
            release=self.release,
            trace_id=trace_id,
            validation={
                "passed": validation.passed,
                "repair_attempted": validation.repair_attempted,
                "repair_count": draft.repair_count,
                "defect_codes": [
                    defect.code for defect in validation.defects
                ],
                "candidate_count": len(candidate_set.candidates),
                "evidence_window_count": len(evidence),
                "reranker_model_id": reranked.model_id,
                "reranker_identity": reranked.model_identity,
                "answer_mode": (
                    "lossless_deterministic"
                    if plan.deliverable == "exact_row"
                    else "grounded_structured"
                ),
                "validation_fingerprint": self._validation_fingerprint(
                    plan,
                    draft,
                ),
            },
        )

    @staticmethod
    def _validation_fingerprint(
        plan: TaskPlan,
        draft: AnswerDraft,
    ) -> str:
        payload = {
            "question": plan.original_question,
            "deliverable": plan.deliverable,
            "fields": [field.field for field in plan.fields],
            "status": draft.status,
            "claims": [
                {
                    "field": claim.field,
                    "values": list(claim.values),
                    "evidence_ids": list(claim.evidence_ids),
                    "fact_ids": list(claim.fact_ids),
                }
                for claim in draft.claims
            ],
            "answer": draft.answer,
        }
        encoded = json.dumps(
            payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        return hashlib.sha256(encoded).hexdigest()

    def _rerank_requirements_first(
        self,
        candidate_set,
        *,
        limit: int,
    ) -> RerankResult:
        requirement_candidates = tuple(
            candidate
            for candidate in candidate_set.candidates
            if candidate.subquestion_ids != ("search_hint",)
        )
        hint_candidates = tuple(
            candidate
            for candidate in candidate_set.candidates
            if candidate.subquestion_ids == ("search_hint",)
        )
        if not requirement_candidates:
            return self.reranker.rerank(candidate_set, limit=limit)
        primary = self.reranker.rerank(
            replace(
                candidate_set,
                candidates=requirement_candidates,
            ),
            limit=limit,
        )
        remaining = limit - len(primary.ranked)
        if remaining <= 0 or not hint_candidates:
            return primary
        expansion = self.reranker.rerank(
            replace(candidate_set, candidates=hint_candidates),
            limit=remaining,
        )
        ranked = primary.ranked + tuple(
            replace(
                item,
                rank=len(primary.ranked) + offset,
            )
            for offset, item in enumerate(expansion.ranked, start=1)
        )
        return replace(primary, ranked=ranked)

    @staticmethod
    def _refused(
        fields,
        answer: str,
    ) -> AnswerDraft:
        return AnswerDraft(
            status="refused",
            answer=answer,
            coverage=tuple(
                FieldCoverage(
                    field=field,
                    state="refused",
                    values=(),
                    evidence_ids=(),
                    detail="Validation failed after one targeted repair.",
                )
                for field in fields
            ),
            claims=(),
            citations=(),
            repair_count=1,
        )


__all__ = ["AnswerService", "SourceRegistry"]
