"""``conversion_jobs.status`` admits ``'partial'``, exercised by what the
database ACCEPTS.

The schema walk in ``test_alembic_migrations.py`` runs on an EMPTY database, so
a CHECK constraint is only ever asserted to apply cleanly — never to admit or
reject anything. Counting columns, or confirming a constraint "exists", proves
nothing about the value the application actually needs to write, which is the
whole content of this one: a conversion that produced some drafts and some
errors is ``'partial'``, and a schema that refused it would fail the write at
the end of a job that had already done its work.

So every claim below is a round trip through a real row, over a database built
by the real migration runner (``python -m alembic``, the same invocation
``test_alembic_migrations.py`` uses) rather than by ``create_all`` — the
constraint under test has to be the one a deployment gets.

Both directions, on the same run: the value must be admitted, AND a value that
was never in the set must still be refused. Either alone is satisfied by a
schema with no constraint at all.
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
    and the subject here is the status CHECK. ``enterprise_id`` is among them
    because it is the isolation key and NOT NULL on every tenant table
    (ADR-017 D1); ``organization_id`` is nullable billing attribution and is
    deliberately left out, which is also the ordinary shape for a row written by
    an account nobody pays for.
    """
    conn = sqlite3.connect(db_path)
    try:
        conn.execute(
            "INSERT INTO conversion_jobs "
            "(id, enterprise_id, user_id, source_file_id, scope, status, "
            " source_type, failure_modes_detected, created_at) "
            "VALUES (?, 'ent', 'user', 'file', 'global', ?, 'document', 1, "
            " CURRENT_TIMESTAMP)",
            (job_id, status),
        )
        conn.commit()
        return True
    except sqlite3.IntegrityError:
        return False
    finally:
        conn.close()


@pytest.fixture
def db(tmp_path):
    """A throwaway database at head — the schema a deployment actually gets."""
    path = tmp_path / "conversion_status.db"
    _alembic("upgrade head", f"sqlite:///{path}")
    return str(path)


def test_a_partial_conversion_can_be_recorded(db):
    """The value the service writes when a job produced drafts AND errors.

    ``'completed'`` beside it is the control: without it this would also pass
    against a table that accepted everything for some unrelated reason, and
    against one where the insert never reached the CHECK at all.
    """
    assert _accepts(db, "j1", "partial") is True
    assert _accepts(db, "j2", "completed") is True


def test_a_status_outside_the_vocabulary_is_still_refused(db):
    """The other half, and the one that makes the first mean something.

    A schema that dropped the constraint rather than carrying it would satisfy
    every assertion about ``'partial'``; only a rejection distinguishes the two.
    """
    assert _accepts(db, "j3", "not-a-status") is False


def test_the_status_vocabulary_is_the_one_the_domain_declares(db):
    """Every value the service can write is a value the column will take.

    Derived from ``ConversionStatus`` rather than listed here, so a member added
    to the enum without the CHECK to match fails at this test instead of at the
    end of a user's conversion.
    """
    from faultmaven.modules.knowledge.domain.models.conversion import ConversionStatus

    declared = [status.value for status in ConversionStatus]
    assert declared, "the enum is empty — this test would assert nothing"
    refused = [
        value
        for index, value in enumerate(declared)
        if not _accepts(db, f"enum_{index}", value)
    ]
    assert refused == [], (
        "the conversion_jobs status CHECK refuses values the domain can "
        f"produce: {refused}"
    )
