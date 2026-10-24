"""Fixtures for the tests that drive a real ``container.initialize()``."""

import pytest


@pytest.fixture
async def fresh_container():
    """The DI container singleton, reset around the test, and shut down after it.

    These tests fully initialize the singleton every other test in the session
    shares, so they reset it on the way out too. Shutting it down first closes
    what the initialization opened: with service checks on, that is a real
    Redis client whose pool is bound to this test's event loop.
    """
    from faultmaven.container import container

    container.reset()
    yield container
    await container.shutdown()
    container.reset()
