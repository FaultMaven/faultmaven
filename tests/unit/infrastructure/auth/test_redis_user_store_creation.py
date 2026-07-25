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
    created = await _store().create_user(username="slack-agent", account_kind="slack")

    assert created.roles == ["user"]
    assert "admin" not in created.roles


async def test_account_kind_is_recorded():
    created = await _store().create_user(username="slack-agent", account_kind="slack")

    assert created.account_kind == "slack"
