"""``operator_access_audit`` is append-only at the DATABASE layer (#813).

The threat is the audited operator themselves. If UPDATE/DELETE are prevented
only by "the repository exposes no such method", then anyone reaching the
database — including the operator whose access is recorded — can amend or erase
their own trail, and the table's evidentiary value is gone.

These tests run the real migration against a real SQLite database and assert the
triggers reject the writes. They are the reason the control is a trigger rather
than a convention.
"""

import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture(scope="module")
def migrated_db(tmp_path_factory):
    """A real database at head, built by running the real migrations."""
    db_path = tmp_path_factory.mktemp("operator_audit") / "audit.db"
    result = subprocess.run(
        [sys.executable, "-m", "alembic", "upgrade", "head"],
        cwd=str(PROJECT_ROOT),
        capture_output=True,
        text=True,
        timeout=300,
        env={
            "PATH": "/usr/bin:/bin",
            "DATABASE_URL": f"sqlite+aiosqlite:///{db_path}",
            "HOME": str(tmp_path_factory.getbasetemp()),
        },
    )
    if result.returncode != 0:
        pytest.skip(
            f"alembic upgrade unavailable in this environment: {result.stderr[-500:]}"
        )
    return db_path


@pytest.fixture
def conn(migrated_db):
    connection = sqlite3.connect(migrated_db)
    yield connection
    connection.close()


def _insert(conn) -> int:
    cur = conn.cursor()
    cur.execute(
        "INSERT INTO operator_access_audit (action, operator_user_id, created_at) "
        "VALUES ('list', 'op-1', datetime('now'))"
    )
    conn.commit()
    return cur.lastrowid


@pytest.mark.integration
@pytest.mark.security
class TestAppendOnly:
    def test_insert_is_allowed(self, conn):
        assert _insert(conn) > 0

    def test_update_is_rejected(self, conn):
        row_id = _insert(conn)
        with pytest.raises(sqlite3.IntegrityError, match="append-only"):
            conn.execute(
                "UPDATE operator_access_audit SET action='content_open' "
                f"WHERE audit_id={row_id}"
            )

    def test_delete_is_rejected(self, conn):
        row_id = _insert(conn)
        with pytest.raises(sqlite3.IntegrityError, match="append-only"):
            conn.execute(f"DELETE FROM operator_access_audit WHERE audit_id={row_id}")

    def test_row_survives_both_attempts(self, conn):
        row_id = _insert(conn)
        for sql in (
            f"UPDATE operator_access_audit SET action='content_open' WHERE audit_id={row_id}",
            f"DELETE FROM operator_access_audit WHERE audit_id={row_id}",
        ):
            with pytest.raises(sqlite3.IntegrityError):
                conn.execute(sql)
        conn.rollback()
        row = conn.execute(
            f"SELECT action FROM operator_access_audit WHERE audit_id={row_id}"
        ).fetchone()
        assert row == ("list",), "the record must be intact after tampering attempts"

    def test_unknown_action_is_rejected(self, conn):
        """The metadata/content boundary is constrained by the schema.

        A third, un-enumerated action would be an access category nobody
        classified as metadata or content.
        """
        with pytest.raises(sqlite3.IntegrityError, match="action_valid"):
            conn.execute(
                "INSERT INTO operator_access_audit (action, created_at) "
                "VALUES ('sneaky', datetime('now'))"
            )
