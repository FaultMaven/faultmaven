"""A session lives ``SESSION_TIMEOUT_MINUTES`` past its last write (#2301).

``SESSION_TIMEOUT_MINUTES`` was documented as the session TTL, but the session
store wrote every session for a fixed 1800 s and nothing passed the setting in.
The service kept clocks of its own beside it: an inactivity threshold read from
the setting, and a 24-hour ``expires_at`` that heartbeats never moved.

Now there is one clock. The store is built with the setting
(``create_session_store``), every session write sets it as the key's TTL and
stamps the record's ``expires_at`` with the instant it lapses, and the service
holds no timeout of its own. Each test builds the store and the service through
the production factories from settings read out of the environment, never by
handing the store a TTL, so a factory that drops the setting fails here.

The same TTL-from-setting case runs against a real Redis in
``tests/integration/test_real_redis_stores.py``.
"""

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import patch

import fakeredis
import fakeredis.aioredis as fakeredis_aio
import pytest
from fastapi import Response

from faultmaven.config.settings import FaultMavenSettings
from faultmaven.container.providers.infrastructure import create_session_store
from faultmaven.container.providers.services import create_session_service
from faultmaven.models.common import SessionContext
from faultmaven.modules.auth.domain.services.auth_session_service import (
    AuthSessionService,
)
from faultmaven.utils.datetime import parse_utc_timestamp
from tests.utils import minimal_session_service

pytestmark = [pytest.mark.unit, pytest.mark.session]

#: Far from the 30-minute default and from the 1800 s the store used to fix,
#: so a TTL that ignores the setting cannot land inside the tolerance.
TIMEOUT_MINUTES = 7
TIMEOUT_MS = TIMEOUT_MINUTES * 60 * 1000
#: Slack for the time between a write and the read of its TTL.
TOLERANCE_MS = 5000


def _settings(monkeypatch, timeout_minutes=TIMEOUT_MINUTES, **env):
    """The application's settings, with the session variables from the environment."""
    monkeypatch.setenv("SESSION_TIMEOUT_MINUTES", str(timeout_minutes))
    for name, value in env.items():
        monkeypatch.setenv(name, str(value))
    return FaultMavenSettings(_env_file=None)


def _composed(redis, settings):
    """The store and the service, built as composition builds them."""
    store = create_session_store(redis, settings)
    service = create_session_service(
        store,
        settings,
        lambda: pytest.fail("the real session service failed to build"),
    )
    assert isinstance(service, AuthSessionService)
    return store, service


def _about_the_timeout(pttl_ms, timeout_ms=TIMEOUT_MS):
    return timeout_ms - TOLERANCE_MS < pttl_ms <= timeout_ms


@pytest.fixture
async def redis():
    client = fakeredis_aio.FakeRedis(
        server=fakeredis.FakeServer(), decode_responses=True
    )
    yield client
    await client.aclose()


async def test_a_new_session_lives_session_timeout_minutes(monkeypatch, redis):
    store, service = _composed(redis, _settings(monkeypatch))

    session, resumed = await service.create_session("user-a")

    assert resumed is False
    assert _about_the_timeout(await redis.pttl(f"{store.prefix}{session.session_id}"))


async def test_expires_at_is_the_instant_the_ttl_lapses(monkeypatch, redis):
    """The record does not claim a lifetime the key does not have: no 24 hours."""
    store, service = _composed(redis, _settings(monkeypatch))
    before = datetime.now(timezone.utc)

    session, _ = await service.create_session("user-a")

    stored = await service.get_session(session.session_id)
    pttl = await redis.pttl(f"{store.prefix}{session.session_id}")
    lapses_at = datetime.now(timezone.utc) + timedelta(milliseconds=pttl)
    assert stored.expires_at == session.expires_at
    assert abs(stored.expires_at - lapses_at) < timedelta(milliseconds=TOLERANCE_MS)
    assert stored.expires_at <= before + timedelta(minutes=TIMEOUT_MINUTES + 1)


async def test_a_heartbeat_re_arms_the_ttl_and_moves_expires_at(monkeypatch, redis):
    """``update_last_activity`` is what ``POST /sessions/{id}/heartbeat`` calls."""
    store, service = _composed(redis, _settings(monkeypatch))
    session, _ = await service.create_session("user-a")
    key = f"{store.prefix}{session.session_id}"
    # Most of its life gone, as an idle panel's session is before a heartbeat.
    await redis.pexpire(key, 3000)

    assert await service.update_last_activity(session.session_id) is True

    assert _about_the_timeout(await redis.pttl(key))
    stored = await service.get_session(session.session_id)
    assert stored.expires_at > session.expires_at


async def test_a_resume_re_arms_the_session_and_its_client_index(monkeypatch, redis):
    store, service = _composed(redis, _settings(monkeypatch))
    session, _ = await service.create_session("user-a", client_id="laptop")
    key = f"{store.prefix}{session.session_id}"
    client_key = "client_index:user-a:laptop"
    assert _about_the_timeout(await redis.pttl(client_key))
    await redis.pexpire(key, 3000)
    await redis.pexpire(client_key, 3000)

    resumed, was_resumed = await service.create_session("user-a", client_id="laptop")

    assert was_resumed is True
    assert resumed.session_id == session.session_id
    assert _about_the_timeout(await redis.pttl(key))
    assert _about_the_timeout(await redis.pttl(client_key))
    # The caller holds the expiry the resume bought, not the one it found.
    assert resumed.expires_at > session.expires_at


async def test_a_heartbeat_re_arms_the_client_index(monkeypatch, redis):
    """An index set once at creation lapsed under a session kept alive past it,
    and the next resume from that client minted a second session."""
    _, service = _composed(redis, _settings(monkeypatch))
    session, _ = await service.create_session("user-a", client_id="laptop")
    client_key = "client_index:user-a:laptop"
    await redis.pexpire(client_key, 3000)

    await service.update_last_activity(session.session_id)

    assert _about_the_timeout(await redis.pttl(client_key))
    again, was_resumed = await service.create_session("user-a", client_id="laptop")
    assert (again.session_id, was_resumed) == (session.session_id, True)


async def test_the_user_index_follows_its_longest_lived_member(monkeypatch, redis):
    """Two stores over one Redis, as two replicas configured apart would be."""
    longer_store, longer = _composed(redis, _settings(monkeypatch))
    _, shorter = _composed(redis, _settings(monkeypatch, timeout_minutes=2))
    index_key = f"{longer_store.user_index_prefix}user-a"

    first, _ = await longer.create_session("user-a")
    assert _about_the_timeout(await redis.pttl(index_key))

    second, _ = await shorter.create_session("user-a")

    assert _about_the_timeout(
        await redis.pttl(f"{longer_store.prefix}{second.session_id}"), 2 * 60 * 1000
    )
    # Not lowered to the shorter member's TTL.
    assert _about_the_timeout(await redis.pttl(index_key))
    assert [s.session_id for s in await longer.list_sessions("user-a")] == [
        first.session_id,
        second.session_id,
    ]


async def test_the_limit_counts_every_live_session_however_long_idle(
    monkeypatch, redis
):
    """The TTL is the one inactivity clock. A second threshold on
    ``last_activity`` left a live session out of the count, so a user kept more
    live sessions than the limit allows."""
    store, service = _composed(
        redis, _settings(monkeypatch, SESSION_MAX_SESSIONS_PER_USER=2)
    )
    idle_since = datetime.now(timezone.utc) - timedelta(minutes=TIMEOUT_MINUTES + 3)
    seeded = []
    for n in range(2):
        session = SessionContext(
            session_id=f"seeded-{n}",
            user_id="user-a",
            created_at=idle_since,
            last_activity=idle_since + timedelta(seconds=n),
        )
        # Written now (an update, an archive), so live, but idle by its clock.
        await store.save(session)
        seeded.append(session.session_id)

    newest, _ = await service.create_session("user-a")

    assert [s.session_id for s in await service.list_sessions("user-a")] == [
        seeded[1],
        newest.session_id,
    ]


async def test_the_degraded_stand_in_lives_on_the_same_clock():
    """``MinimalSessionService`` has no Redis: each write re-arms ``expires_at``."""
    service = minimal_session_service(
        SimpleNamespace(
            session=SimpleNamespace(
                timeout_minutes=TIMEOUT_MINUTES, max_sessions_per_user=10
            )
        )
    )
    before = datetime.now(timezone.utc)
    session, _ = await service.create_session("user-a")
    timeout = timedelta(minutes=TIMEOUT_MINUTES)
    assert before + timeout <= session.expires_at <= before + timeout * 1.01

    session.expires_at = datetime.now(timezone.utc) + timedelta(seconds=3)
    assert await service.update_last_activity(session.session_id) is True

    assert session.expires_at >= datetime.now(timezone.utc) + timeout * 0.99


async def test_the_mint_reports_the_server_timeout_and_the_session_expiry(
    monkeypatch, redis
):
    """``POST /api/v1/sessions`` answered the request's ``timeout_minutes`` and
    ``created_at`` plus it, a lifetime nothing enforced."""
    from faultmaven.modules.auth.api.session import (
        AuthSessionCreateRequest,
        create_session,
    )
    from faultmaven.modules.auth.domain.models.auth import DevUser

    settings = _settings(monkeypatch)
    store, service = _composed(redis, settings)
    user = DevUser(
        user_id="user-a",
        username="a",
        email="a@example.com",
        display_name="A",
        created_at=datetime.now(timezone.utc),
    )

    with patch(
        "faultmaven.modules.auth.api.session.get_settings", return_value=settings
    ):
        body = await create_session(
            request=AuthSessionCreateRequest(timeout_minutes=240),
            session_service=service,
            response=Response(),
            current_user=user,
        )

    assert body["timeout_minutes"] == TIMEOUT_MINUTES
    stored = await service.get_session(body["session_id"])
    answered = parse_utc_timestamp(body["expires_at"])
    assert abs(answered - stored.expires_at) < timedelta(milliseconds=1)
    assert _about_the_timeout(await redis.pttl(f"{store.prefix}{body['session_id']}"))
    assert "timeout_minutes" not in stored.metadata
