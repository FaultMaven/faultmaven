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


def _boot_and_read_composition():
    from faultmaven.container import container
    from faultmaven.main import app

    broker = _RealAppBoot()
    try:
        broker.client()
        return app.state.redis_client, container.session_store
    finally:
        broker.release()


def test_a_second_boot_publishes_a_redis_client_of_its_own_composition():
    first_redis, first_store = _boot_and_read_composition()
    second_redis, second_store = _boot_and_read_composition()

    assert first_redis is not None
    assert second_redis is not None, (
        "the second boot published no Redis client: the container was still "
        "marked initialized after the first boot's shutdown"
    )
    # Composed again, not handed the first boot's objects: the session store is
    # built over the client the second initialize resolved.
    assert second_store is not first_store
    assert second_store.redis_client is second_redis
