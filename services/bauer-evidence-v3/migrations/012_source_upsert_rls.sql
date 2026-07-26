-- Preserve idempotent source registration without widening object grants.
--
-- The source registry uses INSERT ... ON CONFLICT DO UPDATE for a no-op,
-- identity-verifying retry. PostgreSQL evaluates the UPDATE policy for that
-- statement even when the proposed source row is new. The prior predicate
-- called can_write_source(source_id), which looks the source up in the table;
-- before the insert it necessarily returned false and rejected the new row.
--
-- For an existing source, the replacement is capability-equivalent:
-- can_write_source requires the source to belong to the current tenant and
-- requires ingest/admin permission on its knowledge base. Expressing those
-- checks from the proposed/existing row makes both the initial insert and the
-- idempotent conflict path fail closed on the same tenant/KB boundary.

ALTER POLICY source_write
ON bauer_rag_v3.sources
TO bauer_rag_v3_ingester
USING (
    tenant_id = bauer_rag_v3.current_tenant_id()
    AND bauer_rag_v3.can_ingest_kb(kb_id)
)
WITH CHECK (
    tenant_id = bauer_rag_v3.current_tenant_id()
    AND bauer_rag_v3.can_ingest_kb(kb_id)
);
