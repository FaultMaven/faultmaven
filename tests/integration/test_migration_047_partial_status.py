"""Migration 047's CHECK widening, exercised by what the database ACCEPTS.

The migration walk in ``test_alembic_migrations.py`` runs on an EMPTY database,
so a CHECK constraint change there is only ever asserted to apply cleanly —
never to admit or reject anything. Counting columns, or confirming a constraint
"exists", proves nothing about the value the application actually needs to
write, which is the whole content of this migration.

So every claim below is a round trip through a real row, driven by the real
migration runner over the real chain (``python -m alembic``, the same invocation
``test_alembic_migrations.py`` uses):

- At 046 — the state before this change — ``'partial'`` is REJECTED, with
  ``'completed'`` as the accepted control. Without this the upgrade assertion
  cannot distinguish "047 fixed it" from "it was never broken".
- At 047, ``'partial'`` is ACCEPTED and a value that was never valid is still
  REJECTED, so the widening is a widening and not a dropped constraint.
- Back at 046, ``'partial'`` is REJECTED again — the downgrade is real — and a
  ``'partial'`` row written while it was permitted has been relocated rather
  than left to fail the SQLite table rebuild.
"""

import os
import shlex
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

pytestmark = pytest.mark.integration

PROJECT_ROOT = Path(__file__).resolve().parents[2]

BEFORE = "d8e9f0a1b2c3"  # 046 — conversion-draft runbook_id uniqueness
AFTER = "f0a1b2c3d4e5"  # 047 — this migration


def _alembic(command: str, database_url: str) -> None:
    """Run one alembic command, failing loudly. Mirrors the sibling suite's
    runner: ``sys.executable -m alembic`` so PATH cannot hijack it, and
    ``PYTHONPATH`` prepended so ``env.py`` binds to THIS checkout rather than to
    an editable install pointing somewhere else."""
    env = os.environ.copy()
    env["DATABASE_URL"] = database_url
    existing = env.get("PYTHONPATH")
    env["PYTHONPATH"] = (
        f"{PROJECT_ROOT}{os.pathsep}{existing}" if existing else str(PROJECT_ROOT)
    )
    result = subprocess.run(
        [sys.executable, "-m", "alembic", *shlex.split(command)],
        cwd=PROJECT_ROOT,
        env=env,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, f"alembic {command} failed:\n{result.stderr}"


def _accepts(db_path: str, job_id: str, status: str) -> bool:
    """Insert one ``conversion_jobs`` row; True if the CHECK admitted it.

    Only the NOT NULL columns are supplied and the FK targets are not seeded —
    SQLite does not enforce foreign keys unless ``PRAGMA foreign_keys`` is on,
    and the subject here is the status CHECK.
    """
    conn = sqlite3.connect(db_path)
    try:
        conn.execute(
            "INSERT INTO conversion_jobs "
            "(id, organization_id, user_id, source_file_id, scope, status, "
            " source_type, failure_modes_detected, created_at) "
            "VALUES (?, 'org', 'user', 'file', 'global', ?, 'document', 1, "
            " CURRENT_TIMESTAMP)",
            (job_id, status),
        )
        conn.commit()
        return True
    except sqlite3.IntegrityError:
        return False
    finally:
        conn.close()


def _statuses(db_path: str) -> dict:
    conn = sqlite3.connect(db_path)
    try:
        return dict(conn.execute("SELECT id, status FROM conversion_jobs").fetchall())
    finally:
        conn.close()


@pytest.fixture
def db(tmp_path):
    """A throwaway database migrated to 046 — the state before this change."""
    path = tmp_path / "mig047.db"
    _alembic(f"upgrade {BEFORE}", f"sqlite:///{path}")
    return str(path)


def test_the_state_before_this_migration_really_does_reject_partial(db):
    assert _accepts(db, "control", "completed") is True
    assert _accepts(db, "target", "partial") is False


def test_upgrade_admits_partial_and_still_rejects_a_bogus_status(db):
    _alembic(f"upgrade {AFTER}", f"sqlite:///{db}")

    assert _accepts(db, "j1", "partial") is True
    assert _accepts(db, "j2", "completed") is True
    # The widening is a widening. A value that was never in the set must still
    # be refused, or the migration dropped the constraint rather than extending
    # it — which every assertion about 'partial' would also satisfy.
    assert _accepts(db, "j3", "not-a-status") is False


def test_downgrade_rejects_partial_again_and_relocates_existing_rows(db):
    """The reverse leg, and the data transform that makes it survivable.

    A deployment that used the feature has ``'partial'`` rows. On SQLite the
    downgrade REBUILDS the table, copying every row into one whose CHECK
    rejects that value — so without the UPDATE the downgrade fails on exactly
    the deployments that exercised it.
    """
    url = f"sqlite:///{db}"
    _alembic(f"upgrade {AFTER}", url)
    assert _accepts(db, "used-the-feature", "partial") is True
    assert _accepts(db, "ordinary", "completed") is True

    _alembic(f"downgrade {BEFORE}", url)

    assert _statuses(db) == {
        "used-the-feature": "completed",
        "ordinary": "completed",
    }
    assert _accepts(db, "j4", "partial") is False
    assert _accepts(db, "j5", "completed") is True
