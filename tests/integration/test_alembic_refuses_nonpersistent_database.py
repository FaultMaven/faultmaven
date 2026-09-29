"""``alembic upgrade head`` refuses a non-persistent ``DATABASE_URL`` (#1704).

The API, the jobs runner and every ``fm-*`` command refuse an empty or
in-memory ``DATABASE_URL`` (#1647, #1659). The migration entrypoint did not:
``alembic/env.py`` read the variable with ``os.getenv`` and never applied the
shared predicate, so ``sqlite:///file:fmx?mode=memory&uri=true`` "ran upgrade"
into a database that vanished with the process and exited 0 — a misconfigured
migration Job reported success. A set-but-empty value fell back to
``data/faultmaven.db``, where the app's predicate refuses it.

``env.py`` now refuses through ``require_persistent_database_url``, the rule and
message every other entrypoint uses, printed the way
``faultmaven/cli/_database_gate.py`` prints it, and exits 1.

Each case runs the real command, ``python -m alembic upgrade head``, in a child
from the repository root, with ``PYTHONPATH`` pinned to this checkout so
``env.py`` imports the tree under test. The positive control migrates a
temporary file with the same child, so a refusal is the gate and not a child
that cannot migrate anything.

``DATABASE_URL`` UNSET is deliberately not run: it migrates
``<repository root>/data/faultmaven.db``, the repository's own development
database, and that default is unchanged by #1704.
"""

from __future__ import annotations

import os
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

from faultmaven.config.persistent_database import DEFAULT_DATABASE_URL

pytestmark = pytest.mark.integration

PROJECT_ROOT = Path(__file__).resolve().parents[2]

REFUSAL = "configures no persistent database"


def _upgrade_head(database_url: str) -> subprocess.CompletedProcess:
    env = os.environ.copy()
    env["DATABASE_URL"] = database_url
    existing = env.get("PYTHONPATH")
    env["PYTHONPATH"] = (
        f"{PROJECT_ROOT}{os.pathsep}{existing}" if existing else str(PROJECT_ROOT)
    )
    # env.py loads the repository's .env; DATABASE_URL is always set here, which
    # a .env cannot override, but nothing else in it should reach the child.
    env["PYTHON_DOTENV_DISABLED"] = "1"
    return subprocess.run(
        [sys.executable, "-m", "alembic", "upgrade", "head"],
        cwd=PROJECT_ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=300,
    )


@pytest.mark.parametrize(
    "database_url",
    [
        "sqlite:///file:fmx?mode=memory&uri=true",
        "sqlite+aiosqlite:///:memory:",
        "sqlite:///:memory:",
        ":memory:",
        "sqlite+aiosqlite:///file:x?uri=true&vfs=memdb",
        "",
    ],
    ids=[
        "file-uri-mode-memory",
        "aiosqlite-memory",
        "sqlite-memory",
        "bare-memory",
        "file-uri-vfs-memdb",
        "empty",
    ],
)
def test_upgrade_head_refuses_a_non_persistent_database_url(database_url):
    result = _upgrade_head(database_url)
    detail = f"stdout:\n{result.stdout[-2000:]}\nstderr:\n{result.stderr[-3000:]}"

    assert result.returncode == 1, detail
    assert "❌ Refusing to run: DATABASE_URL=" in result.stderr, detail
    assert REFUSAL in result.stderr, detail
    assert DEFAULT_DATABASE_URL in result.stderr, detail
    assert "Running upgrade" not in result.stdout + result.stderr, detail
    assert "Traceback" not in result.stderr, detail


def test_positive_control_a_file_database_is_migrated(tmp_path):
    db = tmp_path / "migrated.db"

    result = _upgrade_head(f"sqlite+aiosqlite:///{db}")

    assert result.returncode == 0, result.stderr[-3000:]
    assert REFUSAL not in result.stderr
    connection = sqlite3.connect(db)
    try:
        tables = {
            row[0]
            for row in connection.execute(
                "select name from sqlite_master where type='table'"
            )
        }
    finally:
        connection.close()
    assert "alembic_version" in tables, sorted(tables)
