from __future__ import annotations

import ast
import hashlib
import importlib.util
import re
import sys
import tempfile
import unittest
from pathlib import Path


SERVICE_ROOT = Path(__file__).resolve().parents[1]
MIGRATIONS_DIR = SERVICE_ROOT / "migrations"
MODULE_PATH = SERVICE_ROOT / "bauer_evidence_v3" / "migrations.py"


def load_module():
    spec = importlib.util.spec_from_file_location(
        "bauer_evidence_v3_migrations_test_module",
        MODULE_PATH,
    )
    if spec is None or spec.loader is None:
        raise RuntimeError("Could not load migrations module")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


MIGRATIONS = load_module()


class MigrationDiscoveryTests(unittest.TestCase):
    def test_numbered_migrations_are_contiguous_and_checksummed(self):
        discovered = MIGRATIONS.discover_migrations(MIGRATIONS_DIR)
        self.assertEqual(
            [migration.version for migration in discovered],
            list(range(1, 20)),
        )
        self.assertEqual(
            [migration.filename for migration in discovered],
            [
                "001_extensions_and_migrations.sql",
                "002_registry_acl.sql",
                "003_source_evidence.sql",
                "004_search_projections.sql",
                "005_jobs.sql",
                "006_qa_eval.sql",
                "007_rls_roles.sql",
                "008_indexes.sql",
                "009_runtime_roles.sql",
                "010_runtime_role_hardening.sql",
                "011_rls_policy_role_scoping.sql",
                "012_source_upsert_rls.sql",
                "013_qa_guard_lock_authority.sql",
                "014_immutable_release_membership.sql",
                "015_release_membership_registration.sql",
                "016_reader_rls_scope_sets.sql",
                "017_authorization_audit_scope_set.sql",
                "018_materialize_reader_scope_dependencies.sql",
                "019_initplan_reader_scope_policies.sql",
            ],
        )
        for migration in discovered:
            expected = hashlib.sha256(migration.path.read_bytes()).hexdigest()
            self.assertEqual(migration.checksum, expected)
            self.assertRegex(migration.checksum, r"^[0-9a-f]{64}$")

    def test_module_does_not_import_psycopg_at_import_time(self):
        tree = ast.parse(MODULE_PATH.read_text(encoding="utf-8"))
        top_level_imports = [
            node
            for node in tree.body
            if isinstance(node, (ast.Import, ast.ImportFrom))
        ]
        imported = {
            alias.name
            for node in top_level_imports
            for alias in node.names
        }
        self.assertNotIn("psycopg", imported)
        self.assertIn(
            "psycopg.ClientCursor",
            MODULE_PATH.read_text(encoding="utf-8"),
        )

    def test_discovery_rejects_a_numbering_gap(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "001_first.sql").write_text("SELECT 1;\n", encoding="utf-8")
            (root / "003_third.sql").write_text("SELECT 3;\n", encoding="utf-8")
            with self.assertRaises(MIGRATIONS.MigrationStateError):
                MIGRATIONS.discover_migrations(root)

    def test_checksum_drift_is_rejected(self):
        discovered = MIGRATIONS.discover_migrations(MIGRATIONS_DIR)
        first = discovered[0]
        applied = {
            first.version: MIGRATIONS.AppliedMigration(
                version=first.version,
                filename=first.filename,
                checksum="0" * 64,
            )
        }
        with self.assertRaises(MIGRATIONS.MigrationDriftError):
            MIGRATIONS.validate_applied_migrations(discovered, applied)

    def test_applied_history_must_be_a_contiguous_prefix(self):
        discovered = MIGRATIONS.discover_migrations(MIGRATIONS_DIR)
        first, second, third = discovered[:3]
        applied = {
            first.version: MIGRATIONS.AppliedMigration(
                first.version,
                first.filename,
                first.checksum,
            ),
            third.version: MIGRATIONS.AppliedMigration(
                third.version,
                third.filename,
                third.checksum,
            ),
        }
        with self.assertRaises(MIGRATIONS.MigrationStateError):
            MIGRATIONS.validate_applied_migrations(discovered, applied)
        self.assertEqual(second.version, 2)


class MigrationStructureTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.files = {
            path.name: path.read_text(encoding="utf-8")
            for path in sorted(MIGRATIONS_DIR.glob("*.sql"))
        }
        cls.all_sql = "\n".join(cls.files.values()).casefold()

    def test_schema_is_additive_and_isolated(self):
        self.assertIn("create schema if not exists bauer_rag_v3", self.all_sql)
        for forbidden in (
            "drop schema",
            "drop table",
            "truncate table",
            "bauer_rag_v2.",
            "langchain_pg_embedding",
        ):
            self.assertNotIn(forbidden, self.all_sql)

    def test_required_registry_and_evidence_tables_exist(self):
        required = (
            "schema_migrations",
            "tenants",
            "knowledge_bases",
            "principals",
            "kb_grants",
            "source_grants",
            "objects",
            "sources",
            "source_versions",
            "knowledge_releases",
            "active_releases",
            "release_activations",
            "artifact_sets",
            "release_sources",
            "pages",
            "sections",
            "blocks",
            "tables",
            "table_segments",
            "table_cells",
            "provenance_spans",
            "entities",
            "entity_mentions",
            "facts",
            "fact_provenance",
        )
        for table in required:
            self.assertIn(
                f"create table if not exists bauer_rag_v3.{table}",
                self.all_sql,
            )

    def test_search_is_release_scoped_and_source_native(self):
        search_sql = self.files["004_search_projections.sql"].casefold()
        for table in ("search_units", "exact_terms", "nav_nodes", "nav_edges"):
            self.assertIn(
                f"create table if not exists bauer_rag_v3.{table}",
                search_sql,
            )
        for field in (
            "release_id",
            "source_version_id",
            "artifact_set_id",
            "primary_provenance_id",
            "search_vector",
            "embedding vector(1024)",
            "is_citable",
            "generated_summary",
        ):
            self.assertIn(field, search_sql)
        self.assertIn(
            "not is_citable or primary_provenance_id is not null",
            " ".join(search_sql.split()),
        )
        self.assertIn(
            "not generated_summary or not is_citable",
            " ".join(search_sql.split()),
        )

    def test_fact_values_are_typed_and_keep_conflicts(self):
        evidence_sql = self.files["003_source_evidence.sql"].casefold()
        for field in (
            "value_kind",
            "numeric_value numeric",
            "date_value date",
            "boolean_value boolean",
            "unit_raw",
            "unit_ucum",
            "qualifiers jsonb",
            "primary_provenance_id",
        ):
            self.assertIn(field, evidence_sql)
        self.assertNotIn(
            "unique (artifact_set_id, subject_key, predicate)",
            evidence_sql,
        )
        self.assertIn("'numeric'", evidence_sql)

    def test_queue_has_leases_retries_dependencies_and_dead_letters(self):
        jobs_sql = self.files["005_jobs.sql"].casefold()
        for table in (
            "jobs",
            "job_dependencies",
            "job_attempts",
            "dead_letters",
            "job_events",
        ):
            self.assertIn(
                f"create table if not exists bauer_rag_v3.{table}",
                jobs_sql,
            )
        for field in (
            "idempotency_key",
            "attempt_count",
            "max_attempts",
            "leased_by",
            "lease_token",
            "lease_expires_at",
            "heartbeat_at",
            "retry_wait",
        ):
            self.assertIn(field, jobs_sql)
        self.assertIn(
            "unique (queue_name, idempotency_key)",
            jobs_sql,
        )

    def test_qa_eval_and_atomic_activation_gates_exist(self):
        qa_sql = self.files["006_qa_eval.sql"].casefold()
        rls_sql = self.files["007_rls_roles.sql"].casefold()
        for table in (
            "qa_checks",
            "review_decisions",
            "eval_suites",
            "independent_gold_attestations",
            "eval_cases",
            "eval_runs",
            "eval_results",
        ):
            self.assertIn(
                f"create table if not exists bauer_rag_v3.{table}",
                qa_sql,
            )
        for fragment in (
            "create or replace function bauer_rag_v3.activate_release",
            "create or replace function bauer_rag_v3.can_evaluate_release",
            "release_row.status in ('validating', 'ready')",
            "using (bauer_rag_v3.can_evaluate_release(release_id))",
            "pg_advisory_xact_lock",
            "expected_source_count",
            "unresolved blocking qa checks",
            "verified gold",
            "on conflict (kb_id)",
        ):
            self.assertIn(fragment, rls_sql)

    def test_rls_and_serving_indexes_exist(self):
        rls_sql = self.files["007_rls_roles.sql"].casefold()
        indexes_sql = self.files["008_indexes.sql"].casefold()
        for table in (
            "sources",
            "source_versions",
            "artifact_sets",
            "facts",
            "search_units",
            "exact_terms",
            "jobs",
            "qa_checks",
            "independent_gold_attestations",
            "eval_runs",
        ):
            self.assertIn(
                f"alter table bauer_rag_v3.{table} "
                "enable row level security",
                " ".join(rls_sql.split()),
            )
        for fragment in (
            "using gin (search_vector)",
            "gin_trgm_ops",
            "using hnsw (embedding vector_cosine_ops)",
            "where state in ('queued', 'retry_wait')",
            "where state = 'running'",
            "ix_v3_qa_unresolved_blocker",
        ):
            self.assertIn(fragment, " ".join(indexes_sql.split()))

    def test_runtime_roles_are_least_privilege_and_activation_is_admin_only(self):
        bootstrap_sql = self.files["009_runtime_roles.sql"].casefold()
        roles_sql = self.files["010_runtime_role_hardening.sql"].casefold()
        for role in (
            "bauer_rag_v3_reader",
            "bauer_rag_v3_ingester",
            "bauer_rag_v3_evaluator",
            "bauer_rag_v3_reviewer",
            "bauer_rag_v3_admin",
        ):
            self.assertIn(f"create role {role}", bootstrap_sql)
            self.assertIn(f"alter role {role}", roles_sql)
            self.assertIn("nologin", roles_sql)
            self.assertIn("nosuperuser", roles_sql)
            self.assertIn("nobypassrls", roles_sql)
        self.assertIn(
            "grant bauer_rag_v3_reader to bauer_rag_v3_reviewer",
            bootstrap_sql,
        )
        for forbidden_parent in (
            "bauer_rag_v3_ingester",
            "bauer_rag_v3_evaluator",
            "bauer_rag_v3_admin",
        ):
            self.assertNotIn(
                f"grant {forbidden_parent} to bauer_rag_v3_reviewer",
                bootstrap_sql,
            )
        self.assertIn(
            "revoke create on schema bauer_rag_v3 from public",
            roles_sql,
        )
        self.assertIn(
            "revoke execute on all functions in schema bauer_rag_v3",
            roles_sql,
        )
        self.assertIn(
            "grant execute on function bauer_rag_v3.activate_release( "
            "uuid, uuid, uuid, text, boolean ) "
            "to bauer_rag_v3_admin",
            " ".join(roles_sql.split()),
        )
        self.assertNotIn(
            "grant execute on all functions in schema bauer_rag_v3 "
            "to bauer_rag_v3_admin",
            " ".join(roles_sql.split()),
        )
        self.assertNotIn(
            "grant select on all tables in schema bauer_rag_v3 "
            "to bauer_rag_v3_reader",
            " ".join(roles_sql.split()),
        )

    def test_every_rls_policy_is_scoped_to_runtime_group_roles(self):
        policy_sources = "\n".join(
            (
                self.files["007_rls_roles.sql"],
                self.files["010_runtime_role_hardening.sql"],
            )
        ).casefold()
        scoping_sql = self.files["011_rls_policy_role_scoping.sql"].casefold()
        created = {
            (policy, table)
            for policy, table in re.findall(
                r"create\s+policy\s+([a-z0-9_]+)\s+"
                r"on\s+bauer_rag_v3\.([a-z0-9_]+)",
                policy_sources,
            )
        }
        altered = {
            (policy, table): tuple(
                role.strip()
                for role in role_list.split(",")
            )
            for policy, table, role_list in re.findall(
                r"alter\s+policy\s+([a-z0-9_]+)\s+"
                r"on\s+bauer_rag_v3\.([a-z0-9_]+)\s+"
                r"to\s+([^;]+);",
                scoping_sql,
            )
        }
        self.assertEqual(set(altered), created)
        allowed_roles = {
            "bauer_rag_v3_reader",
            "bauer_rag_v3_ingester",
            "bauer_rag_v3_evaluator",
            "bauer_rag_v3_reviewer",
            "bauer_rag_v3_admin",
        }
        for targets in altered.values():
            self.assertTrue(targets)
            self.assertTrue(set(targets) <= allowed_roles)
            self.assertNotIn("public", targets)

        expected_sensitive_scopes = {
            ("kb_read", "knowledge_bases"): ("bauer_rag_v3_reader",),
            ("kb_admin_write", "knowledge_bases"): ("bauer_rag_v3_admin",),
            ("source_read", "sources"): ("bauer_rag_v3_reader",),
            ("source_write", "sources"): ("bauer_rag_v3_ingester",),
            ("release_write", "knowledge_releases"): ("bauer_rag_v3_admin",),
            ("job_read", "jobs"): ("bauer_rag_v3_ingester",),
            ("job_write", "jobs"): ("bauer_rag_v3_ingester",),
            ("review_decision_write", "review_decisions"): (
                "bauer_rag_v3_evaluator",
            ),
            ("eval_run_write", "eval_runs"): ("bauer_rag_v3_evaluator",),
            (
                "independent_gold_attestation_read",
                "independent_gold_attestations",
            ): ("bauer_rag_v3_reviewer", "bauer_rag_v3_admin"),
            (
                "authorization_audit_admin_read",
                "authorization_audit",
            ): ("bauer_rag_v3_admin",),
        }
        for policy_key, expected_targets in expected_sensitive_scopes.items():
            self.assertEqual(altered[policy_key], expected_targets)

    def test_source_upsert_policy_uses_row_kb_capability(self):
        source_policy_sql = " ".join(
            self.files["012_source_upsert_rls.sql"].casefold().split()
        )
        self.assertIn(
            "alter policy source_write on bauer_rag_v3.sources "
            "to bauer_rag_v3_ingester",
            source_policy_sql,
        )
        self.assertEqual(
            source_policy_sql.count(
                "tenant_id = bauer_rag_v3.current_tenant_id() "
                "and bauer_rag_v3.can_ingest_kb(kb_id)"
            ),
            2,
        )
        policy_definition = source_policy_sql.split(
            "alter policy source_write",
            maxsplit=1,
        )[1]
        self.assertNotIn("can_write_source(source_id)", policy_definition)

    def test_compiler_qa_guard_locks_release_as_schema_owner(self):
        guard_sql = " ".join(
            self.files["013_qa_guard_lock_authority.sql"].casefold().split()
        )
        self.assertIn(
            "alter function bauer_rag_v3.guard_compiler_qa_mutation() "
            "security definer",
            guard_sql,
        )
        self.assertIn(
            "alter function bauer_rag_v3.guard_compiler_qa_mutation() "
            "set search_path = bauer_rag_v3, pg_temp",
            guard_sql,
        )
        self.assertIn(
            "revoke all on function "
            "bauer_rag_v3.guard_compiler_qa_mutation() from public, "
            "bauer_rag_v3_reader, bauer_rag_v3_ingester, "
            "bauer_rag_v3_evaluator, bauer_rag_v3_reviewer, "
            "bauer_rag_v3_admin",
            guard_sql,
        )
        self.assertNotIn("grant ", guard_sql)

    def test_release_membership_update_privilege_is_revoked(self):
        membership_sql = " ".join(
            self.files[
                "014_immutable_release_membership.sql"
            ].casefold().split()
        )
        self.assertIn(
            "revoke update on bauer_rag_v3.release_sources "
            "from bauer_rag_v3_ingester",
            membership_sql,
        )
        self.assertNotIn("grant ", membership_sql)

    def test_release_membership_registration_is_the_only_ingester_write_path(self):
        registration_sql = " ".join(
            self.files[
                "015_release_membership_registration.sql"
            ].casefold().split()
        )
        function_sql = registration_sql.split(
            "create or replace function "
            "bauer_rag_v3.register_release_source_membership",
            maxsplit=1,
        )[1].split("revoke all", maxsplit=1)[0]
        for requirement in (
            "security definer",
            "set search_path = bauer_rag_v3, pg_temp",
            "current_setting('app.knowledge_base_id', true)",
            "bauer_rag_v3.current_tenant_id() is null",
            "cardinality(bauer_rag_v3.current_principal_ids()) = 0",
            "not bauer_rag_v3.can_write_release(target_release_id)",
            "not bauer_rag_v3.can_write_source(target_source_id)",
            "on conflict (release_id, source_id) do nothing",
            "release already contains incompatible source membership",
        ):
            self.assertIn(requirement, function_sql)
        self.assertIn(
            "grant execute on function "
            "bauer_rag_v3.register_release_source_membership( "
            "uuid, uuid, uuid, uuid, uuid, integer, boolean ) "
            "to bauer_rag_v3_ingester",
            registration_sql,
        )
        self.assertIn(
            "revoke insert, update on bauer_rag_v3.release_sources "
            "from bauer_rag_v3_ingester",
            registration_sql,
        )
        self.assertNotIn(
            "grant update on bauer_rag_v3.knowledge_releases",
            registration_sql,
        )

    def test_reader_scope_sets_preserve_acl_rules_and_replace_hot_policies(self):
        scope_sql = " ".join(
            self.files["016_reader_rls_scope_sets.sql"].casefold().split()
        )
        for helper in (
            "current_knowledge_base_id()",
            "readable_kb_ids()",
            "readable_source_ids()",
            "readable_release_ids()",
            "readable_artifact_set_ids()",
            "readable_nav_node_ids()",
        ):
            self.assertIn(
                f"create or replace function bauer_rag_v3.{helper}",
                scope_sql,
            )
            self.assertIn(
                f"revoke all on function bauer_rag_v3.{helper} from public",
                scope_sql,
            )
            self.assertIn(
                f"grant execute on function bauer_rag_v3.{helper} "
                "to bauer_rag_v3_reader",
                scope_sql,
            )
        for requirement in (
            "security definer",
            "grant_row.permission in ('read', 'ingest', 'admin')",
            "source_row.visibility = 'inherited'",
            "grant_row.permission in ('read', 'admin')",
            "alter policy search_unit_read",
            "alter policy provenance_access",
            "alter policy table_cell_access",
            "alter policy nav_edge_read",
        ):
            self.assertIn(requirement, scope_sql)

    def test_authorization_audit_checks_complete_scope_sets_once(self):
        audit_sql = " ".join(
            self.files[
                "017_authorization_audit_scope_set.sql"
            ].casefold().split()
        )
        for requirement in (
            "create or replace function "
            "bauer_rag_v3.record_authorization_audit(",
            "readable_kb_scope := bauer_rag_v3.readable_kb_ids()",
            "readable_release_scope := "
            "bauer_rag_v3.readable_release_ids()",
            "readable_source_scope := "
            "bauer_rag_v3.readable_source_ids()",
            "target_source_ids <@ readable_source_scope",
        ):
            self.assertIn(requirement, audit_sql)
        self.assertNotIn(
            "from unnest(target_source_ids)",
            audit_sql,
        )

    def test_nested_reader_scopes_are_materialized_once(self):
        scope_sql = " ".join(
            self.files[
                "018_materialize_reader_scope_dependencies.sql"
            ].casefold().split()
        )
        for requirement in (
            "with readable_kbs as materialized",
            "with readable_sources as materialized",
            "readable_releases as materialized",
            "join readable_sources",
            "join readable_releases",
        ):
            self.assertIn(requirement, scope_sql)
        self.assertNotIn(
            "artifact.source_id = any "
            "(bauer_rag_v3.readable_source_ids())",
            scope_sql,
        )

    def test_reader_policies_use_uncorrelated_scope_initplans(self):
        scope_sql = " ".join(
            self.files[
                "019_initplan_reader_scope_policies.sql"
            ].casefold().split()
        )
        scope_sql_without_spaces = scope_sql.replace(" ", "")
        for policy in (
            "kb_read",
            "source_read",
            "source_version_read",
            "release_read",
            "active_release_read",
            "release_source_read",
            "artifact_read",
            "page_access",
            "section_access",
            "block_access",
            "table_access",
            "table_segment_access",
            "table_cell_access",
            "provenance_access",
            "entity_access",
            "entity_mention_access",
            "fact_access",
            "fact_provenance_access",
            "search_unit_read",
            "exact_term_read",
            "nav_node_read",
            "nav_edge_read",
        ):
            self.assertIn(f"alter policy {policy}", scope_sql)
        for helper in (
            "readable_kb_ids()",
            "readable_source_ids()",
            "readable_release_ids()",
            "readable_artifact_set_ids()",
            "readable_nav_node_ids()",
        ):
            self.assertIn(
                f"selectunnest(bauer_rag_v3.{helper})",
                scope_sql_without_spaces,
            )
        self.assertNotIn(
            "app.authorized_source_ids",
            scope_sql,
        )

    def test_hardened_reader_cannot_read_control_or_gold_tables(self):
        roles_sql = self.files["010_runtime_role_hardening.sql"].casefold()
        compact = " ".join(roles_sql.split())
        reader_grant = compact.split(
            "-- compilation repositories",
            maxsplit=1,
        )[0].split(
            "-- serving registry",
            maxsplit=1,
        )[1]
        for serving_table in (
            "schema_migrations",
            "knowledge_bases",
            "knowledge_releases",
            "active_releases",
            "sources",
            "source_versions",
            "artifact_sets",
            "release_sources",
            "pages",
            "blocks",
            "tables",
            "table_cells",
            "provenance_spans",
            "facts",
            "search_units",
            "exact_terms",
            "nav_nodes",
            "nav_edges",
        ):
            self.assertIn(f"bauer_rag_v3.{serving_table}", reader_grant)
        for forbidden_table in (
            "tenants",
            "principals",
            "kb_grants",
            "source_grants",
            "objects",
            "release_activations",
            "jobs",
            "qa_checks",
            "review_decisions",
            "eval_suites",
            "independent_gold_attestations",
            "eval_cases",
            "eval_runs",
            "eval_results",
            "authorization_audit",
        ):
            self.assertNotIn(
                f"bauer_rag_v3.{forbidden_table}",
                reader_grant,
            )

    def test_hardened_ingester_cannot_mutate_control_acl_review_or_gold(self):
        roles_sql = self.files["010_runtime_role_hardening.sql"].casefold()
        compact = " ".join(roles_sql.split())
        mutation_grants = compact.split(
            "-- compilation repositories",
            maxsplit=1,
        )[1].split(
            "-- gold/review persistence",
            maxsplit=1,
        )[0]
        for required_table in (
            "objects",
            "sources",
            "source_versions",
            "artifact_sets",
            "release_sources",
            "search_units",
            "qa_checks",
            "jobs",
            "job_attempts",
            "dead_letters",
        ):
            self.assertIn(f"bauer_rag_v3.{required_table}", mutation_grants)
        for forbidden_table in (
            "tenants",
            "knowledge_bases",
            "principals",
            "kb_grants",
            "source_grants",
            "knowledge_releases",
            "active_releases",
            "release_activations",
            "review_decisions",
            "eval_suites",
            "independent_gold_attestations",
            "eval_cases",
            "eval_runs",
            "eval_results",
            "authorization_audit",
        ):
            self.assertNotIn(
                f"bauer_rag_v3.{forbidden_table}",
                mutation_grants,
            )
        update_grant = mutation_grants.split(
            "grant select, insert, update on",
            maxsplit=1,
        )[1].split(
            "to bauer_rag_v3_ingester",
            maxsplit=1,
        )[0]
        for immutable_evidence_table in (
            "pages",
            "sections",
            "blocks",
            "tables",
            "table_segments",
            "table_cells",
            "provenance_spans",
            "entities",
            "entity_mentions",
            "facts",
            "fact_provenance",
            "search_units",
            "exact_terms",
            "nav_edges",
        ):
            self.assertNotIn(
                f"bauer_rag_v3.{immutable_evidence_table}",
                update_grant,
            )

    def test_finalized_evidence_and_registry_identity_are_immutable(self):
        roles_sql = self.files["010_runtime_role_hardening.sql"].casefold()
        compact = " ".join(roles_sql.split())
        for trigger in (
            "trg_v3_guard_object_registry",
            "trg_v3_guard_source_registry",
            "trg_v3_guard_source_version_registry",
            "trg_v3_guard_artifact_set_transition",
            "trg_v3_guard_compiler_qa_mutation",
            "trg_v3_guard_release_spec",
            "trg_v3_guard_review_decision_append",
            "trg_v3_guard_eval_suite",
            "trg_v3_guard_eval_case_append",
            "trg_v3_guard_eval_case_insert",
            "trg_v3_guard_eval_run",
            "trg_v3_guard_eval_run_insert",
            "trg_v3_guard_eval_result_append",
            "trg_v3_guard_eval_result_insert",
            "trg_v3_guard_independent_gold_attestation_append",
        ):
            self.assertIn(f"create trigger {trigger}", compact)
        self.assertIn("finalized artifact sets are immutable", compact)
        self.assertIn(
            "immutable release specification fields cannot change",
            compact,
        )
        self.assertIn(
            "compiler qa is immutable when release is %",
            compact,
        )
        self.assertIn("terminal evaluation runs are immutable", compact)
        self.assertIn(
            "immutable evaluation suite manifest fields cannot change",
            compact,
        )
        self.assertIn(
            "evaluation cases are frozen after the first suite run",
            compact,
        )
        self.assertIn(
            "evaluation runs must be inserted in a clean running state",
            compact,
        )
        self.assertIn(
            "evaluation results require a running parent run",
            compact,
        )

    def test_release_projections_freeze_before_validation(self):
        roles_sql = self.files["010_runtime_role_hardening.sql"].casefold()
        compact = " ".join(roles_sql.split())
        predicate = compact.split(
            "create or replace function bauer_rag_v3.can_write_release",
            maxsplit=1,
        )[1].split(
            "create or replace function "
            "bauer_rag_v3.guard_release_specification",
            maxsplit=1,
        )[0]
        self.assertIn("release_row.status = 'building'", predicate)
        self.assertNotIn("'validating'", predicate)

    def test_admin_control_plane_has_no_blanket_or_truncate_grant(self):
        roles_sql = self.files["010_runtime_role_hardening.sql"].casefold()
        compact = " ".join(roles_sql.split())
        self.assertNotIn("grant all on all tables", compact)
        self.assertNotIn("grant all on all sequences", compact)
        self.assertNotIn("grant execute on all functions", compact)
        self.assertNotIn("grant truncate", compact)
        self.assertIn(
            "revoke truncate on all tables in schema bauer_rag_v3",
            compact,
        )
        self.assertIn(
            "grant update ( status, validation_started_at, ready_at, "
            "failed_at, retired_at, error ) on "
            "bauer_rag_v3.knowledge_releases",
            compact,
        )
        evaluator_grants = compact.split(
            "-- gold/review persistence",
            maxsplit=1,
        )[1].split(
            "-- control-plane privileges",
            maxsplit=1,
        )[0]
        self.assertIn(
            "grant update ( status, passed, aggregate_metrics, "
            "hard_failures, completed_at, error ) on "
            "bauer_rag_v3.eval_runs to bauer_rag_v3_evaluator",
            evaluator_grants,
        )
        admin_grants = compact.split(
            "-- control-plane privileges",
            maxsplit=1,
        )[1].split(
            "-- even a runtime admin",
            maxsplit=1,
        )[0]
        self.assertIn(
            "grant select on bauer_rag_v3.review_decisions, "
            "bauer_rag_v3.eval_suites, bauer_rag_v3.eval_cases, "
            "bauer_rag_v3.eval_runs, bauer_rag_v3.eval_results, "
            "bauer_rag_v3.independent_gold_attestations "
            "to bauer_rag_v3_admin",
            admin_grants,
        )
        self.assertNotIn("insert on bauer_rag_v3.eval_", admin_grants)
        self.assertNotIn("update (gold_status)", admin_grants)
        self.assertNotIn(
            "bauer_rag_v3.active_releases to bauer_rag_v3_admin",
            compact,
        )

    def test_independent_gold_requires_reviewer_attestation(self):
        qa_sql = self.files["006_qa_eval.sql"].casefold()
        rls_sql = self.files["007_rls_roles.sql"].casefold()
        roles_sql = self.files["010_runtime_role_hardening.sql"].casefold()
        compact_qa = " ".join(qa_sql.split())
        compact_rls = " ".join(rls_sql.split())
        compact_roles = " ".join(roles_sql.split())

        attestation_table = compact_qa.split(
            "create table if not exists "
            "bauer_rag_v3.independent_gold_attestations",
            maxsplit=1,
        )[1].split(
            "create table if not exists bauer_rag_v3.eval_cases",
            maxsplit=1,
        )[0]
        for field in (
            "gold_attestation_id uuid primary key",
            "tenant_id uuid not null",
            "eval_suite_id uuid not null unique",
            "manifest_sha256 char(64) not null",
            "reviewer_principal_id uuid not null",
            "reviewer_database_role text not null",
            "decision text not null",
            "review_evidence_sha256 char(64) not null",
            "attested_at timestamptz not null",
        ):
            self.assertIn(field, attestation_table)
        self.assertIn(
            "references bauer_rag_v3.eval_suites"
            "(tenant_id, eval_suite_id) on delete restrict",
            attestation_table,
        )
        self.assertIn(
            "references bauer_rag_v3.principals"
            "(tenant_id, principal_id) on delete restrict",
            attestation_table,
        )

        self.assertIn(
            "alter table bauer_rag_v3.independent_gold_attestations "
            "enable row level security",
            compact_rls,
        )
        self.assertIn(
            "create policy independent_gold_attestation_read on "
            "bauer_rag_v3.independent_gold_attestations for select "
            "using (tenant_id = bauer_rag_v3.current_tenant_id())",
            compact_rls,
        )
        self.assertIn(
            "attestation.manifest_sha256 = suite.manifest_sha256",
            compact_rls,
        )
        self.assertIn("attestation.decision = 'approve'", compact_rls)

        function_sql = compact_roles.split(
            "create or replace function "
            "bauer_rag_v3.attest_independent_gold",
            maxsplit=1,
        )[1].split(
            "create or replace function "
            "bauer_rag_v3.record_authorization_audit",
            maxsplit=1,
        )[0]
        for requirement in (
            "security definer",
            "bauer_rag_v3_reviewer",
            "bauer_rag_v3_reader",
            "cardinality(context_principal_ids) <> 1",
            "principal.tenant_id = context_tenant_id",
            "principal.principal_type = 'user'",
            "suite.manifest_sha256",
            "suite.gold_status",
            "'independent_bauer_verified'",
            "review_evidence_sha256",
            "session_user",
            "independent gold suite already has an attestation",
        ):
            self.assertIn(requirement, function_sql)
        for forbidden_tier in (
            "bauer_rag_v3_ingester",
            "bauer_rag_v3_evaluator",
            "bauer_rag_v3_admin",
        ):
            self.assertIn(
                f"not pg_has_role( session_user, '{forbidden_tier}', "
                "'member' )",
                function_sql,
            )
        for owner_catalog in (
            "pg_catalog.pg_namespace",
            "pg_catalog.pg_class",
            "pg_catalog.pg_proc",
        ):
            self.assertIn(owner_catalog, function_sql)
        self.assertIn(
            "grant select on "
            "bauer_rag_v3.independent_gold_attestations "
            "to bauer_rag_v3_reviewer",
            compact_roles,
        )
        self.assertIn(
            "grant execute on function "
            "bauer_rag_v3.attest_independent_gold( "
            "uuid, text, text, text ) to bauer_rag_v3_reviewer",
            compact_roles,
        )
        self.assertNotIn(
            "insert on bauer_rag_v3.independent_gold_attestations "
            "to bauer_rag_v3_reviewer",
            compact_roles,
        )
        attestation_table_grants = [
            statement
            for statement in re.findall(
                r"(?m)^[ \t]*grant\b.*?;",
                roles_sql,
                flags=re.DOTALL,
            )
            if "bauer_rag_v3.independent_gold_attestations" in statement
        ]
        self.assertEqual(len(attestation_table_grants), 2)
        for statement in attestation_table_grants:
            self.assertRegex(
                " ".join(statement.split()),
                r"^grant select on ",
            )
        self.assertEqual(
            compact_roles.count(
                "grant execute on function "
                "bauer_rag_v3.attest_independent_gold("
            ),
            1,
        )

    def test_authorization_audit_is_narrow_and_append_only(self):
        roles_sql = self.files["010_runtime_role_hardening.sql"].casefold()
        compact = " ".join(roles_sql.split())
        self.assertIn(
            "create table if not exists "
            "bauer_rag_v3.authorization_audit",
            compact,
        )
        self.assertIn(
            "create or replace function "
            "bauer_rag_v3.record_authorization_audit",
            compact,
        )
        self.assertIn("security definer", compact)
        self.assertIn(
            "authorization audit tenant does not match request context",
            compact,
        )
        self.assertIn(
            "authorization audit knowledge base is not readable",
            compact,
        )
        self.assertIn(
            "authorization audit release is not readable",
            compact,
        )
        self.assertIn(
            "authorization audit source scope is not readable",
            compact,
        )
        self.assertIn(
            "before update or delete on "
            "bauer_rag_v3.authorization_audit",
            compact,
        )
        self.assertIn(
            "revoke update, delete, truncate on "
            "bauer_rag_v3.authorization_audit "
            "from bauer_rag_v3_admin",
            compact,
        )
        self.assertIn(
            "revoke update on sequence "
            "bauer_rag_v3.authorization_audit_authorization_audit_id_seq "
            "from bauer_rag_v3_admin",
            compact,
        )
        self.assertIn(
            "grant execute on function "
            "bauer_rag_v3.record_authorization_audit",
            compact,
        )
        audit_table = compact.split(
            "create table if not exists "
            "bauer_rag_v3.authorization_audit",
            maxsplit=1,
        )[1].split(
            "create index if not exists "
            "ix_v3_authorization_audit_tenant_time",
            maxsplit=1,
        )[0]
        for forbidden_payload in (
            "prompt text",
            "answer text",
            "evidence text",
            "metadata jsonb",
        ):
            self.assertNotIn(forbidden_payload, audit_table)

    def test_future_objects_require_a_reviewed_explicit_grant(self):
        roles_sql = self.files["010_runtime_role_hardening.sql"].casefold()
        compact = " ".join(roles_sql.split())
        self.assertIn(
            "revoke select on tables from bauer_rag_v3_reader",
            compact,
        )
        self.assertIn(
            "revoke select, insert, update, delete on tables from "
            "bauer_rag_v3_ingester, bauer_rag_v3_evaluator, "
            "bauer_rag_v3_reviewer, "
            "bauer_rag_v3_admin",
            compact,
        )
        defaults = compact.split(
            "-- undo migration 009's unsafe future-object grants",
            maxsplit=1,
        )[1]
        self.assertNotIn("grant ", defaults)

    def test_readiness_verifier_contains_no_schema_mutation(self):
        source = MODULE_PATH.read_text(encoding="utf-8")
        tree = ast.parse(source)
        verifier = next(
            node
            for node in tree.body
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
            and node.name == "verify_schema_version"
        )
        verifier_source = ast.get_source_segment(source, verifier) or ""
        lowered = verifier_source.casefold()
        self.assertNotIn("run_migrations(", lowered)
        self.assertNotIn("create table", lowered)
        self.assertNotIn("alter table", lowered)


if __name__ == "__main__":
    unittest.main()
