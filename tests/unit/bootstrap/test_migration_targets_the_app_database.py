"""The startup migration targets the database the app opens (#1636).

The app opens ``get_settings().database.database_url`` -- built once and
cached -- while ``alembic upgrade head`` runs in a subprocess whose
``alembic/env.py`` reads ``DATABASE_URL`` from ITS environment and otherwise
falls back to ``<project_root>/data/faultmaven.db``, with ``cwd=project_root``.
Two sources. A test that booted the app under ``patch.dict(os.environ, ...,
clear=True)`` separated them: the app opened its cached per-worker file, the
migration built the shared default, and the bootstrap died on ``no such table:
enterprises``. ``run_alembic_migrations`` now hands the subprocess the app's own
URL, so the environment cannot separate them.

These pin both halves: the URL passed is the settings URL whatever the
environment holds, and a relative SQLite path is resolved where the APP would
resolve it (this process's cwd), not in the subprocess's ``project_root``.
"""

from __future__ import annotations

import os
import sqlite3
import subprocess
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from faultmaven.bootstrap import data_init

pytestmark = pytest.mark.unit


def _settings(url: str) -> SimpleNamespace:
    return SimpleNamespace(database=SimpleNamespace(database_url=url))


@pytest.mark.parametrize(
    "url, expected",
    [
        (
            "sqlite+aiosqlite:///./data/faultmaven.db",
            "sqlite+aiosqlite:///{cwd}/data/faultmaven.db",
        ),
        ("sqlite+aiosqlite:///rel.db", "sqlite+aiosqlite:///{cwd}/rel.db"),
        ("sqlite+aiosqlite:////abs/x.db", "sqlite+aiosqlite:////abs/x.db"),
        ("sqlite+aiosqlite:///:memory:", "sqlite+aiosqlite:///:memory:"),
        # Non-SQLite passes through byte-for-byte: re-rendering could re-quote
        # a password.
        (
            "postgresql+asyncpg://u:p%40ss@db:5432/fm",
            "postgresql+asyncpg://u:p%40ss@db:5432/fm",
        ),
    ],
)
def test_the_migration_url_is_the_app_url_resolved_where_the_app_resolves_it(
    url, expected
):
    with patch("faultmaven.config.settings.get_settings", return_value=_settings(url)):
        got = data_init.migration_database_url()

    assert got == expected.format(cwd=os.getcwd())


@pytest.mark.parametrize(
    "ambient",
    [None, "sqlite+aiosqlite:////somewhere/else.db"],
    ids=["environment-cleared", "environment-disagrees"],
)
def test_the_subprocess_is_handed_the_settings_url_not_the_environment(
    ambient, monkeypatch, tmp_path
):
    """What the subprocess receives, under the two ways the env can diverge."""
    app_url = f"sqlite+aiosqlite:///{tmp_path}/app.db"
    if ambient is None:
        monkeypatch.delenv("DATABASE_URL", raising=False)
    else:
        monkeypatch.setenv("DATABASE_URL", ambient)

    seen = {}

    def _run(*args, **kwargs):
        seen["env"] = kwargs.get("env")
        return subprocess.CompletedProcess(args, 0, stdout="", stderr="")

    with (
        patch(
            "faultmaven.config.settings.get_settings",
            return_value=_settings(app_url),
        ),
        patch("subprocess.run", side_effect=_run),
    ):
        assert data_init.run_alembic_migrations() is True

    assert seen["env"] is not None, "the subprocess inherited the ambient env"
    assert seen["env"]["DATABASE_URL"] == app_url


@pytest.mark.integration
def test_a_real_migration_under_a_cleared_environment_builds_the_app_database(
    monkeypatch, tmp_path
):
    """End to end: the real ``alembic upgrade head`` subprocess, env cleared of
    ``DATABASE_URL``, lands its tables in the file the app would open."""
    db = tmp_path / "app.db"
    monkeypatch.delenv("DATABASE_URL", raising=False)

    with patch(
        "faultmaven.config.settings.get_settings",
        return_value=_settings(f"sqlite+aiosqlite:///{db}"),
    ):
        assert data_init.run_alembic_migrations() is True

    assert db.exists(), "the migration built some other file"
    names = {
        row[0]
        for row in sqlite3.connect(db).execute(
            "select name from sqlite_master where type='table'"
        )
    }
    assert "enterprises" in names
