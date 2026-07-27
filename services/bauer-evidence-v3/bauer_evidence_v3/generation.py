from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Protocol

import httpx

from .evidence import EvidencePackage
from .planner import QueryPlan
from .validation import ValidationViolation


@dataclass(frozen=True, slots=True)
class GenerationRequest:
    question: str
    plan: QueryPlan
    evidence: EvidencePackage
    previous_answer: str | None = None
    violations: tuple[ValidationViolation, ...] = ()


class ModelGateway(Protocol):
    async def generate(self, request: GenerationRequest) -> str: ...


class ExtractiveModelGateway:
    """Safe local fallback used by the golden vertical slice."""

    async def generate(self, request: GenerationRequest) -> str:
        if not request.evidence.citations:
            return "I cannot confirm the answer because no authorized evidence was found."
        first = request.evidence.citations[0]
        constraints = " ".join(
            value
            for values in request.plan.mandatory_constraints.values()
            for value in values
        )
        prefix = f"For {constraints}, " if constraints else ""
        return f"{prefix}{first.content} [{first.citation_id}]"


class SequenceModelGateway:
    """Deterministic fake that makes the one-repair contract testable."""

    def __init__(self, answers: list[str]) -> None:
        self.answers = list(answers)
        self.requests: list[GenerationRequest] = []

    async def generate(self, request: GenerationRequest) -> str:
        self.requests.append(request)
        if not self.answers:
            raise RuntimeError("sequence model has no answer left")
        return self.answers.pop(0)


class OpenAICompatibleModelGateway:
    def __init__(
        self,
        *,
        base_url: str,
        api_key: str,
        model: str,
        timeout_seconds: float = 90.0,
        max_output_tokens: int = 2_048,
    ) -> None:
        if not base_url or not api_key or not model:
            raise ValueError("base_url, api_key, and model are required")
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.model = model
        self.timeout_seconds = timeout_seconds
        self.max_output_tokens = max_output_tokens

    async def generate(self, request: GenerationRequest) -> str:
        system = (
            "You are the Bauer V3 evidence answerer. Use only supplied authorized evidence. "
            "Preserve all mandatory constraints. Attach the stable citation in the same sentence "
            "as every important number, identifier, quotation, or engineering claim. If evidence "
            "is insufficient, say that it cannot be confirmed. Never treat navigation summaries "
            "as factual support. Every factual sentence must end with one or more supplied stable "
            "citation IDs. A refusal must be a short standalone sentence without an adversative "
            "such as 'but' or 'however'. Do not use quotation marks unless the quoted words occur "
            "verbatim in the cited evidence. Treat each citation as a separate source row and "
            "never merge values from different rows unless the question explicitly asks for a "
            "comparison. Preserve labels, units, slash-grouped values, and row boundaries exactly. "
            "Do not infer a label or unit for a bare column value. Do not explain a footnote marker "
            "unless the supplied evidence contains the footnote text. Answer only the requested "
            "engineering facts. Do not mention navigation, table-of-contents entries, evidence "
            "titles, filenames, or paths. When the question asks for source-file citations, satisfy "
            "that request only with adjacent stable citation IDs; do not add a source list. "
            "Use complete prose sentences, "
            "not Markdown tables, "
            "lists, headings, labels, or sentence fragments. Return only the answer, with no "
            "validation commentary."
        )
        repair = ""
        if request.previous_answer is not None:
            compact_violations = [
                {
                    "code": item.code,
                    "value": item.value,
                    "message": item.message,
                }
                for item in request.violations
            ]
            repair = (
                "\n\nThe previous answer failed deterministic validation. Repair it once without "
                "adding evidence or dropping constraints. Return only the repaired answer. End "
                "every factual sentence with its supplied citation. Keep any refusal as a separate "
                "standalone sentence, and do not use quotation marks unless they are verbatim from "
                "the cited evidence. Preserve each cited row's labels, units, slash-grouped values, "
                "and row boundary exactly; remove any inferred label, unit, footnote explanation, "
                "cross-row merge, navigation claim, filename, path, or source list. Use adjacent "
                "stable citation IDs to satisfy any request for source-file citations. Use complete "
                "prose sentences only; do not use Markdown tables, "
                "lists, headings, labels, or fragments.\nPrevious answer:\n"
                f"{request.previous_answer}\nViolations:\n"
                f"{json.dumps(compact_violations, ensure_ascii=False)}"
            )
        user = (
            f"Question:\n{request.question}\n\n"
            f"Mandatory constraints:\n{json.dumps(request.plan.mandatory_constraints, ensure_ascii=False)}"
            f"\n\nEvidence release: {request.evidence.release_id}\n"
            f"{request.evidence.to_prompt()}{repair}"
        )
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }
        payload = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            "temperature": 0,
            "max_tokens": self.max_output_tokens,
            "stream": False,
        }
        async with httpx.AsyncClient(timeout=self.timeout_seconds) as client:
            response = await client.post(
                f"{self.base_url}/chat/completions",
                headers=headers,
                json=payload,
            )
            response.raise_for_status()
            body = response.json()
        try:
            answer = body["choices"][0]["message"]["content"]
        except (KeyError, IndexError, TypeError) as exc:
            raise RuntimeError("model gateway returned an invalid chat-completion response") from exc
        if not isinstance(answer, str) or not answer.strip():
            raise RuntimeError("model gateway returned an empty answer")
        return answer.strip()
