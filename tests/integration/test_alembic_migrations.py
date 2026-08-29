"""Integration tests for Alembic database migration infrastructure.

Tests verify:
- Migration application to clean database
- Table creation verification
- Rollback functionality
- Re-application after rollback
- Helper script commands

The suite is self-contained: it migrates a throwaway SQLite file via
``sys.executable -m alembic``. No running services, no environment variables,
and no ``alembic`` on PATH are required.

Usage:
    pytest tests/integration/test_alembic_migrations.py -v
"""

import os
import shlex
import sqlite3
import subprocess
import sys
from collections import defaultdict
from pathlib import Path

import pytest

from faultmaven.models.rbac import ROLE_PERMISSIONS, Permission, Role
from faultmaven.models.rbac_seed import SYSTEM_ROLE_IDS

# Test database path
PROJECT_ROOT = Path(__file__).parent.parent.parent
TEST_DB = str(PROJECT_ROOT / "test_migration.db")

# Current head revision
HEAD_REVISION = "e9f0a1b2c3d4"  # current head (045 — suggestion validation verdict)
# Parent of the RBAC-seed migration (029). Downgrading here reverses the seed
# (029) regardless of no-op migrations stacked above it — more robust than a
# relative "downgrade -1", which follows whatever the current head is.
RBAC_SEED_PARENT_REVISION = "d0e1f2a3b4c5"  # 028 — polymorphic resource_shares


@pytest.fixture(scope="function")
def clean_database():
    """Ensure clean test database before each test."""
    # Remove any existing test database
    db_files = [TEST_DB, f"{TEST_DB}-shm", f"{TEST_DB}-wal"]
    for db_file in db_files:
        if os.path.exists(db_file):
            os.remove(db_file)

    yield

    # Cleanup after test
    for db_file in db_files:
        if os.path.exists(db_file):
            os.remove(db_file)


@pytest.fixture(scope="function")
def database_url():
    """Provide test database URL."""
    return f"sqlite:///{TEST_DB}"


def run_alembic(command: str, database_url: str) -> subprocess.CompletedProcess:
    """Run an alembic command against the interpreter running these tests.

    Alembic is invoked as ``sys.executable -m alembic`` so it always comes from
    the same environment as the test process. PATH is never consulted, so a
    stale or broken ``alembic`` shim elsewhere on PATH cannot hijack the run,
    and no ``.venv`` needs to exist next to this checkout (a git worktree has
    none).

    ``PYTHONPATH`` is prepended with the checkout root so ``alembic/env.py``'s
    ``import faultmaven`` binds to the tree under test even when the
    environment holds an editable install pointing at a different checkout.
    """
    env = os.environ.copy()
    env["DATABASE_URL"] = database_url
    existing_pythonpath = env.get("PYTHONPATH")
    env["PYTHONPATH"] = (
        f"{PROJECT_ROOT}{os.pathsep}{existing_pythonpath}"
        if existing_pythonpath
        else str(PROJECT_ROOT)
    )

    result = subprocess.run(
        [sys.executable, "-m", "alembic", *shlex.split(command)],
        cwd=PROJECT_ROOT,
        env=env,
        capture_output=True,
        text=True,
    )
    return result


def get_tables(db_path: str) -> list[str]:
    """Get list of tables from SQLite database."""
    conn = sqlite3.connect(db_path)
    cursor = conn.cursor()
    cursor.execute("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name;")
    tables = [row[0] for row in cursor.fetchall()]
    conn.close()
    return tables


def query_rows(db_path: str, sql: str) -> list[tuple]:
    """Run a read query against the SQLite database and return all rows."""
    conn = sqlite3.connect(db_path)
    try:
        cursor = conn.cursor()
        cursor.execute(sql)
        return cursor.fetchall()
    finally:
        conn.close()


def get_current_revision(database_url: str) -> str:
    """Get current alembic revision."""
    result = run_alembic("current", database_url)
    # Parse output like "424078e5aa04 (head)"
    output = result.stdout.strip()
    for line in output.split("\n"):
        if "INFO" not in line and line.strip():
            return line.split()[0] if line.split() else ""
    return ""


# Expected tables from all migrations.
# (agent_tool_calls v1 removed in storage redesign 2026-04 phase 1;
#  evidence_artifacts + standalone_evidence removed in phase 2;
#  evidence_needs + evidence_need_fulfillment added in migration 014;
#  agent_executions + agent_tool_calls dropped in migration 041 — the
#  orchestrator that wrote them was deleted in #982 and nothing replaced it)
EXPECTED_TABLES = [
    "alembic_version",
    "case_actions",
    "case_checkpoints",
    "case_entities",
    "case_messages",
    "case_tags",
    "cases",
    "causal_edges",
    "causal_node_evidence",
    "causal_nodes",
    "conversion_drafts",
    "conversion_jobs",
    "enterprises",
    "evidence",
    "evidence_need_fulfillment",
    "evidence_needs",
    "hypotheses",
    "hypothesis_evidence",
    "investigation_sessions",
    "knowledge_items",
    "knowledge_suggestions",
    "config_overrides",
    "oauth_authorization_codes",
    "operator_access_audit",
    "operator_access_grants",
    "organization_members",
    "organizations",
    "permissions",
    "reports",
    "resource_shares",
    "role_permissions",
    "roles",
    "solutions",
    "sso_org_mappings",
    "team_members",
    "teams",
    "uploaded_files",
    "user_audit_log",
    "users",
]


def test_head_revision_constant_matches_filesystem():
    """Flag when a new migration lands without bumping HEAD_REVISION.

    The constant stays hard-coded (not derived from ScriptDirectory) so the
    `alembic upgrade head` assertions in this module remain meaningful
    instead of tautological — this test catches the drift.
    """
    from alembic.config import Config
    from alembic.script import ScriptDirectory

    cfg = Config(str(PROJECT_ROOT / "alembic.ini"))
    sd = ScriptDirectory.from_config(cfg)
    heads = sd.get_heads()
    assert len(heads) == 1, f"Expected a single alembic head, got {heads}"
    actual_head = heads[0]
    assert HEAD_REVISION == actual_head, (
        f"HEAD_REVISION ({HEAD_REVISION}) is out of date. "
        f"Latest migration on disk is {actual_head}. "
        f"Update the constant in tests/integration/test_alembic_migrations.py."
    )


class TestAlembicMigrationInfrastructure:
    """Test suite for Alembic migration infrastructure."""

    def test_migration_applies_to_clean_database(self, clean_database, database_url):
        """Migration applies successfully to clean SQLite database."""
        result = run_alembic("upgrade head", database_url)

        assert result.returncode == 0, f"Migration failed: {result.stderr}"
        output = result.stderr + result.stdout
        assert (
            HEAD_REVISION in output
        ), f"Expected head revision {HEAD_REVISION} in migration output. Output: {output}"

    def test_tables_created_correctly(self, clean_database, database_url):
        """All expected tables are created after migration."""
        run_alembic("upgrade head", database_url)

        tables = get_tables(TEST_DB)

        assert len(tables) == len(
            EXPECTED_TABLES
        ), f"Expected {len(EXPECTED_TABLES)} tables, got {len(tables)}. Missing: {set(EXPECTED_TABLES) - set(tables)}, Extra: {set(tables) - set(EXPECTED_TABLES)}"
        for expected_table in EXPECTED_TABLES:
            assert expected_table in tables, f"Missing table: {expected_table}"

    def test_migration_revision_correct(self, clean_database, database_url):
        """Migration revision matches expected head revision."""
        run_alembic("upgrade head", database_url)

        revision = get_current_revision(database_url)

        assert (
            revision == HEAD_REVISION
        ), f"Expected revision {HEAD_REVISION}, got {revision}"

    def test_migration_rollback(self, clean_database, database_url):
        """Migration can be rolled back successfully."""
        run_alembic("upgrade head", database_url)

        tables_before = get_tables(TEST_DB)
        assert len(tables_before) == len(
            EXPECTED_TABLES
        ), f"Expected {len(EXPECTED_TABLES)} tables initially, got {len(tables_before)}"

        # Rollback to base (multiple migrations, downgrade base goes to empty)
        result = run_alembic("downgrade base", database_url)
        assert result.returncode == 0, f"Rollback failed: {result.stderr}"

        # After full rollback, only alembic_version should remain
        tables_after = get_tables(TEST_DB)
        assert (
            len(tables_after) <= 1
        ), f"Expected 0-1 tables after full rollback, got {len(tables_after)}: {tables_after}"

    def test_migration_reapply_after_rollback(self, clean_database, database_url):
        """Migration can be re-applied after rollback."""
        run_alembic("upgrade head", database_url)
        run_alembic("downgrade base", database_url)

        # Re-apply
        result = run_alembic("upgrade head", database_url)
        assert result.returncode == 0, f"Re-application failed: {result.stderr}"

        # Verify tables restored
        tables = get_tables(TEST_DB)
        assert len(tables) == len(
            EXPECTED_TABLES
        ), f"Expected {len(EXPECTED_TABLES)} tables after re-application, got {len(tables)}"
        assert (
            "knowledge_suggestions" in tables
        ), "knowledge_suggestions table should be restored"
        assert "config_overrides" in tables, "config_overrides table should be restored"

        # Verify revision (should be back at head)
        revision = get_current_revision(database_url)
        assert (
            revision == HEAD_REVISION
        ), f"Expected revision {HEAD_REVISION}, got {revision}"

    def test_migration_history_command(self, database_url):
        """Alembic history command works."""
        result = run_alembic("history", database_url)

        assert result.returncode == 0, f"History command failed: {result.stderr}"
        output = result.stdout + result.stderr
        assert (
            HEAD_REVISION in output
        ), f"Head revision should be in history. Output: {output}"
        assert (
            "clean_baseline" in output.lower()
        ), f"Clean baseline migration should be in history. Output: {output}"


class TestGlobalKbPlatformTierMigration:
    """Migration 033 (#770): global rows org-free, reversible WITH data.

    The downgrade restamps global rows to the standalone org; regression: it
    originally ran the restamp UPDATE against the still-present
    ``knowledge_items_global_org_check``, which fails on any database that
    actually contains global rows (i.e. every seeded deployment). Clean-DB
    up/down tests cannot catch that, so this one carries data.
    """

    _STANDALONE_ORG = "00000000-0000-0000-0000-000000000001"

    def _insert_rows(self):
        conn = sqlite3.connect(TEST_DB)
        try:
            conn.execute(
                "INSERT INTO knowledge_items "
                "(item_id, organization_id, scope, title, content, item_type) "
                "VALUES ('kb_aaaaaaaaaaaa', NULL, 'global', 'G', 'body', 'runbook')"
            )
            conn.execute(
                "INSERT INTO knowledge_items "
                "(item_id, organization_id, scope, owner_id, title, content, item_type) "
                "VALUES ('ki_p1', 'org-1', 'personal', NULL, 'P', 'body', 'runbook')"
            )
            conn.commit()
        finally:
            conn.close()

    def _org_by_item(self):
        return dict(
            query_rows(TEST_DB, "SELECT item_id, organization_id FROM knowledge_items")
        )

    def test_downgrade_restamps_and_upgrade_renormalizes_with_data(
        self, clean_database, database_url
    ):
        result = run_alembic("upgrade head", database_url)
        assert result.returncode == 0, result.stderr
        self._insert_rows()

        down = run_alembic("downgrade b4c5d6e7f8a9", database_url)
        assert (
            down.returncode == 0
        ), f"033 downgrade must succeed with global rows present: {down.stderr}"
        orgs = self._org_by_item()
        assert orgs["kb_aaaaaaaaaaaa"] == self._STANDALONE_ORG
        assert orgs["ki_p1"] == "org-1"

        up = run_alembic("upgrade head", database_url)
        assert up.returncode == 0, up.stderr
        orgs = self._org_by_item()
        assert orgs["kb_aaaaaaaaaaaa"] is None, "global rows renormalized org-free"
        assert orgs["ki_p1"] == "org-1"

    def test_check_constraint_enforced_after_upgrade(
        self, clean_database, database_url
    ):
        """A tenant-org-stamped global row is unrepresentable post-033."""
        result = run_alembic("upgrade head", database_url)
        assert result.returncode == 0, result.stderr

        conn = sqlite3.connect(TEST_DB)
        try:
            with pytest.raises(sqlite3.IntegrityError, match="global_org_check"):
                conn.execute(
                    "INSERT INTO knowledge_items "
                    "(item_id, organization_id, scope, title, content, item_type) "
                    "VALUES ('kb_bbbbbbbbbbbb', 'org-1', 'global', 'X', 'x', 'runbook')"
                )
        finally:
            conn.close()


class TestConversionLiveCaseUniquenessMigration:
    """Migration 034: at most one live case-conversion per case.

    A nullable ``conversion_jobs.live_case_id`` with a unique index is the
    multi-replica dedup backstop. Newest-wins initialization runs before the
    index is built, so a pre-migration DB that already raced (two live
    case-source jobs for one case) can still be upgraded: the newest job takes
    the key, the older stays NULL. Clean-DB up/down cannot exercise that, so
    this test seeds at the parent revision and upgrades across the boundary.
    """

    _PARENT = "c5d6e7f8a9b0"  # 033 — the revision before 034

    def _seed_two_live_jobs_one_case(self):
        """Two case-source jobs for the same case, each with a live draft, one
        newer than the other — the exact pre-migration duplicate the index
        would otherwise reject."""
        conn = sqlite3.connect(TEST_DB)
        try:
            for cid, created in (
                ("conv_old", "2026-07-22 10:00:00"),
                ("conv_new", "2026-07-22 12:00:00"),
            ):
                conn.execute(
                    "INSERT INTO conversion_jobs "
                    "(id, organization_id, scope, status, source_file_id, "
                    "source_type, case_id, created_at) "
                    "VALUES (?, 'org-1', 'personal', 'completed', ?, 'case', "
                    "'case-race', ?)",
                    (cid, f"file_{cid}", created),
                )
                conn.execute(
                    "INSERT INTO conversion_drafts "
                    "(id, organization_id, conversion_id, runbook_id, title, "
                    "file_path, status, source_type) "
                    "VALUES (?, 'org-1', ?, 'rb', 'T', '/x.md', 'draft', 'case')",
                    (f"{cid}_d", cid),
                )
            conn.commit()
        finally:
            conn.close()

    def test_newest_live_case_job_takes_key_oldest_stays_null(
        self, clean_database, database_url
    ):
        up = run_alembic(f"upgrade {self._PARENT}", database_url)
        assert up.returncode == 0, up.stderr
        self._seed_two_live_jobs_one_case()

        head = run_alembic("upgrade head", database_url)
        assert (
            head.returncode == 0
        ), f"034 upgrade must seed newest-wins then build the index: {head.stderr}"

        rows = dict(query_rows(TEST_DB, "SELECT id, live_case_id FROM conversion_jobs"))
        assert rows["conv_new"] == "case-race", "newest live job takes the key"
        assert rows["conv_old"] is None, "older duplicate live job stays NULL"

    def test_unique_index_exists_and_is_unique(self, clean_database, database_url):
        result = run_alembic("upgrade head", database_url)
        assert result.returncode == 0, result.stderr

        idx = query_rows(
            TEST_DB,
            "SELECT name, \"unique\" FROM pragma_index_list('conversion_jobs') "
            "WHERE name = 'uq_conversion_jobs_live_case_id'",
        )
        assert idx, "unique index uq_conversion_jobs_live_case_id must exist"
        assert idx[0][1] == 1, "the live_case_id index must be UNIQUE"


class TestRbacSeed:
    """Migration 029 seeds the system RBAC roles/permissions/grants.

    These assertions tie the frozen seed snapshot in the migration to the live
    authority model (``faultmaven/models/rbac.py``) and the runtime role-id
    constant (``rbac_seed.SYSTEM_ROLE_IDS``) — so the migration can never
    silently drift from either.
    """

    def test_system_roles_seeded_with_stable_ids(self, clean_database, database_url):
        """The three system roles exist with the IDs SYSTEM_ROLE_IDS promises."""
        run_alembic("upgrade head", database_url)

        rows = query_rows(
            TEST_DB, "SELECT role_id, name, scope, is_system_role FROM roles"
        )
        by_name = {
            name: (role_id, scope, is_sys) for role_id, name, scope, is_sys in rows
        }

        assert set(by_name) == {role.value for role in Role}
        for role in Role:
            role_id, scope, is_sys = by_name[role.value]
            assert role_id == SYSTEM_ROLE_IDS[role], f"stale id for {role.value}"
            assert scope == "organization"
            assert is_sys in (1, True)

    def test_permissions_seeded_match_enum(self, clean_database, database_url):
        """Every Permission in the model is seeded as a (resource, action) row."""
        run_alembic("upgrade head", database_url)

        rows = query_rows(TEST_DB, "SELECT resource, action FROM permissions")
        seeded = {f"{resource}:{action}" for resource, action in rows}

        assert seeded == {perm.value for perm in Permission}

    def test_role_permission_grants_match_model(self, clean_database, database_url):
        """role_permissions reproduces ROLE_PERMISSIONS exactly."""
        run_alembic("upgrade head", database_url)

        rows = query_rows(
            TEST_DB,
            "SELECT r.name, p.resource || ':' || p.action "
            "FROM role_permissions rp "
            "JOIN roles r ON r.role_id = rp.role_id "
            "JOIN permissions p ON p.permission_id = rp.permission_id",
        )
        actual = defaultdict(set)
        for role_name, perm_value in rows:
            actual[role_name].add(perm_value)

        expected = {
            role.value: {perm.value for perm in perms}
            for role, perms in ROLE_PERMISSIONS.items()
        }
        assert dict(actual) == expected

    def test_seed_is_reversible_and_idempotent(self, clean_database, database_url):
        """Downgrade removes exactly the seed; re-upgrade restores it without dupes."""
        run_alembic("upgrade head", database_url)
        assert len(query_rows(TEST_DB, "SELECT role_id FROM roles")) == 3

        # Step back to before 029 (tables remain; seed rows are deleted). Target
        # 029's parent explicitly so later no-op migrations (e.g. 030 RLS) don't
        # shift what a relative "downgrade -1" would reverse.
        result = run_alembic(f"downgrade {RBAC_SEED_PARENT_REVISION}", database_url)
        assert result.returncode == 0, f"downgrade failed: {result.stderr}"
        assert query_rows(TEST_DB, "SELECT role_id FROM roles") == []
        assert query_rows(TEST_DB, "SELECT permission_id FROM permissions") == []
        assert query_rows(TEST_DB, "SELECT role_id FROM role_permissions") == []

        # Re-apply — counts return to exactly the seed, no duplication.
        run_alembic("upgrade head", database_url)
        assert len(query_rows(TEST_DB, "SELECT role_id FROM roles")) == 3
        assert len(query_rows(TEST_DB, "SELECT permission_id FROM permissions")) == 14


class TestHelperScript:
    """Test suite for migration helper script."""

    def test_helper_script_exists_and_executable(self):
        """Helper script exists and is executable."""
        script_path = PROJECT_ROOT / "scripts" / "db_migrate.sh"

        assert script_path.exists(), "Helper script db_migrate.sh not found"
        assert os.access(script_path, os.X_OK), "Helper script is not executable"


class TestDatabaseSchemaIntegrity:
    """Test suite for database schema integrity."""

    def test_cases_table_structure(self, clean_database, database_url):
        """Cases table has correct structure."""
        run_alembic("upgrade head", database_url)

        conn = sqlite3.connect(TEST_DB)
        cursor = conn.cursor()
        cursor.execute("PRAGMA table_info(cases);")
        columns = {row[1]: row[2] for row in cursor.fetchall()}
        conn.close()

        expected_columns = [
            "case_id",
            "user_id",
            "organization_id",
            "title",
            "state",
            "created_at",
            "updated_at",
        ]

        for col in expected_columns:
            assert (
                col in columns
            ), f"Missing column in cases table: {col}. Available: {list(columns.keys())}"

    def test_foreign_keys_exist(self, clean_database, database_url):
        """Foreign key relationships are created."""
        run_alembic("upgrade head", database_url)

        conn = sqlite3.connect(TEST_DB)
        cursor = conn.cursor()
        cursor.execute("PRAGMA foreign_key_list(evidence);")
        fks = cursor.fetchall()
        conn.close()

        assert len(fks) > 0, "No foreign keys found on evidence table"

        fk_tables = [fk[2] for fk in fks]
        assert "cases" in fk_tables, "Evidence table should have FK to cases table"

    def test_config_overrides_structure(self, clean_database, database_url):
        """config_overrides table has correct structure (Phase 2: + category/source)."""
        run_alembic("upgrade head", database_url)

        conn = sqlite3.connect(TEST_DB)
        cursor = conn.cursor()
        cursor.execute("PRAGMA table_info(config_overrides);")
        columns = {row[1]: row[2] for row in cursor.fetchall()}
        conn.close()

        for col in ["key", "value", "category", "source", "updated_at", "updated_by"]:
            assert (
                col in columns
            ), f"Missing column in config_overrides: {col}. Available: {list(columns.keys())}"


class TestOperatorAccessAuditAppendOnly:
    """``operator_access_audit`` is append-only at the DATABASE layer (#813).

    The threat is the audited operator themselves. If UPDATE/DELETE were
    prevented only by "the repository exposes no such method", anyone reaching
    the database — including the operator whose access is recorded — could amend
    or erase their own trail, and the table would have no evidentiary value.
    These run the real migration and assert the triggers reject the writes.
    """

    @staticmethod
    def _insert(conn) -> int:
        cursor = conn.cursor()
        cursor.execute(
            "INSERT INTO operator_access_audit (action, operator_user_id, created_at) "
            "VALUES ('list', 'op-1', datetime('now'))"
        )
        conn.commit()
        return cursor.lastrowid

    def test_update_and_delete_are_rejected(self, clean_database, database_url):
        result = run_alembic("upgrade head", database_url)
        assert result.returncode == 0, result.stderr

        conn = sqlite3.connect(TEST_DB)
        try:
            row_id = self._insert(conn)
            assert row_id > 0, "INSERT must be allowed — the table is append-ONLY"

            for sql in (
                "UPDATE operator_access_audit SET action='content_open' "
                f"WHERE audit_id={row_id}",
                f"DELETE FROM operator_access_audit WHERE audit_id={row_id}",
            ):
                with pytest.raises(sqlite3.IntegrityError, match="append-only"):
                    conn.execute(sql)
            conn.rollback()

            cursor = conn.cursor()
            cursor.execute(
                f"SELECT action FROM operator_access_audit WHERE audit_id={row_id}"
            )
            assert cursor.fetchone() == (
                "list",
            ), "the record must be intact after tampering attempts"
        finally:
            conn.close()

    def test_unknown_action_is_rejected(self, clean_database, database_url):
        """A third, un-enumerated action would be an access category nobody
        classified as either metadata or content."""
        result = run_alembic("upgrade head", database_url)
        assert result.returncode == 0, result.stderr

        conn = sqlite3.connect(TEST_DB)
        try:
            with pytest.raises(sqlite3.IntegrityError, match="action_valid"):
                conn.execute(
                    "INSERT INTO operator_access_audit (action, created_at) "
                    "VALUES ('sneaky', datetime('now'))"
                )
        finally:
            conn.close()

    def test_deleting_an_audited_operator_is_not_blocked(
        self, clean_database, database_url
    ):
        """Removing a user must not be blocked by their own audit rows.

        A foreign key with ON DELETE SET NULL would execute as an UPDATE against
        this table, which the append-only trigger rejects — so deleting any
        operator who had ever been audited would fail. The column is
        deliberately not a foreign key for that reason; this pins it.
        """
        result = run_alembic("upgrade head", database_url)
        assert result.returncode == 0, result.stderr

        conn = sqlite3.connect(TEST_DB)
        try:
            conn.execute("PRAGMA foreign_keys=ON")
            conn.execute(
                "INSERT INTO users (user_id, enterprise_id, username, email, "
                "display_name, timezone, locale, is_active, is_email_verified, "
                "created_at, updated_at, account_kind) "
                "SELECT 'u-1', enterprise_id, 'op', 'op@example.com', 'Op', 'UTC', "
                "'en', 1, 0, datetime('now'), datetime('now'), 'individual' "
                "FROM enterprises LIMIT 1"
            )
            conn.execute(
                "INSERT INTO operator_access_audit (action, operator_user_id, created_at) "
                "VALUES ('list', 'u-1', datetime('now'))"
            )
            conn.commit()

            conn.execute("DELETE FROM users WHERE user_id='u-1'")
            conn.commit()

            cursor = conn.cursor()
            cursor.execute(
                "SELECT COUNT(*) FROM operator_access_audit WHERE operator_user_id='u-1'"
            )
            assert cursor.fetchone()[0] == 1, "the evidence must outlive the account"
        finally:
            conn.close()


class TestOperatorAccessGrantsImmutability:
    """A break-glass grant's justification is immutable at the DATABASE (#815).

    Revocation and approval are legitimate UPDATEs, so the table cannot simply
    be append-only. What must not change is *why* access was taken and *how
    long* it was allowed: an operator who can widen ``expires_at`` or rewrite
    ``reason`` after the fact has converted a time-boxed, justified read into
    whatever the review would find acceptable. These run the real migration and
    assert the triggers reject those writes.
    """

    # The columns migration 036 pins, paired with a value that differs from the
    # one the fixture inserts. Every one of them is swept, because the guarantee
    # is "the justification cannot be rewritten" — not "these two columns I
    # happened to test".
    IMMUTABLE_COLUMNS = {
        "grant_id": "'g-other'",
        "operator_user_id": "'op-other'",
        "operator_username": "'someone.else@example.com'",
        "target_case_id": "'case-other'",
        "target_organization_id": "'org-other'",
        "reason": "'a different justification entirely'",
        "created_at": "datetime('now', '-1 day')",
        "expires_at": "datetime('now', '+30 day')",
        "deployment_mode": "'standalone'",
    }

    @staticmethod
    def _insert(conn, grant_id: str = "g-1") -> None:
        conn.execute(
            "INSERT INTO operator_access_grants "
            "(grant_id, operator_user_id, target_case_id, target_organization_id, "
            " reason, created_at, expires_at, approval_state) "
            f"VALUES ('{grant_id}', 'op-1', 'case-1', 'org-1', "
            "'investigating a stuck investigation for the customer', "
            "datetime('now'), datetime('now', '+1 hour'), 'auto_approved')"
        )
        conn.commit()

    @pytest.mark.parametrize("column", sorted(IMMUTABLE_COLUMNS))
    def test_justification_columns_cannot_be_rewritten(
        self, clean_database, database_url, column
    ):
        result = run_alembic("upgrade head", database_url)
        assert result.returncode == 0, result.stderr

        conn = sqlite3.connect(TEST_DB)
        try:
            self._insert(conn)
            new_value = self.IMMUTABLE_COLUMNS[column]
            with pytest.raises(sqlite3.IntegrityError, match="immutable"):
                conn.execute(
                    f"UPDATE operator_access_grants SET {column}={new_value} "
                    "WHERE grant_id='g-1'"
                )
            conn.rollback()
        finally:
            conn.close()

    def test_revocation_and_approval_are_permitted(self, clean_database, database_url):
        """The mutable columns must stay mutable.

        A trigger that rejected every UPDATE would make the grant unrevokable,
        which removes the operator's ability to end their own access early — a
        strictly worse posture than the one it was meant to enforce.
        """
        result = run_alembic("upgrade head", database_url)
        assert result.returncode == 0, result.stderr

        conn = sqlite3.connect(TEST_DB)
        try:
            self._insert(conn)
            conn.execute(
                "UPDATE operator_access_grants SET revoked_at=datetime('now'), "
                "revoked_by='op-2' WHERE grant_id='g-1'"
            )
            conn.execute(
                "UPDATE operator_access_grants SET approval_state='approved', "
                "approved_by='op-2', approved_at=datetime('now') "
                "WHERE grant_id='g-1'"
            )
            conn.commit()

            cursor = conn.cursor()
            cursor.execute(
                "SELECT revoked_by, approval_state FROM operator_access_grants "
                "WHERE grant_id='g-1'"
            )
            assert cursor.fetchone() == ("op-2", "approved")
        finally:
            conn.close()

    def test_delete_is_rejected(self, clean_database, database_url):
        """A grant is the evidence of why an access was authorised."""
        result = run_alembic("upgrade head", database_url)
        assert result.returncode == 0, result.stderr

        conn = sqlite3.connect(TEST_DB)
        try:
            self._insert(conn)
            with pytest.raises(sqlite3.IntegrityError, match="cannot be deleted"):
                conn.execute("DELETE FROM operator_access_grants WHERE grant_id='g-1'")
            conn.rollback()
        finally:
            conn.close()

    @pytest.mark.parametrize(
        "sql,label",
        [
            pytest.param(
                "UPDATE operator_access_grants SET revoked_at=NULL, revoked_by=NULL "
                "WHERE grant_id='g-1'",
                "cleared",
                id="cleared",
            ),
            pytest.param(
                "UPDATE operator_access_grants SET revoked_at=datetime('now', '+1 day') "
                "WHERE grant_id='g-1'",
                "moved",
                id="moved-later",
            ),
        ],
    )
    def test_a_revoked_grant_cannot_be_un_revoked(
        self, clean_database, database_url, sql, label
    ):
        """Revocation is monotonic, enforced by the database.

        ``revoked_at`` cannot live in the immutable set — revoking would then be
        impossible — but leaving it freely mutable makes the ONE permitted update
        the one that *widens* access: clearing it brings a revoked grant back to
        life for the remainder of its TTL. Nothing above the database would stop
        that; the repository's read-modify-write guard only covers its own path.
        """
        result = run_alembic("upgrade head", database_url)
        assert result.returncode == 0, result.stderr

        conn = sqlite3.connect(TEST_DB)
        try:
            self._insert(conn)
            conn.execute(
                "UPDATE operator_access_grants SET revoked_at=datetime('now'), "
                "revoked_by='op-2' WHERE grant_id='g-1'"
            )
            conn.commit()

            with pytest.raises(sqlite3.IntegrityError, match="monotonic"):
                conn.execute(sql)
            conn.rollback()

            cursor = conn.cursor()
            cursor.execute(
                "SELECT revoked_at IS NOT NULL FROM operator_access_grants "
                "WHERE grant_id='g-1'"
            )
            assert cursor.fetchone() == (1,), f"the grant must stay revoked ({label})"
        finally:
            conn.close()

    @pytest.mark.parametrize("target", ["auto_approved", "approved", "pending"])
    def test_a_denied_grant_cannot_be_approved(
        self, clean_database, database_url, target
    ):
        """A denial is final, for the same reason a revocation is.

        ``approval_state`` cannot simply be pinned — ``pending → approved`` is
        the legitimate widening the approval seam exists to perform — so the
        guard has to name the direction it refuses. Swept across every state a
        denial could be flipped into, because the rule is "denial is terminal",
        not "denial cannot become approved".
        """
        result = run_alembic("upgrade head", database_url)
        assert result.returncode == 0, result.stderr

        conn = sqlite3.connect(TEST_DB)
        try:
            self._insert(conn)
            conn.execute(
                "UPDATE operator_access_grants SET approval_state='denied' "
                "WHERE grant_id='g-1'"
            )
            conn.commit()

            with pytest.raises(sqlite3.IntegrityError, match="denial is final"):
                conn.execute(
                    f"UPDATE operator_access_grants SET approval_state='{target}' "
                    "WHERE grant_id='g-1'"
                )
            conn.rollback()
        finally:
            conn.close()

    def test_pending_can_still_be_approved(self, clean_database, database_url):
        """The approval seam must keep working.

        A guard that pinned ``approval_state`` outright would make the
        customer-approval workstream a schema change rather than a transition —
        which is the whole reason the state machine ships now.
        """
        result = run_alembic("upgrade head", database_url)
        assert result.returncode == 0, result.stderr

        conn = sqlite3.connect(TEST_DB)
        try:
            self._insert(conn)
            conn.execute(
                "UPDATE operator_access_grants SET approval_state='pending' "
                "WHERE grant_id='g-1'"
            )
            conn.execute(
                "UPDATE operator_access_grants SET approval_state='approved', "
                "approved_by='customer-admin', approved_at=datetime('now') "
                "WHERE grant_id='g-1'"
            )
            conn.commit()

            cursor = conn.cursor()
            cursor.execute(
                "SELECT approval_state, approved_by FROM operator_access_grants "
                "WHERE grant_id='g-1'"
            )
            assert cursor.fetchone() == ("approved", "customer-admin")
        finally:
            conn.close()

    def test_the_first_revocation_is_still_permitted(
        self, clean_database, database_url
    ):
        """The monotonicity guard must not make a grant unrevokable.

        Pinning `revoked_at` unconditionally would remove the operator's ability
        to end their own access early — a strictly worse posture than the one the
        guard is meant to enforce.
        """
        result = run_alembic("upgrade head", database_url)
        assert result.returncode == 0, result.stderr

        conn = sqlite3.connect(TEST_DB)
        try:
            self._insert(conn)
            conn.execute(
                "UPDATE operator_access_grants SET revoked_at=datetime('now'), "
                "revoked_by='op-2' WHERE grant_id='g-1'"
            )
            conn.commit()

            cursor = conn.cursor()
            cursor.execute(
                "SELECT revoked_by FROM operator_access_grants WHERE grant_id='g-1'"
            )
            assert cursor.fetchone() == ("op-2",)
        finally:
            conn.close()

    def test_unknown_approval_state_is_rejected(self, clean_database, database_url):
        """A state outside the vocabulary would be silently non-live — or worse,
        silently live — depending on which predicate read it."""
        result = run_alembic("upgrade head", database_url)
        assert result.returncode == 0, result.stderr

        conn = sqlite3.connect(TEST_DB)
        try:
            with pytest.raises(sqlite3.IntegrityError, match="approval_state_valid"):
                conn.execute(
                    "INSERT INTO operator_access_grants "
                    "(grant_id, operator_user_id, target_case_id, "
                    " target_organization_id, reason, created_at, expires_at, "
                    " approval_state) "
                    "VALUES ('g-2', 'op-1', 'case-1', 'org-1', 'because', "
                    "datetime('now'), datetime('now', '+1 hour'), 'definitely_fine')"
                )
        finally:
            conn.close()

    def test_expiry_must_be_after_creation(self, clean_database, database_url):
        """A grant whose window has already closed at creation is not a window."""
        result = run_alembic("upgrade head", database_url)
        assert result.returncode == 0, result.stderr

        conn = sqlite3.connect(TEST_DB)
        try:
            with pytest.raises(sqlite3.IntegrityError, match="window_valid"):
                conn.execute(
                    "INSERT INTO operator_access_grants "
                    "(grant_id, operator_user_id, target_case_id, "
                    " target_organization_id, reason, created_at, expires_at, "
                    " approval_state) "
                    "VALUES ('g-3', 'op-1', 'case-1', 'org-1', 'because', "
                    "datetime('now'), datetime('now', '-1 hour'), 'auto_approved')"
                )
        finally:
            conn.close()


# Test markers for different categories
pytestmark = pytest.mark.integration
