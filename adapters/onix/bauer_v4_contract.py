"""Map future ONIX state into the same host-neutral V4 request."""

from __future__ import annotations

from bauer_evidence_v4.contracts import (
    AuthorizationEnvelope,
    ClientContext,
    TypedConstraint,
    V4AnswerRequest,
)


def build_v4_request(
    *,
    request_id: str,
    original_user_question: str,
    locale: str,
    instance: str,
    signed_scope: str,
    selected_filters: list[TypedConstraint] | None = None,
    search_hint: str | None = None,
) -> V4AnswerRequest:
    return V4AnswerRequest(
        request_id=request_id,
        question=original_user_question,
        search_hint=search_hint,
        locale=locale,
        client=ClientContext(type="onix", instance=instance),
        authorization=AuthorizationEnvelope(signed_scope=signed_scope),
        typed_constraints=selected_filters or [],
    )
