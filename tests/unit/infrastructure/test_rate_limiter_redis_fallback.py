"""The rate limiter degrades loudly rather than switching itself off.

``RedisRateLimiter.initialize`` runs lazily on the first request — long after
the boot gate that raises ``RedisUnavailableError`` could have failed the boot.
Letting that refusal propagate here leaves ``_redis`` None and
``check_rate_limit`` failing open: no rate limiting at all, which is strictly
worse than the per-replica limiting an in-process FakeRedis gives. So the
limiter catches that one error, logs it at ERROR and keeps limiting.

The refusal is produced by the REAL factory (a cloud settings object plus a
``redis`` module that cannot construct a client), not by a stubbed raise, so
this pins the actual wiring between the two.
"""

import logging

import pytest

import faultmaven.infrastructure.redis_client as rc
from faultmaven.config.settings import (
    DatabaseSettings,
    DeploymentMode,
    FaultMavenSettings,
)
from faultmaven.infrastructure.protection.rate_limiter import RedisRateLimiter
from faultmaven.infrastructure.redis_client import is_fakeredis

pytestmark = pytest.mark.unit

_REDIS_URL = "redis://:s3cr3t@redis.internal:6379/0"


class _UnconstructableRedisModule:
    """Stand-in for ``redis.asyncio`` on a host that cannot reach Redis."""

    def Redis(self, **kwargs):  # noqa: N802 - mirrors the redis API
        raise ConnectionError("connection refused")

    def from_url(self, url, **kwargs):
        raise ConnectionError("connection refused")


@pytest.fixture(autouse=True)
def _reset_fakeredis_singleton():
    rc.reset_fakeredis_client()
    yield
    rc.reset_fakeredis_client()


@pytest.fixture
def cloud_without_redis(monkeypatch):
    settings = FaultMavenSettings(_env_file=None)
    settings.deployment_mode = DeploymentMode.CLOUD
    settings.database = DatabaseSettings(_env_file=None, redis_url=_REDIS_URL)
    monkeypatch.setattr("faultmaven.config.settings.get_settings", lambda: settings)
    monkeypatch.setattr(rc, "redis", _UnconstructableRedisModule())
    monkeypatch.setattr(rc, "REDIS_AVAILABLE", True)
    return settings


async def test_cloud_redis_refusal_leaves_the_limiter_limiting(
    cloud_without_redis, caplog
):
    limiter = RedisRateLimiter(redis_url=_REDIS_URL)

    with caplog.at_level(logging.ERROR):
        await limiter.initialize()  # must not raise on the request path

    assert limiter._redis is not None, "fail-open: rate limiting would be OFF"
    assert is_fakeredis(limiter._redis)

    messages = [r.getMessage() for r in caplog.records]
    assert any("FakeRedis" in m for m in messages), messages
    # Loud, but never at the cost of the credential.
    assert not any("s3cr3t" in m for m in messages), messages
