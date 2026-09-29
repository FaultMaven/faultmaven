"""``alembic upgrade head`` refuses a non-persistent ``DATABASE_URL`` (#1704).

The API, the jobs runner and every ``fm-*`` command refuse an empty or
in-memory ``DATABASE_URL`` (#1647, #1659). The migration entrypoint did not:
``alembic/env.py`` read the variable with ``os.getenv`` and never applied the
shared predicate, so ``sqlite:///file:fmx?mode=memory&uri=true`` "ran upgrade"
into a database that vanished with the process and exited 0 — a misconfigured
migration Job reported success. A set-but-empty value fell back to
``data/faultmaven.db``, where the app's predicate refuses it.

``env.py`` now reads the URL through ``configured_database_url()`` — the
``DatabaseSettings`` reader the app's settings are built from, so a lowercase
``database_url`` counts as it does for the app — and, in online mode, refuses
through the same exit helper as the ``fm-*`` commands, exiting 1. Offline
(``--sql``) opens no database, so it is not refused. Only the URL is read: a
database setting alembic never uses does not refuse a migration.

Each case runs the real command, ``python -m alembic -c <copy>/alembic.ini
upgrade head``, on a COPY of ``alembic.ini`` and ``alembic/`` in ``tmp_path``,
with ``PYTHONPATH`` pinned to this checkout so ``env.py`` imports the tree under
test. ``env.py`` resolves its project root from its own path, so on the copy a
regressed fallback would migrate ``tmp_path/data/faultmaven.db``, never the
checkout's own database, and there is no ``.env`` there to load. The copy's
``data/`` is created up front: SQLite creates a database file but not its
directory, so without it a regressed fallback would fail before creating
anything and the "no fallback database" assertion could never fail. The
positive control migrates a temporary file with the same child, so a refusal is
the gate and not a child that cannot migrate anything.

``DATABASE_URL`` UNSET is deliberately not run: its fallback is the project
root's ``data/faultmaven.db``, unchanged by #1704.
"""

from __future__ import annotations

import os
import shutil
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

from faultmaven.config.persistent_database import DEFAULT_DATABASE_URL
from faultmaven.config.settings import set_env_var

pytestmark = pytest.mark.integration

PROJECT_ROOT = Path(__file__).resolve().parents[2]

REFUSAL = "configures no persistent database"


@pytest.fixture
def alembic_copy(tmp_path) -> Path:
    """``alembic.ini`` and ``alembic/`` copied into ``tmp_path``: the root the
    child's ``env.py`` resolves its fallback database against, with its
    ``data/`` directory already there."""
    shutil.copy2(PROJECT_ROOT / "alembic.ini", tmp_path / "alembic.ini")
    shutil.copytree(
        PROJECT_ROOT / "alembic",
        tmp_path / "alembic",
        ignore=shutil.ignore_patterns("__pycache__"),
    )
    (tmp_path / "data").mkdir()
    return tmp_path


def _upgrade_head(root: Path, database_env: dict[str, str], *extra: str):
    # Each variable is set as its ONLY spelling (the xdist worker sets a
    # DATABASE_URL, and the settings bind any case), so ``database_env`` is the
    # whole of it. Every case names the URL in some spelling.
    assert any(name.upper() == "DATABASE_URL" for name in database_env)
    env = dict(os.environ)
    for name, value in database_env.items():
        set_env_var(env, name, value)
    existing = env.get("PYTHONPATH")
    env["PYTHONPATH"] = (
        f"{PROJECT_ROOT}{os.pathsep}{existing}" if existing else str(PROJECT_ROOT)
    )
    env["PYTHON_DOTENV_DISABLED"] = "1"
    return subprocess.run(
        [sys.executable, "-m", "alembic", "-c", str(root / "alembic.ini")]
        + ["upgrade", "head", *extra],
        cwd=root,
        env=env,
        capture_output=True,
        text=True,
        timeout=300,
    )


@pytest.mark.parametrize(
    "database_env",
    [
        {"DATABASE_URL": "sqlite:///file:fmx?mode=memory&uri=true"},
        {"DATABASE_URL": "sqlite+aiosqlite:///:memory:"},
        {"DATABASE_URL": "sqlite:///:memory:"},
        {"DATABASE_URL": ":memory:"},
        {"DATABASE_URL": "sqlite+aiosqlite:///file:x?uri=true&vfs=memdb"},
        {"DATABASE_URL": ""},
        {"database_url": "sqlite:///:memory:"},
    ],
    ids=[
        "file-uri-mode-memory",
        "aiosqlite-memory",
        "sqlite-memory",
        "bare-memory",
        "file-uri-vfs-memdb",
        "empty",
        "lowercase-only-sqlite-memory",
    ],
)
def test_upgrade_head_refuses_a_non_persistent_database_url(alembic_copy, database_env):
    result = _upgrade_head(alembic_copy, database_env)
    detail = f"stdout:\n{result.stdout[-2000:]}\nstderr:\n{result.stderr[-3000:]}"

    assert result.returncode == 1, detail
    assert "❌ Refusing to run: DATABASE_URL=" in result.stderr, detail
    assert REFUSAL in result.stderr, detail
    assert DEFAULT_DATABASE_URL in result.stderr, detail
    assert "Running upgrade" not in result.stdout + result.stderr, detail
    assert "Traceback" not in result.stderr, detail
    assert not (
        alembic_copy / "data" / "faultmaven.db"
    ).exists(), "the refused run fell back to the default file"


def test_offline_sql_opens_no_database_and_is_not_refused(alembic_copy):
    """``upgrade head --sql`` only takes the dialect from the URL: an in-memory
    one is fine there, as on ``main`` (#1778 review, R3)."""
    result = _upgrade_head(alembic_copy, {"DATABASE_URL": "sqlite://"}, "--sql")

    assert result.returncode == 0, result.stderr[-3000:]
    assert REFUSAL not in result.stderr
    assert "CREATE TABLE" in result.stdout


@pytest.mark.parametrize("mode", ["online", "offline-sql"])
@pytest.mark.parametrize(
    "name, value",
    [
        ("KB_REPAIR_MAX_ROWS", "0"),
        ("KB_REPAIR_MAX_ROWS", "not-a-number-marker-7731"),
        ("REDIS_PORT", "tcp://10.0.0.1:6379"),
    ],
    ids=["kb-repair-out-of-range", "kb-repair-unparseable", "redis-port-k8s-link"],
)
def test_a_database_setting_alembic_never_uses_does_not_refuse(
    alembic_copy, name, value, mode
):
    """alembic reads only the URL. Validating every database field made the
    startup migration refuse on ``REDIS_PORT`` whenever the environment the
    subprocess inherited no longer matched the app's cached settings, and made
    ``--sql``, which opens no database, refuse on a field unrelated to it
    (#1778 review). ``tcp://…`` is the value Kubernetes service links inject."""
    db = alembic_copy / "valid.db"
    extra = ("--sql",) if mode == "offline-sql" else ()

    result = _upgrade_head(
        alembic_copy, {"DATABASE_URL": f"sqlite+aiosqlite:///{db}", name: value}, *extra
    )
    detail = f"stdout:\n{result.stdout[-2000:]}\nstderr:\n{result.stderr[-3000:]}"

    assert result.returncode == 0, detail
    assert "Refusing to run" not in result.stderr, detail
    if mode == "offline-sql":
        assert "CREATE TABLE" in result.stdout, detail
    else:
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


def test_positive_control_a_file_database_is_migrated(alembic_copy):
    db = alembic_copy / "migrated.db"

    result = _upgrade_head(alembic_copy, {"DATABASE_URL": f"sqlite+aiosqlite:///{db}"})

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
