"""Fixtures for the tests that drive a real ``container.initialize()``."""

import pytest


@pytest.fixture
async def fresh_container():
    """The DI container singleton, reset around the test, and shut down after it.

    These tests fully initialize the singleton every other test in the session
    shares, so they end every hold they took on the way out, and the shutdown
    closes what the compositions opened and resets the container: with
    service checks on, the KB and evidence ChromaDB clients, and a real Redis
    client whose pool is bound to this test's event loop.
    """
    from faultmaven.container import container

    container.reset()
    yield container
    await container.release_every_hold()


@pytest.fixture
def environment(monkeypatch):
    """Compose under the given ``ENVIRONMENT``; the settings singleton is rebuilt
    from it and again afterwards."""
    from faultmaven.config.settings import get_settings, reset_settings

    def _set(value: str):
        monkeypatch.setenv("ENVIRONMENT", value)
        reset_settings()
        return get_settings()

    yield _set
    monkeypatch.undo()
    reset_settings()


@pytest.fixture
def boom(monkeypatch):
    error = RuntimeError("boom")

    def _break(target: str):
        def _raise(*_args, **_kwargs):
            raise error

        monkeypatch.setattr(f"{target}.__init__", _raise)
        return error

    return _break
