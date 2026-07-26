-- Insert immutable release membership through one scope-checking owner path.
--
-- PostgreSQL's foreign-key check takes a key-share lock on the parent release.
-- Granting any UPDATE privilege on knowledge_releases solely to satisfy that
-- internal lock would widen the compiler's control-plane authority. Keep the
-- release table read-only to the ingester and expose only this bounded,
-- context-validated membership operation.

CREATE OR REPLACE FUNCTION bauer_rag_v3.register_release_source_membership(
    target_kb_id uuid,
    target_release_id uuid,
    target_source_id uuid,
    target_source_version_id uuid,
    target_artifact_set_id uuid,
    target_ordinal integer,
    target_reused boolean
)
RETURNS uuid
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = bauer_rag_v3, pg_temp
AS $$
DECLARE
    inserted_source_id uuid;
    context_kb_id uuid;
BEGIN
    context_kb_id := nullif(
        current_setting('app.knowledge_base_id', true),
        ''
    )::uuid;
    IF context_kb_id IS NULL
       OR context_kb_id IS DISTINCT FROM target_kb_id
       OR bauer_rag_v3.current_tenant_id() IS NULL
       OR cardinality(bauer_rag_v3.current_principal_ids()) = 0
       OR NOT bauer_rag_v3.can_write_release(target_release_id)
       OR NOT bauer_rag_v3.can_write_source(target_source_id) THEN
        RAISE EXCEPTION 'release membership scope is not writable'
            USING ERRCODE = '42501';
    END IF;
    IF target_ordinal < 0 THEN
        RAISE EXCEPTION 'release membership ordinal must be non-negative'
            USING ERRCODE = '22023';
    END IF;

    INSERT INTO bauer_rag_v3.release_sources AS membership (
        kb_id,
        release_id,
        source_id,
        source_version_id,
        artifact_set_id,
        ordinal,
        action
    )
    VALUES (
        target_kb_id,
        target_release_id,
        target_source_id,
        target_source_version_id,
        target_artifact_set_id,
        target_ordinal,
        CASE WHEN target_reused THEN 'reused' ELSE 'compiled' END
    )
    ON CONFLICT (release_id, source_id) DO NOTHING
    RETURNING membership.source_id
    INTO inserted_source_id;

    IF inserted_source_id IS NOT NULL THEN
        RETURN inserted_source_id;
    END IF;
    IF EXISTS (
        SELECT 1
        FROM bauer_rag_v3.release_sources AS membership
        WHERE membership.kb_id = target_kb_id
          AND membership.release_id = target_release_id
          AND membership.source_id = target_source_id
          AND membership.source_version_id = target_source_version_id
          AND membership.artifact_set_id = target_artifact_set_id
          AND membership.ordinal = target_ordinal
    ) THEN
        RETURN target_source_id;
    END IF;
    RAISE EXCEPTION
        'release already contains incompatible source membership'
        USING ERRCODE = '55000';
END
$$;

REVOKE ALL
ON FUNCTION bauer_rag_v3.register_release_source_membership(
    uuid,
    uuid,
    uuid,
    uuid,
    uuid,
    integer,
    boolean
)
FROM PUBLIC,
     bauer_rag_v3_reader,
     bauer_rag_v3_evaluator,
     bauer_rag_v3_reviewer,
     bauer_rag_v3_admin;

GRANT EXECUTE
ON FUNCTION bauer_rag_v3.register_release_source_membership(
    uuid,
    uuid,
    uuid,
    uuid,
    uuid,
    integer,
    boolean
)
TO bauer_rag_v3_ingester;

REVOKE INSERT, UPDATE
ON bauer_rag_v3.release_sources
FROM bauer_rag_v3_ingester;
