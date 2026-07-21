#!/usr/bin/env python3
import argparse
import hashlib
from pathlib import Path


MODELS_SHA256 = "d632438774a1219beafe09356c06380fe7c52045d53010e1e34309ae039535f1"
ROUTES_SHA256 = "15dca8622a6f9fa3ca3ba3d4d841786994e301f9198502a4addc79ed857455fd"

OLD_MODEL = """class QueryMultipleBody(BaseModel):
    query: str
    file_ids: List[str]
    k: int = 4
"""

NEW_MODEL = """class QueryMultipleBody(BaseModel):
    query: str
    file_ids: List[str]
    k: int = 4
    entity_id: Optional[str] = None
"""

OLD_ROUTE = '''@router.post("/query_multiple")
async def query_embeddings_by_file_ids(request: Request, body: QueryMultipleBody):
    try:
        # Get the embedding of the query text
        embedding = get_cached_query_embedding(body.query)

        # Perform similarity search with the query embedding and filter by the file_ids in metadata
        if isinstance(vector_store, AsyncPgVector):
            documents = await vector_store.asimilarity_search_with_score_by_vector(
                embedding,
                k=body.k,
                filter={"file_id": {"$in": body.file_ids}},
                executor=request.app.state.thread_pool,
            )
        else:
            documents = vector_store.similarity_search_with_score_by_vector(
                embedding, k=body.k, filter={"file_id": {"$in": body.file_ids}}
            )

        documents = _apply_distance_threshold(documents)

        # Ensure documents list is not empty
        if not documents:
            raise HTTPException(
                status_code=404, detail="No documents found for the given query"
            )

        return documents
    except HTTPException as http_exc:
        logger.error(
            "HTTP Exception in query_embeddings_by_file_ids | Status: %d | Detail: %s",
            http_exc.status_code,
            http_exc.detail,
        )
        raise http_exc
    except Exception as e:
        logger.error(
            "Error in query multiple embeddings | File IDs: %s | Query: %s | Error: %s | Traceback: %s",
            body.file_ids,
            body.query,
            str(e),
            traceback.format_exc(),
        )
        raise HTTPException(status_code=500, detail=str(e))
'''

NEW_ROUTE = '''@router.post("/query_multiple")
async def query_embeddings_by_file_ids(request: Request, body: QueryMultipleBody):
    from app.batch_authorization import filter_authorized_documents

    if not body.file_ids:
        raise HTTPException(status_code=400, detail="file_ids must not be empty")
    if len(body.file_ids) > 1000:
        raise HTTPException(status_code=400, detail="At most 1000 file_ids are allowed")

    if not hasattr(request.state, "user"):
        user_authorized = body.entity_id if body.entity_id else "public"
    else:
        user_authorized = (
            body.entity_id if body.entity_id else request.state.user.get("id")
        )

    try:
        embedding = get_cached_query_embedding(body.query)

        if isinstance(vector_store, AsyncPgVector):
            documents = await vector_store.asimilarity_search_with_score_by_vector(
                embedding,
                k=body.k,
                filter={"file_id": {"$in": body.file_ids}},
                executor=request.app.state.thread_pool,
            )
        else:
            documents = vector_store.similarity_search_with_score_by_vector(
                embedding, k=body.k, filter={"file_id": {"$in": body.file_ids}}
            )

        documents = _apply_distance_threshold(documents)
        documents, denied = filter_authorized_documents(documents, user_authorized)
        if denied:
            logger.warning(
                "Dropped %d unauthorized result(s) from query_multiple for namespace %s",
                denied,
                user_authorized,
            )

        if not documents:
            raise HTTPException(
                status_code=404, detail="No authorized documents found for the given query"
            )

        return documents
    except HTTPException as http_exc:
        logger.error(
            "HTTP Exception in query_embeddings_by_file_ids | Status: %d | Detail: %s",
            http_exc.status_code,
            http_exc.detail,
        )
        raise http_exc
    except Exception as e:
        logger.error(
            "Error in query multiple embeddings | File IDs: %s | Query: %s | Error: %s | Traceback: %s",
            body.file_ids,
            body.query,
            str(e),
            traceback.format_exc(),
        )
        raise HTTPException(status_code=500, detail=str(e))
'''


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def replace_once(path: Path, expected_hash: str, old: str, new: str) -> None:
    actual_hash = sha256(path)
    if actual_hash != expected_hash:
        raise RuntimeError(
            f"Refusing to patch {path}: expected SHA-256 {expected_hash}, got {actual_hash}"
        )

    content = path.read_text(encoding="utf-8")
    if content.count(old) != 1:
        raise RuntimeError(f"Expected exactly one patch target in {path}")
    path.write_text(content.replace(old, new), encoding="utf-8")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", type=Path, default=Path("/app"))
    args = parser.parse_args()

    replace_once(args.root / "app/models.py", MODELS_SHA256, OLD_MODEL, NEW_MODEL)
    replace_once(
        args.root / "app/routes/document_routes.py",
        ROUTES_SHA256,
        OLD_ROUTE,
        NEW_ROUTE,
    )


if __name__ == "__main__":
    main()
