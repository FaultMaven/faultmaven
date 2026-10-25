"""The per-user session index, on FakeRedis: the per-user limit applies (#2297).

``RedisSessionStore.list_sessions()`` was a stub returning ``[]``, so
``AuthSessionService``'s ``max_sessions_per_user`` counted nothing and limited
nothing. The store now keeps one set per user, ``sessions:user:<user_id>``,
written in the same script as the session and pruned on read of every member
whose session key is gone.

Each test asserts a live session in the listing as its positive control, so
none of them holds for a store that lists nothing. The same behaviours run
against a real Redis in ``tests/integration/test_real_redis_stores.py``.
"""

import asyncio

import fakeredis
import fakeredis.aioredis as fakeredis_aio
import pytest

from faultmaven.modules.auth.domain.services.auth_session_service import (
    AuthSessionService,
)
from faultmaven.modules.auth.infrastructure.stores.redis_session_store import (
    RedisSessionStore,
)

pytestmark = [pytest.mark.unit, pytest.mark.session]

LIMIT = 3


@pytest.fixture
async def redis():
    client = fakeredis_aio.FakeRedis(
        server=fakeredis.FakeServer(), decode_responses=True
    )
    yield client
    await client.aclose()


@pytest.fixture
def store(redis):
    return RedisSessionStore(redis)


@pytest.fixture
def service(store):
    return AuthSessionService(session_store=store, max_sessions_per_user=LIMIT)


async def _ids(service, user_id):
    return [s.session_id for s in await service.list_sessions(user_id)]


def _index_key(user_id):
    """The documented key of a user's index (the store's ``user_index_prefix``)."""
    return f"sessions:user:{user_id}"


async def _indexed(redis, user_id):
    return await redis.smembers(_index_key(user_id))


async def test_one_session_over_the_limit_evicts_the_oldest(service, redis):
    created = []
    for _ in range(LIMIT + 1):
        session, resumed = await service.create_session("user-a")
        assert resumed is False
        created.append(session.session_id)

    assert await _ids(service, "user-a") == created[1:]
    assert await service.get_session(created[0]) is None
    assert await _indexed(redis, "user-a") == set(created[1:])


async def test_the_listing_holds_only_that_users_live_sessions(service):
    a1, _ = await service.create_session("user-a")
    b1, _ = await service.create_session("user-b")
    a2, _ = await service.create_session("user-a")

    assert await _ids(service, "user-a") == [a1.session_id, a2.session_id]
    assert await _ids(service, "user-b") == [b1.session_id]
    assert await _ids(service, "user-c") == []


async def test_one_users_limit_leaves_another_users_sessions(service, redis):
    other, _ = await service.create_session("user-b")
    for _ in range(LIMIT + 1):
        await service.create_session("user-a")

    assert len(await _ids(service, "user-a")) == LIMIT
    assert await _ids(service, "user-b") == [other.session_id]
    assert await _indexed(redis, "user-b") == {other.session_id}


async def test_a_deleted_session_leaves_the_index(service, redis):
    gone, _ = await service.create_session("user-a")
    kept, _ = await service.create_session("user-a")

    assert await service.delete_session(gone.session_id) is True

    assert await _ids(service, "user-a") == [kept.session_id]
    assert await _indexed(redis, "user-a") == {kept.session_id}


async def test_an_expired_session_leaves_the_index(service, redis, store):
    lapsed, _ = await service.create_session("user-a")
    kept, _ = await service.create_session("user-a")

    # The session key's own TTL lapses; Redis tells the index nothing.
    await redis.pexpire(f"{store.prefix}{lapsed.session_id}", 1)
    await asyncio.sleep(0.05)

    assert await _ids(service, "user-a") == [kept.session_id]
    # The read pruned it, so the next count starts from the live set.
    assert await _indexed(redis, "user-a") == {kept.session_id}


async def test_the_index_ttl_follows_its_longest_lived_member(service, redis, store):
    first, _ = await service.create_session("user-a")
    index_key = _index_key("user-a")
    # The index first: both expire at one instant, so the later read is lower.
    index_ttl = await redis.pttl(index_key)
    session_ttl = await redis.pttl(f"{store.prefix}{first.session_id}")

    assert 0 < session_ttl <= index_ttl <= store.default_ttl * 1000

    # A shorter-lived session does not shorten the index under a longer one.
    store.default_ttl = 60
    await service.create_session("user-a")
    assert await redis.ttl(index_key) > 60


async def test_the_index_lapses_with_its_last_session(service, store, redis):
    store.default_ttl = 1
    session, _ = await service.create_session("user-a")
    index_key = _index_key("user-a")

    assert await redis.exists(index_key) == 1
    assert await _ids(service, "user-a") == [session.session_id]

    await asyncio.sleep(1.1)

    assert await redis.exists(index_key) == 0
    assert await _ids(service, "user-a") == []


def test_the_limit_is_set_by_its_session_variable_and_is_at_least_one(monkeypatch):
    """``SESSION_MAX_SESSIONS_PER_USER`` names the limit, like the other session
    settings, and below one is refused: at 0 every create would fail."""
    from pydantic import ValidationError

    from faultmaven.config.settings import SessionSettings

    monkeypatch.setenv("SESSION_MAX_SESSIONS_PER_USER", "4")
    assert SessionSettings().max_sessions_per_user == 4

    monkeypatch.setenv("SESSION_MAX_SESSIONS_PER_USER", "0")
    with pytest.raises(ValidationError, match="max_sessions_per_user|SESSION_MAX"):
        SessionSettings()
