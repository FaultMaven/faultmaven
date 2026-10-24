"""The container closes the Redis client it opened, and only that one.

``register_infrastructure`` builds the shared Redis client, so the container
owns it. A real client's pool is bound to the event loop it was opened on, and
nothing else closed it: the API lifespan, the jobs runner and every test that
initialized a container left it open, which surfaced on the Test Cloud job's
real Redis as ``ResourceWarning: unclosed transport``. The FakeRedis stand-in is
a process-wide singleton shared beyond any one container, so shutdown leaves it
alone.
"""

from __future__ import annotations

import logging
from unittest.mock import AsyncMock

import pytest

from faultmaven.container import DIContainer
from faultmaven.container.registry import DependencyRegistry
from faultmaven.infrastructure.redis_client import get_fakeredis_client

pytestmark = pytest.mark.unit


@pytest.fixture
def container():
    """A container of this test's own; the process singleton is untouched."""
    instance = object.__new__(DIContainer)
    instance._registry = DependencyRegistry()
    instance._logger = logging.getLogger(__name__)
    yield instance


class _RealClient:
    """Stands in for ``redis.asyncio.Redis``: anything not from ``fakeredis``."""

    def __init__(self):
        self.aclose = AsyncMock()


async def test_shutdown_closes_the_real_client_it_opened(container):
    client = _RealClient()
    container._register_service("redis_client", client)
    container.redis_client = client

    await container.shutdown()

    client.aclose.assert_awaited_once()
    # Terminal: nothing hands the closed client out again.
    assert getattr(container, "redis_client", None) is None
    assert container.get_service("redis_client") is None


async def test_shutdown_is_idempotent(container):
    client = _RealClient()
    container.redis_client = client

    await container.shutdown()
    await container.shutdown()

    client.aclose.assert_awaited_once()


async def test_shutdown_leaves_the_shared_fakeredis_open(container, monkeypatch):
    stand_in = get_fakeredis_client()
    closed = AsyncMock()
    monkeypatch.setattr(stand_in, "aclose", closed)
    container.redis_client = stand_in

    await container.shutdown()

    closed.assert_not_awaited()
    assert get_fakeredis_client() is stand_in


async def test_shutdown_without_a_client_does_nothing(container):
    await container.shutdown()

    assert getattr(container, "redis_client", None) is None


def test_the_api_lifespan_shuts_the_container_down_after_serving():
    """The API process is the container's owner: its lifespan awaits
    ``container.shutdown()`` in the shutdown half, after the ``yield``."""
    import ast
    import inspect

    from faultmaven.bootstrap import lifespan as lifespan_module

    tree = ast.parse(inspect.getsource(lifespan_module.lifespan))
    body = tree.body[0].body
    after_yield = body[
        next(
            i
            for i, node in enumerate(body)
            if isinstance(node, ast.Expr) and isinstance(node.value, ast.Yield)
        )
        + 1 :
    ]
    awaited = {
        ast.unparse(node.value.func)
        for statement in after_yield
        for node in ast.walk(statement)
        if isinstance(node, ast.Await) and isinstance(node.value, ast.Call)
    }
    assert "container.shutdown" in awaited, sorted(awaited)
