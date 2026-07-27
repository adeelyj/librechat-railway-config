-- The fact retrieval channel joins each citable fact search unit to its
-- canonical fact by artifact set and primary provenance. The earlier
-- artifact-only indexes forced PostgreSQL to rescan every fact in an artifact
-- for each candidate unit, which exceeded the serving statement timeout on
-- the full 373-source release.
--
-- Keep the index partial because rejected facts are never eligible for
-- retrieval. Included columns cover scoring and projection without changing
-- fact identity, RLS, release pinning, or authorization semantics.

CREATE INDEX IF NOT EXISTS ix_v3_fact_provenance_lookup
    ON bauer_rag_v3.facts (
        artifact_set_id,
        primary_provenance_id
    )
    INCLUDE (
        fact_id,
        subject_key,
        predicate,
        numeric_value,
        unit_ucum,
        unit_raw,
        verification_status
    )
    WHERE verification_status <> 'rejected';
