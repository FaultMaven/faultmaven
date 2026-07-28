"""``SKIP_SERVICE_CHECKS`` must not buy a cloud pod an in-process FakeRedis.

The provider's skip branch used to return FakeRedis unconditionally, so under
``DEPLOYMENT_MODE=cloud`` one env var reproduced exactly the per-replica
degradation the Redis gate exists to refuse — sessions, token revocation, rate
limits and idempotency all going process-local, silently. Same shape as the
``AUTH_MODE`` incident (#881): an escape hatch meant for standalone/CI applying
where the guarantee has to hold.

The skip branch therefore goes THROUGH the gate: standalone keeps the skip,
cloud refuses to boot. Settings are the REAL ``FaultMavenSettings`` — a
stand-in's ``is_cloud`` is truthy in both modes, which would let the standalone
half of this pair pass against a dead gate.
"""

import pytest

from faultmaven.config.settings import DeploymentMode, FaultMavenSettings
from faultmaven.container.providers.infrastructure import create_redis_client
from faultmaven.infrastructure.redis_client import (
    RedisUnavailableError,
    is_fakeredis,
    reset_fakeredis_client,
)

pytestmark = [pytest.mark.unit, pytest.mark.security]


@pytest.fixture(autouse=True)
def _reset_fakeredis_singleton():
    reset_fakeredis_client()
    yield
    reset_fakeredis_client()


def _skipping_settings(monkeypatch, *, cloud: bool) -> FaultMavenSettings:
    """Real settings with SKIP_SERVICE_CHECKS on, installed as the ambient ones."""
    settings = FaultMavenSettings(_env_file=None)
    settings.deployment_mode = (
        DeploymentMode.CLOUD if cloud else DeploymentMode.STANDALONE
    )
    settings.server.skip_service_checks = True
    # The gate reads the ambient settings, not the argument.
    monkeypatch.setattr("faultmaven.config.settings.get_settings", lambda: settings)
    return settings


async def test_skip_service_checks_cannot_substitute_fakeredis_under_cloud(monkeypatch):
    settings = _skipping_settings(monkeypatch, cloud=True)
    assert settings.is_cloud is True

    with pytest.raises(RedisUnavailableError, match="SKIP_SERVICE_CHECKS"):
        await create_redis_client(settings)


async def test_skip_service_checks_still_returns_fakeredis_under_standalone(
    monkeypatch,
):
    """Standalone is single-process: FakeRedis is the intended backend there."""
    settings = _skipping_settings(monkeypatch, cloud=False)

    client = await create_redis_client(settings)

    assert is_fakeredis(client)
