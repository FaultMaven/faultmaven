"""RedisUserStore creates ordinary users, not admins.

``DevUser.__post_init__`` defaults roles to ['admin'], so a store that omits
roles at construction silently grants admin to every account it creates —
including the service account the D10 provisioning path creates — while the
DatabaseUserStore path creates ['user']. The container picks between the two
stores at runtime, so this divergence is invisible to the caller.
"""

from unittest.mock import AsyncMock

import pytest

from faultmaven.infrastructure.auth.user_store import RedisUserStore

pytestmark = pytest.mark.asyncio


def _store() -> RedisUserStore:
    redis = AsyncMock()
    redis.get.return_value = None
    redis.set = AsyncMock()
    redis.sadd = AsyncMock()
    return RedisUserStore(redis_client=redis)


async def test_created_user_is_not_an_admin():
    created = await _store().create_user(
        username="slack-agent", account_kind="service", service_channel="slack"
    )

    assert created.roles == ["user"]
    assert "admin" not in created.roles


async def test_the_kind_and_the_channel_are_recorded():
    """Both, because they answer different questions (ADR-017 D6): the kind
    says a human or an agent, the channel says which integration — and only the
    channel decides the derived ``cases.source``."""
    created = await _store().create_user(
        username="slack-agent", account_kind="service", service_channel="slack"
    )

    assert created.account_kind == "service"
    assert created.service_channel == "slack"


async def test_a_human_serves_no_channel():
    """The default, and the direction that matters: a human whose channel came
    back 'slack' would have every case they open stamped as a Slack case."""
    created = await _store().create_user(username="alice")

    assert created.account_kind == "individual"
    assert created.service_channel is None
