-- Validate the complete request source scope with one ACL-scope evaluation.
--
-- The authorization audit previously called can_read_source() once for every
-- source in the signed request. A normal 373-file Agent therefore rebuilt the
-- same readable-source set 373 times before retrieval began. Keep the same
-- tenant, KB, release, source, and principal checks while evaluating each
-- readable scope exactly once per audit call.

CREATE OR REPLACE FUNCTION bauer_rag_v3.record_authorization_audit(
    target_tenant_id uuid,
    target_kb_id uuid,
    target_release_id uuid,
    target_source_ids uuid[],
    audit_action text,
    audit_decision text,
    audit_reason_code text,
    audit_request_id text DEFAULT NULL
)
RETURNS bigint
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = bauer_rag_v3, pg_temp
AS $$
DECLARE
    context_principal_ids uuid[];
    readable_kb_scope uuid[];
    readable_release_scope uuid[];
    readable_source_scope uuid[];
    new_audit_id bigint;
BEGIN
    IF target_tenant_id IS NULL
       OR target_tenant_id IS DISTINCT FROM
          bauer_rag_v3.current_tenant_id() THEN
        RAISE EXCEPTION
            'authorization audit tenant does not match request context'
            USING ERRCODE = '42501';
    END IF;

    context_principal_ids :=
        bauer_rag_v3.current_principal_ids();
    IF cardinality(context_principal_ids) = 0
       OR array_position(context_principal_ids, NULL) IS NOT NULL
       OR cardinality(context_principal_ids) > 256 THEN
        RAISE EXCEPTION
            'authorization audit requires a bounded principal context'
            USING ERRCODE = '42501';
    END IF;

    readable_kb_scope := bauer_rag_v3.readable_kb_ids();
    IF target_kb_id IS NOT NULL
       AND NOT target_kb_id = ANY (readable_kb_scope) THEN
        RAISE EXCEPTION
            'authorization audit knowledge base is not readable'
            USING ERRCODE = '42501';
    END IF;

    readable_release_scope := bauer_rag_v3.readable_release_ids();
    IF target_release_id IS NOT NULL
       AND (
           NOT target_release_id = ANY (readable_release_scope)
           OR (
               target_kb_id IS NOT NULL
               AND NOT EXISTS (
                   SELECT 1
                   FROM bauer_rag_v3.knowledge_releases release_row
                   WHERE release_row.release_id = target_release_id
                     AND release_row.kb_id = target_kb_id
               )
           )
       ) THEN
        RAISE EXCEPTION
            'authorization audit release is not readable'
            USING ERRCODE = '42501';
    END IF;

    target_source_ids := coalesce(
        target_source_ids,
        ARRAY[]::uuid[]
    );
    IF array_position(target_source_ids, NULL) IS NOT NULL
       OR cardinality(target_source_ids) > 1024 THEN
        RAISE EXCEPTION
            'authorization audit source scope is invalid'
            USING ERRCODE = '22023';
    END IF;

    readable_source_scope := bauer_rag_v3.readable_source_ids();
    IF NOT target_source_ids <@ readable_source_scope
       OR (
           target_kb_id IS NOT NULL
           AND EXISTS (
               SELECT 1
               FROM bauer_rag_v3.sources source_row
               WHERE source_row.source_id = ANY (target_source_ids)
                 AND source_row.kb_id IS DISTINCT FROM target_kb_id
           )
       ) THEN
        RAISE EXCEPTION
            'authorization audit source scope is not readable'
            USING ERRCODE = '42501';
    END IF;

    IF audit_action IS NULL
       OR btrim(audit_action) = ''
       OR length(audit_action) > 128 THEN
        RAISE EXCEPTION 'authorization audit action is invalid'
            USING ERRCODE = '22023';
    END IF;
    IF audit_decision IS NULL
       OR audit_decision NOT IN ('allow', 'deny', 'error') THEN
        RAISE EXCEPTION 'authorization audit decision is invalid'
            USING ERRCODE = '22023';
    END IF;
    IF audit_reason_code IS NULL
       OR btrim(audit_reason_code) = ''
       OR length(audit_reason_code) > 128 THEN
        RAISE EXCEPTION 'authorization audit reason code is invalid'
            USING ERRCODE = '22023';
    END IF;
    IF audit_request_id IS NOT NULL
       AND (
           btrim(audit_request_id) = ''
           OR length(audit_request_id) > 256
       ) THEN
        RAISE EXCEPTION 'authorization audit request id is invalid'
            USING ERRCODE = '22023';
    END IF;

    INSERT INTO bauer_rag_v3.authorization_audit (
        tenant_id,
        actor_principal_ids,
        database_role,
        request_id,
        action,
        decision,
        reason_code,
        target_kb_id,
        target_release_id,
        target_source_ids
    )
    VALUES (
        target_tenant_id,
        context_principal_ids,
        session_user,
        audit_request_id,
        audit_action,
        audit_decision,
        audit_reason_code,
        target_kb_id,
        target_release_id,
        target_source_ids
    )
    RETURNING authorization_audit_id INTO new_audit_id;

    RETURN new_audit_id;
END
$$;
