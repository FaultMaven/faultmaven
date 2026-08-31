"""Infrastructure smoke checks against the services a deployment configures.

These verify that the Redis and PostgreSQL a deployment *says* it has are
actually reachable and usable. They are smoke tests: their whole value is
failing when the infrastructure is down, so their skip guards deliberately ask
"has this environment been told where the service is?" and never "is the
service up?" — gating on reachability would make them tautologies that can only
run when they are already going to pass.

Until #1257 all three tests here carried ``@pytest.mark.skipif(condition=True,
...)``. A literal condition evaluates nothing, so none of them had ever run, in
CI or anywhere else, and the endpoints they hardcoded (``redis.faultmaven.local``,
a ``faultmaven_dev`` database on localhost) appear nowhere else in the repo. The
third test — ``test_docker_compose_services`` — was dropped rather than repaired:
it asserted the reachability of Redis and PostgreSQL *as Docker Compose
services*, and this repo's ``docker-compose.yml`` defines neither (it runs the
API and Dashboard; Redis is in-process FakeRedis). Its two constituent checks
survive as the two tests below, now reading the endpoints from configuration.
"""

import os
import uuid

import pytest
from sqlalchemy import create_engine, make_url, text

# Conditional redis import - only available in the cloud profile
try:
    import redis

    REDIS_AVAILABLE = True
except ImportError:
    redis = None
    REDIS_AVAILABLE = False

pytestmark = pytest.mark.cloud

# The discrete host/port/db path, which is what the cloud ConfigMap uses. The
# settings default host is the in-cluster service name, so an unset REDIS_HOST
# means "this environment has not been told where Redis is" — which is why the
# gate reads the raw environment rather than the settings default.
_REDIS_HOST = os.environ.get("REDIS_HOST")

# A Postgres smoke check is only meaningful where a Postgres is configured.
# Same condition the `postgres` marker documents.
_DATABASE_URL = os.environ.get("DATABASE_URL", "")
_POSTGRES_CONFIGURED = _DATABASE_URL.startswith("postgresql")


@pytest.mark.skipif(
    not REDIS_AVAILABLE, reason="Redis not available - requires the cloud profile"
)
@pytest.mark.skipif(
    not _REDIS_HOST, reason="REDIS_HOST is unset - no Redis endpoint configured"
)
def test_redis_connection():
    """The configured Redis answers, and round-trips a value.

    Beyond ``PING``: a replica or a Redis that has hit ``maxmemory`` answers
    the ping and refuses the write, which is the failure a deployment actually
    suffers.
    """
    client = redis.Redis(
        host=_REDIS_HOST,
        port=int(os.environ.get("REDIS_PORT", "6379")),
        db=int(os.environ.get("REDIS_DB", "0")),
        decode_responses=True,
        socket_connect_timeout=5,
    )
    # Unique per run: this may be the same database the rest of the suite uses.
    probe_key = f"fm:health_check:{uuid.uuid4()}"
    try:
        assert client.ping() is True
        client.set(probe_key, "ok", ex=60)
        assert client.get(probe_key) == "ok"
    except redis.ConnectionError as e:
        pytest.fail(f"Redis connection failed at {_REDIS_HOST}: {e}")
    finally:
        try:
            client.delete(probe_key)
        except redis.RedisError:
            pass


@pytest.mark.skipif(
    not _POSTGRES_CONFIGURED,
    reason="DATABASE_URL is not a PostgreSQL URL - no Postgres configured",
)
def test_postgres_connection():
    """The configured PostgreSQL accepts a connection and answers a query."""
    # DATABASE_URL carries the app's async driver (postgresql+asyncpg://).
    # This check is synchronous, so drop the driver and let SQLAlchemy pick the
    # sync default rather than handing asyncpg to create_engine().
    url = make_url(_DATABASE_URL).set(drivername="postgresql")

    engine = create_engine(
        url,
        pool_pre_ping=True,
        connect_args={"connect_timeout": 5},
    )
    try:
        with engine.connect() as conn:
            assert conn.execute(text("SELECT 1")).scalar() == 1
    except Exception as e:
        pytest.fail(f"PostgreSQL connection failed: {e}")
    finally:
        engine.dispose()
