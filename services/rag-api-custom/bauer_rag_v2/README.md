# Bauer RAG V2 runtime

This package adds a versioned Bauer retrieval path to the existing LibreChat RAG API. It is
additive: V1 continues to use `langchain_pg_embedding` and `/query_multiple`; V2 uses only the
`bauer_rag_v2` PostgreSQL schema and `/query_v2`.

## Runtime flow

1. RAG API startup runs `schema.sql` under a PostgreSQL advisory lock.
2. An administrator creates an inactive index run.
3. Reviewed UTF-8 Markdown derivatives are staged by checksum. Each document retains namespace,
   file ID, source identity, source type, location, entities, table rows, and embeddings.
4. Activation checks the exact expected file count and switches all documents in one transaction.
5. `/query_v2` applies both the namespace and supplied file-ID allow-lists to exact, lexical, and
   vector candidate generation. It fuses, deduplicates, reranks, and filters the response again.
6. `/validate_v2` deterministically checks citations, identifiers, high-risk numbers, quotations,
   source types, mandatory constraints, and required refusals.

The normal LibreChat request returns eight evidence items. Evaluation may request ten to calculate
Recall@10. Debug output is administrator/debug-allow-list only and LibreChat never requests it.

## Configuration

| Variable | Requirement |
| --- | --- |
| `BAUER_RAG_V2_NAMESPACE_IDS` | Required, comma-separated private V2 Agent IDs. Empty means fail closed. |
| `BAUER_RAG_V2_ADMIN_USER_IDS` | Optional additional index administrators. LibreChat `ADMIN` users are accepted. |
| `BAUER_RAG_V2_DEBUG_USER_IDS` | Optional users permitted to request debug output. |
| `BAUER_RAG_V2_MAX_INDEX_FILE_BYTES` | Optional per-derivative upload limit; default 25 MiB. |
| `BAUER_RAG_V2_EMBED_BATCH_SIZE` | Optional serial embedding batch size, clamped to 1-128; default 64. |
| `BAUER_RAG_V2_MIN_EVIDENCE_SCORE` | Optional post-rerank threshold, clamped to 0-1; starting default 0.08. Freeze it before holdout. |
| `BAUER_RAG_V2_RERANK_URL` | Optional OpenAI-compatible rerank endpoint. |
| `BAUER_RAG_V2_RERANK_MODEL` | Required with the rerank URL. |
| `BAUER_RAG_V2_RERANK_API_KEY` | Optional bearer token for the rerank endpoint. |
| `BAUER_RAG_V2_RERANK_TIMEOUT_SECONDS` | Optional bounded timeout; deterministic reranking is the fallback. |

LibreChat separately requires `RAG_V2_AGENT_IDS` with the same private V2 Agent ID. No other Agent
selects V2.

The schema and repository require the deployed 1024-dimensional embedding model. A dimension
mismatch fails indexing or querying rather than storing an incompatible vector.

## Failure behavior

- Missing or mismatched namespace authorization fails closed.
- Schema or extension initialization failure disables V2 with HTTP 503 and is logged, but does not
  prevent the existing V1 API from starting.
- A failed candidate channel is recorded as degraded; all channels failing returns an error.
- Remote reranker failure uses deterministic fusion/reranking.
- Any indexing error marks that run failed. Start a new run; already staged checksum-identical
  records are reusable and never become active through the failed run.
- Partial runs cannot activate, and failed staging never changes the active corpus.
- Empty retrieval produces an explicit "not established" refusal.

See [the runbook](../../../docs/bauer-rag-v2-runbook.md) for staging, evaluation, deployment, and
rollback controls.
