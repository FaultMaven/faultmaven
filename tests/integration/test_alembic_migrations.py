"""Integration tests for Alembic database migration infrastructure.

Tests verify:
- Migration application to clean database
- Table creation verification
- Rollback functionality
- Re-application after rollback
- Helper script commands

Usage:
    pytest tests/integration/test_alembic_migrations.py -v
"""

import os
import sqlite3
import subprocess
from pathlib import Path

import pytest

# Test database path
PROJECT_ROOT = Path(__file__).parent.parent.parent
TEST_DB = str(PROJECT_ROOT / "test_migration.db")

# Current head revision
HEAD_REVISION = "b2c3d4e5f607"  # current head (013 — cases.disposition_eligibility)


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
    """Run alembic command with proper environment."""
    env = os.environ.copy()
    env["DATABASE_URL"] = database_url

    # Try venv alembic first (local dev), fallback to PATH (CI)
    alembic_path = PROJECT_ROOT / ".venv" / "bin" / "alembic"
    alembic_cmd = str(alembic_path) if alembic_path.exists() else "alembic"

    result = subprocess.run(
        f"{alembic_cmd} {command}",
        shell=True,
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


def get_current_revision(database_url: str) -> str:
    """Get current alembic revision."""
    result = run_alembic("current", database_url)
    # Parse output like "424078e5aa04 (head)"
    output = result.stdout.strip()
    for line in output.split("\n"):
        if "INFO" not in line and line.strip():
            return line.split()[0] if line.split() else ""
    return ""


# Expected tables from all migrations
# 16 domain tables + 11 auth/RBAC tables + 1 config table + 2 conversion tables
# + 1 reports table + 1 case_entities (phase 4a) + alembic_version = 32
# (agent_tool_calls v1 removed in storage redesign 2026-04 phase 1;
#  evidence_artifacts + standalone_evidence removed in phase 2)
EXPECTED_TABLES = [
    "agent_executions",
    "agent_tool_calls",
    "alembic_version",
    "case_actions",
    "case_checkpoints",
    "case_entities",
    "case_messages",
    "case_tags",
    "cases",
    "conversion_drafts",
    "conversion_jobs",
    "enterprises",
    "evidence",
    "hypotheses",
    "hypothesis_evidence",
    "investigation_sessions",
    "knowledge_items",
    "knowledge_suggestions",
    "llm_config_overrides",
    "oauth_authorization_codes",
    "oauth_revoked_tokens",
    "organization_members",
    "organizations",
    "permissions",
    "reports",
    "role_permissions",
    "roles",
    "solutions",
    "team_members",
    "teams",
    "uploaded_files",
    "user_audit_log",
    "users",
]


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
        assert (
            "llm_config_overrides" in tables
        ), "llm_config_overrides table should be restored"

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
            "status",
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

    def test_llm_config_overrides_structure(self, clean_database, database_url):
        """LLM config overrides table has correct structure."""
        run_alembic("upgrade head", database_url)

        conn = sqlite3.connect(TEST_DB)
        cursor = conn.cursor()
        cursor.execute("PRAGMA table_info(llm_config_overrides);")
        columns = {row[1]: row[2] for row in cursor.fetchall()}
        conn.close()

        for col in ["key", "value", "updated_at", "updated_by"]:
            assert (
                col in columns
            ), f"Missing column in llm_config_overrides: {col}. Available: {list(columns.keys())}"


# Test markers for different categories
pytestmark = pytest.mark.integration
