"""A session-store driver failure leaves ``RedisSessionStore`` as its contract's
``SessionStoreException``, chained; a programming error propagates as itself
(#2985).

The store turned every ``Exception`` into the builtin ``ConnectionError``, which
no caller of ``ISessionStore`` catches as a store failure, and reported a bug
the same way. Each wrapped call is driven over a client whose Redis call fails
with the driver's ``ConnectionError`` or ``TimeoutError``, or the socket's
``OSError``.
"""

import json
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock

import pytest
from redis.exceptions import ConnectionError as RedisConnectionError
from redis.exceptions import TimeoutError as RedisTimeoutError

from faultmaven.exceptions import SessionStoreException
from faultmaven.modules.auth.infrastructure.stores.redis_session_store import (
    RedisSessionStore,
)
from faultmaven.utils.serialization import to_json_compatible

pytestmark = [pytest.mark.unit]

FAILURES = [
    pytest.param(RedisConnectionError("refused"), id="redis-connection"),
    pytest.param(RedisTimeoutError("timed out"), id="redis-timeout"),
    pytest.param(OSError("reset by peer"), id="os-error"),
]


def _record(expires_in: timedelta) -> str:
    now = datetime.now(timezone.utc)
    return json.dumps(
        {
            "session_id": "s-1",
            "user_id": "u-1",
            "created_at": to_json_compatible(now),
            "last_activity": to_json_compatible(now),
            "expires_at": to_json_compatible(now + expires_in),
        }
    )


LIVE = _record(timedelta(minutes=5))
EXPIRED = _record(timedelta(minutes=-5))


def _client(failure: BaseException, **answers) -> MagicMock:
    """Every Redis call raises ``failure`` but those ``answers`` names."""
    client = MagicMock()
    for name in (
        "get",
        "set",
        "delete",
        "exists",
        "eval",
        "srem",
        "incr",
        "expire",
        "smembers",
    ):
        setattr(client, name, AsyncMock(side_effect=failure))
    client.pipeline.return_value.execute = AsyncMock(side_effect=failure)

    async def scan_iter(**_):
        raise failure
        yield  # an async generator

    client.scan_iter = scan_iter
    for name, value in answers.items():
        setattr(client, name, AsyncMock(return_value=value))
    return client


def _session():
    return RedisSessionStore._to_session_context(json.loads(LIVE))


CALLS = [
    pytest.param(lambda s: s.get("s-1"), {}, id="get"),
    pytest.param(lambda s: s.set("s-1", {"a": 1}), {}, id="set"),
    pytest.param(
        lambda s: s.find_by_user_and_client("u-1", "c-1"),
        {},
        id="find_by_user_and_client",
    ),
    pytest.param(
        lambda s: s.index_session_by_client("u-1", "c-1", "s-1", 60),
        {},
        id="index_session_by_client",
    ),
    pytest.param(lambda s: s.increment_counter("n", 60), {}, id="increment_counter"),
    pytest.param(lambda s: s.save(_session()), {}, id="save"),
    pytest.param(
        lambda s: s.mutate_session("s-1", lambda c: c), {}, id="mutate_session-read"
    ),
    pytest.param(
        lambda s: s.mutate_session("s-1", lambda c: c),
        {"get": LIVE},
        id="mutate_session-write",
    ),
    pytest.param(
        lambda s: s.mutate_session("s-1", lambda c: c),
        {"get": EXPIRED},
        id="mutate_session-expired-delete",
    ),
    pytest.param(
        lambda s: s.mutate_session("s-1", lambda c: c),
        {"get": EXPIRED, "eval": 1},
        id="mutate_session-expired-unindex",
    ),
    pytest.param(lambda s: s.list_sessions("u-1"), {}, id="list_sessions"),
    pytest.param(lambda s: s.delete("s-1"), {}, id="delete"),
    pytest.param(lambda s: s.exists("s-1"), {}, id="exists"),
    pytest.param(lambda s: s.extend_ttl("s-1"), {}, id="extend_ttl"),
    pytest.param(
        lambda s: s.delete_keys_holding("*", "c-1"), {}, id="delete_keys_holding"
    ),
    pytest.param(lambda s: s.delete_session("s-1"), {"get": LIVE}, id="delete_session"),
    pytest.param(
        lambda s: s.delete_session("s-1"), {"get": "{}"}, id="delete_session-no-user"
    ),
]


@pytest.mark.parametrize("failure", FAILURES)
@pytest.mark.parametrize(("call", "answers"), CALLS)
async def test_a_driver_failure_is_a_session_store_exception(failure, call, answers):
    store = RedisSessionStore(_client(failure, **answers), session_ttl_seconds=60)

    with pytest.raises(SessionStoreException) as raised:
        await call(store)

    assert raised.value.__cause__ is failure


@pytest.mark.parametrize(
    "call",
    [
        pytest.param(lambda s: s.set("s-1", {"x": object()}), id="set"),
        pytest.param(
            lambda s: s.save(
                _session().model_copy(update={"metadata": {"x": object()}})
            ),
            id="save",
        ),
    ],
)
async def test_a_programming_error_propagates_as_itself(call):
    """``_serialize`` raising inside the wrapped call is a bug, not an outage."""
    client = _client(AssertionError("Redis is never reached"), set=True, eval=1)
    store = RedisSessionStore(client, session_ttl_seconds=60)

    with pytest.raises(TypeError, match="not JSON serializable"):
        await call(store)
