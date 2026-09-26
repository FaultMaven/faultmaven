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
