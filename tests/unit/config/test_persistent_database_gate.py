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

from faultmaven.config.persistent_database import (
    DEFAULT_DATABASE_URL,
    NonPersistentDatabaseError,
    require_persistent_database,
)
from faultmaven.config.settings import persistent_database_configured


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
