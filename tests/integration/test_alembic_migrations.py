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
import shutil
import sqlite3
import subprocess
import sys
from collections import defaultdict
from pathlib import Path
from typing import Optional

import pytest
from sqlalchemy.engine import make_url

from faultmaven.config.settings import set_env_var
from faultmaven.models.rbac import ROLE_PERMISSIONS, Permission, Role
from faultmaven.models.rbac_seed import SYSTEM_ROLE_IDS
from tests.conftest import TEST_DATA_ROOT_ENV

PROJECT_ROOT = Path(__file__).parent.parent.parent
#: Under this process's own data root (#1772), not the checkout: one fixed file
#: there was migrated by every xdist worker at once.
TEST_DB = os.path.join(os.environ[TEST_DATA_ROOT_ENV], "test_migration.db")

# Current head revision. The chain is ADR-017's clean baseline (which replaced
# the 001-053 chain) plus additive revisions on top of it, so the seed
# assertions below reverse the whole schema with "downgrade base" and each
# additive revision is stepped over on its own.
#: 020_org_permissions_are_management_only
HEAD_REVISION = "596f06c52909"  # pragma: allowlist secret
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
#: ``011_remove_knowledge_suggestions``: the knowledge-suggestion table and
#: ``knowledge_items.source_suggestion_id`` are dropped (#1897).
REMOVE_SUGGESTIONS_REVISION = "2d05706532a9"  # pragma: allowlist secret
#: ``012_case_driver``: ``cases.driver_id``, who holds the investigation
#: writes (ADR-020, #1898).
CASE_DRIVER_REVISION = "6c1f0d2a9b47"  # pragma: allowlist secret
#: ``013_evidence_is_submitted_data``: ``evidence_source_invariant`` admits a
#: fileless row only for an engine inference (#1930).
EVIDENCE_SUBMITTED_DATA_REVISION = "9b3e5c71d2a4"  # pragma: allowlist secret
#: ``014_admin_case_metadata_driver``: the operator case-metadata page function
#: names each case's effective driver (#1926). 015 parents onto it.
ADMIN_CASE_METADATA_DRIVER_REVISION = "831be28d1434"  # pragma: allowlist secret
#: ``015_case_creator_restrict``: an account that created cases cannot be
#: deleted (#1923) — two triggers on SQLite, the key on PostgreSQL. 016 parents
#: onto it.
CASE_CREATOR_RESTRICT_REVISION = "9904ee0cad86"  # pragma: allowlist secret
#: ``016_no_email_derived_display_names``: email-derived display names become
#: ``Member <4 chars of user_id>`` (#1924).
NO_EMAIL_DERIVED_NAMES_REVISION = "b5334d07ae6c"  # pragma: allowlist secret
#: ``017_drop_investigation_sessions``: the investigation-sessions table is
#: dropped (#1935).
DROP_INVESTIGATION_SESSIONS_REVISION = "f95a5b3a58eb"  # pragma: allowlist secret
#: ``018_runbook_drafts_in_the_database``: a draft's text is
#: ``conversion_drafts.content``; ``file_path`` is dropped (#1950).
DRAFTS_IN_THE_DATABASE_REVISION = "c4441b83ba24"  # pragma: allowlist secret
#: ``019_verified_by_is_the_display_name``: a verified runbook's frontmatter
#: ``verified_by`` names its verifier by their display name (#1948).
VERIFIED_BY_DISPLAY_NAME_REVISION = "82bc95a1c02f"  # pragma: allowlist secret
#: ``020_org_permissions_are_management_only``: the ``cases:*`` and
#: ``evidence:*`` permissions and their grants are deleted; organization roles
#: carry only ``org:manage_users`` / ``org:manage_settings`` (#1040).
ORG_PERMISSIONS_MANAGEMENT_ONLY_REVISION = "596f06c52909"  # pragma: allowlist secret
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


#: One template database per revision a test starts from (#2171). Under this
#: process's data root, which is per process and removed at exit.
TEMPLATE_DIR = os.path.join(os.environ[TEST_DATA_ROOT_ENV], "migration-templates")
#: Each requested revision -> the ``version_num`` its build reached. ``head``,
#: the one symbolic name the tests use, is mapped by its first build.
_reached: dict[str, str] = {}
#: Each reached ``version_num`` -> its read-only template.
_templates: dict[str, str] = {}
#: Each requested revision whose build failed -> the failure.
_failed_builds: dict[str, str] = {}


def _build_template(revision: str) -> None:
    """Run the real ``upgrade <revision>`` into a new template and record it."""
    template = os.path.join(TEMPLATE_DIR, f"{revision}.db")
    os.makedirs(TEMPLATE_DIR, exist_ok=True)
    build = run_alembic(f"upgrade {revision}", f"sqlite:///{template}")
    if build.returncode != 0:
        failure = (
            f"{shlex.join(build.args)} failed building the template for "
            f"{revision} (exit {build.returncode})\n"
            f"stdout:\n{build.stdout}\nstderr:\n{build.stderr}"
        )
        if build.returncode > 0:  # a signal (an OOM kill) says nothing of it
            _failed_builds[revision] = failure
        raise RuntimeError(failure)
    [(version,)] = query_rows(template, "SELECT version_num FROM alembic_version")
    if revision != "head":
        assert version == revision, f"upgrade {revision} reached {version}"
    os.chmod(template, 0o444)
    _templates.setdefault(version, template)
    _reached[revision] = version


def prepare_database(revision: str, database_url: str) -> None:
    """Make ``database_url``'s file a clean database migrated to ``revision``.

    For a test whose subject comes after that state. The first request for a
    revision in this process runs the real ``upgrade <revision>`` (a fresh
    ``python -m alembic``) into a new, read-only template, and every request
    copies it, so the schema, rows and ``alembic_version`` are that upgrade's.
    Both the template and each copy are checked for the revision they hold. A
    failed build is kept, unless a signal killed it: every test that asks for
    the revision fails with alembic's output, and the partial template is never
    copied.
    """
    url = make_url(database_url)
    target = url.database
    assert url.get_backend_name() == "sqlite" and target, database_url
    assert os.path.isabs(target), database_url
    if revision in _failed_builds:
        raise RuntimeError(_failed_builds[revision])
    if revision not in _reached and revision not in _templates:
        _build_template(revision)
    version = _reached.get(revision, revision)
    # Journal mode is ``delete``: a hot ``-journal`` would roll back into the copy.
    for sidecar in ("-journal", "-wal", "-shm"):
        Path(f"{target}{sidecar}").unlink(missing_ok=True)
    shutil.copyfile(_templates[version], target)
    copied = query_rows(target, "SELECT version_num FROM alembic_version")
    assert copied == [(version,)], f"{target} holds {copied}, not {version}"


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
# committed keyed turn (#1888). ``knowledge_suggestions`` is gone: 011 removed
# the knowledge-suggestion subsystem (#1897). ``investigation_sessions`` is
# gone: 017 retired the investigation-sessions subsystem (#1935).
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
    "knowledge_items",
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


class TestPrepareDatabase:
    """``prepare_database``'s contract, which the setup calls below rely on."""

    @pytest.fixture
    def builds(self, monkeypatch, tmp_path) -> list[str]:
        """A stand-in ``run_alembic`` whose ``upgrade <rev>`` reaches ``rev``
        (``head`` reaches ``rev_b`` and ``rev_short`` reaches ``rev_a``) and
        creates a table named after it; ``rev_broken`` fails and ``rev_killed``
        dies by a signal. The caches and template directory are this test's
        own. Returns the commands it ran."""
        commands: list[str] = []

        def build(command: str, database_url: str) -> subprocess.CompletedProcess:
            commands.append(command)
            revision = command.removeprefix("upgrade ")
            argv = ["python", "-m", "alembic", *command.split()]
            if revision == "rev_broken":
                return subprocess.CompletedProcess(argv, 1, "partial", "FAILED: boom")
            if revision == "rev_killed":
                return subprocess.CompletedProcess(argv, -9, "", "")
            reached = {"head": "rev_b", "rev_short": "rev_a"}.get(revision, revision)
            conn = sqlite3.connect(database_url.removeprefix("sqlite:///"))
            conn.execute("CREATE TABLE alembic_version (version_num VARCHAR(32))")
            conn.execute("INSERT INTO alembic_version VALUES (?)", (reached,))
            conn.execute(f'CREATE TABLE "{reached}" (id INTEGER)')
            conn.commit()
            conn.close()
            return subprocess.CompletedProcess(argv, 0, "", "")

        module = sys.modules[__name__]
        monkeypatch.setattr(module, "run_alembic", build)
        for cache in ("_reached", "_templates", "_failed_builds"):
            monkeypatch.setattr(module, cache, {})
        monkeypatch.setattr(module, "TEMPLATE_DIR", str(tmp_path / "templates"))
        return commands

    @staticmethod
    def _version(db: Path) -> list:
        return query_rows(str(db), "SELECT version_num FROM alembic_version")

    def test_each_revision_is_built_once_and_head_shares_its_id(self, builds, tmp_path):
        db = tmp_path / "test.db"
        for revision, reached in [
            ("head", "rev_b"),
            ("rev_b", "rev_b"),
            ("rev_a", "rev_a"),
            ("head", "rev_b"),
            ("rev_a", "rev_a"),
        ]:
            prepare_database(revision, f"sqlite:///{db}")
            assert self._version(db) == [(reached,)]
            assert get_tables(str(db)) == ["alembic_version", reached]
        assert builds == ["upgrade head", "upgrade rev_a"]

    def test_a_template_at_another_revision_is_refused(self, builds, tmp_path):
        url = f"sqlite:///{tmp_path / 'test.db'}"
        with pytest.raises(AssertionError, match="upgrade rev_short reached rev_a"):
            prepare_database("rev_short", url)
        prepare_database("rev_b", url)
        os.chmod(_templates["rev_b"], 0o644)
        conn = sqlite3.connect(_templates["rev_b"])
        conn.execute("UPDATE alembic_version SET version_num = 'rev_z'")
        conn.commit()
        conn.close()
        with pytest.raises(AssertionError, match=r"holds \[\('rev_z',\)\], not rev_b"):
            prepare_database("rev_b", url)

    def test_each_copy_is_a_fresh_file(self, builds, tmp_path):
        db = tmp_path / "test.db"
        prepare_database("rev_a", f"sqlite:///{db}")
        built = Path(_templates["rev_a"]).read_bytes()
        conn = sqlite3.connect(db)
        conn.execute('INSERT INTO "rev_a" VALUES (1)')
        conn.commit()
        conn.close()
        prepare_database("rev_a", f"sqlite:///{db}")
        assert query_rows(str(db), 'SELECT id FROM "rev_a"') == []
        assert Path(_templates["rev_a"]).read_bytes() == built

    @pytest.mark.skipif(os.geteuid() == 0, reason="root writes through any mode")
    def test_a_template_cannot_be_written_through_an_alias(self, builds, tmp_path):
        prepare_database("rev_a", f"sqlite:///{tmp_path / 'test.db'}")
        alias = tmp_path / "alias.db"
        os.link(_templates["rev_a"], alias)
        conn = sqlite3.connect(alias)
        try:
            with pytest.raises(sqlite3.OperationalError, match="readonly database"):
                conn.execute('INSERT INTO "rev_a" VALUES (1)')
                conn.commit()
        finally:
            conn.close()

    def test_a_failed_build_raises_with_its_output_and_stays_failed(
        self, builds, tmp_path
    ):
        db = tmp_path / "test.db"
        for _ in range(2):
            with pytest.raises(RuntimeError) as raised:
                prepare_database("rev_broken", f"sqlite:///{db}")
            assert str(raised.value) == (
                "python -m alembic upgrade rev_broken failed building the template "
                "for rev_broken (exit 1)\nstdout:\npartial\nstderr:\nFAILED: boom"
            )
        # A build killed by a signal is tried again by the next request.
        for _ in range(2):
            with pytest.raises(RuntimeError, match=r"rev_killed \(exit -9\)"):
                prepare_database("rev_killed", f"sqlite:///{db}")
        assert builds == ["upgrade rev_broken"] + ["upgrade rev_killed"] * 2
        assert not db.exists()

    def test_stale_sidecars_are_removed_before_the_copy(self, builds, tmp_path):
        """A writer killed mid-transaction leaves a synced ``-journal`` holding
        page 1 of a database with another schema, which SQLite rolls back
        into whatever file it next finds there."""
        db = tmp_path / "test.db"
        conn = sqlite3.connect(db, isolation_level=None)
        conn.execute("CREATE TABLE leftover (data BLOB)")
        conn.execute("PRAGMA cache_size=1")  # the inserts spill: a synced journal
        conn.execute("BEGIN")
        conn.execute("CREATE TABLE extra (x)")  # journals page 1, the schema
        conn.executemany("INSERT INTO leftover VALUES (randomblob(3000))", [()] * 20)
        journal = Path(f"{db}-journal").read_bytes()
        conn.execute("ROLLBACK")
        conn.close()
        Path(f"{db}-journal").write_bytes(journal)
        for sidecar in ("-wal", "-shm"):
            Path(f"{db}{sidecar}").write_bytes(b"left by an earlier test")
        prepare_database("rev_a", f"sqlite:///{db}")
        for sidecar in ("-journal", "-wal", "-shm"):
            assert not Path(f"{db}{sidecar}").exists()
        assert get_tables(str(db)) == ["alembic_version", "rev_a"]

    def test_only_an_absolute_sqlite_file_is_prepared(self, builds):
        for url in ("sqlite:///relative.db", "sqlite://", "postgresql://h/db"):
            with pytest.raises(AssertionError):
                prepare_database("rev_a", url)
        assert builds == []


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
        prepare_database("head", database_url)

        tables = get_tables(TEST_DB)

        assert len(tables) == len(
            EXPECTED_TABLES
        ), f"Expected {len(EXPECTED_TABLES)} tables, got {len(tables)}. Missing: {set(EXPECTED_TABLES) - set(tables)}, Extra: {set(tables) - set(EXPECTED_TABLES)}"
        for expected_table in EXPECTED_TABLES:
            assert expected_table in tables, f"Missing table: {expected_table}"

    def test_migration_revision_correct(self, clean_database, database_url):
        """Migration revision matches expected head revision."""
        prepare_database("head", database_url)

        revision = get_current_revision(database_url)

        assert (
            revision == HEAD_REVISION
        ), f"Expected revision {HEAD_REVISION}, got {revision}"

    def test_migration_rollback(self, clean_database, database_url):
        """Migration can be rolled back successfully."""
        prepare_database("head", database_url)

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
        prepare_database("head", database_url)
        run_alembic("downgrade base", database_url)

        # Re-apply
        result = run_alembic("upgrade head", database_url)
        assert result.returncode == 0, f"Re-application failed: {result.stderr}"

        # Verify tables restored
        tables = get_tables(TEST_DB)
        assert len(tables) == len(
            EXPECTED_TABLES
        ), f"Expected {len(EXPECTED_TABLES)} tables after re-application, got {len(tables)}"
        assert "knowledge_items" in tables, "knowledge_items table should be restored"
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
    """Revisions 003, 004, 005 and 014 change PostgreSQL functions and nothing
    else.

    SQLite has no row-level security to bypass and no definer functions, so on
    SQLite each is a no-op in both directions: stepped down to its parent and
    back up, it must not touch a single table. Their PostgreSQL halves:
    ``tests/integration/security/test_admin_case_metadata_postgres.py`` (003);
    ``tests/integration/security/test_definer_functions_postgres.py``, whose
    step test starts from what 004 leaves and asserts it, and steps 005 down to
    004 and back (005); ``test_admin_case_metadata_postgres.py`` again (014).

    015 is stepped here too: it adds no table on either dialect (a key change on
    PostgreSQL, two triggers on SQLite), so the same table-set check holds. Its
    triggers are pinned by ``TestCaseCreatorRestrictRevision``, and its key by
    ``tests/integration/security/test_case_creator_restrict_postgres.py``.

    016 is a data-only revision (it relabels display names), so the same
    table-set check holds. Its rows are pinned by
    ``TestNoEmailDerivedNamesRevision``, and on PostgreSQL by
    ``tests/integration/security/test_no_email_derived_names_postgres.py``.

    019 is data-only too (it rewrites runbook text). Its rows are pinned by
    ``TestVerifiedByIsTheDisplayNameRevision``, and on PostgreSQL by
    ``tests/integration/security/test_verified_by_display_name_postgres.py``.

    020 is data-only as well (it prunes the RBAC seed to the management axis
    and rewrites the system roles' descriptions). Its rows are pinned by
    ``TestOrgPermissionsAreManagementOnlyRevision``, and on PostgreSQL by
    ``tests/integration/security/test_org_permissions_management_only_postgres.py``.
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
            pytest.param(
                ADMIN_CASE_METADATA_DRIVER_REVISION,
                EVIDENCE_SUBMITTED_DATA_REVISION,
                id="014_admin_case_metadata_driver",
            ),
            pytest.param(
                CASE_CREATOR_RESTRICT_REVISION,
                ADMIN_CASE_METADATA_DRIVER_REVISION,
                id="015_case_creator_restrict",
            ),
            pytest.param(
                NO_EMAIL_DERIVED_NAMES_REVISION,
                CASE_CREATOR_RESTRICT_REVISION,
                id="016_no_email_derived_display_names",
            ),
            pytest.param(
                VERIFIED_BY_DISPLAY_NAME_REVISION,
                DRAFTS_IN_THE_DATABASE_REVISION,
                id="019_verified_by_is_the_display_name",
            ),
            pytest.param(
                ORG_PERMISSIONS_MANAGEMENT_ONLY_REVISION,
                VERIFIED_BY_DISPLAY_NAME_REVISION,
                id="020_org_permissions_are_management_only",
            ),
        ],
    )
    def test_steps_down_to_its_parent_and_up_without_touching_a_table(
        self, clean_database, database_url, revision, parent
    ):
        prepare_database(revision, database_url)
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
        prepare_database("head", database_url)
        before = get_tables(TEST_DB)
        assert set(LLM_USAGE_TABLES) <= set(before)

        # To the ledger's parent, stepping over whatever was added after it.
        result = run_alembic(f"downgrade {BASELINE_REVISION}", database_url)
        assert result.returncode == 0, result.stderr
        assert get_current_revision(database_url) == BASELINE_REVISION
        # Less the ledger and 010's receipts; plus the tables the baseline
        # creates and 009, 011 and 017 drop, which stepping down over them
        # restores.
        assert get_tables(TEST_DB) == sorted(
            (set(before) - set(LLM_USAGE_TABLES) - {"turn_receipts"})
            | {"case_checkpoints", "knowledge_suggestions", "investigation_sessions"}
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
        prepare_database("head", database_url)
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
        prepare_database("head", database_url)
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
        prepare_database("head", database_url)
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
        prepare_database(DEFINER_SEARCH_PATH_REVISION, database_url)
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
        prepare_database(CONVERSION_SOURCE_REF_REVISION, database_url)
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
        prepare_database(CONVERSION_SOURCE_REF_REVISION, database_url)
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
        prepare_database(PROBLEM_STATUS_REVISION, database_url)
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
        prepare_database(RUNBOOK_SEVERITY_REVISION, database_url)
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
        prepare_database("head", database_url)
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
        prepare_database(RUNBOOK_SEVERITY_REVISION, database_url)
        assert self._schema(), "positive control: 008 has the table"

        result = run_alembic(f"upgrade {DROP_CASE_CHECKPOINTS_REVISION}", database_url)
        assert result.returncode == 0, result.stderr
        assert self._schema() == []

    def test_downgrade_recreates_the_table_as_the_parent_had_it(
        self, clean_database, database_url
    ):
        prepare_database(RUNBOOK_SEVERITY_REVISION, database_url)
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
        prepare_database(RUNBOOK_SEVERITY_REVISION, database_url)
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
        prepare_database(DROP_CASE_CHECKPOINTS_REVISION, database_url)
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
        prepare_database("head", database_url)
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


class TestRemoveKnowledgeSuggestionsRevision:
    """011 removes the knowledge-suggestion subsystem's storage (#1897):
    ``knowledge_suggestions`` and ``knowledge_items.source_suggestion_id`` are
    dropped, and the downgrade recreates both, empty, as 010 had them."""

    @staticmethod
    def _suggestions_schema() -> list:
        """The table and its indexes, as SQLite stores their DDL."""
        return query_rows(
            TEST_DB,
            "SELECT type, name, sql FROM sqlite_master "
            "WHERE tbl_name = 'knowledge_suggestions' ORDER BY type, name",
        )

    @staticmethod
    def _items_columns() -> list:
        """``knowledge_items``' columns (name, type, notnull, default, pk),
        order-free: the downgrade re-adds the column last."""
        return sorted(
            row[1:]
            for row in query_rows(TEST_DB, 'PRAGMA table_info("knowledge_items")')
        )

    @staticmethod
    def _items_indexes() -> list:
        return query_rows(
            TEST_DB,
            "SELECT name, sql FROM sqlite_master WHERE type = 'index' "
            "AND tbl_name = 'knowledge_items' ORDER BY name",
        )

    def test_upgrade_drops_the_table_and_the_column(self, clean_database, database_url):
        prepare_database(TURN_RECEIPTS_REVISION, database_url)
        assert self._suggestions_schema(), "positive control: 010 has the table"
        assert "source_suggestion_id" in [c[0] for c in self._items_columns()]
        parent_items_indexes = self._items_indexes()
        assert "ix_knowledge_items_source_suggestion_id" in [
            name for name, _ in parent_items_indexes
        ]

        result = run_alembic(f"upgrade {REMOVE_SUGGESTIONS_REVISION}", database_url)
        assert result.returncode == 0, result.stderr
        assert self._suggestions_schema() == []
        assert "source_suggestion_id" not in [c[0] for c in self._items_columns()]
        # Only the column's own index went; the rest of the table is untouched
        # (a plain DROP COLUMN, not a rebuild).
        assert self._items_indexes() == [
            (name, sql)
            for name, sql in parent_items_indexes
            if name != "ix_knowledge_items_source_suggestion_id"
        ]

    def test_downgrade_recreates_both_as_the_parent_had_them(
        self, clean_database, database_url
    ):
        prepare_database(TURN_RECEIPTS_REVISION, database_url)
        parent_suggestions = self._suggestions_schema()
        assert [name for kind, name, _ in parent_suggestions if kind == "index"] == [
            "ix_knowledge_suggestions_case_id",
            "ix_knowledge_suggestions_created_at",
            "ix_knowledge_suggestions_enterprise_id",
            "ix_knowledge_suggestions_extracted_by",
            "ix_knowledge_suggestions_knowledge_item_id",
            "ix_knowledge_suggestions_organization_id",
            "ix_knowledge_suggestions_pii_scan_status",
            "ix_knowledge_suggestions_status",
            "sqlite_autoindex_knowledge_suggestions_1",  # the primary key
        ]
        parent_columns = self._items_columns()
        parent_indexes = self._items_indexes()

        result = run_alembic(f"upgrade {REMOVE_SUGGESTIONS_REVISION}", database_url)
        assert result.returncode == 0, result.stderr
        result = run_alembic(f"downgrade {TURN_RECEIPTS_REVISION}", database_url)
        assert result.returncode == 0, result.stderr

        assert get_current_revision(database_url) == TURN_RECEIPTS_REVISION
        assert self._suggestions_schema() == parent_suggestions
        assert self._items_columns() == parent_columns
        assert self._items_indexes() == parent_indexes

        result = run_alembic(f"upgrade {REMOVE_SUGGESTIONS_REVISION}", database_url)
        assert result.returncode == 0, result.stderr
        assert self._suggestions_schema() == []

    def test_no_foreign_key_targets_the_dropped_table(
        self, clean_database, database_url
    ):
        """Why the drop needs no ``PRAGMA foreign_keys`` guard: dropping a
        table runs ON DELETE actions only for rows that REFERENCE it, and at
        the parent revision nothing does."""
        prepare_database(TURN_RECEIPTS_REVISION, database_url)
        referencing = []
        for (table,) in query_rows(
            TEST_DB, "SELECT name FROM sqlite_master WHERE type = 'table'"
        ):
            for row in query_rows(TEST_DB, f'PRAGMA foreign_key_list("{table}")'):
                if row[2] == "knowledge_suggestions":
                    referencing.append(table)
        assert referencing == []

    def test_the_postgresql_policy_drop_and_restore_are_dialect_guarded(self):
        """Offline (``--sql``): on PostgreSQL the upgrade drops the table's
        tenant policy by name before the table, and the downgrade enrols the
        recreated table in RLS with the baseline's tenant policy. The SQLite
        runs above are the other arm: they would fail on the policy DDL."""
        pg = "postgresql://offline@localhost/offline"
        up = run_alembic(
            f"upgrade {TURN_RECEIPTS_REVISION}:{REMOVE_SUGGESTIONS_REVISION} --sql",
            pg,
        )
        assert up.returncode == 0, up.stderr
        sql = up.stdout
        policy_drop = (
            'DROP POLICY "knowledge_suggestions_tenant_isolation" '
            'ON "knowledge_suggestions"'
        )
        assert policy_drop in sql
        assert sql.index(policy_drop) < sql.index("DROP TABLE knowledge_suggestions;")
        assert "DROP INDEX ix_knowledge_items_source_suggestion_id;" in sql
        assert "ALTER TABLE knowledge_items DROP COLUMN source_suggestion_id;" in sql

        down = run_alembic(
            f"downgrade {REMOVE_SUGGESTIONS_REVISION}:{TURN_RECEIPTS_REVISION} --sql",
            pg,
        )
        assert down.returncode == 0, down.stderr
        sql = down.stdout
        assert (
            "ALTER TABLE knowledge_items ADD COLUMN source_suggestion_id VARCHAR(36);"
            in sql
        )
        assert "CREATE TABLE knowledge_suggestions" in sql
        assert "pii_scan_result JSONB" in sql
        assert 'ALTER TABLE "knowledge_suggestions" ENABLE ROW LEVEL SECURITY' in sql
        assert (
            'CREATE POLICY "knowledge_suggestions_tenant_isolation" '
            'ON "knowledge_suggestions" '
            "USING (enterprise_id = current_setting('app.current_enterprise_id', "
            "true))"
        ) in sql


class TestCaseDriverRevision:
    """012 adds ``cases.driver_id`` (ADR-020, #1898): one nullable column, its
    foreign key to ``users`` (ON DELETE SET NULL) and its index; the downgrade
    removes exactly that."""

    @staticmethod
    def _driver_column() -> list:
        return [
            row
            for row in query_rows(TEST_DB, 'PRAGMA table_info("cases")')
            if row[1] == "driver_id"
        ]

    @staticmethod
    def _cases_indexes() -> list:
        return sorted(
            row[1] for row in query_rows(TEST_DB, 'PRAGMA index_list("cases")')
        )

    @staticmethod
    def _cases_foreign_keys() -> list:
        return sorted(
            (row[2], row[3], row[4], row[6])
            for row in query_rows(TEST_DB, 'PRAGMA foreign_key_list("cases")')
        )

    def test_upgrade_adds_the_column_and_downgrade_removes_only_it(
        self, clean_database, database_url
    ):
        prepare_database(REMOVE_SUGGESTIONS_REVISION, database_url)
        tables = get_tables(TEST_DB)
        indexes = self._cases_indexes()
        foreign_keys = self._cases_foreign_keys()
        assert self._driver_column() == []

        result = run_alembic(f"upgrade {CASE_DRIVER_REVISION}", database_url)
        assert result.returncode == 0, result.stderr
        assert get_tables(TEST_DB) == tables
        column = self._driver_column()
        assert len(column) == 1
        _, _, col_type, notnull, default, pk = column[0]
        assert (col_type, notnull, default, pk) == ("VARCHAR(36)", 0, None, 0)
        assert self._cases_indexes() == sorted(indexes + ["ix_cases_driver_id"])
        assert self._cases_foreign_keys() == sorted(
            foreign_keys + [("users", "driver_id", "user_id", "SET NULL")]
        )

        result = run_alembic(f"downgrade {REMOVE_SUGGESTIONS_REVISION}", database_url)
        assert result.returncode == 0, result.stderr
        assert get_current_revision(database_url) == REMOVE_SUGGESTIONS_REVISION
        assert self._driver_column() == []
        assert self._cases_indexes() == indexes
        assert self._cases_foreign_keys() == foreign_keys
        assert get_tables(TEST_DB) == tables

    def test_a_deleted_driver_hands_the_case_back_through_the_key(
        self, clean_database, database_url
    ):
        """ON DELETE SET NULL on the driver: the creator drives again, and the
        case row survives."""
        prepare_database("head", database_url)
        conn = sqlite3.connect(TEST_DB)
        try:
            conn.execute("PRAGMA foreign_keys=ON")
            enterprise = conn.execute(
                "SELECT enterprise_id FROM enterprises LIMIT 1"
            ).fetchone()[0]
            for uid in ("creator-0000", "driver-00000"):
                conn.execute(
                    "INSERT INTO users (user_id, enterprise_id, username, email, "
                    "display_name, hashed_password) VALUES (?, ?, ?, ?, ?, 'x')",
                    (uid, enterprise, uid, f"{uid}@example.com", uid),
                )
            conn.execute(
                "INSERT INTO cases (case_id, enterprise_id, user_id, driver_id, "
                "title) VALUES ('case_000000000001', ?, 'creator-0000', "
                "'driver-00000', 't')",
                (enterprise,),
            )
            conn.execute("DELETE FROM users WHERE user_id = 'driver-00000'")
            row = conn.execute(
                "SELECT user_id, driver_id FROM cases "
                "WHERE case_id = 'case_000000000001'"
            ).fetchone()
        finally:
            conn.close()
        assert row == ("creator-0000", None)

    def test_the_postgresql_upgrade_names_the_key_and_changes_no_policy(self):
        pg = "postgresql://offline@localhost/offline"
        up = run_alembic(
            f"upgrade {REMOVE_SUGGESTIONS_REVISION}:{CASE_DRIVER_REVISION} --sql", pg
        )
        assert up.returncode == 0, up.stderr
        sql = up.stdout
        assert "ALTER TABLE cases ADD COLUMN driver_id VARCHAR(36);" in sql
        assert (
            "ALTER TABLE cases ADD CONSTRAINT cases_driver_id_fkey FOREIGN "
            "KEY(driver_id) REFERENCES users (user_id) ON DELETE SET NULL;"
        ) in sql
        assert "CREATE INDEX ix_cases_driver_id ON cases (driver_id);" in sql
        # ``cases`` is already enterprise-scoped: no RLS statement here.
        assert "ROW LEVEL SECURITY" not in sql
        assert "CREATE POLICY" not in sql

        down = run_alembic(
            f"downgrade {CASE_DRIVER_REVISION}:{REMOVE_SUGGESTIONS_REVISION} --sql", pg
        )
        assert down.returncode == 0, down.stderr
        assert "DROP INDEX ix_cases_driver_id;" in down.stdout
        assert "ALTER TABLE cases DROP COLUMN driver_id;" in down.stdout


class TestCaseCreatorRestrictRevision:
    """015 refuses deleting a case's creator (#1923). On SQLite the stored
    ``cases.user_id`` clause stays ``SET NULL`` (rebuilding ``cases`` under its
    16 inbound keys is unsafe). A ``BEFORE DELETE`` trigger on ``users``
    aborts the delete, and a ``BEFORE UPDATE`` trigger on ``cases`` aborts a
    creator turned NULL — the ``SET NULL`` action of a ``REPLACE INTO users``,
    whose implicit delete fires no delete trigger with ``recursive_triggers``
    off. The downgrade drops exactly those two triggers. The PostgreSQL half
    (the key itself) is
    ``tests/integration/security/test_case_creator_restrict_postgres.py``."""

    TRIGGER = "users_delete_refuses_case_creator"
    NULLING_TRIGGER = "cases_creator_never_nulled"

    @staticmethod
    def _triggers() -> list:
        return sorted(
            row[0]
            for row in query_rows(
                TEST_DB, "SELECT name FROM sqlite_master WHERE type = 'trigger'"
            )
        )

    @staticmethod
    def _creator_key() -> list:
        return [
            (row[2], row[3], row[4], row[6])
            for row in query_rows(TEST_DB, 'PRAGMA foreign_key_list("cases")')
            if row[3] == "user_id"
        ]

    @staticmethod
    def _seed(conn) -> str:
        """In the seeded enterprise: a CREATOR whose case a DRIVER drives, and
        a BYSTANDER with none. Returns the enterprise id."""
        enterprise = conn.execute(
            "SELECT enterprise_id FROM enterprises LIMIT 1"
        ).fetchone()[0]
        for uid in ("creator-0000", "driver-00000", "bystander-00"):
            conn.execute(
                "INSERT INTO users (user_id, enterprise_id, username, email, "
                "display_name, hashed_password) VALUES (?, ?, ?, ?, ?, 'x')",
                (uid, enterprise, uid, f"{uid}@example.com", uid),
            )
        conn.execute(
            "INSERT INTO cases (case_id, enterprise_id, user_id, driver_id, "
            "title) VALUES ('case_000000000001', ?, 'creator-0000', "
            "'driver-00000', 't')",
            (enterprise,),
        )
        conn.commit()
        return enterprise

    @staticmethod
    def _case(conn) -> tuple:
        return conn.execute(
            "SELECT user_id, driver_id FROM cases "
            "WHERE case_id = 'case_000000000001'"
        ).fetchone()

    def test_upgrade_adds_the_trigger_and_downgrade_removes_only_it(
        self, clean_database, database_url
    ):
        prepare_database(ADMIN_CASE_METADATA_DRIVER_REVISION, database_url)
        tables, triggers = get_tables(TEST_DB), self._triggers()
        assert self.TRIGGER not in triggers
        assert self.NULLING_TRIGGER not in triggers
        assert self._creator_key() == [("users", "user_id", "user_id", "SET NULL")]

        for _ in range(2):  # up, down, up
            result = run_alembic(
                f"upgrade {CASE_CREATOR_RESTRICT_REVISION}", database_url
            )
            assert result.returncode == 0, result.stderr
            assert self._triggers() == sorted(
                triggers + [self.TRIGGER, self.NULLING_TRIGGER]
            )
            assert get_tables(TEST_DB) == tables
            # The stored clause is unchanged: the trigger aborts before it.
            assert self._creator_key() == [("users", "user_id", "user_id", "SET NULL")]

            result = run_alembic(
                f"downgrade {ADMIN_CASE_METADATA_DRIVER_REVISION}", database_url
            )
            assert result.returncode == 0, result.stderr
            assert (
                get_current_revision(database_url)
                == ADMIN_CASE_METADATA_DRIVER_REVISION
            )
            assert self._triggers() == triggers
            assert get_tables(TEST_DB) == tables

    @pytest.mark.parametrize("foreign_keys", ["ON", "OFF"])
    def test_a_creator_is_refused_and_a_driver_releases(
        self, clean_database, database_url, foreign_keys
    ):
        """The trigger holds whether or not the connection turned foreign keys
        on; the driver's ``ON DELETE SET NULL`` is the key's, so it needs them."""
        prepare_database("head", database_url)
        conn = sqlite3.connect(TEST_DB)
        try:
            conn.execute(f"PRAGMA foreign_keys={foreign_keys}")
            self._seed(conn)

            with pytest.raises(sqlite3.IntegrityError, match="fm-reassign-cases"):
                conn.execute("DELETE FROM users WHERE user_id = 'creator-0000'")
            conn.rollback()
            assert self._case(conn) == ("creator-0000", "driver-00000")

            conn.execute("DELETE FROM users WHERE user_id = 'bystander-00'")
            if foreign_keys == "ON":
                conn.execute("DELETE FROM users WHERE user_id = 'driver-00000'")
                assert self._case(conn) == ("creator-0000", None)
            conn.commit()
        finally:
            conn.close()

    @pytest.mark.parametrize("verb", ["INSERT OR REPLACE INTO", "REPLACE INTO"])
    def test_a_replace_that_would_take_a_creator_is_refused(
        self, clean_database, database_url, verb
    ):
        """``REPLACE`` removes the row whose username it collides with by an
        implicit delete that fires no delete trigger with ``recursive_triggers``
        off (the sqlite3 default). The key's ``SET NULL`` still runs, as an
        UPDATE of ``cases``, and that is what is refused."""
        prepare_database("head", database_url)
        conn = sqlite3.connect(TEST_DB)
        try:
            conn.execute("PRAGMA foreign_keys=ON")
            assert conn.execute("PRAGMA recursive_triggers").fetchone() == (0,)
            enterprise = self._seed(conn)

            with pytest.raises(sqlite3.IntegrityError, match="cannot lose its creator"):
                conn.execute(
                    f"{verb} users (user_id, enterprise_id, username, email, "
                    "display_name, hashed_password) VALUES ('usurper-000', ?, "
                    "'creator-0000', 'usurper@example.com', 'U', 'x')",
                    (enterprise,),
                )
            conn.rollback()

            assert self._case(conn) == ("creator-0000", "driver-00000")
            assert conn.execute(
                "SELECT user_id FROM users WHERE username = 'creator-0000'"
            ).fetchone() == ("creator-0000",)
        finally:
            conn.close()

    def test_moving_a_case_to_another_account_still_works(
        self, clean_database, database_url
    ):
        """``fm-reassign-cases`` writes one set creator over another: the
        nulling guard lets it through."""
        prepare_database("head", database_url)
        conn = sqlite3.connect(TEST_DB)
        try:
            conn.execute("PRAGMA foreign_keys=ON")
            self._seed(conn)

            conn.execute(
                "UPDATE cases SET user_id = 'bystander-00', driver_id = NULL, "
                "version = version + 1 WHERE case_id = 'case_000000000001'"
            )
            conn.commit()

            assert self._case(conn) == ("bystander-00", None)
            # And the old creator, now owning nothing, can go.
            conn.execute("DELETE FROM users WHERE user_id = 'creator-0000'")
            conn.commit()
        finally:
            conn.close()

    def test_a_bulk_delete_that_takes_a_creator_is_refused_whole(
        self, clean_database, database_url
    ):
        prepare_database("head", database_url)
        conn = sqlite3.connect(TEST_DB)
        try:
            conn.execute("PRAGMA foreign_keys=ON")
            enterprise = self._seed(conn)
            before = conn.execute(
                "SELECT count(*) FROM users WHERE enterprise_id = ?", (enterprise,)
            ).fetchone()

            with pytest.raises(sqlite3.IntegrityError, match="fm-reassign-cases"):
                conn.execute("DELETE FROM users WHERE enterprise_id = ?", (enterprise,))
            conn.rollback()

            assert (
                conn.execute(
                    "SELECT count(*) FROM users WHERE enterprise_id = ?", (enterprise,)
                ).fetchone()
                == before
            )
        finally:
            conn.close()

    def test_deleting_the_enterprise_still_removes_its_cases_and_users(
        self, clean_database, database_url
    ):
        prepare_database("head", database_url)
        conn = sqlite3.connect(TEST_DB)
        try:
            conn.execute("PRAGMA foreign_keys=ON")
            enterprise = self._seed(conn)

            conn.execute(
                "DELETE FROM enterprises WHERE enterprise_id = ?", (enterprise,)
            )
            conn.commit()

            assert conn.execute(
                "SELECT count(*) FROM users WHERE enterprise_id = ?", (enterprise,)
            ).fetchone() == (0,)
            assert self._case(conn) is None
        finally:
            conn.close()

    def test_the_postgresql_statements_swap_the_key_and_add_no_trigger(self):
        pg = "postgresql://offline@localhost/offline"
        up = run_alembic(
            f"upgrade {ADMIN_CASE_METADATA_DRIVER_REVISION}:"
            f"{CASE_CREATOR_RESTRICT_REVISION} --sql",
            pg,
        )
        assert up.returncode == 0, up.stderr
        assert "ALTER TABLE cases DROP CONSTRAINT cases_user_id_fkey;" in up.stdout
        assert (
            "ALTER TABLE cases ADD CONSTRAINT cases_user_id_fkey FOREIGN "
            "KEY(user_id) REFERENCES users (user_id) ON DELETE RESTRICT;"
        ) in up.stdout
        assert "TRIGGER" not in up.stdout

        down = run_alembic(
            f"downgrade {CASE_CREATOR_RESTRICT_REVISION}:"
            f"{ADMIN_CASE_METADATA_DRIVER_REVISION} --sql",
            pg,
        )
        assert down.returncode == 0, down.stderr
        assert (
            "ALTER TABLE cases ADD CONSTRAINT cases_user_id_fkey FOREIGN "
            "KEY(user_id) REFERENCES users (user_id) ON DELETE SET NULL;"
        ) in down.stdout


class TestNoEmailDerivedNamesRevision:
    """016 relabels the display names derived from an email (#1924) and nothing
    else. The downgrade restores nothing, and the offline script is the same
    UPDATE an online run executes."""

    #: (user_id, username, email, display_name, sso_provider, expected after 016)
    ROWS = (
        # SSO, JIT stored the username (the email's local part): relabelled.
        (
            "3f9a1c2e-0000-4000-8000-000000000001",
            "sam",
            "sam@acme.com",
            "sam",
            "workos",
            "Member 3f9a",
        ),
        # SSO, the IdP sent a name: kept.
        (
            "a1b2c3d4-0000-4000-8000-000000000002",
            "jane",
            "jane@acme.com",
            "Jane Doe",
            "workos",
            "Jane Doe",
        ),
        # SSO whose username was renamed after JIT, name equal to neither the
        # username nor the local part: the documented false negative, kept.
        (
            "b0c0d0e0-0000-4000-8000-000000000003",
            "kim-renamed",
            "kimberly@acme.com",
            "kim",
            "workos",
            "kim",
        ),
        # SSO, the IdP sent the local part in another case as the name.
        (
            "f00dface-0000-4000-8000-000000000004",
            "frank-2",
            "frank@acme.com",
            "FRANK",
            "workos",
            "Member f00d",
        ),
        # SSO, the IdP sent an address as the name.
        (
            "0a0b0c0d-0000-4000-8000-000000000005",
            "pat",
            "pat@acme.com",
            "pat@example.com",
            "workos",
            "Member 0a0b",
        ),
        # Local, an address as the username and no display name given: the
        # database store title-cased the address. Relabelled.
        (
            "c3d4e5f6-0000-4000-8000-000000000006",
            "alice.smith@example.com",
            "alice.smith@example.com",
            "Alice Smith@Example Com",
            None,
            "Member c3d4",
        ),
        # Local, a name wrapping an address.
        (
            "1a2b3c4d-0000-4000-8000-000000000007",
            "quinn",
            "quinn@example.com",
            "Quinn <quinn@example.com>",
            None,
            "Member 1a2b",
        ),
        # Local, the account's own address as the name.
        (
            "5e6f7a8b-0000-4000-8000-000000000008",
            "erin",
            "erin@example.com",
            "erin@example.com",
            None,
            "Member 5e6f",
        ),
        # Local, matches only the Redis store's local-part form: a name the
        # user may have typed. Counted, kept.
        (
            "e5f60718-0000-4000-8000-000000000009",
            "bob.jones@example.com",
            "bob.jones@example.com",
            "Bob Jones",
            None,
            "Bob Jones",
        ),
        # A typed name for sam.smith@: separators are not normalised. Kept.
        (
            "9c8d7e6f-0000-4000-8000-00000000000a",
            "samsmith",
            "sam.smith@example.com",
            "Sam Smith",
            None,
            "Sam Smith",
        ),
        # A LIKE wildcard in the name must not match: "sam_x" is not "samAx".
        (
            "2b3c4d5e-0000-4000-8000-00000000000b",
            "samax",
            "samAx@example.com",
            "sam_x",
            None,
            "sam_x",
        ),
        # Local, the database store's title-cased username, which is its real
        # email's local part: relabelled.
        (
            "a1ce0000-0000-4000-8000-00000000000e",
            "alice",
            "alice@acme.com",
            "Alice",
            None,
            "Member a1ce",
        ),
        # Local, registered with no email: the store synthesized one from the
        # username, so the name is not judged against it. Kept.
        (
            "b0b00000-0000-4000-8000-00000000000f",
            "bob",
            "bob@faultmaven.example",
            "Bob",
            None,
            "Bob",
        ),
        # Local, a plain username's title-cased form: chosen, not derived. Kept.
        (
            "07182930-0000-4000-8000-00000000000c",
            "carol_x",
            "carol@example.com",
            "Carol X",
            None,
            "Carol X",
        ),
        # Local, an address as the username with an explicit name: kept.
        (
            "92ab3c4d-0000-4000-8000-00000000000d",
            "dan@example.com",
            "dan@example.com",
            "Daniel",
            None,
            "Daniel",
        ),
    )
    RELABELLED = sum(1 for row in ROWS if row[3] != row[5])

    def _seed(self) -> None:
        conn = sqlite3.connect(TEST_DB)
        try:
            enterprise = conn.execute(
                "SELECT enterprise_id FROM enterprises LIMIT 1"
            ).fetchone()[0]
            for user_id, username, email, display_name, provider, _ in self.ROWS:
                conn.execute(
                    "INSERT INTO users (user_id, enterprise_id, username, email, "
                    "display_name, hashed_password, sso_provider, sso_provider_id) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        user_id,
                        enterprise,
                        username,
                        email,
                        display_name,
                        None if provider else "x",
                        provider,
                        f"sub_{user_id}" if provider else None,
                    ),
                )
            conn.commit()
        finally:
            conn.close()

    def _names(self) -> dict:
        ids = tuple(row[0] for row in self.ROWS)
        return {
            user_id: name
            for user_id, name in query_rows(
                TEST_DB, "SELECT user_id, display_name FROM users"
            )
            if user_id in ids
        }

    def test_upgrade_relabels_only_the_derived_names(
        self, clean_database, database_url
    ):
        prepare_database(CASE_CREATOR_RESTRICT_REVISION, database_url)
        self._seed()
        tables = get_tables(TEST_DB)

        result = run_alembic(f"upgrade {NO_EMAIL_DERIVED_NAMES_REVISION}", database_url)
        assert result.returncode == 0, result.stderr
        assert self._names() == {row[0]: row[5] for row in self.ROWS}
        assert self.RELABELLED == 7
        assert "016: relabelled 7 email-derived display name(s)" in result.stderr
        assert "016: left 1 display name(s) that match only the Redis" in (
            result.stderr
        )
        assert get_tables(TEST_DB) == tables

    def test_downgrade_restores_nothing_and_a_rerun_changes_nothing(
        self, clean_database, database_url
    ):
        prepare_database(CASE_CREATOR_RESTRICT_REVISION, database_url)
        self._seed()
        assert (
            run_alembic(
                f"upgrade {NO_EMAIL_DERIVED_NAMES_REVISION}", database_url
            ).returncode
            == 0
        )
        relabelled = self._names()

        result = run_alembic(
            f"downgrade {CASE_CREATOR_RESTRICT_REVISION}", database_url
        )
        assert result.returncode == 0, result.stderr
        assert get_current_revision(database_url) == CASE_CREATOR_RESTRICT_REVISION
        assert self._names() == relabelled

        result = run_alembic(f"upgrade {NO_EMAIL_DERIVED_NAMES_REVISION}", database_url)
        assert result.returncode == 0, result.stderr
        assert "016: relabelled 0 email-derived display name(s)" in result.stderr
        assert self._names() == relabelled

    @pytest.mark.parametrize(
        "url", ["postgresql://offline@localhost/offline", "sqlite:///offline.db"]
    )
    def test_offline_emits_the_online_update(self, url):
        """``--sql`` prints the very statement an online run executes, with no
        doubled ``%`` from a pyformat driver's escaping."""
        import importlib.util

        path = next(
            (PROJECT_ROOT / "alembic" / "versions").glob(
                f"*_{NO_EMAIL_DERIVED_NAMES_REVISION}_*.py"
            )
        )
        spec = importlib.util.spec_from_file_location("rev016", path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)

        up = run_alembic(
            f"upgrade {CASE_CREATOR_RESTRICT_REVISION}:"
            f"{NO_EMAIL_DERIVED_NAMES_REVISION} --sql",
            url,
        )
        assert up.returncode == 0, up.stderr
        assert f"{module.RELABEL};" in up.stdout
        assert "%%" not in up.stdout
        assert "ROW LEVEL SECURITY" not in up.stdout


class TestDropInvestigationSessionsRevision:
    """017 retires the investigation-sessions subsystem (#1935):
    ``investigation_sessions`` is dropped, and its downgrade recreates the
    table, empty, exactly as 016 had it."""

    @staticmethod
    def _schema() -> list:
        """The table and its indexes, as SQLite stores their DDL."""
        return query_rows(
            TEST_DB,
            "SELECT type, name, sql FROM sqlite_master "
            "WHERE tbl_name = 'investigation_sessions' ORDER BY type, name",
        )

    def test_upgrade_drops_the_table(self, clean_database, database_url):
        prepare_database(NO_EMAIL_DERIVED_NAMES_REVISION, database_url)
        assert self._schema(), "positive control: 016 has the table"
        tables = get_tables(TEST_DB)

        result = run_alembic(
            f"upgrade {DROP_INVESTIGATION_SESSIONS_REVISION}", database_url
        )
        assert result.returncode == 0, result.stderr
        assert self._schema() == []
        assert get_tables(TEST_DB) == sorted(set(tables) - {"investigation_sessions"})

    def test_downgrade_recreates_the_table_as_the_parent_had_it(
        self, clean_database, database_url
    ):
        prepare_database(NO_EMAIL_DERIVED_NAMES_REVISION, database_url)
        parent = self._schema()
        assert [name for kind, name, _ in parent if kind == "index"] == [
            "ix_investigation_sessions_case_id",
            "ix_investigation_sessions_created_at",
            "ix_investigation_sessions_enterprise_id",
            "ix_investigation_sessions_organization_id",
            "ix_investigation_sessions_state",
            "ix_investigation_sessions_user_id",
            "sqlite_autoindex_investigation_sessions_1",  # the primary key
        ]

        result = run_alembic(
            f"upgrade {DROP_INVESTIGATION_SESSIONS_REVISION}", database_url
        )
        assert result.returncode == 0, result.stderr
        result = run_alembic(
            f"downgrade {NO_EMAIL_DERIVED_NAMES_REVISION}", database_url
        )
        assert result.returncode == 0, result.stderr

        assert get_current_revision(database_url) == NO_EMAIL_DERIVED_NAMES_REVISION
        assert self._schema() == parent

    @staticmethod
    def _session_rbac() -> tuple[list, list]:
        """The ``sessions:*`` permission rows and every grant of one."""
        permissions = query_rows(
            TEST_DB,
            "SELECT permission_id, resource, action, description FROM permissions "
            "WHERE resource = 'sessions' ORDER BY permission_id",
        )
        grants = query_rows(
            TEST_DB,
            "SELECT rp.role_id, rp.permission_id FROM role_permissions rp "
            "JOIN permissions p ON p.permission_id = rp.permission_id "
            "WHERE p.resource = 'sessions' ORDER BY rp.role_id, rp.permission_id",
        )
        return permissions, grants

    def test_the_session_permissions_go_and_come_back_exactly(
        self, clean_database, database_url
    ):
        """The four ``sessions:*`` permissions and their nine grants are deleted
        by the upgrade, nothing else in the RBAC seed moves, and the downgrade
        re-seeds exactly the parent's rows."""
        prepare_database(NO_EMAIL_DERIVED_NAMES_REVISION, database_url)
        parent_permissions, parent_grants = self._session_rbac()
        assert {
            permission_id: (resource, action, description)
            for permission_id, resource, action, description in parent_permissions
        } == {
            "f7c7be05-bf80-5504-832d-210827a0e450": ("sessions", "read", None),
            "cd3ba768-fd85-5c10-83da-830194251e29": ("sessions", "create", None),
            "7123672b-6cb6-529d-87b1-6c74101c44a3": ("sessions", "execute", None),
            "7a9a6bde-3a6a-5086-b5c1-7703d524ce59": ("sessions", "manage", None),
        }, "positive control: 016 carries the baseline's four rows"
        assert (
            len(parent_grants) == 9
        ), "positive control: 4 admin + 4 member + 1 viewer"
        others = query_rows(
            TEST_DB,
            "SELECT permission_id FROM permissions WHERE resource <> 'sessions' "
            "ORDER BY permission_id",
        )
        other_grants = query_rows(
            TEST_DB,
            "SELECT role_id, permission_id FROM role_permissions "
            "WHERE permission_id NOT IN (SELECT permission_id FROM permissions "
            "WHERE resource = 'sessions') ORDER BY role_id, permission_id",
        )

        result = run_alembic(
            f"upgrade {DROP_INVESTIGATION_SESSIONS_REVISION}", database_url
        )
        assert result.returncode == 0, result.stderr
        assert self._session_rbac() == ([], [])
        # No orphan grant survives on SQLite, where the cascade may be off.
        assert query_rows(
            TEST_DB,
            "SELECT COUNT(*) FROM role_permissions WHERE permission_id NOT IN "
            "(SELECT permission_id FROM permissions)",
        ) == [(0,)]
        assert (
            query_rows(
                TEST_DB,
                "SELECT permission_id FROM permissions ORDER BY permission_id",
            )
            == others
        )
        assert (
            query_rows(
                TEST_DB,
                "SELECT role_id, permission_id FROM role_permissions "
                "ORDER BY role_id, permission_id",
            )
            == other_grants
        )

        result = run_alembic(
            f"downgrade {NO_EMAIL_DERIVED_NAMES_REVISION}", database_url
        )
        assert result.returncode == 0, result.stderr
        assert self._session_rbac() == (parent_permissions, parent_grants)

    def test_no_foreign_key_targets_the_dropped_table(
        self, clean_database, database_url
    ):
        """Why the drop needs no ``PRAGMA foreign_keys`` guard: dropping a
        table runs ON DELETE actions only for rows that REFERENCE it, and at
        the parent revision nothing does."""
        prepare_database(NO_EMAIL_DERIVED_NAMES_REVISION, database_url)
        referencing = []
        for (table,) in query_rows(
            TEST_DB, "SELECT name FROM sqlite_master WHERE type = 'table'"
        ):
            for row in query_rows(TEST_DB, f'PRAGMA foreign_key_list("{table}")'):
                if row[2] == "investigation_sessions":
                    referencing.append(table)
        assert referencing == []

    def test_the_postgresql_downgrade_restores_row_level_security(self):
        """Offline (``--sql``): the upgrade is a bare ``DROP TABLE`` (the policy
        goes with the table), and the downgrade enrols the recreated table in
        RLS with the baseline's tenant policy."""
        pg = "postgresql://offline@localhost/offline"
        up = run_alembic(
            f"upgrade {NO_EMAIL_DERIVED_NAMES_REVISION}:"
            f"{DROP_INVESTIGATION_SESSIONS_REVISION} --sql",
            pg,
        )
        assert up.returncode == 0, up.stderr
        assert "DROP TABLE investigation_sessions;" in up.stdout
        assert "POLICY" not in up.stdout
        # Grants first, then the permissions; no row_security toggle, because
        # neither table is enrolled in RLS.
        grants = up.stdout.index("DELETE FROM role_permissions")
        perms = up.stdout.index("DELETE FROM permissions WHERE resource = 'sessions'")
        assert grants < perms
        assert "row_security" not in up.stdout

        down = run_alembic(
            f"downgrade {DROP_INVESTIGATION_SESSIONS_REVISION}:"
            f"{NO_EMAIL_DERIVED_NAMES_REVISION} --sql",
            pg,
        )
        assert down.returncode == 0, down.stderr
        sql = down.stdout
        assert "CREATE TABLE investigation_sessions" in sql
        assert 'ALTER TABLE "investigation_sessions" ENABLE ROW LEVEL SECURITY' in sql
        assert (
            'CREATE POLICY "investigation_sessions_tenant_isolation" ON '
            '"investigation_sessions" '
            "USING (enterprise_id = current_setting('app.current_enterprise_id', "
            "true))"
        ) in sql


class TestRunbookDraftsInTheDatabaseRevision:
    """018 gives every draft its text in ``conversion_drafts.content`` and drops
    ``file_path`` (#1950). The backfill reads files relative to the working
    directory the migration runs in, so these runs use a working directory of
    their own, holding (or not) a ``data/knowledge`` tree."""

    ENTERPRISE = "00000000-0000-0000-0000-000000000002"
    PUBLISHED = "---\nid: rb-verified\nstatus: verified\n---\n\n# Published\n"
    ON_DISK = "---\nid: rb-file\nstatus: draft\n---\n\n# From the file\n"

    #: (draft id, job id, runbook id, status, knowledge_item_id, file_path)
    DRAFTS = (
        # Verified: its text is the published runbook's; no file needed.
        ("d_verified", "job_doc", "rb-verified", "verified", "ki_1", "gone.md"),
        # Live, its file is in place: its text is the file's.
        (
            "d_file",
            "job_doc",
            "rb-file",
            "draft",
            None,
            "data/knowledge/global/rb-file.md",
        ),
        # Live, file gone, the only draft of a live case job: discarded, and
        # the case's live key released.
        (
            "d_gone",
            "job_case_1",
            "rb-gone",
            "draft",
            None,
            "data/knowledge/user_u1/rb-gone.md",
        ),
        # Live, file gone, but its job keeps a live draft with text: the key
        # stays.
        (
            "d_gone_2",
            "job_case_2",
            "rb-gone-2",
            "draft",
            None,
            "data/knowledge/user_u1/rb-gone-2.md",
        ),
        (
            "d_kept",
            "job_case_2",
            "rb-kept",
            "draft",
            None,
            "data/knowledge/user_u1/rb-kept.md",
        ),
        # Live, its path climbs out of the tree to a file that exists: never
        # read, so discarded.
        ("d_escape", "job_doc", "rb-escape", "draft", None, "outside.md"),
        # Already discarded: text ''.
        ("d_old", "job_doc", "rb-old", "discarded", None, "data/knowledge/old.md"),
    )
    JOBS = (("job_doc", None), ("job_case_1", "case-1"), ("job_case_2", "case-2"))

    def _seed(self, *, drafts=DRAFTS) -> None:
        conn = sqlite3.connect(TEST_DB)
        try:
            conn.execute(
                "INSERT INTO knowledge_items (item_id, enterprise_id, title, "
                "content, item_type) VALUES ('ki_1', ?, 'Published', ?, 'runbook')",
                (self.ENTERPRISE, self.PUBLISHED),
            )
            for job_id, case_id in self.JOBS:
                conn.execute(
                    "INSERT INTO uploaded_files (file_id, enterprise_id, filename, "
                    "size_bytes) VALUES (?, ?, 'src', 1)",
                    (f"file_{job_id}", self.ENTERPRISE),
                )
                conn.execute(
                    "INSERT INTO conversion_jobs (id, enterprise_id, source_file_id, "
                    "scope, source_type, case_id, live_case_id) VALUES "
                    "(?, ?, ?, 'personal', ?, ?, ?)",
                    (
                        job_id,
                        self.ENTERPRISE,
                        f"file_{job_id}",
                        "case" if case_id else "document",
                        case_id,
                        case_id,
                    ),
                )
            for draft_id, job_id, runbook_id, status, item, path in drafts:
                conn.execute(
                    "INSERT INTO conversion_drafts (id, enterprise_id, conversion_id, "
                    "runbook_id, title, file_path, status, knowledge_item_id) "
                    "VALUES (?, ?, ?, ?, 't', ?, ?, ?)",
                    (draft_id, self.ENTERPRISE, job_id, runbook_id, path, status, item),
                )
            conn.commit()
        finally:
            conn.close()

    @staticmethod
    def _workdir(tmp_path, *, with_tree: bool) -> Path:
        """A working directory like the application root: optionally holding
        ``data/knowledge`` with the two files the seed's paths name, plus a
        file OUTSIDE the tree that an escaping path names."""
        work = tmp_path / "app"
        work.mkdir()
        (work / "outside.md").write_text("SECRET, NOT A RUNBOOK", encoding="utf-8")
        if with_tree:
            kb = work / "data" / "knowledge"
            (kb / "global").mkdir(parents=True)
            (kb / "user_u1").mkdir()
            (kb / "global" / "rb-file.md").write_text(
                TestRunbookDraftsInTheDatabaseRevision.ON_DISK, encoding="utf-8"
            )
            (kb / "user_u1" / "rb-kept.md").write_text(
                "---\nid: rb-kept\n---\n\n# Kept\n", encoding="utf-8"
            )
        return work

    @staticmethod
    def _run_in(workdir: Path, command: str, database_url: str):
        """``run_alembic`` from another working directory: a copy of
        ``alembic.ini`` whose script location is absolute."""
        ini = (PROJECT_ROOT / "alembic.ini").read_text(encoding="utf-8")
        ini = ini.replace(
            "script_location = alembic",
            f"script_location = {PROJECT_ROOT / 'alembic'}",
        )
        config = workdir / "alembic.ini"
        config.write_text(ini, encoding="utf-8")
        env = os.environ.copy()
        set_env_var(env, "DATABASE_URL", database_url)
        env["PYTHONPATH"] = os.pathsep.join(
            part for part in (str(PROJECT_ROOT), env.get("PYTHONPATH")) if part
        )
        return subprocess.run(
            [sys.executable, "-m", "alembic", "-c", str(config), *shlex.split(command)],
            cwd=workdir,
            env=env,
            capture_output=True,
            text=True,
        )

    @staticmethod
    def _drafts() -> dict:
        return {
            row[0]: row[1:]
            for row in query_rows(
                TEST_DB, "SELECT id, status, content FROM conversion_drafts"
            )
        }

    @staticmethod
    def _live_keys() -> dict:
        return dict(query_rows(TEST_DB, "SELECT id, live_case_id FROM conversion_jobs"))

    @staticmethod
    def _columns() -> dict:
        return {
            row[1]: row[3]
            for row in query_rows(TEST_DB, "PRAGMA table_info(conversion_drafts)")
        }

    def test_upgrade_fills_discards_and_releases(
        self, clean_database, database_url, tmp_path
    ):
        prepare_database(DROP_INVESTIGATION_SESSIONS_REVISION, database_url)
        self._seed()
        work = self._workdir(tmp_path, with_tree=True)

        result = self._run_in(
            work, f"upgrade {DRAFTS_IN_THE_DATABASE_REVISION}", database_url
        )

        assert result.returncode == 0, result.stderr
        drafts = self._drafts()
        assert drafts["d_verified"] == ("verified", self.PUBLISHED)
        assert drafts["d_file"] == ("draft", self.ON_DISK)
        assert drafts["d_kept"] == ("draft", "---\nid: rb-kept\n---\n\n# Kept\n")
        for gone in ("d_gone", "d_gone_2", "d_escape", "d_old"):
            assert drafts[gone] == ("discarded", ""), gone
        assert self._live_keys() == {
            "job_doc": None,
            "job_case_1": None,
            "job_case_2": "case-2",
        }
        assert "018: filled 2 draft(s) from their files" in result.stderr
        assert (
            "018: discarding 3 live draft(s) whose text is gone: "
            "d_escape, d_gone, d_gone_2" in result.stderr
        )
        columns = self._columns()
        assert "file_path" not in columns
        assert columns["content"] == 1, "content is NOT NULL"
        # The rebuild kept the partial unique index.
        assert any(
            "status <> 'discarded'" in (sql or "")
            for (sql,) in query_rows(
                TEST_DB,
                "SELECT sql FROM sqlite_master WHERE type = 'index' "
                "AND name = 'uq_conversion_drafts_enterprise_runbook_id'",
            )
        )

    def test_without_the_tree_the_upgrade_refuses_and_changes_nothing(
        self, clean_database, database_url, tmp_path
    ):
        prepare_database(DROP_INVESTIGATION_SESSIONS_REVISION, database_url)
        self._seed()
        work = self._workdir(tmp_path, with_tree=False)

        result = self._run_in(
            work, f"upgrade {DRAFTS_IN_THE_DATABASE_REVISION}", database_url
        )

        assert result.returncode != 0
        assert "018 refused: 5 live runbook draft(s)" in result.stderr
        assert str(work) in result.stderr
        assert (
            get_current_revision(database_url) == DROP_INVESTIGATION_SESSIONS_REVISION
        )
        assert "content" not in self._columns()
        assert (
            dict(query_rows(TEST_DB, "SELECT id, status FROM conversion_drafts"))[
                "d_gone"
            ]
            == "draft"
        )

    def test_without_the_tree_published_drafts_alone_still_migrate(
        self, clean_database, database_url, tmp_path
    ):
        """The guard fires only when a draft would be LOST: a database whose
        live drafts are all published migrates from anywhere."""
        prepare_database(DROP_INVESTIGATION_SESSIONS_REVISION, database_url)
        self._seed(drafts=self.DRAFTS[:1] + self.DRAFTS[-1:])
        work = self._workdir(tmp_path, with_tree=False)

        result = self._run_in(
            work, f"upgrade {DRAFTS_IN_THE_DATABASE_REVISION}", database_url
        )

        assert result.returncode == 0, result.stderr
        assert self._drafts() == {
            "d_verified": ("verified", self.PUBLISHED),
            "d_old": ("discarded", ""),
        }

    def test_downgrade_restores_a_nullable_file_path_and_drops_the_text(
        self, clean_database, database_url, tmp_path
    ):
        prepare_database(DROP_INVESTIGATION_SESSIONS_REVISION, database_url)
        self._seed()
        work = self._workdir(tmp_path, with_tree=True)
        assert (
            self._run_in(
                work, f"upgrade {DRAFTS_IN_THE_DATABASE_REVISION}", database_url
            ).returncode
            == 0
        )
        statuses = {k: v[0] for k, v in self._drafts().items()}

        result = run_alembic(
            f"downgrade {DROP_INVESTIGATION_SESSIONS_REVISION}", database_url
        )

        assert result.returncode == 0, result.stderr
        columns = self._columns()
        assert "content" not in columns
        assert columns["file_path"] == 0, "file_path comes back nullable"
        assert (
            dict(query_rows(TEST_DB, "SELECT id, status FROM conversion_drafts"))
            == statuses
        )
        assert query_rows(
            TEST_DB,
            "SELECT COUNT(*) FROM conversion_drafts WHERE file_path IS NOT NULL",
        ) == [(0,)]

        # And up again: every live draft now has no file to read, the
        # published one keeps its text.
        result = self._run_in(
            work, f"upgrade {DRAFTS_IN_THE_DATABASE_REVISION}", database_url
        )
        assert result.returncode == 0, result.stderr
        assert self._drafts()["d_verified"] == ("verified", self.PUBLISHED)

    def test_offline_postgresql_warns_first_and_emits_the_rest(self):
        """PostgreSQL renders: the warning is the first statement, before the
        column exists, and the file step is marked where it would run."""
        result = run_alembic(
            f"upgrade {DROP_INVESTIGATION_SESSIONS_REVISION}:"
            f"{DRAFTS_IN_THE_DATABASE_REVISION} --sql",
            "postgresql://offline@localhost/offline",
        )
        assert result.returncode == 0, result.stderr
        sql = result.stdout
        revision = sql.index("-- Running upgrade f95a5b3a58eb -> c4441b83ba24")
        warning = sql.index("-- WARNING (018): this script cannot read draft files")
        add = sql.index("ALTER TABLE conversion_drafts ADD COLUMN content TEXT")
        verified = sql.index("UPDATE conversion_drafts SET content = (SELECT")
        note = sql.index("-- 018 step 2")
        release = sql.index("UPDATE conversion_jobs SET live_case_id = NULL")
        discard = sql.index("UPDATE conversion_drafts SET status = 'discarded'")
        empty = sql.index("UPDATE conversion_drafts SET content = '' WHERE")
        assert revision < warning < add < verified < note < release < discard < empty
        assert "DISCARDS every live runbook draft" in sql
        assert sql.index("SET LOCAL row_security = off") < verified
        assert empty < sql.index("SET LOCAL row_security TO DEFAULT")
        assert "ALTER TABLE conversion_drafts DROP COLUMN file_path" in sql
        assert "ALTER COLUMN content SET NOT NULL" in sql

    def test_offline_sqlite_refuses_to_render(self):
        """SQLite is standalone, whose drafts' text is in files a script cannot
        read: rendering would hand an operator a script that discards them."""
        result = run_alembic(
            f"upgrade {DROP_INVESTIGATION_SESSIONS_REVISION}:"
            f"{DRAFTS_IN_THE_DATABASE_REVISION} --sql",
            "sqlite:///offline.db",
        )
        assert result.returncode != 0
        assert (
            "step (b) reads draft files; standalone migrates online from the app "
            "root" in result.stderr
        )
        assert "ALTER TABLE conversion_drafts" not in result.stdout


class TestVerifiedByIsTheDisplayNameRevision:
    """019 rewrites a verified runbook's frontmatter ``verified_by`` to its
    verifier's current display name (#1948): the published item and its
    verified draft alike, byte-equal. Runbooks are chosen by the verifier
    foreign key, and only a value derived from the verifier's account (the
    username, an address, the real email's local part) is rewritten; a name the
    owner chose stays. Uploads and the pack keep their own text."""

    OTHER_ENTERPRISE = "0e0e0e0e-0000-4000-8000-0000000000ee"
    ALICE = "a11ce000-0000-4000-8000-000000000001"
    KIM = "c1a00000-0000-4000-8000-000000000002"
    DANA = "da0a0000-0000-4000-8000-000000000003"
    ELSEWHERE = "e15e0000-0000-4000-8000-000000000004"
    FRANK = "f4a0c000-0000-4000-8000-000000000005"
    ROBERT = "40be4700-0000-4000-8000-000000000006"

    #: (user_id, username, email, display_name, in the other enterprise)
    USERS = (
        (ALICE, "alice.smith", "alice.smith@example.com", "Alice Smith", False),
        # Renamed since the verification: the runbook still holds "kim", which
        # is neither the username now nor the email's local part.
        (KIM, "kim-renamed", "kimberly@example.com", "Kim Lee", False),
        (DANA, "dana", "dana@example.com", "Dana", False),
        # Anchored in another enterprise than the runbook's: verify finds no
        # account there, and writes "".
        (ELSEWHERE, "eve", "eve@example.com", "Eve Example", True),
        # A username that is not the email's local part.
        (FRANK, "frank-2", "frank@acme.com", "Frank Ng", False),
        # Registered with no email: the store synthesized one from the username
        # it had then, so its local part is not an email-derived name.
        (ROBERT, "robert2", "robert@faultmaven.example", "Robert R", False),
    )

    @staticmethod
    def _runbook(rb_id: str, verified_by_line: Optional[str]) -> str:
        """A runbook as verify's serialiser writes it: keys sorted, one blank
        line, the body stripped."""
        line = f"{verified_by_line}\n" if verified_by_line is not None else ""
        return (
            f"---\nid: {rb_id}\nstatus: verified\ntitle: Disk full\n{line}"
            "version: 1.0.0\n---\n\n# Disk full\n\nRun df -h on node@host."
        )

    #: (item id, verifier FK, verification_level, source_url, verified_by line,
    #:  the line expected after 019 — None: the text is unchanged)
    ITEMS = (
        # Verify's frontmatter path wrote the username, unquoted.
        (
            "ki_unquoted",
            ALICE,
            1,
            "conversion:job_1",
            "verified_by: alice.smith",
            'verified_by: "Alice Smith"',
        ),
        # The same value quoted.
        (
            "ki_quoted",
            ALICE,
            1,
            "conversion:job_1",
            'verified_by: "alice.smith"',
            'verified_by: "Alice Smith"',
        ),
        # The username in another case, padded: compared folded and stripped.
        # Frank's username is not his email's local part, so only the username
        # rule can match it.
        (
            "ki_mixed",
            FRANK,
            1,
            "conversion:job_1",
            'verified_by: "  FRANK-2 "',
            'verified_by: "Frank Ng"',
        ),
        # An address.
        (
            "ki_address",
            ALICE,
            1,
            "conversion:job_1",
            "verified_by: alice.smith@example.com",
            'verified_by: "Alice Smith"',
        ),
        # The real email's local part, in another case: account-derived.
        (
            "ki_localpart",
            FRANK,
            1,
            "conversion:job_1",
            "verified_by: FRANK",
            'verified_by: "Frank Ng"',
        ),
        # A username renamed since the verification: the documented false
        # negative, left as it is.
        ("ki_renamed", KIM, 1, "conversion:job_1", "verified_by: kim", None),
        # A name the owner chose: not derived from the account, kept.
        ("ki_team", ALICE, 1, "conversion:job_1", "verified_by: SRE Team", None),
        # The local part of a SYNTHESIZED email is not an email-derived name.
        ("ki_synth", ROBERT, 1, "conversion:job_1", "verified_by: robert", None),
        # Already the display name: untouched, byte for byte.
        ("ki_current", DANA, 1, "conversion:job_1", "verified_by: Dana", None),
        # The verifier's account is gone (FK cleared): emptied.
        (
            "ki_gone",
            None,
            1,
            "conversion:job_1",
            "verified_by: gone@example.com",
            'verified_by: ""',
        ),
        # The verifier is gone and the value holds no '@': nothing shows it
        # was derived from the account, so it stays.
        (
            "ki_gone_name",
            None,
            1,
            "conversion:job_1",
            "verified_by: Former Lead",
            None,
        ),
        # The verifier is anchored in another enterprise: emptied.
        (
            "ki_elsewhere",
            ELSEWHERE,
            1,
            "conversion:job_1",
            "verified_by: eve",
            'verified_by: ""',
        ),
        # No verified_by key: nothing to rewrite.
        ("ki_nokey", ALICE, 1, "conversion:job_1", None, None),
        # Uploaded (no verifier, EXPERIMENTAL): the uploader's own text.
        (
            "ki_upload",
            None,
            0,
            "conversion:forged",
            "verified_by: up@example.com",
            None,
        ),
        # The pack (COMMUNITY, no verifier, and no source: NULL, as shipped).
        (
            "kb_0123456789ab",
            None,
            1,
            None,
            "verified_by: sre@example.com",
            None,
        ),
    )
    #: Verify's old fallback for a frontmatter that did not parse: the literal
    #: ``verified_by: ""`` replaced with the quoted username. The body quotes
    #: the same line, which is never touched.
    UNPARSEABLE = (
        '---\nid: rb-broken\ntitle: [unclosed\nverified_by: "alice.smith"\n'
        '---\n\n# Broken\n\nverified_by: "alice.smith"\n'
    )
    #: A frontmatter that does not parse and lacks the line; only the body holds
    #: it. Left as it is (and logged).
    UNPARSEABLE_BODY_ONLY = (
        "---\nid: rb-broken-2\ntitle: [unclosed\nstatus: verified\n---\n\n"
        '# Broken\n\nexample verified_by: "alice.smith"\n'
    )
    #: Owner text the parser cannot read, stored unvalidated: an impossible date
    #: (``ValueError``) and nesting too deep (``RecursionError``). The migration
    #: completes and leaves each row as it is: (item id, verification_level,
    #: content). The upload's goes through the operator scan for '@' values.
    CANNOT_PARSE = (
        (
            "ki_baddate",
            1,
            "---\nid: rb-date\nlast_updated: 2024-02-30\nverified_by: alice.smith\n"
            "---\n\n# Body\n",
        ),
        (
            "ki_deep",
            1,
            "---\nid: rb-deep\nverified_by: alice.smith\nnest: "
            + "[" * 3000
            + "]" * 3000
            + "\n---\n\n# Body\n",
        ),
        (
            "ki_upload_baddate",
            0,
            "---\nid: rb-up\nlast_updated: 2024-02-30\nverified_by: up@example.com\n"
            "---\n\n# Body\n",
        ),
    )
    #: A value held through a YAML anchor or alias is shared with another key: a
    #: splice would rewrite the wrong span or drop the anchor. Left as it is.
    SHARED = (
        (
            "ki_alias",
            "---\nowner: &who alice.smith\nverified_by: *who\nstatus: verified\n"
            "---\n\n# Body\n",
        ),
        (
            "ki_anchor",
            "---\nverified_by: &who alice.smith\nowner: *who\nstatus: verified\n"
            "---\n\n# Body\n",
        ),
    )
    #: Texts stored verbatim, as ``PUT /knowledge/documents/{id}`` stores an
    #: owner's edit: (item id, before, after). Only the value changes.
    VERBATIM = (
        # Comment, unsorted keys, quoting, list indent, a timestamp, the
        # trailing newline, and the body quoting the line.
        (
            "ki_edited",
            '---\n# reviewed quarterly\ntitle: "Disk full"\nverified_by: alice.smith\n'
            "status: verified\ntags:\n  - disk\n  - linux\nts: 2024-05-01T10:00:00Z\n"
            "---\n\n# Disk full\n\nedited body\nverified_by: alice.smith\n",
            '---\n# reviewed quarterly\ntitle: "Disk full"\n'
            'verified_by: "Alice Smith"\n'
            "status: verified\ntags:\n  - disk\n  - linux\nts: 2024-05-01T10:00:00Z\n"
            "---\n\n# Disk full\n\nedited body\nverified_by: alice.smith\n",
        ),
        # CRLF line endings throughout, kept.
        (
            "ki_crlf",
            "---\r\nid: rb-crlf\r\nstatus: verified\r\nverified_by: alice.smith\r\n"
            "---\r\n\r\n# Disk full\r\n\r\nRun df -h.\r\n",
            "---\r\nid: rb-crlf\r\nstatus: verified\r\n"
            'verified_by: "Alice Smith"\r\n'
            "---\r\n\r\n# Disk full\r\n\r\nRun df -h.\r\n",
        ),
        # A folded block scalar: its span ends past its line break, kept.
        (
            "ki_folded",
            "---\nid: rb-folded\nverified_by: >-\n  alice.smith\nstatus: verified\n"
            "---\n\nbody\n",
            '---\nid: rb-folded\nverified_by: "Alice Smith"\nstatus: verified\n'
            "---\n\nbody\n",
        ),
    )

    def _expected(self, item_id: str, line: Optional[str], after: Optional[str]):
        before = self._runbook(item_id, line)
        if after is None:
            return before
        return before.replace(f"{line}\n", f"{after}\n")

    def _seed(self) -> str:
        conn = sqlite3.connect(TEST_DB)
        try:
            conn.execute("PRAGMA foreign_keys = ON")
            enterprise = conn.execute(
                "SELECT enterprise_id FROM enterprises LIMIT 1"
            ).fetchone()[0]
            conn.execute(
                "INSERT INTO enterprises (enterprise_id, name, slug) "
                "VALUES (?, 'Other', 'other')",
                (self.OTHER_ENTERPRISE,),
            )
            for user_id, username, email, name, elsewhere in self.USERS:
                conn.execute(
                    "INSERT INTO users (user_id, enterprise_id, username, email, "
                    "display_name, hashed_password) VALUES (?, ?, ?, ?, ?, 'x')",
                    (
                        user_id,
                        self.OTHER_ENTERPRISE if elsewhere else enterprise,
                        username,
                        email,
                        name,
                    ),
                )
            conn.execute(
                "INSERT INTO uploaded_files (file_id, enterprise_id, filename, "
                "size_bytes) VALUES ('file_1', ?, 'src', 1)",
                (enterprise,),
            )
            conn.execute(
                "INSERT INTO conversion_jobs (id, enterprise_id, source_file_id, "
                "scope, source_type) VALUES ('job_1', ?, 'file_1', 'personal', "
                "'document')",
                (enterprise,),
            )
            rows = [
                (item_id, verifier, level, source, self._runbook(item_id, line))
                for item_id, verifier, level, source, line, _ in self.ITEMS
            ]
            rows.append(
                ("ki_broken", self.ALICE, 1, "conversion:job_1", self.UNPARSEABLE)
            )
            rows.append(
                (
                    "ki_broken_body",
                    self.ALICE,
                    1,
                    "conversion:job_1",
                    self.UNPARSEABLE_BODY_ONLY,
                )
            )
            rows.extend(
                (item_id, self.ALICE, 1, "conversion:job_1", before)
                for item_id, before, _ in self.VERBATIM
            )
            rows.extend(
                (
                    item_id,
                    self.ALICE if level else None,
                    level,
                    "conversion:job_1",
                    content,
                )
                for item_id, level, content in self.CANNOT_PARSE
            )
            rows.extend(
                (item_id, self.ALICE, 1, "conversion:job_1", content)
                for item_id, content in self.SHARED
            )
            for item_id, verifier, level, source, content in rows:
                conn.execute(
                    "INSERT INTO knowledge_items (item_id, enterprise_id, title, "
                    "content, item_type, verified_by, verification_level, "
                    "source_url) VALUES (?, ?, 'Disk full', ?, 'runbook', ?, ?, ?)",
                    (item_id, enterprise, content, verifier, level, source),
                )
                if item_id.startswith("kb_"):
                    continue
                # Every item but the pack's has its verified draft; an upload's
                # names its owner as the verifier.
                conn.execute(
                    "INSERT INTO conversion_drafts (id, enterprise_id, "
                    "conversion_id, runbook_id, title, content, status, "
                    "knowledge_item_id, verified_by) "
                    "VALUES (?, ?, 'job_1', ?, 't', ?, 'verified', ?, ?)",
                    (
                        f"d_{item_id}",
                        enterprise,
                        f"rb_{item_id}",
                        content,
                        item_id,
                        (
                            self.ALICE
                            if item_id in ("ki_upload", "ki_upload_baddate")
                            else verifier
                        ),
                    ),
                )
            # A discarded draft: nothing serves its text.
            conn.execute(
                "INSERT INTO conversion_drafts (id, enterprise_id, conversion_id, "
                "runbook_id, title, content, status) VALUES ('d_discarded', ?, "
                "'job_1', 'rb_discarded', 't', ?, 'discarded')",
                (enterprise, self._runbook("x", "verified_by: alice.smith")),
            )
            conn.commit()
            return enterprise
        finally:
            conn.close()

    @staticmethod
    def _items() -> dict:
        return dict(query_rows(TEST_DB, "SELECT item_id, content FROM knowledge_items"))

    @staticmethod
    def _drafts() -> dict:
        return dict(query_rows(TEST_DB, "SELECT id, content FROM conversion_drafts"))

    def _migrate_seeded(self, database_url):
        prepare_database(DRAFTS_IN_THE_DATABASE_REVISION, database_url)
        self._seed()
        return run_alembic(f"upgrade {VERIFIED_BY_DISPLAY_NAME_REVISION}", database_url)

    def _assert_row(self, items: dict, item_id: str) -> None:
        _, _, _, _, line, after = next(row for row in self.ITEMS if row[0] == item_id)
        assert items[item_id] == self._expected(item_id, line, after), item_id

    def test_upgrade_rewrites_the_account_derived_values(
        self, clean_database, database_url
    ):
        result = self._migrate_seeded(database_url)

        assert result.returncode == 0, result.stderr
        items = self._items()
        for row in self.ITEMS:
            self._assert_row(items, row[0])
        # The fallback line in the frontmatter only; the body's copy stays.
        assert items["ki_broken"] == self.UNPARSEABLE.replace(
            '"alice.smith"', '"Alice Smith"', 1
        )
        assert items["ki_broken"].endswith('verified_by: "alice.smith"\n')
        assert items["ki_broken_body"] == self.UNPARSEABLE_BODY_ONLY
        assert (
            "019: rewrote verified_by in 11 published runbook(s) and 11 verified "
            "draft(s); 2 of those runbooks have no verifier account and now hold ''"
        ) in result.stderr
        assert (
            "019: left 6 runbook text(s) whose frontmatter does not parse and "
            "holds no verified_by line verify wrote: ki_baddate, ki_broken_body, "
            "ki_deep, d_ki_baddate, d_ki_broken_body, d_ki_deep"
        ) in result.stderr
        assert (
            "019: left 2 runbook(s) not verified through FaultMaven (uploads, the "
            "pack) whose own verified_by holds an '@': kb_0123456789ab, ki_upload"
        ) in result.stderr
        assert (
            "019: left 6 verified runbook(s) whose verified_by is not derived from "
            "the verifier's account (a name the owner chose): ki_alias, ki_anchor, "
            "ki_gone_name, ki_renamed, ki_synth, ki_team"
        ) in result.stderr

    def test_a_name_the_owner_chose_stays(self, clean_database, database_url):
        """``verified_by`` is a free name-or-team field: ``SRE Team`` is not
        derived from the verifier's account, so it survives, as does a plain
        name on a runbook whose verifier is gone."""
        assert self._migrate_seeded(database_url).returncode == 0
        items = self._items()
        self._assert_row(items, "ki_team")
        self._assert_row(items, "ki_gone_name")
        assert "verified_by: SRE Team\n" in items["ki_team"]

    def test_the_real_emails_local_part_is_account_derived(
        self, clean_database, database_url
    ):
        """``FRANK`` for ``frank@acme.com`` (username ``frank-2``) is rewritten;
        ``robert`` for a synthesized ``robert@faultmaven.example`` is not."""
        assert self._migrate_seeded(database_url).returncode == 0
        items = self._items()
        assert 'verified_by: "Frank Ng"\n' in items["ki_localpart"]
        self._assert_row(items, "ki_synth")

    def test_only_the_value_changes_in_a_text_stored_verbatim(
        self, clean_database, database_url
    ):
        """An owner-edited runbook, a CRLF one and a folded value: the rewrite
        changes the ``verified_by`` value's characters and nothing else, so
        comments, key order, quoting, line endings and the body stay byte for
        byte."""
        assert self._migrate_seeded(database_url).returncode == 0
        items = self._items()
        for item_id, before, after in self.VERBATIM:
            assert items[item_id] == after, item_id
            if item_id == "ki_folded":
                continue  # Two value lines become one; the equality above holds.
            changed = [
                (old, new)
                for old, new in zip(
                    before.splitlines(keepends=True),
                    items[item_id].splitlines(keepends=True),
                )
                if old != new
            ]
            assert len(changed) == 1, (item_id, changed)
            assert changed[0][0].startswith("verified_by:"), (item_id, changed)

    def test_owner_text_the_parser_cannot_read_does_not_stop_the_migration(
        self, clean_database, database_url
    ):
        """An impossible date and nesting too deep raise ``ValueError`` and
        ``RecursionError`` from the YAML loader, not ``YAMLError``. The
        migration completes (a failed one stops a standalone boot) and leaves
        those rows, the upload's included, exactly as they were."""
        result = self._migrate_seeded(database_url)

        assert result.returncode == 0, result.stderr[-3000:]
        items, drafts = self._items(), self._drafts()
        for item_id, _, content in self.CANNOT_PARSE:
            assert items[item_id] == content, item_id
            assert drafts[f"d_{item_id}"] == content, item_id

    def test_a_value_held_through_an_anchor_or_alias_stays(
        self, clean_database, database_url
    ):
        """Left by the anchor guard itself (counted with the values not derived
        from the account), not only by the re-read net behind it."""
        assert self._migrate_seeded(database_url).returncode == 0
        items = self._items()
        module = self._migration_module()
        for item_id, content in self.SHARED:
            assert items[item_id] == content, item_id
            with pytest.raises(module._NotDerived):
                module.rewrite_verified_by(
                    content, "Alice Smith", "alice.smith", "alice.smith@example.com"
                )

    @staticmethod
    def _migration_module():
        import importlib.util

        path = next(
            (PROJECT_ROOT / "alembic" / "versions").glob(
                f"*_{VERIFIED_BY_DISPLAY_NAME_REVISION}_*.py"
            )
        )
        spec = importlib.util.spec_from_file_location("rev019", path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module

    @pytest.mark.parametrize(
        "name",
        ["x\x7fy", "x\x85y", "a\u2028b", 'say "hi"', "back\\slash", "two\nlines"],
    )
    def test_a_display_name_reads_back_exactly(self, name):
        """The new value is YAML's own double-quoted form on one line: a
        character YAML would refuse (``\\x7f``) or read differently (NEL,
        U+2028) is escaped, and the block re-reads as exactly ``name``."""
        import yaml

        module = self._migration_module()
        text = "---\nid: x\nverified_by: alice.smith\nstatus: verified\n---\n\nB\n"

        new = module.rewrite_verified_by(
            text, name, "alice.smith", "alice.smith@example.com"
        )

        block = module._FRONTMATTER_RE.match(new).group(1)
        assert yaml.safe_load(block) == {
            "id": "x",
            "verified_by": name,
            "status": "verified",
        }
        assert len(block.splitlines()) == 3, block

    def test_a_splice_that_would_not_read_back_is_refused(self, monkeypatch):
        """The safety net: whatever the splice writes is re-read first, and a
        frontmatter that would no longer parse is never returned."""
        module = self._migration_module()
        monkeypatch.setattr(module, "_quoted", lambda name: '"unterminated')
        text = "---\nid: x\nverified_by: alice.smith\nstatus: verified\n---\n\nB\n"

        with pytest.raises(module._Unsafe):
            module.rewrite_verified_by(
                text, "Alice Smith", "alice.smith", "alice.smith@example.com"
            )

    def test_a_renamed_username_is_the_documented_false_negative(
        self, clean_database, database_url
    ):
        """The runbook holds ``kim``, the username at verification; the account
        is now ``kim-renamed`` with ``kimberly@example.com``. Nothing on record
        ties ``kim`` to the account, so it stays (the docstring's known false
        negative)."""
        assert self._migrate_seeded(database_url).returncode == 0
        self._assert_row(self._items(), "ki_renamed")

    def test_each_verified_draft_holds_its_items_text(
        self, clean_database, database_url
    ):
        """#1950's invariant survives: a verified draft's text is byte-equal to
        the runbook it was published as, the upload's included (both untouched),
        and the discarded draft keeps its text."""
        result = self._migrate_seeded(database_url)

        assert result.returncode == 0, result.stderr
        items = self._items()
        drafts = self._drafts()
        for item_id, content in items.items():
            if item_id.startswith("kb_"):
                continue
            assert drafts[f"d_{item_id}"] == content, item_id
        assert drafts["d_discarded"] == self._runbook("x", "verified_by: alice.smith")

    def test_downgrade_restores_nothing_and_a_rerun_changes_nothing(
        self, clean_database, database_url
    ):
        assert self._migrate_seeded(database_url).returncode == 0
        items, drafts = self._items(), self._drafts()

        result = run_alembic(
            f"downgrade {DRAFTS_IN_THE_DATABASE_REVISION}", database_url
        )
        assert result.returncode == 0, result.stderr
        assert get_current_revision(database_url) == DRAFTS_IN_THE_DATABASE_REVISION
        assert (self._items(), self._drafts()) == (items, drafts)

        result = run_alembic(
            f"upgrade {VERIFIED_BY_DISPLAY_NAME_REVISION}", database_url
        )
        assert result.returncode == 0, result.stderr
        assert (
            "019: rewrote verified_by in 0 published runbook(s) and 0 verified "
            "draft(s)"
        ) in result.stderr
        assert (self._items(), self._drafts()) == (items, drafts)

    @pytest.mark.parametrize(
        "url", ["postgresql://offline@localhost/offline", "sqlite:///offline.db"]
    )
    def test_offline_renders_the_warning_and_no_rewrite(self, url):
        """A script cannot parse frontmatter: it says so, and changes nothing."""
        result = run_alembic(
            f"upgrade {DRAFTS_IN_THE_DATABASE_REVISION}:"
            f"{VERIFIED_BY_DISPLAY_NAME_REVISION} --sql",
            url,
        )
        assert result.returncode == 0, result.stderr
        sql = result.stdout
        revision = sql.index("-- Running upgrade c4441b83ba24 -> 82bc95a1c02f")
        assert revision < sql.index("-- WARNING (019): this script does NOT rewrite")
        assert (
            "alembic stamp c4441b83ba24, then alembic upgrade 82bc95a1c02f, then "
            "alembic stamp head"
        ) in sql
        assert "UPDATE knowledge_items" not in sql
        assert "UPDATE conversion_drafts" not in sql
        assert "row_security" not in sql[revision:]


class TestOrgPermissionsAreManagementOnlyRevision:
    """020 prunes the organization's permissions to the management axis
    (#1040, owner ruling 2026-09-24): the eight ``cases:*`` / ``evidence:*``
    permissions and every grant of one are deleted, ``org:manage_*`` and
    admin's two grants of them are untouched, the three system roles'
    descriptions say what each manages, and the downgrade restores exactly the
    baseline's eight rows, fifteen system-role grants and three descriptions."""

    ADMIN_ROLE_ID = "50551907-a02c-5bf7-9aa4-4a98f3c4eb64"
    MEMBER_ROLE_ID = "5cb4c3f5-227c-5d73-95a5-d9d2e619ca72"
    VIEWER_ROLE_ID = "834b74a5-33a7-5248-9fd0-b040c12aef7b"

    #: The baseline's data permission rows: permission_id -> (resource, action).
    BASELINE_DATA_PERMISSIONS = {
        "f3adedf0-09df-5151-9490-83da7cff6c34": ("cases", "read"),
        "01fe79a0-a217-5998-9ab8-e7adde82ffde": ("cases", "write"),
        "91ed1dc0-2eb1-519f-b1e2-a1e93815cb59": ("cases", "delete"),
        "818985d2-a051-5e55-86bb-1ba378835787": ("cases", "assign"),
        "5de3208e-7f06-5037-b8d9-8f5f90a0fbdf": ("cases", "close"),
        "d06d9c54-a00e-58e3-ad38-19dbd32975ee": ("evidence", "read"),
        "0bd8e72e-a9ef-5ee4-9a67-32b8602c9d94": ("evidence", "upload"),
        "a41fd5f5-f56b-5fb5-82f0-ff9c8f8ef2bf": ("evidence", "delete"),
    }
    CASES_READ_ID = "f3adedf0-09df-5151-9490-83da7cff6c34"
    #: The system roles' descriptions: the baseline's, and 020's.
    BASELINE_DESCRIPTIONS = [
        ("admin", "Full access to organization resources."),
        ("member", "Standard investigator access."),
        ("viewer", "Read-only access."),
    ]
    MANAGEMENT_DESCRIPTIONS = [
        ("admin", "Manages the organization's roster and settings."),
        ("member", "Belongs to the organization; manages nothing."),
        ("viewer", "Belongs to the organization; manages nothing."),
    ]

    @staticmethod
    def _descriptions() -> list:
        return query_rows(TEST_DB, "SELECT name, description FROM roles ORDER BY name")

    ORG_MANAGE_USERS_ID = "d3a93745-5f12-5622-a596-96e518669e90"
    ORG_MANAGE_SETTINGS_ID = "31938541-ec6c-526f-a4ee-06e8d1b59252"

    @staticmethod
    def _data_rbac() -> tuple[list, list]:
        """The ``cases`` / ``evidence`` permission rows and every grant of one."""
        permissions = query_rows(
            TEST_DB,
            "SELECT permission_id, resource, action, description FROM permissions "
            "WHERE resource IN ('cases', 'evidence') ORDER BY permission_id",
        )
        grants = query_rows(
            TEST_DB,
            "SELECT rp.role_id, rp.permission_id FROM role_permissions rp "
            "JOIN permissions p ON p.permission_id = rp.permission_id "
            "WHERE p.resource IN ('cases', 'evidence') "
            "ORDER BY rp.role_id, rp.permission_id",
        )
        return permissions, grants

    @staticmethod
    def _all_rbac() -> tuple[list, list, list]:
        """The three tables, every column, in a fixed order."""
        return (
            query_rows(TEST_DB, "SELECT * FROM permissions ORDER BY permission_id"),
            query_rows(
                TEST_DB,
                "SELECT * FROM role_permissions ORDER BY role_id, permission_id",
            ),
            query_rows(TEST_DB, "SELECT * FROM roles ORDER BY role_id"),
        )

    def test_upgrade_retires_the_data_permissions_and_nothing_else(
        self, clean_database, database_url
    ):
        prepare_database(VERIFIED_BY_DISPLAY_NAME_REVISION, database_url)
        parent_permissions, parent_grants = self._data_rbac()
        assert {
            permission_id: (resource, action, description)
            for permission_id, resource, action, description in parent_permissions
        } == {
            permission_id: (resource, action, None)
            for permission_id, (
                resource,
                action,
            ) in self.BASELINE_DATA_PERMISSIONS.items()
        }, "positive control: 019 carries the baseline's eight rows"
        grants_by_role = defaultdict(int)
        for role_id, _ in parent_grants:
            grants_by_role[role_id] += 1
        assert dict(grants_by_role) == {
            self.ADMIN_ROLE_ID: 8,
            self.MEMBER_ROLE_ID: 5,
            self.VIEWER_ROLE_ID: 2,
        }, "positive control: 8 admin + 5 member + 2 viewer"
        kept_permissions = query_rows(
            TEST_DB,
            "SELECT * FROM permissions WHERE resource NOT IN ('cases', 'evidence') "
            "ORDER BY permission_id",
        )
        kept_grants = query_rows(
            TEST_DB,
            "SELECT * FROM role_permissions WHERE permission_id NOT IN "
            "(SELECT permission_id FROM permissions "
            "WHERE resource IN ('cases', 'evidence')) "
            "ORDER BY role_id, permission_id",
        )
        assert [row[0] for row in kept_permissions] == sorted(
            [self.ORG_MANAGE_USERS_ID, self.ORG_MANAGE_SETTINGS_ID]
        ), "positive control: only org:manage_* is kept"
        assert kept_grants == sorted(
            [
                (self.ADMIN_ROLE_ID, self.ORG_MANAGE_USERS_ID),
                (self.ADMIN_ROLE_ID, self.ORG_MANAGE_SETTINGS_ID),
            ]
        ), "positive control: admin's two org:manage_* grants"
        assert (
            self._descriptions() == self.BASELINE_DESCRIPTIONS
        ), "positive control: the baseline's descriptions"

        result = run_alembic(
            f"upgrade {ORG_PERMISSIONS_MANAGEMENT_ONLY_REVISION}", database_url
        )
        assert result.returncode == 0, result.stderr
        assert self._data_rbac() == ([], [])
        assert self._descriptions() == self.MANAGEMENT_DESCRIPTIONS
        # No orphan grant survives on SQLite, where the cascade may be off.
        assert query_rows(
            TEST_DB,
            "SELECT COUNT(*) FROM role_permissions WHERE permission_id NOT IN "
            "(SELECT permission_id FROM permissions)",
        ) == [(0,)]
        # The org:* rows and admin's grants of them are byte-identical.
        assert self._all_rbac()[:2] == (kept_permissions, kept_grants)

    def test_downgrade_restores_exactly_the_parent_rows(
        self, clean_database, database_url
    ):
        prepare_database(VERIFIED_BY_DISPLAY_NAME_REVISION, database_url)
        parent = self._all_rbac()
        assert len(parent[0]) == 10 and len(parent[1]) == 17, "positive control"
        assert self._descriptions() == self.BASELINE_DESCRIPTIONS

        result = run_alembic(
            f"upgrade {ORG_PERMISSIONS_MANAGEMENT_ONLY_REVISION}", database_url
        )
        assert result.returncode == 0, result.stderr
        assert self._all_rbac() != parent, "positive control: the upgrade moved"
        result = run_alembic(
            f"downgrade {VERIFIED_BY_DISPLAY_NAME_REVISION}", database_url
        )
        assert result.returncode == 0, result.stderr

        assert get_current_revision(database_url) == VERIFIED_BY_DISPLAY_NAME_REVISION
        assert self._all_rbac() == parent
        assert self._descriptions() == self.BASELINE_DESCRIPTIONS

    def test_a_non_system_roles_grant_is_removed_and_not_restored(
        self, clean_database, database_url
    ):
        """A database at the parent that carries a custom role's grant of
        ``cases:read``: the upgrade deletes that grant with the system roles'
        (and leaves the custom role's ``org:manage_users`` grant), and the
        downgrade, which re-seeds only the baseline's grants, does not bring it
        back."""
        custom = "c0570000-0000-4000-8000-0000000000cc"
        prepare_database(VERIFIED_BY_DISPLAY_NAME_REVISION, database_url)
        conn = sqlite3.connect(TEST_DB)
        try:
            conn.execute(
                "INSERT INTO roles (role_id, name, scope, is_system_role) "
                "VALUES (?, 'incident_lead', 'organization', 0)",
                (custom,),
            )
            conn.executemany(
                "INSERT INTO role_permissions (role_id, permission_id) VALUES (?, ?)",
                [(custom, self.CASES_READ_ID), (custom, self.ORG_MANAGE_USERS_ID)],
            )
            conn.commit()
        finally:
            conn.close()

        def custom_grants() -> list:
            return query_rows(
                TEST_DB,
                "SELECT permission_id FROM role_permissions WHERE role_id = "
                f"'{custom}' ORDER BY permission_id",
            )

        assert custom_grants() == sorted(
            [(self.CASES_READ_ID,), (self.ORG_MANAGE_USERS_ID,)]
        ), "positive control: the custom role holds both"

        result = run_alembic(
            f"upgrade {ORG_PERMISSIONS_MANAGEMENT_ONLY_REVISION}", database_url
        )
        assert result.returncode == 0, result.stderr
        assert custom_grants() == [(self.ORG_MANAGE_USERS_ID,)]

        result = run_alembic(
            f"downgrade {VERIFIED_BY_DISPLAY_NAME_REVISION}", database_url
        )
        assert result.returncode == 0, result.stderr
        assert custom_grants() == [(self.ORG_MANAGE_USERS_ID,)]
        # The system roles' grant of the same permission did come back.
        assert query_rows(
            TEST_DB,
            "SELECT role_id FROM role_permissions WHERE permission_id = "
            f"'{self.CASES_READ_ID}' ORDER BY role_id",
        ) == sorted(
            [(self.ADMIN_ROLE_ID,), (self.MEMBER_ROLE_ID,), (self.VIEWER_ROLE_ID,)]
        )

    def test_the_postgresql_script(self):
        """Offline (``--sql``) against PostgreSQL: the grants' ``DELETE``
        precedes the permissions' ``DELETE``, no ``row_security`` is set
        (neither table is enrolled in RLS), and the downgrade inserts the eight
        rows and their fifteen grants."""
        pg = "postgresql://offline@localhost/offline"
        up = run_alembic(
            f"upgrade {VERIFIED_BY_DISPLAY_NAME_REVISION}:"
            f"{ORG_PERMISSIONS_MANAGEMENT_ONLY_REVISION} --sql",
            pg,
        )
        assert up.returncode == 0, up.stderr
        grants = up.stdout.index("DELETE FROM role_permissions")
        perms = up.stdout.index(
            "DELETE FROM permissions WHERE resource IN ('cases', 'evidence')"
        )
        assert grants < perms
        assert "WHERE resource IN ('cases', 'evidence'))" in up.stdout[grants:perms]
        assert "row_security" not in up.stdout
        assert (
            "UPDATE roles SET description='Manages the organization''s roster and "
            "settings.' WHERE roles.role_id = '50551907-a02c-5bf7-9aa4-4a98f3c4eb64';"
        ) in up.stdout
        assert up.stdout.count("UPDATE roles SET description=") == 3

        down = run_alembic(
            f"downgrade {ORG_PERMISSIONS_MANAGEMENT_ONLY_REVISION}:"
            f"{VERIFIED_BY_DISPLAY_NAME_REVISION} --sql",
            pg,
        )
        assert down.returncode == 0, down.stderr
        sql = down.stdout
        assert sql.count("INSERT INTO permissions ") == 8
        assert sql.count("INSERT INTO role_permissions ") == 15
        for permission_id in self.BASELINE_DATA_PERMISSIONS:
            assert permission_id in sql
        assert "DELETE" not in sql
        assert "row_security" not in sql
        assert sql.count("UPDATE roles SET description=") == 3
        for _, description in self.BASELINE_DESCRIPTIONS:
            assert f"description='{description}'" in sql


class TestRbacSeed:
    """The chain seeds the system RBAC roles, permissions and grants.

    The baseline (``001_enterprise_baseline``) seeds them; ``017`` prunes the
    ``sessions:*`` permissions and ``020`` the ``cases:*`` / ``evidence:*``
    ones, leaving the management axis: ``org:manage_users`` and
    ``org:manage_settings``, granted to ``admin`` alone. These assertions tie
    the database at head to the live authority model
    (``faultmaven/models/rbac.py``) and the runtime role-id constant
    (``rbac_seed.SYSTEM_ROLE_IDS``), so the chain cannot silently drift from
    either.
    """

    def test_system_roles_seeded_with_stable_ids(self, clean_database, database_url):
        """The three system roles exist with the IDs SYSTEM_ROLE_IDS promises."""
        prepare_database("head", database_url)

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
        prepare_database("head", database_url)

        rows = query_rows(TEST_DB, "SELECT resource, action FROM permissions")
        seeded = {f"{resource}:{action}" for resource, action in rows}

        assert seeded == {perm.value for perm in Permission}

    def test_role_permission_grants_match_model(self, clean_database, database_url):
        """role_permissions reproduces ROLE_PERMISSIONS exactly."""
        prepare_database("head", database_url)

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

        # A role that grants nothing has no ``role_permissions`` row, so it is
        # absent from ``actual`` rather than mapped to an empty set.
        expected = {
            role.value: {perm.value for perm in perms}
            for role, perms in ROLE_PERMISSIONS.items()
            if perms
        }
        assert dict(actual) == expected

    def test_seed_is_reversible_and_idempotent(self, clean_database, database_url):
        """Downgrade removes the seed with its tables; re-upgrade restores it.

        The seed lives in the baseline, so its reversal is ``downgrade base``
        (which steps back over every additive revision first). What it proves is
        the property that matters: a re-upgrade lands on exactly the seed and not
        a doubled one.
        """
        prepare_database("head", database_url)
        assert len(query_rows(TEST_DB, "SELECT role_id FROM roles")) == 3

        result = run_alembic("downgrade base", database_url)
        assert result.returncode == 0, f"downgrade failed: {result.stderr}"
        assert get_tables(TEST_DB) == [
            "alembic_version"
        ], "the chain's downgrade must drop every table it created"

        # Re-apply — counts return to exactly the seed, no duplication.
        run_alembic("upgrade head", database_url)
        assert len(query_rows(TEST_DB, "SELECT role_id FROM roles")) == 3
        # The baseline seeds 14 permissions and 26 grants; 017 retires the four
        # ``sessions:*`` permissions and their 9 grants (#1935), and 020 the
        # eight ``cases:*`` / ``evidence:*`` ones and their 15 grants (#1040),
        # leaving admin's two ``org:manage_*`` grants.
        assert len(query_rows(TEST_DB, "SELECT permission_id FROM permissions")) == 2
        assert len(query_rows(TEST_DB, "SELECT role_id FROM role_permissions")) == 2


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
        prepare_database("head", database_url)

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
        prepare_database("head", database_url)

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

        On SQLite the chain renders up to 017 and then 018 refuses (it reads
        draft files, which a script cannot; ``TestRunbookDraftsInTheDatabase
        Revision``), so the run fails AFTER printing the SQL before it — still
        offline, still touching no database.
        """
        result = run_helper_script("upgrade --sql", database_url)

        assert result.returncode != 0
        assert "step (b) reads draft files" in result.stderr, result.stderr[-2000:]
        assert "CREATE TABLE cases" in result.stdout
        assert "Running upgrade b5334d07ae6c -> f95a5b3a58eb" in result.stdout
        assert not os.path.exists(TEST_DB) or get_tables(TEST_DB) == []

    def test_upgrade_applies_the_migrations(self, clean_database, database_url):
        result = run_helper_script("upgrade", database_url)

        assert result.returncode == 0, result.stderr
        assert get_current_revision(database_url) == HEAD_REVISION

    @pytest.mark.parametrize("args", ["status --verbose", "heads -v"])
    def test_verbose_reaches_the_commands_that_accept_it(
        self, clean_database, database_url, args
    ):
        if args == "status --verbose":  # ``heads`` reads only the scripts
            prepare_database("head", database_url)

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
        prepare_database("head", database_url)

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
            # The driver beside the creator (ADR-020 D1): NULL = the creator
            # drives.
            "driver_id",
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
        prepare_database("head", database_url)

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
        prepare_database("head", database_url)

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
        prepare_database("head", database_url)

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
        prepare_database("head", database_url)

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
        prepare_database("head", database_url)

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
        prepare_database("head", database_url)

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
        prepare_database("head", database_url)

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
        prepare_database("head", database_url)

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
        prepare_database("head", database_url)

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
        prepare_database("head", database_url)

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
        prepare_database("head", database_url)

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
        prepare_database("head", database_url)

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
        prepare_database("head", database_url)

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
        prepare_database("head", database_url)

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
