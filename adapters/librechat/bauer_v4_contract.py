"""Map a LibreChat request into the host-neutral V4 request.

The complete user message is mandatory. A model-authored file-search query can
only become ``search_hint`` and can never replace ``question``.
"""

from __future__ import annotations

from bauer_evidence_v4.contracts import (
    AuthorizationEnvelope,
    ClientContext,
    V4AnswerRequest,
)


def build_v4_request(
    *,
    request_id: str,
    original_user_question: str,
    file_search_query: str | None,
    locale: str,
    instance: str,
    signed_scope: str,
) -> V4AnswerRequest:
    return V4AnswerRequest(
        request_id=request_id,
        question=original_user_question,
        search_hint=file_search_query,
        locale=locale,
        client=ClientContext(type="librechat", instance=instance),
        authorization=AuthorizationEnvelope(signed_scope=signed_scope),
    )
