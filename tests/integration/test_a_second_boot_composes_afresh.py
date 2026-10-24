"""A second lifespan in one process composes the container afresh.

The lifespan's shutdown closes the container's Redis client. When that left the
container marked initialized, the next lifespan in the process returned early
from ``initialize()`` and published ``app.state.redis_client = None``. The
second module's shared boot then reported ``session_storage: "not initialized"``,
keyed turns skipped their claim, and the protection middleware built clients
of its own. Production runs one lifespan per process; the suite runs many.

Two shared boots of the real app, one after the other, the way two modules
borrowing ``booted_app_client`` run on one worker.
"""

import pytest

from tests.conftest import _RealAppBoot

pytestmark = pytest.mark.integration

# What the composition root publishes that holds the container's Redis client,
# directly or through a store, a knowledge service or an investigation engine.
_HOLD_THE_REDIS_CLIENT = (
    "redis_client",
    "job_service",
    "session_service",
    "user_service",
    "case_service",
    "data_service",
    "knowledge_service",
    "conversion_service",
    "investigation_service",
    "enhanced_agent_service",
)


def _boot_and_read_composition():
    from faultmaven.container import container
    from faultmaven.main import app

    broker = _RealAppBoot()
    try:
        broker.client()
        published = {
            name: getattr(app.state, name, None) for name in _HOLD_THE_REDIS_CLIENT
        }
        composed = (app.state.redis_client, container.session_store)
    finally:
        broker.release()
    left_published = [name for name in _HOLD_THE_REDIS_CLIENT if name in app.state]
    return composed, published, left_published


def test_a_second_boot_publishes_a_redis_client_of_its_own_composition():
    (first_redis, first_store), first, _ = _boot_and_read_composition()
    (second_redis, second_store), second, _ = _boot_and_read_composition()

    assert first_redis is not None
    assert second_redis is not None, (
        "the second boot published no Redis client: the container was still "
        "marked initialized after the first boot's shutdown"
    )
    # Composed again, not handed the first boot's objects: the session store is
    # built over the client the second initialize resolved.
    assert second_store is not first_store
    assert second_store.redis_client is second_redis


def test_a_second_boot_builds_its_own_job_service():
    """``get_job_service`` caches what it builds; the cache goes with the reset."""
    _, first, _ = _boot_and_read_composition()
    _, second, _ = _boot_and_read_composition()

    assert second["job_service"] is not first["job_service"]
    assert second["job_service"].redis_client is second["redis_client"]


def test_a_boot_takes_back_what_it_published_when_it_ends():
    """Nothing left on ``app.state`` names the client the shutdown closed."""
    _, published, left_published = _boot_and_read_composition()

    assert all(published[name] is not None for name in ("redis_client", "job_service"))
    assert left_published == []


def test_every_boot_in_a_process_serves_from_a_working_redis():
    """Each boot's Redis answers on that boot's own event loop (#2241).

    Every ``TestClient`` runs the app on a loop of its own. Under the
    ``SKIP_SERVICE_CHECKS`` the CI jobs set, the container's Redis is the FakeRedis
    stand-in, which was one client for the whole process. Under the cloud
    lockfile's hiredis parser, every boot after the first was handed connections
    bound to the first boot's loop. Its session writes raised, so ``dev-login``
    answered 500, and every other Redis call failed with nothing reporting it.
    """
    from faultmaven.main import app

    answers = []
    for _ in range(3):
        broker = _RealAppBoot()
        try:
            client = broker.client()
            login = client.post("/api/v1/auth/dev-login", json={"username": "admin"})
            pinged = client.portal.call(app.state.redis_client.ping)
            answers.append((login.status_code, pinged))
        finally:
            broker.release()

    assert answers == [(200, True)] * 3
