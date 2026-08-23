"""A failed user lookup is not "no such user" (#1043).

Both user stores used to catch every exception on the read path and return
``None``, so a transient database error, an exhausted connection pool, or a
role/permission problem all surfaced as *absent*. "No such user" is a claim, and
the stores were making it on evidence they did not have.

It is worst on the operator paths, which run during incidents and offboarding:
``fm-remove-org-member`` printed ``No user matches 'alice'`` and exited 1, so the
operator went hunting for the right username while the cutoff had not happened
and the real fault — an unavailable store — stayed invisible.

These pin both halves of the fix, for both stores:

* a lookup that **fails** raises :class:`UserLookupFailed`, carrying which
  lookup broke and the identifier, so a caller can say which;
* a lookup that **completes and matches nothing** still returns ``None``, which
  is what keeps the legitimate absence callers (registration uniqueness, the SSO
  JIT path) working unchanged.

The parametrisation over both stores is the point: they are interchangeable
behind ``container.get_user_store()``, so a fix to one of them leaves the same
bug reachable from the same call sites through the other.
"""

from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from faultmaven.exceptions import RepositoryError, ServiceError, UserLookupFailed
from faultmaven.infrastructure.auth.database_user_store import DatabaseUserStore
from faultmaven.infrastructure.auth.user_store import RedisUserStore

pytestmark = [pytest.mark.unit, pytest.mark.security]

BOOM = RuntimeError("connection pool exhausted")

#: (store lookup, the repository/redis call it makes) for each of the three
#: lookups, so every one is exercised rather than the first one standing in for
#: the other two — they were three separate copies of the same swallow.
LOOKUPS = [
    ("get_user", "abc-123", "user_id"),
    ("get_user_by_username", "alice", "username"),
    ("get_user_by_email", "alice@example.com", "email"),
]


def _database_store(*, failing: bool):
    """A DatabaseUserStore whose repository either raises or reports absence."""
    repo = AsyncMock()
    for method in ("get", "get_by_username", "get_by_email"):
        if failing:
            getattr(repo, method).side_effect = BOOM
        else:
            getattr(repo, method).return_value = None
    return DatabaseUserStore(user_repository=repo)


def _redis_store(*, failing: bool):
    """A RedisUserStore whose backing **client** either raises or reports absence.

    The double is the Redis client, deliberately, so the store's own
    ``_redis_get`` runs for real. Substituting ``_redis_get`` itself is the
    obvious shortcut and it makes this whole file vacuous: that method used to
    catch every exception and return ``None``, *underneath* the lookup methods'
    ``UserLookupFailed`` handling, so the handlers could never fire for a real
    outage — and a test that replaced it would never notice. The layer being
    asserted has to be the layer under test.
    """
    store = RedisUserStore.__new__(RedisUserStore)
    store.user_key_pattern = "user:{}"
    store.username_key_pattern = "username:{}"
    store.email_key_pattern = "email:{}"
    store.redis = SimpleNamespace(
        get=AsyncMock(side_effect=BOOM) if failing else AsyncMock(return_value=None)
    )
    return store


STORES = [("DatabaseUserStore", _database_store), ("RedisUserStore", _redis_store)]


@pytest.mark.parametrize(("store_name", "build"), STORES, ids=[s for s, _ in STORES])
@pytest.mark.parametrize(
    ("method", "identifier", "lookup"), LOOKUPS, ids=[m for m, _, _ in LOOKUPS]
)
async def test_a_failed_lookup_raises_instead_of_reporting_absence(
    store_name, build, method, identifier, lookup
):
    """The whole point: the store must not answer a question it could not ask."""
    store = build(failing=True)

    with pytest.raises(UserLookupFailed) as exc:
        await getattr(store, method)(identifier)

    assert exc.value.lookup == lookup
    assert exc.value.identifier == identifier
    assert exc.value.__cause__ is BOOM
    # The message has to refuse the wrong reading out loud: this text is what an
    # operator sees in a CLI and in the log, and "not found" is the conclusion
    # they will otherwise jump to.
    assert "NOT" in str(exc.value)
    assert "no such user" in str(exc.value)


@pytest.mark.parametrize(("store_name", "build"), STORES, ids=[s for s, _ in STORES])
@pytest.mark.parametrize(
    ("method", "identifier", "lookup"), LOOKUPS, ids=[m for m, _, _ in LOOKUPS]
)
async def test_a_completed_lookup_that_matches_nothing_still_returns_none(
    store_name, build, method, identifier, lookup
):
    """Absence is still absence — the callers that rely on it must keep working.

    Registration uniqueness checks and the SSO JIT provisioning path treat
    ``None`` as a normal outcome and create an account on it. If the fix had
    turned every miss into an exception, both would break; what changed is only
    that they no longer proceed on a *guess*.
    """
    store = build(failing=False)

    assert await getattr(store, method)(identifier) is None


@pytest.mark.parametrize(("store_name", "build"), STORES, ids=[s for s, _ in STORES])
@pytest.mark.parametrize(
    ("method", "identifier", "lookup"), LOOKUPS, ids=[m for m, _, _ in LOOKUPS]
)
async def test_an_empty_identifier_is_absence_not_failure(
    store_name, build, method, identifier, lookup
):
    """Nothing is asked of the store, so "no match" is a completed answer.

    Kept as a return rather than folded into the raising path: an empty string
    matches no user by construction, and raising here would make every caller
    that passes an unset field handle an exception for a question with a known
    answer.
    """
    store = build(failing=True)  # would raise if it reached the store at all

    assert await getattr(store, method)("") is None


def test_it_is_a_repository_error_so_http_hides_the_identifier():
    """Over HTTP this must become a generic 500, not an echo of the store's message.

    ``service_error_handler`` replaces the whole body for any ``ServiceError``,
    so the identifier and the underlying database error reach the log and not
    the client. That is why this hangs off ``RepositoryError`` rather than being
    a bare exception with a helpful message.
    """
    assert issubclass(UserLookupFailed, RepositoryError)
    assert issubclass(UserLookupFailed, ServiceError)


async def test_the_redis_store_treats_an_undecodable_record_as_failure():
    """A record that will not parse is a corrupt account, not a missing one.

    This one is easy to get wrong in the other direction: the JSON decode sits
    inside the same try block, and reporting it as absence is how a
    serialisation bug becomes "the user vanished" mid-incident.
    """
    store = _redis_store(failing=False)
    store.redis.get = AsyncMock(return_value="}{ not json")

    with pytest.raises(UserLookupFailed) as exc:
        await store.get_user("abc-123")

    assert isinstance(exc.value.__cause__, json.JSONDecodeError)
