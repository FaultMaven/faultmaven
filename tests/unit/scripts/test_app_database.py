"""The maintenance scripts open the app's one database.

FaultMaven keeps every table in the database named by ``DATABASE_URL``. The
scripts that open it directly resolve it through ``scripts/app_database.py``
and offer no way to point at a separate "auth" or "cases" database.
"""

import importlib.util
import subprocess
import sys
from pathlib import Path

import dotenv.main
import pytest

from faultmaven.config.persistent_database import (
    DEFAULT_DATABASE_URL as APP_DEFAULT_DATABASE_URL,
)

REPO_ROOT = Path(__file__).resolve().parents[3]
SCRIPTS = REPO_ROOT / "scripts"

DB_SCRIPTS = [
    "backfill_closed_at_timestamps.py",
    "check_duplicate_emails.py",
    "resolve_duplicate_emails.py",
]

RETIRED_ENV = {
    "AUTH_DB_URL": "postgresql+asyncpg://u:p@auth-host:5432/auth_db",
    "AUTH_DB_HOST": "auth-host",
    "AUTH_DB_NAME": "auth_db",
    "CASES_DB_URL": "postgresql+asyncpg://u:p@cases-host:5432/cases_db",
    "CASES_DB_HOST": "cases-host",
    "CASES_DB_NAME": "cases_db",
}

SQLITE_PREFIX = "sqlite+aiosqlite:///"


@pytest.fixture(scope="module")
def app_database():
    spec = importlib.util.spec_from_file_location(
        "app_database", SCRIPTS / "app_database.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def no_env_file(tmp_path):
    return tmp_path / "absent.env"


@pytest.fixture
def real_dotenv(app_database, monkeypatch):
    # tests/conftest.py stubs dotenv.dotenv_values to {} for the whole session so
    # no developer's .env leaks into a test; these tests read a .env they wrote.
    monkeypatch.setattr(app_database, "dotenv_values", dotenv.main.dotenv_values)


def test_database_url_from_the_environment_is_used_verbatim(app_database, no_env_file):
    url = "postgresql+asyncpg://app:secret@db:5432/faultmaven"

    resolved = app_database.resolve_database_url(
        {"DATABASE_URL": url}, env_file=no_env_file
    )

    # Verbatim, async driver included: the scripts use create_async_engine.
    assert resolved == url


def test_env_file_is_consulted_when_the_environment_has_no_database_url(
    app_database, real_dotenv, tmp_path
):
    env_file = tmp_path / ".env"
    env_file.write_text("DATABASE_URL=postgresql+asyncpg://from-dotenv/fm\n")

    assert (
        app_database.resolve_database_url({}, env_file=env_file)
        == "postgresql+asyncpg://from-dotenv/fm"
    )


def test_environment_wins_over_env_file(app_database, real_dotenv, tmp_path):
    env_file = tmp_path / ".env"
    env_file.write_text("DATABASE_URL=postgresql+asyncpg://from-dotenv/fm\n")

    resolved = app_database.resolve_database_url(
        {"DATABASE_URL": "postgresql+asyncpg://from-env/fm"}, env_file=env_file
    )

    assert resolved == "postgresql+asyncpg://from-env/fm"


def test_fallback_is_the_file_the_app_opens_by_default(app_database, no_env_file):
    resolved = app_database.resolve_database_url({}, env_file=no_env_file)

    # The app's shipped default is relative to its working directory; the
    # scripts anchor it at the project root, as alembic/env.py does.
    assert resolved.startswith(SQLITE_PREFIX)
    assert APP_DEFAULT_DATABASE_URL.startswith(SQLITE_PREFIX)
    app_file = (REPO_ROOT / APP_DEFAULT_DATABASE_URL[len(SQLITE_PREFIX) :]).resolve()
    assert Path(resolved[len(SQLITE_PREFIX) :]) == app_file


def test_retired_per_database_variables_are_ignored(app_database, no_env_file):
    with_database_url = {**RETIRED_ENV, "DATABASE_URL": "postgresql+asyncpg://one/fm"}

    assert (
        app_database.resolve_database_url(with_database_url, env_file=no_env_file)
        == "postgresql+asyncpg://one/fm"
    )
    assert (
        app_database.resolve_database_url(RETIRED_ENV, env_file=no_env_file)
        == app_database.DEFAULT_DATABASE_URL
    )


@pytest.mark.parametrize("script", DB_SCRIPTS)
def test_script_offers_no_database_selector(script):
    result = subprocess.run(
        [sys.executable, str(SCRIPTS / script), "--help"],
        capture_output=True,
        text=True,
        timeout=60,
        cwd=REPO_ROOT,
    )

    assert result.returncode == 0, result.stderr
    assert "--database" not in result.stdout
