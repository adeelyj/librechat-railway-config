-- Keep compiler QA lifecycle checks race-free without granting the ingester
-- UPDATE capability on the release-control table.
--
-- guard_compiler_qa_mutation takes a FOR SHARE lock on the parent release so
-- QA cannot race a lifecycle transition. PostgreSQL requires UPDATE privilege
-- to acquire that row lock. The runtime ingester correctly has SELECT only on
-- knowledge_releases, so execute this trigger as the isolated schema owner.
-- The function has no caller-controlled SQL and remains trigger-only: PUBLIC
-- and every runtime group retain no direct EXECUTE privilege.

ALTER FUNCTION bauer_rag_v3.guard_compiler_qa_mutation()
    SECURITY DEFINER;
ALTER FUNCTION bauer_rag_v3.guard_compiler_qa_mutation()
    SET search_path = bauer_rag_v3, pg_temp;

REVOKE ALL
ON FUNCTION bauer_rag_v3.guard_compiler_qa_mutation()
FROM PUBLIC,
     bauer_rag_v3_reader,
     bauer_rag_v3_ingester,
     bauer_rag_v3_evaluator,
     bauer_rag_v3_reviewer,
     bauer_rag_v3_admin;
