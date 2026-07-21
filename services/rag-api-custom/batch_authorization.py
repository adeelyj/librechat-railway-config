from typing import Any, Iterable, List, Tuple


def filter_authorized_documents(
    documents: Iterable[Tuple[Any, float]], authorized_user_id: str
) -> tuple[List[Tuple[Any, float]], int]:
    """Keep only results in the requested RAG namespace.

    LibreChat remains the primary authorization boundary and supplies only
    file IDs already checked against Agent permissions. This additional check
    preserves the namespace behaviour of the single-file `/query` route when
    `/query_multiple` is used.
    """

    authorized: List[Tuple[Any, float]] = []
    denied = 0
    for document, score in documents:
        metadata = getattr(document, "metadata", {}) or {}
        document_user_id = metadata.get("user_id")
        if document_user_id is None or document_user_id == authorized_user_id:
            authorized.append((document, score))
        else:
            denied += 1
    return authorized, denied
