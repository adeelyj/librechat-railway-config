-- Least-privilege group roles.  These are deliberately NOLOGIN roles:
-- deployment-specific login credentials are created by the infrastructure
-- owner and granted exactly one of these memberships.
DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_catalog.pg_roles
        WHERE rolname = 'bauer_rag_v3_reader'
    ) THEN
        CREATE ROLE bauer_rag_v3_reader
            NOLOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION;
    END IF;
    IF NOT EXISTS (
        SELECT 1 FROM pg_catalog.pg_roles
        WHERE rolname = 'bauer_rag_v3_ingester'
    ) THEN
        CREATE ROLE bauer_rag_v3_ingester
            NOLOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION;
    END IF;
    IF NOT EXISTS (
        SELECT 1 FROM pg_catalog.pg_roles
        WHERE rolname = 'bauer_rag_v3_admin'
    ) THEN
        CREATE ROLE bauer_rag_v3_admin
            NOLOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION;
    END IF;
    IF NOT EXISTS (
        SELECT 1 FROM pg_catalog.pg_roles
        WHERE rolname = 'bauer_rag_v3_evaluator'
    ) THEN
        CREATE ROLE bauer_rag_v3_evaluator
            NOLOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION;
    END IF;
    IF NOT EXISTS (
        SELECT 1 FROM pg_catalog.pg_roles
        WHERE rolname = 'bauer_rag_v3_reviewer'
    ) THEN
        CREATE ROLE bauer_rag_v3_reviewer
            NOLOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION;
    END IF;
END
$$;

REVOKE CREATE ON SCHEMA bauer_rag_v3 FROM PUBLIC;
REVOKE ALL ON ALL TABLES IN SCHEMA bauer_rag_v3 FROM PUBLIC;
REVOKE ALL ON ALL SEQUENCES IN SCHEMA bauer_rag_v3 FROM PUBLIC;
REVOKE EXECUTE ON ALL FUNCTIONS IN SCHEMA bauer_rag_v3 FROM PUBLIC;

GRANT USAGE ON SCHEMA bauer_rag_v3
    TO bauer_rag_v3_reader,
       bauer_rag_v3_ingester,
       bauer_rag_v3_evaluator,
       bauer_rag_v3_reviewer,
       bauer_rag_v3_admin;

GRANT SELECT ON ALL TABLES IN SCHEMA bauer_rag_v3
    TO bauer_rag_v3_reader;
GRANT SELECT, INSERT, UPDATE, DELETE
    ON ALL TABLES IN SCHEMA bauer_rag_v3
    TO bauer_rag_v3_ingester, bauer_rag_v3_admin;
REVOKE INSERT, UPDATE, DELETE
    ON bauer_rag_v3.eval_suites,
       bauer_rag_v3.eval_cases,
       bauer_rag_v3.eval_runs,
       bauer_rag_v3.eval_results
    FROM bauer_rag_v3_ingester;
GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA bauer_rag_v3
    TO bauer_rag_v3_ingester, bauer_rag_v3_admin;

-- Read and ingest roles need the RLS predicate functions.  The guarded
-- activation function is removed again below and remains admin-only.
GRANT EXECUTE ON ALL FUNCTIONS IN SCHEMA bauer_rag_v3
    TO bauer_rag_v3_reader, bauer_rag_v3_ingester, bauer_rag_v3_admin;
REVOKE EXECUTE ON FUNCTION bauer_rag_v3.activate_release(
    uuid,
    uuid,
    uuid,
    text,
    boolean
) FROM bauer_rag_v3_reader, bauer_rag_v3_ingester;
GRANT EXECUTE ON FUNCTION bauer_rag_v3.activate_release(
    uuid,
    uuid,
    uuid,
    text,
    boolean
) TO bauer_rag_v3_admin;

-- Privilege inheritance is monotonic: admin includes ingest, and ingest
-- includes read.  Login roles still receive only the group explicitly granted
-- by the infrastructure owner.
GRANT bauer_rag_v3_reader TO bauer_rag_v3_ingester;
GRANT bauer_rag_v3_reader TO bauer_rag_v3_evaluator;
GRANT bauer_rag_v3_reader TO bauer_rag_v3_reviewer;
GRANT bauer_rag_v3_ingester TO bauer_rag_v3_admin;

ALTER DEFAULT PRIVILEGES IN SCHEMA bauer_rag_v3
    REVOKE ALL ON TABLES FROM PUBLIC;
ALTER DEFAULT PRIVILEGES IN SCHEMA bauer_rag_v3
    REVOKE ALL ON SEQUENCES FROM PUBLIC;
ALTER DEFAULT PRIVILEGES IN SCHEMA bauer_rag_v3
    REVOKE EXECUTE ON FUNCTIONS FROM PUBLIC;
ALTER DEFAULT PRIVILEGES IN SCHEMA bauer_rag_v3
    GRANT SELECT ON TABLES TO bauer_rag_v3_reader;
ALTER DEFAULT PRIVILEGES IN SCHEMA bauer_rag_v3
    GRANT SELECT, INSERT, UPDATE, DELETE
    ON TABLES TO bauer_rag_v3_ingester, bauer_rag_v3_admin;
ALTER DEFAULT PRIVILEGES IN SCHEMA bauer_rag_v3
    GRANT USAGE, SELECT
    ON SEQUENCES TO bauer_rag_v3_ingester, bauer_rag_v3_admin;
