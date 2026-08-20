"""fm#1128: user-store and user-service factories select storage by ONE rule.

``GET /auth/me``'s stored-row read (#1123) is only correct if the store
``UserService`` queries is the store login/register wrote to — and the two DI
factories used to decide that with different rules: ``create_user_service``
keyed off "non-empty and not ``:memory:``" while ``create_user_store``
required a ``sqlite``/``postgresql`` substring and fell back to
``RedisUserStore`` otherwise. Under ``DATABASE_URL=''``, ``':memory:'`` or any
unrecognized DSN, accounts landed in one store while ``UserService.get_user``
queried an always-empty other — every ``/auth/me`` silently degraded to the
token principal's view (#1120's symptom) with green tests.

Both factories (and the case-repository factory, which carried a third inline
copy of the rule) now call ``config.settings.persistent_database_configured``
— a free function on the URL, because the factories are exercised throughout
the suite with duck-typed settings stubs and must not require a settings
method. These tests pin:

1. The predicate itself, over representative DSNs — including the unsupported
   dialect, which deliberately counts as configured so both sides point at the
   same database and fail loudly together rather than silently splitting.
2. The invariant the issue is actually about: for every representative DSN,
   ``create_user_store`` picks the database-backed store exactly when
   ``create_user_service`` picks the database-backed repository. RED if either
   factory regrows a local reading of DATABASE_URL.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from faultmaven.config.settings import persistent_database_configured
from faultmaven.container.providers.infrastructure import create_user_store
from faultmaven.container.providers.services import create_user_service
from faultmaven.infrastructure.auth.database_user_store import DatabaseUserStore
from faultmaven.infrastructure.auth.user_store import RedisUserStore
from faultmaven.infrastructure.persistence.user_repository import (
    InMemoryUserRepository,
    SessionlessUserRepository,
)

pytestmark = pytest.mark.unit

#: (DSN, persistent?) — the representative shapes from fm#1128. The mysql DSN
#: is the load-bearing row: it is the documented future-extension path, and it
#: is exactly where the substring rule and the non-empty rule used to diverge.
REPRESENTATIVE_DSNS = [
    ("", False),
    ("   ", False),
    (":memory:", False),
    ("sqlite+aiosqlite:///./data/faultmaven.db", True),
    ("sqlite:///relative.db", True),
    ("postgresql+asyncpg://fm:pw@db:5432/faultmaven", True),
    ("mysql+aiomysql://fm:pw@db:3306/faultmaven", True),
]


def _settings(database_url: str) -> SimpleNamespace:
    # Duck-typed on purpose, carrying ONLY database_url — the factories are
    # exercised with stubs like this across the suite (e.g.
    # test_signing_key_single_authority), so the shared rule must be a free
    # function on the URL, never a method the stub would have to grow.
    return SimpleNamespace(database=SimpleNamespace(database_url=database_url))


# ---------------------------------------------------------------------------
# 1. The predicate
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("dsn,persistent", REPRESENTATIVE_DSNS)
def test_persistent_database_configured(dsn: str, persistent: bool):
    assert persistent_database_configured(dsn) is persistent


def test_persistent_database_configured_accepts_none():
    assert persistent_database_configured(None) is False


# ---------------------------------------------------------------------------
# 2. Factory agreement — the #1128 invariant
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("dsn,persistent", REPRESENTATIVE_DSNS)
def test_user_store_and_user_service_agree_on_the_backing_store(
    dsn: str, persistent: bool
):
    settings = _settings(dsn)

    store = create_user_store(redis_client=SimpleNamespace(), settings=settings)
    service = create_user_service(
        auth_service=SimpleNamespace(),  # truthy is all the factory requires
        token_generator=SimpleNamespace(),
        redis_client=None,
        settings=settings,
    )
    assert service is not None, f"create_user_service returned None for {dsn!r}"

    store_is_db = isinstance(store, DatabaseUserStore)
    service_is_db = isinstance(service.user_repo, SessionlessUserRepository)

    # The invariant: whatever each side is, they are the SAME side.
    assert store_is_db == service_is_db, (
        f"split-brain for DATABASE_URL={dsn!r}: login writes to "
        f"{type(store).__name__} while /auth/me reads "
        f"{type(service.user_repo).__name__}"
    )
    # And both track the shared predicate.
    assert store_is_db is persistent
    if not persistent:
        assert isinstance(store, RedisUserStore)
        assert isinstance(service.user_repo, InMemoryUserRepository)
