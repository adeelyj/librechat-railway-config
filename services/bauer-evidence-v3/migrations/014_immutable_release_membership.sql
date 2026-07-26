-- Release membership is immutable after insertion. The compiler now retries
-- with INSERT ... ON CONFLICT DO NOTHING followed by an exact RLS-protected
-- identity check, so its former no-op UPDATE privilege is unnecessary.

REVOKE UPDATE
ON bauer_rag_v3.release_sources
FROM bauer_rag_v3_ingester;
