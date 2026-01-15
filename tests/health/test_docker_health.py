"""Docker and infrastructure health checks.

Smoke tests to verify Docker services (Redis, PostgreSQL, etc.) are accessible.
These tests are skipped if services are not running.

NOTE: These tests require enterprise edition with infrastructure services.
"""

import pytest
from sqlalchemy import create_engine, text

# Conditional redis import - only available in enterprise edition
try:
    import redis
    REDIS_AVAILABLE = True
except ImportError:
    redis = None
    REDIS_AVAILABLE = False

pytestmark = [
    pytest.mark.enterprise,
    pytest.mark.skipif(not REDIS_AVAILABLE, reason="Redis not available - requires enterprise edition")
]


@pytest.mark.skipif(
    condition=True, reason="Requires Docker services - run manually when Docker is up"
)
def test_redis_connection():
    """Test Redis connection via Docker.

    Verifies that Redis is accessible at the default Docker host/port.
    This is a smoke test to catch basic connectivity issues.
    
    Requires enterprise edition with redis installed.
    """
    if not REDIS_AVAILABLE:
        pytest.skip("Redis not available - requires enterprise edition")
    try:
        client = redis.Redis(
            host="redis.faultmaven.local",
            port=6379,
            decode_responses=True,
            socket_connect_timeout=5,
        )
        # Ping Redis
        assert client.ping() is True
        # Set and get a test value
        client.set("health_check", "ok", ex=60)
        assert client.get("health_check") == "ok"
        client.delete("health_check")
    except redis.ConnectionError as e:
        pytest.fail(f"Redis connection failed: {e}")


@pytest.mark.skipif(
    condition=True, reason="Requires Docker services - run manually when Docker is up"
)
def test_postgres_connection():
    """Test PostgreSQL connection via Docker.

    Verifies that PostgreSQL is accessible at the default Docker host/port.
    This is a smoke test to catch basic connectivity issues.
    """
    try:
        # Use synchronous engine for simple health check
        engine = create_engine(
            "postgresql://faultmaven:faultmaven@localhost:5432/faultmaven_dev",
            pool_pre_ping=True,
            connect_args={"connect_timeout": 5},
        )
        with engine.connect() as conn:
            result = conn.execute(text("SELECT 1"))
            assert result.scalar() == 1
    except Exception as e:
        pytest.fail(f"PostgreSQL connection failed: {e}")


@pytest.mark.skipif(
    condition=True, reason="Requires Docker services - run manually when Docker is up"
)
def test_docker_compose_services():
    """Test that all expected Docker Compose services are reachable.

    This test verifies connectivity to all infrastructure services
    defined in docker-compose.yml.
    """
    services_status = {}

    # Test Redis (only if available)
    if REDIS_AVAILABLE:
        try:
            client = redis.Redis(
                host="redis.faultmaven.local", port=6379, socket_connect_timeout=2
            )
            services_status["redis"] = client.ping()
        except:
            services_status["redis"] = False
    else:
        services_status["redis"] = False  # Redis not available in community edition

    # Test PostgreSQL
    try:
        engine = create_engine(
            "postgresql://faultmaven:faultmaven@localhost:5432/faultmaven_dev",
            connect_args={"connect_timeout": 2},
        )
        with engine.connect() as conn:
            services_status["postgres"] = conn.execute(text("SELECT 1")).scalar() == 1
    except:
        services_status["postgres"] = False

    # Report results
    failed_services = [name for name, status in services_status.items() if not status]
    if failed_services:
        pytest.fail(f"Services unreachable: {', '.join(failed_services)}")
