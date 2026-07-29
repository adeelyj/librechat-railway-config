DROP TRIGGER IF EXISTS active_release_must_be_ready
    ON bauer_rag_v4.active_release_pointers;
DROP TABLE IF EXISTS bauer_rag_v4.active_release_pointers;
DROP FUNCTION IF EXISTS bauer_rag_v4.require_ready_active_release();
DROP TRIGGER IF EXISTS release_sources_immutable
    ON bauer_rag_v4.release_sources;
DROP TRIGGER IF EXISTS release_sources_building_only
    ON bauer_rag_v4.release_sources;
DROP TABLE IF EXISTS bauer_rag_v4.release_sources;
DROP FUNCTION IF EXISTS bauer_rag_v4.require_building_release_membership();
DROP TABLE IF EXISTS bauer_rag_v4.knowledge_releases;
DROP TABLE IF EXISTS bauer_rag_v4.principal_source_grants;
DROP TRIGGER IF EXISTS source_versions_immutable
    ON bauer_rag_v4.source_versions;
DROP TABLE IF EXISTS bauer_rag_v4.source_versions;
DROP TABLE IF EXISTS bauer_rag_v4.source_documents;
DROP TABLE IF EXISTS bauer_rag_v4.principals;
DROP TABLE IF EXISTS bauer_rag_v4.knowledge_bases;
DROP TABLE IF EXISTS bauer_rag_v4.tenants;
DROP FUNCTION IF EXISTS bauer_rag_v4.reject_immutable_mutation();
