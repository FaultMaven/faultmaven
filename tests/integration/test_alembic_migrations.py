"""Integration tests for Alembic database migration infrastructure.

Tests verify TASK-001 implementation:
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
    # Parse output like "da6856719b5f (head)"
    output = result.stdout.strip()
    for line in output.split("\n"):
        if "INFO" not in line and line.strip():
            return line.split()[0] if line.split() else ""
    return ""


class TestAlembicMigrationInfrastructure:
    """Test suite for TASK-001: Alembic Migration Infrastructure."""

    def test_migration_applies_to_clean_database(self, clean_database, database_url):
        """Test 1: Migration applies successfully to clean SQLite database."""
        # Apply migration
        result = run_alembic("upgrade head", database_url)

        # Verify success
        assert result.returncode == 0, f"Migration failed: {result.stderr}"
        assert (
            "Running upgrade  -> 0d4e538d666d" in result.stderr
            or "Running upgrade  -> 0d4e538d666d" in result.stdout
        ), f"Expected consolidated baseline migration. Output: {result.stderr}"

    def test_tables_created_correctly(self, clean_database, database_url):
        """Test 2: All expected tables are created after migration."""
        # Apply migration
        run_alembic("upgrade head", database_url)

        # Get tables
        tables = get_tables(TEST_DB)

        # Expected core tables (consolidated baseline migration creates all tables at once)
        expected_tables = [
            "agent_executions",
            "agent_tool_calls",
            "agent_tool_calls_v2",
            "alembic_version",
            "case_checkpoints",
            "case_messages",
            "case_status_transitions",
            "case_tags",
            "cases",
            "evidence",
            "evidence_artifacts",
            "hypotheses",
            "investigation_sessions",
            "knowledge_items",
            "knowledge_suggestions",
            "oauth_authorization_codes",
            "oauth_revoked_tokens",
            "organization_members",
            "organizations",
            "permissions",
            "role_permissions",
            "roles",
            "sessions",
            "solutions",
            "standalone_evidence",
            "team_members",
            "teams",
            "uploaded_files",
            "user_audit_log",
            "users",
        ]

        # Verify all expected tables exist
        assert len(tables) == len(
            expected_tables
        ), f"Expected {len(expected_tables)} tables, got {len(tables)}. Missing: {set(expected_tables) - set(tables)}, Extra: {set(tables) - set(expected_tables)}"
        for expected_table in expected_tables:
            assert expected_table in tables, f"Missing table: {expected_table}"

    def test_migration_revision_correct(self, clean_database, database_url):
        """Test 3: Migration revision matches expected head revision."""
        # Apply migration
        run_alembic("upgrade head", database_url)

        # Get current revision
        revision = get_current_revision(database_url)

        # Verify revision ID matches the latest migration (gap20_unified_data_processing)
        assert (
            revision == "c7f8e3a1d9b4"
        ), f"Expected revision c7f8e3a1d9b4 (gap20_unified_data_processing), got {revision}"

    def test_migration_rollback(self, clean_database, database_url):
        """Test 4: Migration can be rolled back successfully."""
        # Apply migration
        run_alembic("upgrade head", database_url)

        # Verify tables exist
        tables_before = get_tables(TEST_DB)
        assert (
            len(tables_before) == 30
        ), f"Expected 30 tables initially, got {len(tables_before)}"

        # Rollback latest migration (gap20_unified_data_processing)
        # Should revert to evidence_classification_redesign (a32b2452ebb2)
        result = run_alembic("downgrade -1", database_url)
        assert result.returncode == 0, f"Rollback failed: {result.stderr}"

        # Verify tables still exist (we're back at evidence_classification_redesign)
        tables_after = get_tables(TEST_DB)
        assert (
            len(tables_after) == 30
        ), f"Expected 30 tables after rollback, got {len(tables_after)}: {tables_after}"

        # Verify revision moved back to evidence_classification_redesign
        revision = get_current_revision(database_url)
        assert (
            revision == "a32b2452ebb2"
        ), f"Expected revision a32b2452ebb2 (evidence_classification_redesign) after rollback, got {revision}"

    def test_migration_reapply_after_rollback(self, clean_database, database_url):
        """Test 5: Migration can be re-applied after rollback."""
        # Apply migration
        run_alembic("upgrade head", database_url)

        # Rollback one migration
        run_alembic("downgrade -1", database_url)

        # Re-apply
        result = run_alembic("upgrade head", database_url)
        assert result.returncode == 0, f"Re-application failed: {result.stderr}"

        # Verify tables restored
        tables = get_tables(TEST_DB)
        assert (
            len(tables) == 30
        ), f"Expected 30 tables after re-application, got {len(tables)}"
        assert (
            "knowledge_suggestions" in tables
        ), "knowledge_suggestions table should be restored"
        assert "organizations" in tables, "organizations table should be restored"

        # Verify revision (should be back at head: gap20_unified_data_processing)
        revision = get_current_revision(database_url)
        assert (
            revision == "c7f8e3a1d9b4"
        ), f"Expected revision c7f8e3a1d9b4 (gap20_unified_data_processing), got {revision}"

    def test_migration_history_command(self, database_url):
        """Test 6: Alembic history command works."""
        result = run_alembic("history", database_url)

        assert result.returncode == 0, f"History command failed: {result.stderr}"
        # Check for consolidated baseline migration
        output = result.stdout + result.stderr
        assert (
            "0d4e538d666d" in output
        ), f"Consolidated baseline revision should be in history. Output: {output}"
        assert (
            "001_consolidated_baseline" in output
        ), f"Consolidated baseline name should be in history. Output: {output}"


class TestHelperScript:
    """Test suite for migration helper script."""

    def test_helper_script_exists_and_executable(self):
        """Test 7: Helper script exists and is executable."""
        script_path = PROJECT_ROOT / "scripts" / "db_migrate.sh"

        assert script_path.exists(), "Helper script db_migrate.sh not found"
        assert os.access(script_path, os.X_OK), "Helper script is not executable"

    @pytest.mark.skip(
        reason="Helper script requires alembic in PATH. "
        "Script assumes system alembic, not venv. "
        "Test would need environment setup to add .venv/bin to PATH."
    )
    def test_helper_script_status_command(self, clean_database, database_url):
        """Test 8: Helper script status command works."""
        # Apply migration first
        run_alembic("upgrade head", database_url)

        # Run helper script
        env = os.environ.copy()
        env["DATABASE_URL"] = database_url

        result = subprocess.run(
            "./scripts/db_migrate.sh status",
            shell=True,
            cwd=PROJECT_ROOT,
            env=env,
            capture_output=True,
            text=True,
        )

        assert result.returncode == 0, f"Helper script failed: {result.stderr}"
        assert (
            "011_add_standalone_evidence" in result.stdout
            or "011_add_standalone_evidence" in result.stderr
        )

    @pytest.mark.skip(
        reason="Helper script requires alembic in PATH. "
        "Script assumes system alembic, not venv. "
        "Test would need environment setup to add .venv/bin to PATH."
    )
    def test_helper_script_history_command(self, database_url):
        """Test 9: Helper script history command works."""
        env = os.environ.copy()
        env["DATABASE_URL"] = database_url

        result = subprocess.run(
            "./scripts/db_migrate.sh history",
            shell=True,
            cwd=PROJECT_ROOT,
            env=env,
            capture_output=True,
            text=True,
        )

        assert result.returncode == 0, f"Helper script history failed: {result.stderr}"
        assert "da6856719b5f" in result.stdout or "da6856719b5f" in result.stderr


class TestDatabaseSchemaIntegrity:
    """Test suite for database schema integrity."""

    def test_cases_table_structure(self, clean_database, database_url):
        """Test 10: Cases table has correct structure."""
        # Apply migration
        run_alembic("upgrade head", database_url)

        # Query table structure
        conn = sqlite3.connect(TEST_DB)
        cursor = conn.cursor()
        cursor.execute("PRAGMA table_info(cases);")
        columns = {row[1]: row[2] for row in cursor.fetchall()}
        conn.close()

        # Verify key columns exist (updated to match actual schema)
        expected_columns = [
            "case_id",
            "user_id",
            "organization_id",  # Migration 012 renamed org_id to organization_id
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
        """Test 11: Foreign key relationships are created."""
        # Apply migration
        run_alembic("upgrade head", database_url)

        # Check foreign keys on evidence table
        conn = sqlite3.connect(TEST_DB)
        cursor = conn.cursor()
        cursor.execute("PRAGMA foreign_key_list(evidence);")
        fks = cursor.fetchall()
        conn.close()

        # Verify at least one foreign key exists (evidence -> cases)
        assert len(fks) > 0, "No foreign keys found on evidence table"

        # Verify it references cases table
        fk_tables = [fk[2] for fk in fks]
        assert "cases" in fk_tables, "Evidence table should have FK to cases table"


# Test markers for different categories
pytestmark = pytest.mark.integration
