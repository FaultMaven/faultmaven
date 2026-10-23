"""The startup migration subprocess is skipped when the schema is at head (#2212).

At head ``alembic upgrade head`` changes nothing and costs a boot ~1.5 s. The
databases are copies of one the real subprocess built (``prepare_database``),
edited where a branch needs another ``alembic_version``; ``run_alembic_migrations``
is a recorder, so "called" means the subprocess would have run.
"""

from __future__ import annotations

import logging
import sqlite3
import sys
from contextlib import closing
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from faultmaven.bootstrap import data_init
from tests.integration.test_alembic_migrations import (
    BASELINE_REVISION,
    prepare_database,
)

pytestmark = pytest.mark.unit

#: A revision id no script carries.
UNKNOWN_REVISION = "0123456789ab"


async def _boot(db: Path | str) -> MagicMock:
    """Run ``initialize_data_layer`` on ``db`` (a file, or a URL); return the recorder."""
    url = f"sqlite+aiosqlite:///{db}" if isinstance(db, Path) else db
    settings = SimpleNamespace(
        run_startup_migrations=True, database=SimpleNamespace(database_url=url)
    )
    with (
        patch.object(data_init, "ensure_data_directories"),
        patch.object(data_init, "run_alembic_migrations") as run_migrations,
        patch("faultmaven.config.settings.get_settings", return_value=settings),
    ):
        await data_init.initialize_data_layer(container=MagicMock())
    return run_migrations


def _versions(db: Path) -> list[str]:
    with closing(sqlite3.connect(db)) as conn:
        return [r[0] for r in conn.execute("SELECT version_num FROM alembic_version")]


def _set_versions(db: Path, *versions: str) -> None:
    with closing(sqlite3.connect(db)) as conn, conn:
        conn.execute("DELETE FROM alembic_version")
        conn.executemany(
            "INSERT INTO alembic_version (version_num) VALUES (?)",
            [(v,) for v in versions],
        )


def _records(caplog, level: int) -> list[str]:
    logged = [r for r in caplog.records if r.name == data_init.__name__]
    return [r.getMessage() for r in logged if r.levelno == level]


@pytest.fixture
def head_db(tmp_path) -> Path:
    """A copy of the database the real ``alembic upgrade head`` built."""
    db = tmp_path / "app.db"
    prepare_database("head", f"sqlite:///{db}")
    return db


async def test_at_head_the_subprocess_is_skipped(head_db, caplog):
    caplog.set_level(logging.INFO, logger=data_init.__name__)
    [head] = _versions(head_db)

    run_migrations = await _boot(head_db)

    run_migrations.assert_not_called()
    [skipped] = [m for m in _records(caplog, logging.INFO) if "already at head" in m]
    assert head in skipped
    assert _records(caplog, logging.WARNING) == []


async def test_from_another_cwd_the_heads_resolve_and_the_skip_holds(
    head_db, tmp_path, monkeypatch
):
    """``script_location = alembic`` is cwd-relative, so the read pins it; and it
    leaves ``sys.path`` alone (alembic would prepend ``prepend_sys_path``)."""
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    monkeypatch.chdir(elsewhere)
    monkeypatch.delenv("PROJECT_ROOT", raising=False)
    monkeypatch.delenv("ALEMBIC_CONFIG", raising=False)
    assert data_init.get_project_root() != elsewhere

    sys_path = list(sys.path)
    assert data_init.alembic_script_heads() == frozenset(_versions(head_db))
    assert sys.path == sys_path
    (await _boot(head_db)).assert_not_called()


@pytest.mark.parametrize("state", ["no-table", "empty-table"])
async def test_a_fresh_database_runs_the_subprocess_without_reading_the_scripts(
    state, head_db, tmp_path, caplog
):
    """A fresh install never needs the scripts' heads, so never imports alembic."""
    if state == "no-table":
        db = tmp_path / "fresh.db"  # does not exist yet
    else:
        db = head_db
        _set_versions(db)

    with patch.object(data_init, "alembic_script_heads") as script_heads:
        run_migrations = await _boot(db)

    run_migrations.assert_called_once()
    script_heads.assert_not_called()
    assert _records(caplog, logging.WARNING) == []


@pytest.mark.parametrize("state", ["older", "unknown", "head-and-unknown"])
async def test_a_database_off_the_heads_runs_the_subprocess(state, head_db, caplog):
    [head] = _versions(head_db)
    versions = {
        "older": (BASELINE_REVISION,),
        "unknown": (UNKNOWN_REVISION,),
        "head-and-unknown": (head, UNKNOWN_REVISION),
    }[state]
    _set_versions(head_db, *versions)

    (await _boot(head_db)).assert_called_once()
    assert _records(caplog, logging.WARNING) == []


@pytest.mark.parametrize(
    "cause, exception",
    [("database-unreadable", "DatabaseError"), ("no-alembic-ini", "FileNotFoundError")],
)
async def test_a_check_that_raises_warns_and_runs_the_subprocess(
    cause, exception, head_db, tmp_path, monkeypatch, caplog
):
    if cause == "database-unreadable":
        head_db.write_bytes(b"this is not a database" * 64)
    else:
        monkeypatch.setenv("PROJECT_ROOT", str(tmp_path / "no-project-here"))
        monkeypatch.delenv("ALEMBIC_CONFIG", raising=False)

    (await _boot(head_db)).assert_called_once()
    [warning] = _records(caplog, logging.WARNING)
    assert exception in warning


@pytest.mark.parametrize("url", ["", "sqlite+aiosqlite:///:memory:"])
async def test_a_non_persistent_url_goes_straight_to_the_subprocess(url, caplog):
    """``run_alembic_migrations`` owns that skip; the check must not get there by
    failing on ``make_url("")``."""
    with patch.object(data_init, "_database_revisions") as database_revisions:
        run_migrations = await _boot(url)

    run_migrations.assert_called_once()
    database_revisions.assert_not_called()
    assert _records(caplog, logging.WARNING) == []
