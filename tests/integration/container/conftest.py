"""Fixtures for the tests that drive a real ``container.initialize()``."""

import pytest


@pytest.fixture
async def fresh_container():
    """The DI container singleton, reset around the test, and shut down after it.

    These tests fully initialize the singleton every other test in the session
    shares, so they release every hold they took on the way out, and the last
    release closes what the compositions opened and resets the container: with
    service checks on, the KB and evidence ChromaDB clients, and a real Redis
    client whose pool is bound to this test's event loop.
    """
    from faultmaven.container import container

    container.reset()
    yield container
    while not await container.shutdown():
        pass
