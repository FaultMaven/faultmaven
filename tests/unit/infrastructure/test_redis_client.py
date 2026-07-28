"""Tests for the central Redis client factory.

Two guarantees are pinned here:

1. **Credentials reach the client.** Both the sync factory and the async entry
   point resolve host/port/password/db through one config builder, so a
   password-protected Redis is actually authenticated against. The async path
   used to construct ``redis.Redis(host=..., port=...)`` with no password and no
   db, which authenticated anonymously and then fell back to FakeRedis.

2. **The FakeRedis fallback is not silent under cloud.** FakeRedis is
   in-process; under cloud the Redis store is deployment-wide (sessions, token
   revocation, rate limits), so substituting it degrades those to per-replica.
   Cloud must refuse to boot instead.

Settings are REAL ``FaultMavenSettings``/``DatabaseSettings`` — a stand-in with
a hand-written ``is_cloud`` or plain-string password would let a dead gate pass.
"""

import logging

import pytest

import faultmaven.infrastructure.redis_client as rc
from faultmaven.config.settings import (
    DatabaseSettings,
    DeploymentMode,
    FaultMavenSettings,
)
from faultmaven.infrastructure.redis_client import (
    RedisClientFactory,
    RedisUnavailableError,
    get_async_redis_client,
    is_fakeredis,
)

# The credential tests below are data-leakage tests (a password reaching a log or
# a boot-refusal message), so this module carries the security marker too.
pytestmark = [pytest.mark.unit, pytest.mark.security]


# --------------------------------------------------------------------------- #
# Fixtures / doubles
# --------------------------------------------------------------------------- #


class _FakeClient:
    """Stand-in for redis.asyncio.Redis — answers ping, records being closed."""

    def __init__(self, ping_error=None):
        self._ping_error = ping_error
        self.closed = False

    async def ping(self):
        if self._ping_error is not None:
            raise self._ping_error
        return True

    async def aclose(self):
        self.closed = True


class _FakeRedisModule:
    """Stand-in for the ``redis.asyncio`` module, capturing constructor kwargs."""

    def __init__(self, ping_error=None, construct_error=None):
        self.redis_kwargs = []
        self.from_url_calls = []
        self.clients = []
        self._ping_error = ping_error
        self._construct_error = construct_error

    def _client(self):
        client = _FakeClient(self._ping_error)
        self.clients.append(client)
        return client

    def Redis(self, **kwargs):  # noqa: N802 - mirrors the redis API
        if self._construct_error is not None:
            raise self._construct_error
        self.redis_kwargs.append(kwargs)
        return self._client()

    def from_url(self, url, **kwargs):
        if self._construct_error is not None:
            raise self._construct_error
        self.from_url_calls.append((url, kwargs))
        return self._client()


def _settings(
    *,
    cloud: bool = False,
    redis_url=None,
    redis_host="redis.internal",
    redis_port=6379,
    redis_db=0,
    redis_password=None,
) -> FaultMavenSettings:
    """A real settings object with an explicitly-pinned database section.

    Values are passed as init kwargs so ambient REDIS_* env vars cannot leak in.
    """
    settings = FaultMavenSettings(_env_file=None)
    settings.deployment_mode = (
        DeploymentMode.CLOUD if cloud else DeploymentMode.STANDALONE
    )
    settings.database = DatabaseSettings(
        _env_file=None,
        redis_url=redis_url,
        redis_host=redis_host,
        redis_port=redis_port,
        redis_db=redis_db,
        redis_password=redis_password,
    )
    return settings


@pytest.fixture
def use_settings(monkeypatch):
    """Install a real settings object as what the factory's get_settings returns."""

    def _install(**kwargs):
        settings = _settings(**kwargs)
        monkeypatch.setattr("faultmaven.config.settings.get_settings", lambda: settings)
        return settings

    return _install


@pytest.fixture
def fake_redis_module(monkeypatch):
    """Install a fake ``redis.asyncio`` module and return the installer."""

    def _install(**kwargs):
        module = _FakeRedisModule(**kwargs)
        monkeypatch.setattr(rc, "redis", module)
        monkeypatch.setattr(rc, "REDIS_AVAILABLE", True)
        return module

    return _install


@pytest.fixture(autouse=True)
def _reset_fakeredis_singleton():
    rc.reset_fakeredis_client()
    yield
    rc.reset_fakeredis_client()


# --------------------------------------------------------------------------- #
# Credentials reach the async client
# --------------------------------------------------------------------------- #


async def test_async_client_gets_password_and_db_from_settings(
    use_settings, fake_redis_module
):
    """The regression: the async client must authenticate and select the db."""
    use_settings(
        redis_host="redis.internal",
        redis_port=6380,
        redis_db=3,
        redis_password="s3cr3t",
    )
    module = fake_redis_module()

    client = await get_async_redis_client()

    assert not is_fakeredis(client)
    assert len(module.redis_kwargs) == 1
    kwargs = module.redis_kwargs[0]
    assert kwargs["host"] == "redis.internal"
    assert kwargs["port"] == 6380
    assert kwargs["password"] == "s3cr3t"
    assert kwargs["db"] == 3


async def test_async_client_reports_auth_state_without_leaking_password(
    use_settings, fake_redis_module, caplog
):
    """The success log states auth yes/no, like the sync path — never the secret."""
    use_settings(redis_password="s3cr3t")
    fake_redis_module()

    with caplog.at_level(logging.INFO, logger=rc.logger.name):
        await get_async_redis_client()

    messages = [r.getMessage() for r in caplog.records]
    assert any("auth: yes" in m for m in messages), messages
    assert not any("s3cr3t" in m for m in messages), messages


async def test_async_client_reports_no_auth_when_password_unset(
    use_settings, fake_redis_module, caplog
):
    use_settings(redis_password=None)
    fake_redis_module()

    with caplog.at_level(logging.INFO, logger=rc.logger.name):
        await get_async_redis_client()

    assert any("auth: no" in r.getMessage() for r in caplog.records)


async def test_async_explicit_url_argument_overrides_settings(
    use_settings, fake_redis_module
):
    use_settings(redis_host="from-settings", redis_password="s3cr3t")
    module = fake_redis_module()

    await get_async_redis_client(redis_url="redis://:other@explicit:6379/9")

    assert module.redis_kwargs == []
    assert module.from_url_calls[0][0] == "redis://:other@explicit:6379/9"


async def test_async_settings_url_takes_precedence_over_discrete_fields(
    use_settings, fake_redis_module
):
    """REDIS_URL carries host/port/password/db itself, so it wins wholesale."""
    use_settings(
        redis_url="redis://:urlpw@urlhost:6381/7",
        redis_host="discrete-host",
        redis_password="discrete-pw",
    )
    module = fake_redis_module()

    await get_async_redis_client()

    assert module.redis_kwargs == []
    assert module.from_url_calls[0][0] == "redis://:urlpw@urlhost:6381/7"


# --------------------------------------------------------------------------- #
# Credentials reach the sync client
# --------------------------------------------------------------------------- #


def test_sync_client_gets_password_and_db_from_settings(
    use_settings, fake_redis_module
):
    use_settings(redis_host="redis.internal", redis_db=4, redis_password="s3cr3t")
    module = fake_redis_module()

    RedisClientFactory.create_client()

    kwargs = module.redis_kwargs[0]
    assert kwargs["password"] == "s3cr3t"
    assert kwargs["db"] == 4


def test_sync_explicit_args_override_settings(use_settings, fake_redis_module):
    use_settings(
        redis_host="from-settings",
        redis_port=6379,
        redis_db=4,
        redis_password="settings-pw",
    )
    module = fake_redis_module()

    RedisClientFactory.create_client(
        host="explicit", port=6390, password="explicit-pw", db=11
    )

    kwargs = module.redis_kwargs[0]
    assert kwargs["host"] == "explicit"
    assert kwargs["port"] == 6390
    assert kwargs["password"] == "explicit-pw"
    assert kwargs["db"] == 11


def test_explicit_db_zero_is_not_swallowed(use_settings, fake_redis_module):
    """``db or settings`` would silently reroute an explicit db=0 to db=7."""
    use_settings(redis_db=7)
    module = fake_redis_module()

    RedisClientFactory.create_client(db=0)

    assert module.redis_kwargs[0]["db"] == 0


# --------------------------------------------------------------------------- #
# The FakeRedis fallback is fatal under cloud, warned under standalone
# --------------------------------------------------------------------------- #


async def test_async_connection_failure_under_cloud_fails_the_boot(
    use_settings, fake_redis_module
):
    use_settings(cloud=True)
    fake_redis_module(ping_error=ConnectionError("Authentication required"))

    with pytest.raises(RedisUnavailableError, match="Authentication required"):
        await get_async_redis_client()


async def test_async_connection_failure_under_standalone_keeps_fakeredis(
    use_settings, fake_redis_module
):
    use_settings(cloud=False)
    fake_redis_module(ping_error=ConnectionError("connection refused"))

    client = await get_async_redis_client()

    assert is_fakeredis(client)


def test_sync_construction_failure_under_cloud_fails_the_boot(
    use_settings, fake_redis_module
):
    use_settings(cloud=True)
    fake_redis_module(construct_error=RuntimeError("bad redis config"))

    with pytest.raises(RedisUnavailableError, match="bad redis config"):
        RedisClientFactory.create_client()


def test_sync_construction_failure_under_standalone_keeps_fakeredis(
    use_settings, fake_redis_module
):
    use_settings(cloud=False)
    fake_redis_module(construct_error=RuntimeError("bad redis config"))

    assert is_fakeredis(RedisClientFactory.create_client())


async def test_async_missing_redis_package_under_cloud_fails_the_boot(
    use_settings, monkeypatch
):
    use_settings(cloud=True)
    monkeypatch.setattr(rc, "redis", None)
    monkeypatch.setattr(rc, "REDIS_AVAILABLE", False)

    with pytest.raises(RedisUnavailableError, match="redis package"):
        await get_async_redis_client()


def test_sync_missing_redis_package_under_cloud_fails_the_boot(
    use_settings, monkeypatch
):
    use_settings(cloud=True)
    monkeypatch.setattr(rc, "redis", None)
    monkeypatch.setattr(rc, "REDIS_AVAILABLE", False)

    with pytest.raises(RedisUnavailableError, match="redis package"):
        RedisClientFactory.create_client()


async def test_async_missing_redis_package_under_standalone_keeps_fakeredis(
    use_settings, monkeypatch
):
    use_settings(cloud=False)
    monkeypatch.setattr(rc, "redis", None)
    monkeypatch.setattr(rc, "REDIS_AVAILABLE", False)

    assert is_fakeredis(await get_async_redis_client())


async def test_async_no_redis_config_under_cloud_fails_the_boot(
    use_settings, fake_redis_module
):
    """No URL and no host: still fatal, whatever entry path got us here."""
    use_settings(cloud=True, redis_url=None, redis_host="")
    fake_redis_module()

    with pytest.raises(RedisUnavailableError, match="no Redis URL or host"):
        await get_async_redis_client()


async def test_async_no_redis_config_under_standalone_keeps_fakeredis(
    use_settings, fake_redis_module
):
    use_settings(cloud=False, redis_url=None, redis_host="")
    fake_redis_module()

    assert is_fakeredis(await get_async_redis_client())


def test_sync_no_redis_config_under_cloud_fails_the_boot(
    use_settings, fake_redis_module
):
    """The guard sits on the shared path, so the sync entry point refuses too."""
    use_settings(cloud=True, redis_url=None, redis_host="")
    fake_redis_module()

    with pytest.raises(RedisUnavailableError, match="no Redis URL or host"):
        RedisClientFactory.create_client()


# --------------------------------------------------------------------------- #
# One construction path — the async entry point delegates rather than repeats
# --------------------------------------------------------------------------- #


async def test_async_client_is_pooled_and_timeout_bounded_like_the_sync_one(
    use_settings, fake_redis_module
):
    """A second construction block drifts: this one had dropped max_connections,
    so every cloud client ran an unbounded pool while the sync one was capped."""
    use_settings()
    module = fake_redis_module()

    await get_async_redis_client()

    kwargs = module.redis_kwargs[0]
    assert kwargs["max_connections"] == 20
    assert kwargs["socket_connect_timeout"] == 5
    assert kwargs["socket_timeout"] == 10


async def test_async_client_that_fails_ping_releases_its_pool(
    use_settings, fake_redis_module
):
    """The rejected client owns a connection pool; discarding it must not leak it."""
    use_settings(cloud=False)
    module = fake_redis_module(ping_error=ConnectionError("connection refused"))

    await get_async_redis_client()

    assert module.clients[0].closed is True


# --------------------------------------------------------------------------- #
# The password never reaches a log line or the refusal message
# --------------------------------------------------------------------------- #

_URL_WITH_PASSWORD = "redis://:s3cr3t@redis.internal:6379/0"
_URL_ECHOING_ERROR = f"Error connecting to {_URL_WITH_PASSWORD}: auth failed"


async def test_url_password_is_not_logged_when_the_error_echoes_the_url(
    use_settings, fake_redis_module, caplog
):
    """Redis errors quote the URL back with the password inline; standalone logs it."""
    use_settings(cloud=False, redis_url=_URL_WITH_PASSWORD)
    fake_redis_module(ping_error=ConnectionError(_URL_ECHOING_ERROR))

    with caplog.at_level(logging.DEBUG, logger=rc.logger.name):
        client = await get_async_redis_client()

    assert is_fakeredis(client)
    messages = [r.getMessage() for r in caplog.records]
    assert not any("s3cr3t" in m for m in messages), messages
    assert any("***" in m for m in messages), messages


async def test_url_password_is_not_in_the_cloud_refusal_message(
    use_settings, fake_redis_module
):
    """Under cloud the same text is raised, and the refusal is surfaced to operators."""
    use_settings(cloud=True, redis_url=_URL_WITH_PASSWORD)
    fake_redis_module(ping_error=ConnectionError(_URL_ECHOING_ERROR))

    with pytest.raises(RedisUnavailableError) as exc:
        await get_async_redis_client()

    assert "s3cr3t" not in str(exc.value)
    # Still diagnosable: the target survives, only the secret is masked.
    assert "redis.internal" in str(exc.value)


def test_sync_construction_error_does_not_leak_the_url_password(
    use_settings, fake_redis_module
):
    use_settings(cloud=True, redis_url=_URL_WITH_PASSWORD)
    fake_redis_module(construct_error=ValueError(_URL_ECHOING_ERROR))

    with pytest.raises(RedisUnavailableError) as exc:
        RedisClientFactory.create_client()

    assert "s3cr3t" not in str(exc.value)


def test_sync_construction_error_does_not_leak_the_discrete_password(
    use_settings, fake_redis_module
):
    """Same guarantee on the host/port branch, where the password is its own field."""
    use_settings(cloud=True, redis_password="s3cr3t")
    fake_redis_module(construct_error=ValueError("AUTH failed for password s3cr3t"))

    with pytest.raises(RedisUnavailableError) as exc:
        RedisClientFactory.create_client()

    assert "s3cr3t" not in str(exc.value)
