"""The persistent-database boot gate refuses exactly what the predicate rejects (fm#1647).

The gate is ``require_persistent_database``; the rule is
``persistent_database_configured``, which the store factories key off. The two
must agree row for row — the store-selection contract pins the predicate, and
this pins that the gate refuses on every False row and on no True row. Where the
gate is CALLED from is pinned by the boot tests
(``tests/integration/test_boot_refuses_nonpersistent_database.py`` and the jobs
runner tests), because a direct call proves nothing about placement.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from pydantic_settings import BaseSettings

from faultmaven.config.persistent_database import (
    DEFAULT_DATABASE_URL,
    NonPersistentDatabaseError,
    require_persistent_database,
    require_persistent_database_url,
)
from faultmaven.config.settings import (
    DatabaseSettings,
    configured_database_url,
    persistent_database_configured,
)
from tests.utils import delenv_every_spelling


def _settings(url):
    return SimpleNamespace(database=SimpleNamespace(database_url=url))


NON_PERSISTENT = [
    "",
    "   ",
    None,
    ":memory:",
    "sqlite+aiosqlite:///:memory:",
    "sqlite:///:memory:",
    "sqlite://",
    "sqlite+aiosqlite:///",
    "sqlite:///file:db?mode=memory&cache=shared&uri=true",
    # #1659: a substring rule called these persistent.
    "sqlite+aiosqlite:///?timeout=30",
    "sqlite+aiosqlite://?check_same_thread=false",
    "sqlite+aiosqlite:///file:x?uri=true&mode=memor%79",
    "file::memory:?cache=shared",
    "sqlite:///file:d?uri=true&cache=shared%26mode%3Dmemory",
    "sqlite:///file:x?uri=true&vfs=memdb",
    "sqlite:///file:?uri=true",
    # Values that do not parse, some carrying a password: refused, and the
    # message must not print them (test below).
    "postgresql+asyncpg://fm:Pa55wordValue@db:5432x/faultmaven",
    "host=db user=fm password=Pa55wordValue dbname=faultmaven",
]

PERSISTENT = [
    DEFAULT_DATABASE_URL,
    "sqlite+aiosqlite:////var/lib/faultmaven/faultmaven.db",
    "postgresql+asyncpg://fm@db:5432/faultmaven",
]


@pytest.mark.unit
@pytest.mark.parametrize("url", NON_PERSISTENT)
def test_refuses_every_non_persistent_url(url):
    assert persistent_database_configured(url) is False  # the row is what it says
    with pytest.raises(NonPersistentDatabaseError) as exc:
        require_persistent_database(_settings(url))
    message = str(exc.value)
    assert DEFAULT_DATABASE_URL in message
    assert "needs a database" in message
    assert "standalone" in message


@pytest.mark.unit
@pytest.mark.parametrize("url", PERSISTENT)
def test_accepts_every_persistent_url(url):
    assert persistent_database_configured(url) is True
    require_persistent_database(_settings(url))  # does not raise


@pytest.mark.unit
@pytest.mark.parametrize("url", NON_PERSISTENT)
def test_the_bare_url_form_refuses_every_non_persistent_url_with_the_same_message(
    url,
):
    """``require_persistent_database_url`` is what ``alembic/env.py`` calls, with
    no settings to hand (#1704). It must refuse the same rows with the same
    message as the settings form, so there is one rule and one message."""
    with pytest.raises(NonPersistentDatabaseError) as bare:
        require_persistent_database_url(url)
    with pytest.raises(NonPersistentDatabaseError) as from_settings:
        require_persistent_database(_settings(url))
    assert str(bare.value) == str(from_settings.value)
    assert DEFAULT_DATABASE_URL in str(bare.value)
    assert "needs a database" in str(bare.value)


@pytest.mark.unit
@pytest.mark.parametrize("url", PERSISTENT)
def test_the_bare_url_form_accepts_every_persistent_url(url):
    require_persistent_database_url(url)  # does not raise


@pytest.fixture
def no_database_url(monkeypatch):
    """Remove every spelling of DATABASE_URL: the settings bind it in any case,
    and the xdist worker sets one."""
    delenv_every_spelling(monkeypatch, "DATABASE_URL")


@pytest.mark.unit
def test_configured_database_url_is_none_when_nothing_sets_it(no_database_url):
    """Unset means the field default applies: the persistent SQLite file."""
    assert configured_database_url() is None


@pytest.mark.unit
@pytest.mark.parametrize(
    "name, value",
    [
        ("DATABASE_URL", "sqlite+aiosqlite:////srv/fm.db"),
        ("database_url", "sqlite+aiosqlite:////srv/fm.db"),
        ("Database_Url", "sqlite+aiosqlite:///:memory:"),
        ("DATABASE_URL", ""),
    ],
    ids=["uppercase", "lowercase", "mixed-case", "set-but-empty"],
)
def test_configured_database_url_reads_every_spelling_the_settings_bind(
    no_database_url, monkeypatch, name, value
):
    """The reader ``alembic/env.py`` judges the URL through, without the full
    settings. An exact-name read missed the lowercase spelling the app's gate
    refuses (#1704)."""
    monkeypatch.setenv(name, value)
    assert configured_database_url() == value


@pytest.mark.unit
def test_configured_database_url_takes_the_last_spelling_as_the_settings_do(
    no_database_url, monkeypatch
):
    monkeypatch.setenv("DATABASE_URL", "sqlite:///a.db")
    monkeypatch.setenv("database_url", "")

    assert configured_database_url() == ""
    assert DatabaseSettings().database_url == ""  # the settings bind the same


@pytest.mark.unit
def test_configured_database_url_validates_no_other_field(no_database_url, monkeypatch):
    """It reads the URL and nothing else. Validating the whole
    ``DatabaseSettings`` made the startup migration refuse on ``REDIS_PORT``, a
    field alembic never uses, when the environment it inherited no longer
    matched the app's cached settings (#1778 review)."""
    delenv_every_spelling(monkeypatch, "REDIS_PORT")
    monkeypatch.setenv("REDIS_PORT", "not_a_number")

    assert configured_database_url() is None
    monkeypatch.setenv("database_url", "sqlite:///:memory:")
    assert configured_database_url() == "sqlite:///:memory:"


@pytest.mark.unit
def test_database_settings_read_the_environment_only():
    """``configured_database_url`` reads ``DatabaseSettings``' environment source
    alone, which is the whole of what the class binds only while it declares no
    ``env_file`` or ``secrets_dir`` and keeps the default source order. A class
    that gains another source must take this reader with it."""
    config = DatabaseSettings.model_config
    assert config.get("env_file") is None, config.get("env_file")
    assert config.get("secrets_dir") is None, config.get("secrets_dir")
    assert (
        DatabaseSettings.settings_customise_sources.__func__
        is BaseSettings.settings_customise_sources.__func__
    )


@pytest.mark.unit
def test_settings_without_a_database_section_are_refused():
    """A settings object with no ``database`` is not a database — refuse, never pass."""
    with pytest.raises(NonPersistentDatabaseError):
        require_persistent_database(SimpleNamespace())


@pytest.mark.unit
def test_is_a_runtime_error():
    """The lifespan and the container treat RuntimeError as a deliberate refusal."""
    assert issubclass(NonPersistentDatabaseError, RuntimeError)


#: A credential that must never appear in a refusal or its log line (#1659).
_PASSWORD = "Pa55wordValue"


@pytest.mark.unit
@pytest.mark.parametrize(
    "url",
    [
        # Unparseable: shown not at all, because it cannot be masked.
        "postgresql+asyncpg://fm:Pa55wordValue@db:5432x/faultmaven",
        "postgresql+asyncpg://fm:Pa55wordValue@db:port/faultmaven",
        "host=db user=fm password=Pa55wordValue dbname=faultmaven",
        # Parseable and in memory: the whole authority and every query value
        # outside SQLite's own parameters are masked. An unescaped '@' pushes
        # a password's tail into the host, and a name list misses pwd/sig.
        "sqlite+aiosqlite://fm:Pa55wordValue@/",
        "sqlite+aiosqlite://Pa55wordValue@/:memory:",
        "sqlite+aiosqlite://fm:x@Pa55wordValue@/:memory:",
        "sqlite+aiosqlite:///:memory:?password=Pa55wordValue",
        "sqlite+aiosqlite:///:memory:?pwd=Pa55wordValue",
        "sqlite+aiosqlite:///:memory:?odbc_connect=Pa55wordValue",
        "sqlite+aiosqlite:///:memory:?Pa55wordValue=1",
    ],
)
def test_the_refusal_never_prints_a_password(url):
    """Before #1659 no PostgreSQL URL reached this message: every non-SQLite
    URL counted as persistent. One that fails to parse now does, and the
    message is raised at boot, so it lands in pod logs."""
    assert persistent_database_configured(url) is False
    with pytest.raises(NonPersistentDatabaseError) as exc:
        require_persistent_database(_settings(url))
    assert _PASSWORD not in str(exc.value)
    assert "configures no persistent database" in str(exc.value)


@pytest.mark.unit
@pytest.mark.parametrize(
    "url",
    [
        "sqlite+aiosqlite:///?timeout=30",
        "sqlite+aiosqlite:///:memory:",
        "sqlite:///file:x?mode=memory&uri=true",
    ],
)
def test_a_parseable_url_is_still_named_in_the_refusal(url):
    """Masking must not cost the operator the value they got wrong: SQLite's
    own parameters (mode, uri, timeout) and the path are shown as given."""
    with pytest.raises(NonPersistentDatabaseError) as exc:
        require_persistent_database(_settings(url))
    assert url in str(exc.value)


@pytest.mark.unit
def test_the_migration_skip_log_never_prints_a_password(caplog):
    """The startup-migration skip logs the URL on the same False arm."""
    from unittest.mock import patch

    from faultmaven.bootstrap import data_init

    url = "postgresql+asyncpg://fm:Pa55wordValue@db:5432x/faultmaven"
    with (
        patch("faultmaven.config.settings.get_settings", return_value=_settings(url)),
        patch("subprocess.run") as run,
        caplog.at_level("INFO"),
    ):
        assert data_init.run_alembic_migrations() is False
    run.assert_not_called()
    logged = " ".join(r.getMessage() for r in caplog.records)
    assert "Skipping startup Alembic migrations" in logged
    assert _PASSWORD not in logged
