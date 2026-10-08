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

import json
import os
import shlex
import sqlite3
import subprocess
import sys
from collections import defaultdict
from pathlib import Path

import pytest

from faultmaven.config.settings import set_env_var
from faultmaven.models.rbac import ROLE_PERMISSIONS, Permission, Role
from faultmaven.models.rbac_seed import SYSTEM_ROLE_IDS

# Test database path
PROJECT_ROOT = Path(__file__).parent.parent.parent
TEST_DB = str(PROJECT_ROOT / "test_migration.db")

# Current head revision. The chain is ADR-017's clean baseline (which replaced
# the 001-053 chain) plus additive revisions on top of it, so the seed
# assertions below reverse the whole schema with "downgrade base" and each
# additive revision is stepped over on its own.
#: 010_turn_receipts
HEAD_REVISION = "afdd293ca6ab"  # pragma: allowlist secret
#: The baseline, which every additive revision parents onto.
BASELINE_REVISION = "a1e0c17bd001"  # 001_enterprise_baseline
#: The first additive revision.
LLM_USAGE_REVISION = "65913afe773c"  # 002_llm_usage_ledger  # pragma: allowlist secret
#: The cross-enterprise operator case metadata functions.
ADMIN_CASE_METADATA_REVISION = "baa28e79ebab"  # 003_admin_case_metadata
#: The baseline's definer trigger guards re-settled.
DEFINER_TRIGGER_HARDENING_REVISION = "14d4bfdd406e"  # 004_definer_trigger_hardening
#: The definer functions re-created with no schema a caller can create in.
DEFINER_SEARCH_PATH_REVISION = "1c5a2ad13a65"  # 005_definer_search_path_without_public
#: ``006_kb_conversion_source_storage_ref_null``: KB conversion-source rows
#: stop carrying a filesystem path in storage_ref.
CONVERSION_SOURCE_REF_REVISION = "f37066de2792"  # pragma: allowlist secret
#: ``007_problem_status_single_source``: ``symptom_verified`` becomes
#: ``problem_status`` in the progress blob, and ``captured`` leaves
#: ``hypotheses.state``.
PROBLEM_STATUS_REVISION = "497ae8900ae2"
#: ``008_runbook_severity_admits_info``: ``conversion_drafts_severity_check``
#: admits the spec's severity vocabulary, ``info`` included (#1886).
RUNBOOK_SEVERITY_REVISION = "558d7f3cfed1"  # pragma: allowlist secret
DROP_CASE_CHECKPOINTS_REVISION = "1e713f2d0e74"  # pragma: allowlist secret
#: ``010_turn_receipts``: one row per committed keyed turn (#1888).
TURN_RECEIPTS_REVISION = "afdd293ca6ab"  # pragma: allowlist secret
#: The tables 002_llm_usage_ledger adds (#640).
LLM_USAGE_TABLES = ["llm_turn_spend", "llm_usage_daily"]


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
    set_env_var(env, "DATABASE_URL", database_url)
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


def run_helper_script(args: str, database_url: str) -> subprocess.CompletedProcess:
    """Run ``scripts/db_migrate.sh`` the way a developer does.

    The script calls a bare ``alembic``, so the directory of the interpreter
    running these tests goes first on PATH: the ``alembic`` it finds is the one
    installed next to that interpreter, not a shim elsewhere on PATH.
    ``PYTHONPATH`` pins ``import faultmaven`` to this checkout, as in
    ``run_alembic``.
    """
    env = os.environ.copy()
    set_env_var(env, "DATABASE_URL", database_url)
    env["PATH"] = f"{Path(sys.executable).parent}{os.pathsep}{env.get('PATH', '')}"
    existing_pythonpath = env.get("PYTHONPATH")
    env["PYTHONPATH"] = (
        f"{PROJECT_ROOT}{os.pathsep}{existing_pythonpath}"
        if existing_pythonpath
        else str(PROJECT_ROOT)
    )
    return subprocess.run(
        [str(PROJECT_ROOT / "scripts" / "db_migrate.sh"), *shlex.split(args)],
        cwd=PROJECT_ROOT,
        env=env,
        capture_output=True,
        text=True,
    )


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


# Every table the chain creates: the baseline's (ADR-017) and the LLM usage
# ledger 002 adds (#640). ``turn_usage`` replaces the organization-keyed
# ``organization_turn_usage``, ``sso_personal_enterprises`` replaces
# ``sso_personal_orgs``, and ``team_invitations`` is new: the consent record a
# team forms by. ``token_revocations`` (#828) is where revocation state lives
# when the cache does not outlive the process. ``case_checkpoints`` is gone:
# 009 retired case checkpoints (#1882). ``turn_receipts`` is 010's: one row per
# committed keyed turn (#1888).
EXPECTED_TABLES = [
    "alembic_version",
    "case_actions",
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
    "llm_turn_spend",
    "llm_usage_daily",
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
    "sso_personal_enterprises",
    "team_invitations",
    "team_members",
    "teams",
    "token_revocations",
    "turn_receipts",
    "turn_usage",
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
            "enterprise_baseline" in output.lower()
        ), f"The enterprise baseline should be in history. Output: {output}"


class TestFunctionOnlyRevisions:
    """Revisions 003, 004 and 005 change PostgreSQL functions and nothing else.

    SQLite has no row-level security to bypass and no definer functions, so on
    SQLite each is a no-op in both directions: stepped down to its parent and
    back up, it must not touch a single table. Their PostgreSQL halves:
    ``tests/integration/security/test_admin_case_metadata_postgres.py`` (003);
    ``tests/integration/security/test_definer_functions_postgres.py``, whose
    step test starts from what 004 leaves and asserts it, and steps 005 down to
    004 and back.
    """

    @pytest.mark.parametrize(
        "revision, parent",
        [
            pytest.param(
                ADMIN_CASE_METADATA_REVISION,
                LLM_USAGE_REVISION,
                id="003_admin_case_metadata",
            ),
            pytest.param(
                DEFINER_TRIGGER_HARDENING_REVISION,
                ADMIN_CASE_METADATA_REVISION,
                id="004_definer_trigger_hardening",
            ),
            pytest.param(
                DEFINER_SEARCH_PATH_REVISION,
                DEFINER_TRIGGER_HARDENING_REVISION,
                id="005_definer_search_path_without_public",
            ),
        ],
    )
    def test_steps_down_to_its_parent_and_up_without_touching_a_table(
        self, clean_database, database_url, revision, parent
    ):
        result = run_alembic(f"upgrade {revision}", database_url)
        assert result.returncode == 0, result.stderr
        before = get_tables(TEST_DB)

        result = run_alembic(f"downgrade {parent}", database_url)
        assert result.returncode == 0, result.stderr
        assert get_current_revision(database_url) == parent
        assert get_tables(TEST_DB) == before

        result = run_alembic(f"upgrade {revision}", database_url)
        assert result.returncode == 0, result.stderr
        assert get_current_revision(database_url) == revision
        assert get_tables(TEST_DB) == before


class TestLlmUsageLedgerRevision:
    """002_llm_usage_ledger is the chain's first ADDITIVE revision (#640).

    The baseline is not amended: a deployment receives these two tables through
    its normal migration run. So the revision must step down and back up on its
    own without touching anything the baseline owns, and the SQLite half of its
    constraints must hold, because standalone is where most of these rows live.
    """

    def test_downgrade_one_removes_exactly_the_ledger(
        self, clean_database, database_url
    ):
        assert run_alembic("upgrade head", database_url).returncode == 0
        before = get_tables(TEST_DB)
        assert set(LLM_USAGE_TABLES) <= set(before)

        # To the ledger's parent, stepping over whatever was added after it.
        result = run_alembic(f"downgrade {BASELINE_REVISION}", database_url)
        assert result.returncode == 0, result.stderr
        assert get_current_revision(database_url) == BASELINE_REVISION
        # Less the ledger and 010's receipts; plus the table the baseline
        # creates and 009 drops, which stepping down over 009 restores.
        assert get_tables(TEST_DB) == sorted(
            (set(before) - set(LLM_USAGE_TABLES) - {"turn_receipts"})
            | {"case_checkpoints"}
        )

        result = run_alembic("upgrade head", database_url)
        assert result.returncode == 0, result.stderr
        assert get_tables(TEST_DB) == before
        assert get_current_revision(database_url) == HEAD_REVISION

    @staticmethod
    def _daily(conn, kind: str, subject_id: str, actor: str = "u1") -> None:
        conn.execute(
            "INSERT INTO llm_usage_daily (enterprise_id, usage_date, "
            "billing_subject_kind, billing_subject_id, actor_user_id, provider, "
            "model, outcome) VALUES (?, '2026-09-29', ?, ?, ?, 'anthropic', "
            "'claude-sonnet-4-6', 'kept')",
            (
                TestStandaloneTenancySeed.STANDALONE_ENTERPRISE_ID,
                kind,
                subject_id,
                actor,
            ),
        )

    @pytest.mark.parametrize(
        "kind, subject_id",
        [("none", "org-1"), ("account", ""), ("organization", ""), ("team", "t1")],
    )
    def test_the_subject_id_is_tied_to_its_kind(
        self, clean_database, database_url, kind, subject_id
    ):
        """``none`` and only ``none`` carries the empty id, and there is no
        fourth kind."""
        assert run_alembic("upgrade head", database_url).returncode == 0
        conn = sqlite3.connect(TEST_DB)
        try:
            with pytest.raises(sqlite3.IntegrityError, match="llm_usage_daily_"):
                self._daily(conn, kind, subject_id)
        finally:
            conn.close()

    def test_a_row_with_no_subject_and_no_actor_is_admitted(
        self, clean_database, database_url
    ):
        """A job's call: metering must not refuse what the cap would."""
        assert run_alembic("upgrade head", database_url).returncode == 0
        conn = sqlite3.connect(TEST_DB)
        try:
            self._daily(conn, "none", "", actor="")
            conn.commit()
            assert query_rows(
                TEST_DB,
                "SELECT billing_subject_kind, actor_user_id FROM llm_usage_daily",
            ) == [("none", "")]
        finally:
            conn.close()

    def test_a_turn_row_goes_with_its_case(self, clean_database, database_url):
        """Q4: turn rows are deleted with their case; nothing else holds them."""
        assert run_alembic("upgrade head", database_url).returncode == 0
        enterprise = TestStandaloneTenancySeed.STANDALONE_ENTERPRISE_ID
        conn = sqlite3.connect(TEST_DB)
        try:
            conn.execute("PRAGMA foreign_keys=ON")
            conn.execute(
                "INSERT INTO users (user_id, enterprise_id, username, email, "
                "display_name, created_at, updated_at) VALUES ('u1', ?, 'u1', "
                "'u1@example.com', 'U One', datetime('now'), datetime('now'))",
                (enterprise,),
            )
            conn.execute(
                "INSERT INTO cases (case_id, enterprise_id, user_id, title, "
                "created_at, updated_at) VALUES ('case_1', ?, 'u1', 't', "
                "datetime('now'), datetime('now'))",
                (enterprise,),
            )
            conn.execute(
                "INSERT INTO llm_turn_spend (enterprise_id, case_id, turn_number, "
                "billing_subject_kind, billing_subject_id, occurred_at) "
                "VALUES (?, 'case_1', 1, 'account', 'u1', datetime('now'))",
                (enterprise,),
            )
            conn.commit()
            conn.execute("DELETE FROM cases WHERE case_id = 'case_1'")
            conn.commit()
            assert query_rows(TEST_DB, "SELECT * FROM llm_turn_spend") == []
        finally:
            conn.close()


class TestConversionSourceStorageRefRevision:
    """006 clears the filesystem paths two KB writers stored in
    ``uploaded_files.storage_ref`` (#836), and nothing else.

    Its PostgreSQL half, where the UPDATE runs under ``row_security = off``:
    ``tests/integration/security/test_conversion_source_storage_ref_postgres.py``.
    """

    #: ``(file_id, case_id, storage_ref, upload_source)`` as seeded at the
    #: parent revision, and what each ``storage_ref`` must be after the upgrade.
    #: The last two rows each fail exactly one term of the predicate, so each
    #: term is what keeps its row: the fourth carries a case, and the fifth is
    #: caseless but not a conversion source.
    SEED = [
        (
            "file_kb_path",
            None,
            "data/knowledge/global/pool-exhausted.md",
            "conversion_source",
        ),
        ("file_evidence", "case_1", "evidence/case_1/abc123", "file_upload"),
        ("file_kb_null", None, None, "conversion_source"),
        ("file_cased_source", "case_1", "evidence/case_1/def456", "conversion_source"),
        ("file_caseless_upload", None, "evidence/orphan/ghi789", "file_upload"),
    ]
    AFTER = {
        "file_kb_path": None,
        "file_evidence": "evidence/case_1/abc123",
        "file_kb_null": None,
        "file_cased_source": "evidence/case_1/def456",
        "file_caseless_upload": "evidence/orphan/ghi789",
    }

    def _seed(self) -> None:
        enterprise = TestStandaloneTenancySeed.STANDALONE_ENTERPRISE_ID
        conn = sqlite3.connect(TEST_DB)
        try:
            conn.execute("PRAGMA foreign_keys=ON")
            conn.execute(
                "INSERT INTO users (user_id, enterprise_id, username, email, "
                "display_name, created_at, updated_at) VALUES ('u1', ?, 'u1', "
                "'u1@example.com', 'U One', datetime('now'), datetime('now'))",
                (enterprise,),
            )
            conn.execute(
                "INSERT INTO cases (case_id, enterprise_id, user_id, title, "
                "created_at, updated_at) VALUES ('case_1', ?, 'u1', 't', "
                "datetime('now'), datetime('now'))",
                (enterprise,),
            )
            for file_id, case_id, storage_ref, upload_source in self.SEED:
                conn.execute(
                    "INSERT INTO uploaded_files (file_id, enterprise_id, case_id, "
                    "uploaded_by, filename, size_bytes, storage_ref, upload_source) "
                    "VALUES (?, ?, ?, 'u1', 'f.md', 1, ?, ?)",
                    (file_id, enterprise, case_id, storage_ref, upload_source),
                )
            conn.commit()
        finally:
            conn.close()

    @staticmethod
    def _refs() -> dict:
        return dict(
            query_rows(TEST_DB, "SELECT file_id, storage_ref FROM uploaded_files")
        )

    def test_only_the_conversion_source_paths_are_cleared(
        self, clean_database, database_url
    ):
        result = run_alembic(f"upgrade {DEFINER_SEARCH_PATH_REVISION}", database_url)
        assert result.returncode == 0, result.stderr
        self._seed()
        seeded = {file_id: ref for file_id, _, ref, _ in self.SEED}
        assert self._refs() == seeded

        result = run_alembic(f"upgrade {CONVERSION_SOURCE_REF_REVISION}", database_url)
        assert result.returncode == 0, result.stderr
        assert self._refs() == self.AFTER
        # The count is the paths removed: the row that was already NULL is not
        # in it.
        assert "cleared storage_ref on 1 KB conversion-source row(s)" in result.stderr

        # Nothing to restore: the downgrade steps back and leaves every row.
        result = run_alembic(f"downgrade {DEFINER_SEARCH_PATH_REVISION}", database_url)
        assert result.returncode == 0, result.stderr
        assert get_current_revision(database_url) == DEFINER_SEARCH_PATH_REVISION
        assert self._refs() == self.AFTER

    def test_the_postgresql_statements_turn_row_security_off_around_the_update(
        self,
    ):
        """Offline (``--sql``) needs no server, so the PostgreSQL ordering is
        checked here as well as in the PostgreSQL lane: off, the UPDATE, then
        back to the value before for whatever runs later in the transaction."""
        result = run_alembic(
            f"upgrade {DEFINER_SEARCH_PATH_REVISION}:{CONVERSION_SOURCE_REF_REVISION} "
            "--sql",
            "postgresql://offline@localhost/offline",
        )
        assert result.returncode == 0, result.stderr
        sql = result.stdout
        off = sql.index("SET LOCAL row_security = off;")
        update = sql.index(
            "UPDATE uploaded_files SET storage_ref = NULL WHERE upload_source = "
            "'conversion_source' AND case_id IS NULL AND storage_ref IS NOT NULL;"
        )
        restored = sql.index("SET LOCAL row_security TO DEFAULT;")
        assert off < update < restored, sql


class TestProblemStatusRevision:
    """007 moves ``symptom_verified`` to ``problem_status`` and retires the
    ``captured`` hypothesis state — and the SQLite table rebuild that drops
    ``captured`` from the CHECK keeps every row that references a hypothesis.

    Its PostgreSQL half, where the UPDATEs run under ``row_security = off``:
    ``tests/integration/security/test_problem_status_migration_postgres.py``.
    """

    #: ``(case_id, progress)`` as seeded at the parent revision.
    PROGRESS = [
        ("case_1", '{"symptom_verified": true, "cause_state": "unknown"}'),
        ("case_2", '{"symptom_verified": false}'),
        ("case_3", "{}"),
    ]
    #: ``(hypothesis_id, state)`` as seeded, all on case_1.
    HYPOTHESES = [("hyp_queued", "captured"), ("hyp_active", "active")]

    def _seed(self) -> None:
        enterprise = TestStandaloneTenancySeed.STANDALONE_ENTERPRISE_ID
        conn = sqlite3.connect(TEST_DB)
        try:
            conn.execute("PRAGMA foreign_keys=ON")
            conn.execute(
                "INSERT INTO users (user_id, enterprise_id, username, email, "
                "display_name, created_at, updated_at) VALUES ('u1', ?, 'u1', "
                "'u1@example.com', 'U One', datetime('now'), datetime('now'))",
                (enterprise,),
            )
            for case_id, progress in self.PROGRESS:
                conn.execute(
                    "INSERT INTO cases (case_id, enterprise_id, user_id, title, "
                    "progress, created_at, updated_at) VALUES (?, ?, 'u1', 't', ?, "
                    "datetime('now'), datetime('now'))",
                    (case_id, enterprise, progress),
                )
            for hypothesis_id, state in self.HYPOTHESES:
                conn.execute(
                    "INSERT INTO hypotheses (hypothesis_id, enterprise_id, case_id, "
                    "statement, category, state) VALUES (?, ?, 'case_1', 's', "
                    "'code', ?)",
                    (hypothesis_id, enterprise, state),
                )
            conn.execute(
                "INSERT INTO evidence (evidence_id, enterprise_id, case_id, "
                "category, summary) VALUES ('ev_1', ?, 'case_1', "
                "'symptom_evidence', 's')",
                (enterprise,),
            )
            conn.execute(
                "INSERT INTO hypothesis_evidence (hypothesis_id, evidence_id, "
                "enterprise_id, relationship_type) VALUES "
                "('hyp_queued', 'ev_1', ?, 'supports')",
                (enterprise,),
            )
            conn.commit()
        finally:
            conn.close()

    @staticmethod
    def _progress() -> dict:
        return {
            case_id: json.loads(progress)
            for case_id, progress in query_rows(
                TEST_DB, "SELECT case_id, progress FROM cases"
            )
        }

    @staticmethod
    def _hypotheses() -> dict:
        return {
            hypothesis_id: (state, reason)
            for hypothesis_id, state, reason in query_rows(
                TEST_DB,
                "SELECT hypothesis_id, state, retirement_reason FROM hypotheses",
            )
        }

    def test_upgrade_moves_the_key_retires_the_queue_and_keeps_the_links(
        self, clean_database, database_url
    ):
        result = run_alembic(f"upgrade {CONVERSION_SOURCE_REF_REVISION}", database_url)
        assert result.returncode == 0, result.stderr
        self._seed()

        result = run_alembic(f"upgrade {PROBLEM_STATUS_REVISION}", database_url)
        assert result.returncode == 0, result.stderr
        assert "moved symptom_verified on 2 row(s)" in result.stderr
        assert "retired queued hypotheses on 1 row(s)" in result.stderr

        progress = self._progress()
        assert progress["case_1"] == {
            "problem_status": "verified",
            "cause_state": "unknown",
        }
        assert progress["case_2"] == {"problem_status": "unverified"}
        assert progress["case_3"] == {}  # no key: the field's default applies

        hypotheses = self._hypotheses()
        assert hypotheses["hyp_active"] == ("active", None)
        state, reason = hypotheses["hyp_queued"]
        assert state == "retired"
        assert reason == "Proposed before the problem was verified, and never pursued."

        # The rebuild ran with foreign keys off: the link survives.
        assert query_rows(TEST_DB, "SELECT hypothesis_id FROM hypothesis_evidence") == [
            ("hyp_queued",)
        ]

        conn = sqlite3.connect(TEST_DB)
        try:
            with pytest.raises(sqlite3.IntegrityError, match="hypotheses_state_check"):
                conn.execute(
                    "INSERT INTO hypotheses (hypothesis_id, enterprise_id, "
                    "case_id, statement, category, state) VALUES ('hyp_x', ?, "
                    "'case_1', 's', 'code', 'captured')",
                    (TestStandaloneTenancySeed.STANDALONE_ENTERPRISE_ID,),
                )
            conn.execute(
                "INSERT INTO hypotheses (hypothesis_id, enterprise_id, case_id, "
                "statement, category) VALUES ('hyp_default', ?, 'case_1', 's', "
                "'code')",
                (TestStandaloneTenancySeed.STANDALONE_ENTERPRISE_ID,),
            )
            conn.commit()
        finally:
            conn.close()
        assert self._hypotheses()["hyp_default"] == ("active", None)

    def test_downgrade_restores_exactly_what_it_moved(
        self, clean_database, database_url
    ):
        result = run_alembic(f"upgrade {CONVERSION_SOURCE_REF_REVISION}", database_url)
        assert result.returncode == 0, result.stderr
        self._seed()
        before_progress = self._progress()
        result = run_alembic(f"upgrade {PROBLEM_STATUS_REVISION}", database_url)
        assert result.returncode == 0, result.stderr

        result = run_alembic(
            f"downgrade {CONVERSION_SOURCE_REF_REVISION}", database_url
        )
        assert result.returncode == 0, result.stderr
        assert get_current_revision(database_url) == CONVERSION_SOURCE_REF_REVISION
        assert self._progress() == before_progress
        assert self._hypotheses() == {
            "hyp_queued": ("captured", None),
            "hyp_active": ("active", None),
        }
        assert query_rows(TEST_DB, "SELECT hypothesis_id FROM hypothesis_evidence") == [
            ("hyp_queued",)
        ]

    def test_the_rebuild_refuses_to_run_with_foreign_keys_on(self):
        """With foreign keys enforced, dropping the old table would run the ON
        DELETE actions of the rows that reference it. The guard reads the
        pragma on the migration's own connection."""
        import importlib.util

        path = next(
            (PROJECT_ROOT / "alembic" / "versions").glob(
                f"*_{PROBLEM_STATUS_REVISION}_*.py"
            )
        )
        spec = importlib.util.spec_from_file_location("rev_007", path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)

        class _Bind:
            def execute(self, _statement):
                class _Result:
                    @staticmethod
                    def scalar():
                        return 1

                return _Result()

        class _Context:
            as_sql = False

        class _Op:
            @staticmethod
            def get_context():
                return _Context()

            @staticmethod
            def get_bind():
                return _Bind()

        module.op = _Op()
        with pytest.raises(RuntimeError, match="foreign_keys=OFF"):
            module._refuse_cascading_rebuild()

    def test_the_postgresql_statements_turn_row_security_off_around_the_updates(
        self,
    ):
        """Offline (``--sql``) needs no server: off, both UPDATEs, back to the
        value before, then the constraint and default change in place."""
        result = run_alembic(
            f"upgrade {CONVERSION_SOURCE_REF_REVISION}:{PROBLEM_STATUS_REVISION} "
            "--sql",
            "postgresql://offline@localhost/offline",
        )
        assert result.returncode == 0, result.stderr
        sql = result.stdout
        off = sql.index("SET LOCAL row_security = off;")
        progress = sql.index(
            "UPDATE cases SET progress = (progress - 'symptom_verified')"
        )
        retire = sql.index("UPDATE hypotheses SET state = 'retired'")
        restored = sql.index("SET LOCAL row_security TO DEFAULT;")
        drop = sql.index(
            "ALTER TABLE hypotheses DROP CONSTRAINT hypotheses_state_check;"
        )
        assert off < progress < retire < restored < drop, sql
        assert "'captured'" not in sql[drop:], sql


class TestRunbookSeverityRevision:
    """008 widens ``conversion_drafts_severity_check`` to the spec's severity
    vocabulary, ``info`` included (#1886). On SQLite it rebuilds the table, so
    the rows, the indexes and the other constraints must come back unchanged.
    That the widened set equals ``RunbookSeverity`` is pinned by
    ``tests/unit/modules/knowledge/test_runbook_taxonomy_one_owner.py``.
    """

    #: ``(id, runbook_id, status, severity)`` seeded at the parent revision.
    DRAFTS = [
        ("d_high", "rb-high", "verified", "high"),
        ("d_none", "rb-none", "draft", None),
        ("d_gone", "rb-high", "discarded", "low"),
    ]

    @staticmethod
    def _connect() -> sqlite3.Connection:
        return sqlite3.connect(TEST_DB)

    def _seed(self) -> None:
        enterprise = TestStandaloneTenancySeed.STANDALONE_ENTERPRISE_ID
        conn = self._connect()
        try:
            conn.execute(
                "INSERT INTO conversion_jobs (id, enterprise_id, source_file_id, "
                "scope) VALUES ('job_1', ?, 'file_1', 'personal')",
                (enterprise,),
            )
            for draft_id, runbook_id, status, severity in self.DRAFTS:
                self._insert(conn, draft_id, runbook_id, status, severity)
            conn.commit()
        finally:
            conn.close()

    @staticmethod
    def _insert(conn, draft_id, runbook_id, status, severity) -> None:
        conn.execute(
            "INSERT INTO conversion_drafts (id, enterprise_id, conversion_id, "
            "runbook_id, title, file_path, status, severity) VALUES "
            "(?, ?, 'job_1', ?, 't', 'f.md', ?, ?)",
            (
                draft_id,
                TestStandaloneTenancySeed.STANDALONE_ENTERPRISE_ID,
                runbook_id,
                status,
                severity,
            ),
        )

    @staticmethod
    def _drafts() -> list:
        return query_rows(
            TEST_DB,
            "SELECT id, runbook_id, status, severity FROM conversion_drafts "
            "ORDER BY id",
        )

    @staticmethod
    def _indexes() -> list:
        return query_rows(
            TEST_DB,
            "SELECT name, sql FROM sqlite_master WHERE type = 'index' "
            "AND tbl_name = 'conversion_drafts' ORDER BY name",
        )

    def test_upgrade_admits_info_and_keeps_rows_and_indexes(
        self, clean_database, database_url
    ):
        result = run_alembic(f"upgrade {PROBLEM_STATUS_REVISION}", database_url)
        assert result.returncode == 0, result.stderr
        self._seed()
        rows, indexes = self._drafts(), self._indexes()
        conn = self._connect()
        try:
            with pytest.raises(sqlite3.IntegrityError, match="severity_check"):
                self._insert(conn, "d_info", "rb-info", "draft", "info")
        finally:
            conn.close()

        result = run_alembic(f"upgrade {RUNBOOK_SEVERITY_REVISION}", database_url)
        assert result.returncode == 0, result.stderr
        assert self._drafts() == rows
        # The partial unique index among them: the rebuild re-creates every
        # index from the frozen definition, predicate included.
        assert self._indexes() == indexes
        assert any("status <> 'discarded'" in (sql or "") for _, sql in indexes)

        conn = self._connect()
        try:
            self._insert(conn, "d_info", "rb-info", "draft", "info")
            with pytest.raises(sqlite3.IntegrityError, match="severity_check"):
                self._insert(conn, "d_bad", "rb-bad", "draft", "urgent")
            with pytest.raises(sqlite3.IntegrityError, match="status_check"):
                self._insert(conn, "d_bad", "rb-bad", "pending", "low")
            conn.commit()
        finally:
            conn.close()

    def test_downgrade_refuses_while_an_info_row_exists(
        self, clean_database, database_url
    ):
        result = run_alembic(f"upgrade {RUNBOOK_SEVERITY_REVISION}", database_url)
        assert result.returncode == 0, result.stderr
        self._seed()
        conn = self._connect()
        try:
            self._insert(conn, "d_info", "rb-info", "draft", "info")
            conn.commit()
        finally:
            conn.close()

        result = run_alembic(f"downgrade {PROBLEM_STATUS_REVISION}", database_url)
        assert result.returncode != 0
        assert "1 conversion_drafts row(s) hold severity 'info'" in result.stderr
        assert get_current_revision(database_url) == RUNBOOK_SEVERITY_REVISION

        conn = self._connect()
        try:
            conn.execute("DELETE FROM conversion_drafts WHERE id = 'd_info'")
            conn.commit()
        finally:
            conn.close()
        rows = self._drafts()
        result = run_alembic(f"downgrade {PROBLEM_STATUS_REVISION}", database_url)
        assert result.returncode == 0, result.stderr
        assert get_current_revision(database_url) == PROBLEM_STATUS_REVISION
        assert self._drafts() == rows
        conn = self._connect()
        try:
            with pytest.raises(sqlite3.IntegrityError, match="severity_check"):
                self._insert(conn, "d_info", "rb-info", "draft", "info")
        finally:
            conn.close()

    def test_no_foreign_key_targets_the_rebuilt_table(
        self, clean_database, database_url
    ):
        """Why the SQLite rebuild carries no ``PRAGMA foreign_keys`` guard,
        unlike 007's: dropping a table runs ON DELETE actions only for rows
        that REFERENCE it, and nothing does. If a table ever gains such a key,
        this fails and the rebuild needs 007's guard."""
        result = run_alembic("upgrade head", database_url)
        assert result.returncode == 0, result.stderr
        referencing = []
        for (table,) in query_rows(
            TEST_DB, "SELECT name FROM sqlite_master WHERE type = 'table'"
        ):
            for row in query_rows(TEST_DB, f'PRAGMA foreign_key_list("{table}")'):
                if row[2] == "conversion_drafts":
                    referencing.append(table)
        assert referencing == []

    def test_the_downgrade_count_runs_with_row_security_off_on_postgresql(self):
        """``--sql`` cannot show it (offline, the guard has no rows to count),
        so the guard runs against a recording connection: off, the count, back
        to the value before — tenant-wide by construction, as in 006 and 007.
        On SQLite, which has no row security, only the count runs."""
        import importlib.util

        path = next(
            (PROJECT_ROOT / "alembic" / "versions").glob(
                f"*_{RUNBOOK_SEVERITY_REVISION}_*.py"
            )
        )
        spec = importlib.util.spec_from_file_location("rev_008", path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)

        def run_guard(dialect: str) -> list[str]:
            executed: list[str] = []

            class _Result:
                @staticmethod
                def scalar():
                    return 0

            class _Bind:
                def execute(self, statement):
                    executed.append(str(statement))
                    return _Result()

            class _Context:
                as_sql = False

            _Context.dialect = type("D", (), {"name": dialect})()

            class _Op:
                @staticmethod
                def get_context():
                    return _Context()

                @staticmethod
                def get_bind():
                    return _Bind()

            module.op = _Op()
            module._refuse_while_info_rows_exist()
            return executed

        assert run_guard("postgresql") == [
            "SET LOCAL row_security = off",
            str(module.COUNT_INFO_ROWS),
            "SET LOCAL row_security TO DEFAULT",
        ]
        assert run_guard("sqlite") == [str(module.COUNT_INFO_ROWS)]

    def test_the_postgresql_statements_alter_the_constraint_in_place(self):
        """Offline (``--sql``): drop and re-add, no data statement, no
        ``row_security`` change."""
        result = run_alembic(
            f"upgrade {PROBLEM_STATUS_REVISION}:{RUNBOOK_SEVERITY_REVISION} --sql",
            "postgresql://offline@localhost/offline",
        )
        assert result.returncode == 0, result.stderr
        sql = result.stdout
        drop = sql.index(
            "ALTER TABLE conversion_drafts DROP CONSTRAINT "
            "conversion_drafts_severity_check;"
        )
        add = sql.index(
            "ALTER TABLE conversion_drafts ADD CONSTRAINT "
            "conversion_drafts_severity_check CHECK (severity IS NULL OR severity "
            "IN ('critical', 'high', 'medium', 'low', 'info'));"
        )
        assert drop < add, sql
        assert "row_security" not in sql
        assert "UPDATE conversion_drafts" not in sql


class TestDropCaseCheckpointsRevision:
    """009 retires case checkpoints (#1882): ``case_checkpoints`` is dropped,
    and its downgrade recreates the table, empty, exactly as 008 had it."""

    @staticmethod
    def _schema() -> list:
        """The table and its indexes, as SQLite stores their DDL."""
        return query_rows(
            TEST_DB,
            "SELECT type, name, sql FROM sqlite_master "
            "WHERE tbl_name = 'case_checkpoints' ORDER BY type, name",
        )

    def test_upgrade_drops_the_table(self, clean_database, database_url):
        result = run_alembic(f"upgrade {RUNBOOK_SEVERITY_REVISION}", database_url)
        assert result.returncode == 0, result.stderr
        assert self._schema(), "positive control: 008 has the table"

        result = run_alembic(f"upgrade {DROP_CASE_CHECKPOINTS_REVISION}", database_url)
        assert result.returncode == 0, result.stderr
        assert self._schema() == []

    def test_downgrade_recreates_the_table_as_the_parent_had_it(
        self, clean_database, database_url
    ):
        result = run_alembic(f"upgrade {RUNBOOK_SEVERITY_REVISION}", database_url)
        assert result.returncode == 0, result.stderr
        parent = self._schema()
        assert [name for kind, name, _ in parent if kind == "index"] == [
            "ix_case_checkpoints_case_id",
            "ix_case_checkpoints_case_turn",
            "ix_case_checkpoints_created_at",
            "ix_case_checkpoints_enterprise_id",
            "ix_case_checkpoints_organization_id",
            "sqlite_autoindex_case_checkpoints_1",  # the primary key
        ]

        result = run_alembic(f"upgrade {DROP_CASE_CHECKPOINTS_REVISION}", database_url)
        assert result.returncode == 0, result.stderr
        result = run_alembic(f"downgrade {RUNBOOK_SEVERITY_REVISION}", database_url)
        assert result.returncode == 0, result.stderr

        assert get_current_revision(database_url) == RUNBOOK_SEVERITY_REVISION
        assert self._schema() == parent

    def test_no_foreign_key_targets_the_dropped_table(
        self, clean_database, database_url
    ):
        """Why the drop needs no ``PRAGMA foreign_keys`` guard: dropping a
        table runs ON DELETE actions only for rows that REFERENCE it, and at
        the parent revision nothing does."""
        result = run_alembic(f"upgrade {RUNBOOK_SEVERITY_REVISION}", database_url)
        assert result.returncode == 0, result.stderr
        referencing = []
        for (table,) in query_rows(
            TEST_DB, "SELECT name FROM sqlite_master WHERE type = 'table'"
        ):
            for row in query_rows(TEST_DB, f'PRAGMA foreign_key_list("{table}")'):
                if row[2] == "case_checkpoints":
                    referencing.append(table)
        assert referencing == []

    def test_the_postgresql_downgrade_restores_row_level_security(self):
        """Offline (``--sql``): the upgrade is a bare ``DROP TABLE`` (the policy
        goes with the table), and the downgrade enrols the recreated table in
        RLS with the baseline's tenant policy."""
        pg = "postgresql://offline@localhost/offline"
        up = run_alembic(
            f"upgrade {RUNBOOK_SEVERITY_REVISION}:{DROP_CASE_CHECKPOINTS_REVISION} "
            "--sql",
            pg,
        )
        assert up.returncode == 0, up.stderr
        assert "DROP TABLE case_checkpoints;" in up.stdout
        assert "POLICY" not in up.stdout

        down = run_alembic(
            f"downgrade {DROP_CASE_CHECKPOINTS_REVISION}:{RUNBOOK_SEVERITY_REVISION} "
            "--sql",
            pg,
        )
        assert down.returncode == 0, down.stderr
        sql = down.stdout
        assert "CREATE TABLE case_checkpoints" in sql
        assert 'ALTER TABLE "case_checkpoints" ENABLE ROW LEVEL SECURITY' in sql
        assert (
            'CREATE POLICY "case_checkpoints_tenant_isolation" ON "case_checkpoints" '
            "USING (enterprise_id = current_setting('app.current_enterprise_id', "
            "true))"
        ) in sql


class TestTurnReceiptsRevision:
    """010 adds ``turn_receipts`` (#1888): one table, its index, and on
    PostgreSQL the plain tenant policy; its downgrade removes exactly that."""

    @staticmethod
    def _schema() -> list:
        return query_rows(
            TEST_DB,
            "SELECT type, name FROM sqlite_master "
            "WHERE tbl_name = 'turn_receipts' ORDER BY type, name",
        )

    def test_upgrade_adds_the_table_and_downgrade_removes_only_it(
        self, clean_database, database_url
    ):
        result = run_alembic(f"upgrade {DROP_CASE_CHECKPOINTS_REVISION}", database_url)
        assert result.returncode == 0, result.stderr
        parent = get_tables(TEST_DB)
        assert self._schema() == []

        result = run_alembic(f"upgrade {TURN_RECEIPTS_REVISION}", database_url)
        assert result.returncode == 0, result.stderr
        assert get_tables(TEST_DB) == sorted(set(parent) | {"turn_receipts"})
        assert self._schema() == [
            ("index", "ix_turn_receipts_case"),
            ("index", "sqlite_autoindex_turn_receipts_1"),  # the primary key
            ("table", "turn_receipts"),
        ]

        result = run_alembic(
            f"downgrade {DROP_CASE_CHECKPOINTS_REVISION}", database_url
        )
        assert result.returncode == 0, result.stderr
        assert get_current_revision(database_url) == DROP_CASE_CHECKPOINTS_REVISION
        assert get_tables(TEST_DB) == parent

    def test_the_key_leads_with_the_enterprise(self, clean_database, database_url):
        """RLS scopes the table on the enterprise, so the unique key leads with
        it (the ``turn_usage`` lesson, data-model.md)."""
        assert run_alembic("upgrade head", database_url).returncode == 0
        pk = sorted(
            (row[5], row[1])
            for row in query_rows(TEST_DB, 'PRAGMA table_info("turn_receipts")')
            if row[5]
        )
        assert [name for _, name in pk] == [
            "enterprise_id",
            "case_id",
            "author_id",
            "idempotency_key",
        ]

    def test_the_postgresql_upgrade_enrols_the_table_in_row_level_security(self):
        pg = "postgresql://offline@localhost/offline"
        up = run_alembic(
            f"upgrade {DROP_CASE_CHECKPOINTS_REVISION}:{TURN_RECEIPTS_REVISION} --sql",
            pg,
        )
        assert up.returncode == 0, up.stderr
        sql = up.stdout
        assert "CREATE TABLE turn_receipts" in sql
        assert "response JSON NOT NULL" in sql, "json, not jsonb: keys keep order"
        assert 'ALTER TABLE "turn_receipts" ENABLE ROW LEVEL SECURITY' in sql
        assert (
            'CREATE POLICY "turn_receipts_tenant_isolation" ON "turn_receipts" '
            "USING (enterprise_id = current_setting('app.current_enterprise_id', "
            "true))"
        ) in sql


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
        """Downgrade removes the seed with its tables; re-upgrade restores it.

        The seed lives in the baseline, so its reversal is ``downgrade base``
        (which steps back over every additive revision first). What it proves is
        the property that matters: a re-upgrade lands on exactly the seed and not
        a doubled one.
        """
        run_alembic("upgrade head", database_url)
        assert len(query_rows(TEST_DB, "SELECT role_id FROM roles")) == 3

        result = run_alembic("downgrade base", database_url)
        assert result.returncode == 0, f"downgrade failed: {result.stderr}"
        assert get_tables(TEST_DB) == [
            "alembic_version"
        ], "the chain's downgrade must drop every table it created"

        # Re-apply — counts return to exactly the seed, no duplication.
        run_alembic("upgrade head", database_url)
        assert len(query_rows(TEST_DB, "SELECT role_id FROM roles")) == 3
        assert len(query_rows(TEST_DB, "SELECT permission_id FROM permissions")) == 14
        assert len(query_rows(TEST_DB, "SELECT role_id FROM role_permissions")) == 26


class TestStandaloneTenancySeed:
    """The baseline seeds the standalone enterprise and its default team (D8).

    Nothing else in the suite pins them, and everything a standalone deployment
    writes stamps the enterprise id: a baseline that created the tables and not
    these two rows would fail every first write with a foreign-key error rather
    than a message anyone could read.
    """

    STANDALONE_ENTERPRISE_ID = "00000000-0000-0000-0000-000000000002"
    STANDALONE_TEAM_ID = "00000000-0000-0000-0000-000000000003"

    def test_the_default_enterprise_and_team_are_seeded(
        self, clean_database, database_url
    ):
        result = run_alembic("upgrade head", database_url)
        assert result.returncode == 0, result.stderr

        from faultmaven.config import constants

        assert query_rows(TEST_DB, "SELECT enterprise_id, slug FROM enterprises") == [
            (self.STANDALONE_ENTERPRISE_ID, "default")
        ]
        assert query_rows(TEST_DB, "SELECT team_id, enterprise_id FROM teams") == [
            (self.STANDALONE_TEAM_ID, self.STANDALONE_ENTERPRISE_ID)
        ]

        # The seed and the runtime constants must name the same rows; a
        # migration states its values rather than importing them, so this is
        # what stops the two spellings drifting apart.
        assert constants.STANDALONE_ENTERPRISE_ID == self.STANDALONE_ENTERPRISE_ID
        assert constants.STANDALONE_TEAM_ID == self.STANDALONE_TEAM_ID

    def test_the_team_is_parented_by_the_enterprise_not_an_organization(
        self, clean_database, database_url
    ):
        """``teams.organization_id`` is gone: a team may span cost centres."""
        run_alembic("upgrade head", database_url)

        columns = {
            row[1]
            for row in query_rows(TEST_DB, "SELECT * FROM pragma_table_info('teams')")
        }
        assert "enterprise_id" in columns
        assert "organization_id" not in columns


class TestHelperScript:
    """Test suite for migration helper script."""

    def test_helper_script_exists_and_executable(self):
        """Helper script exists and is executable."""
        script_path = PROJECT_ROOT / "scripts" / "db_migrate.sh"

        assert script_path.exists(), "Helper script db_migrate.sh not found"
        assert os.access(script_path, os.X_OK), "Helper script is not executable"

    def test_upgrade_sql_prints_the_sql_and_touches_no_database(
        self, clean_database, database_url
    ):
        """``upgrade --sql`` is offline mode: SQL on stdout, nothing executed.

        alembic takes ``--sql`` only after the subcommand; placed before it,
        alembic rejects the whole invocation.
        """
        result = run_helper_script("upgrade --sql", database_url)

        assert result.returncode == 0, result.stderr
        assert "CREATE TABLE cases" in result.stdout
        assert not os.path.exists(TEST_DB) or get_tables(TEST_DB) == []

    def test_upgrade_applies_the_migrations(self, clean_database, database_url):
        result = run_helper_script("upgrade", database_url)

        assert result.returncode == 0, result.stderr
        assert get_current_revision(database_url) == HEAD_REVISION

    @pytest.mark.parametrize("args", ["status --verbose", "heads -v"])
    def test_verbose_reaches_the_commands_that_accept_it(
        self, clean_database, database_url, args
    ):
        assert run_alembic("upgrade head", database_url).returncode == 0

        result = run_helper_script(args, database_url)

        assert result.returncode == 0, result.stderr
        assert HEAD_REVISION in result.stdout

    @pytest.mark.parametrize(
        "args",
        [
            # The retired two-database selector: there is one database.
            "upgrade --database=auth",
            "upgrade --database=cases",
            # Options the command cannot honour are refused, not dropped.
            "downgrade --sql",
            "upgrade --verbose",
        ],
    )
    def test_an_option_the_command_cannot_honour_is_refused(
        self, clean_database, database_url, args
    ):
        result = run_helper_script(args, database_url)

        assert result.returncode == 2, result.stdout + result.stderr
        # The script's own refusal, not alembic's argparse ("alembic: error:").
        assert "Error: " in result.stderr, result.stderr
        assert not os.path.exists(TEST_DB) or get_tables(TEST_DB) == []


class TestDatabaseSelection:
    """alembic/env.py migrates the one database the application opens."""

    def test_an_x_database_argument_does_not_redirect_the_migration(
        self, clean_database, database_url
    ):
        """A leftover ``-x database=auth`` still migrates ``DATABASE_URL``.

        The two-database selector built a URL for an ``auth_db`` / ``cases_db``
        that the application never opens.
        """
        result = run_alembic("-x database=auth upgrade head", database_url)

        assert result.returncode == 0, result.stderr
        assert set(get_tables(TEST_DB)) == set(EXPECTED_TABLES)


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
            # Both tenant terms, and the pair is the point: ``enterprise_id`` is
            # the isolation key every policy reads, ``organization_id`` is the
            # billing attribution beside it (ADR-017 D2).
            "enterprise_id",
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
        "target_enterprise_id": "'ent-other'",
        "reason": "'a different justification entirely'",
        "created_at": "datetime('now', '-1 day')",
        "expires_at": "datetime('now', '+30 day')",
        "deployment_mode": "'standalone'",
    }

    @staticmethod
    def _insert(conn, grant_id: str = "g-1") -> None:
        conn.execute(
            "INSERT INTO operator_access_grants "
            "(grant_id, operator_user_id, target_case_id, target_enterprise_id, "
            " reason, created_at, expires_at, approval_state) "
            f"VALUES ('{grant_id}', 'op-1', 'case-1', 'ent-1', "
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
                    " target_enterprise_id, reason, created_at, expires_at, "
                    " approval_state) "
                    "VALUES ('g-2', 'op-1', 'case-1', 'ent-1', 'because', "
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
                    " target_enterprise_id, reason, created_at, expires_at, "
                    " approval_state) "
                    "VALUES ('g-3', 'op-1', 'case-1', 'ent-1', 'because', "
                    "datetime('now'), datetime('now', '-1 hour'), 'auto_approved')"
                )
        finally:
            conn.close()


# Test markers for different categories
pytestmark = pytest.mark.integration
