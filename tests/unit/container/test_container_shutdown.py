"""The container closes the Redis client it opened, only that one, and resets.

``register_infrastructure`` builds the shared Redis client, so the container
owns it. A real client's pool is bound to the event loop it was opened on, and
nothing else closed it: the API lifespan, the jobs runner and every test that
initialized a container left it open, which surfaced on the Test Cloud job's
real Redis as ``ResourceWarning: unclosed transport``. The FakeRedis stand-in is
a process-wide singleton shared beyond any one container, so shutdown leaves it
alone.

Shutdown also resets the container. Left marked initialized, a second lifespan
in the same process returned early from ``initialize()`` and published no Redis
client at all.
"""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest
from fastapi import FastAPI

from faultmaven.container import DIContainer
from faultmaven.infrastructure.llm import usage_ledger
from faultmaven.infrastructure.redis_client import get_fakeredis_client

pytestmark = pytest.mark.unit


def _constructed_names() -> set[str]:
    return {name for name, _factory in DIContainer._CONSTRUCTED_STATE}


@pytest.fixture
def container():
    """A container of this test's own; the process singleton is untouched."""
    instance = object.__new__(DIContainer)
    # What BaseDIContainer.__new__ gives the singleton, from the table it builds from.
    for name, factory in DIContainer._CONSTRUCTED_STATE:
        setattr(instance, name, factory())
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
    # Nothing hands the closed client out again.
    assert getattr(container, "redis_client", None) is None
    assert container.get_service("redis_client") is None


async def test_shutdown_leaves_the_container_re_initializable(container):
    """``initialize()`` returns early while ``_initialized`` is set, so a
    container left marked initialized never composes a new client."""
    container._initialized = True
    container._initializing = False
    container.redis_client = _RealClient()

    await container.shutdown()

    assert container._initialized is False
    assert container._initializing is False


async def test_a_close_that_raises_still_resets(container):
    """Otherwise the container stays marked initialized, and the next
    ``initialize()`` returns early and publishes no client."""
    client = _RealClient()
    client.aclose.side_effect = RuntimeError("the pool would not close")
    container._register_service("redis_client", client)
    container.redis_client = client
    container._initialized = True

    with pytest.raises(RuntimeError, match="the pool would not close"):
        await container.shutdown()

    assert container._initialized is False
    assert container.get_service("redis_client") is None


async def test_reset_drops_every_cache_and_attribute_composition_left(container):
    """A cache left behind hands the next composition a service built over the
    previous one's closed client: ``get_job_service`` caches behind
    ``hasattr``, and tools, adapters and extractors are plain attributes."""
    client = _RealClient()
    container._register_service("redis_client", client)
    container.redis_client = client
    job_service = container.get_job_service()
    minimal_case_service = container._create_minimal_case_service()
    container.kb_qa_tool = object()
    container.logs_extractor = object()
    assert job_service is not None and job_service.redis_client is client

    container.reset()

    assert set(vars(container)) <= _constructed_names()
    replacement = _RealClient()
    container._register_service("redis_client", replacement)
    container.redis_client = replacement
    assert container.get_job_service() is not job_service
    assert container.get_job_service().redis_client is replacement
    assert container._create_minimal_case_service() is not minimal_case_service


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


@pytest.fixture
def fresh_singleton(monkeypatch):
    """A singleton built by the real ``__new__``; the process one put back after."""
    monkeypatch.setattr(DIContainer, "_instance", None)
    return DIContainer()


def test_reset_keeps_exactly_what_construction_set(fresh_singleton):
    """The names construction set survive a reset, and nothing else does.

    Holds before and after #2242: the hand-kept set agreed with ``__new__``
    then. Pinned so that moving construction to one table keeps it.
    """
    constructed = set(vars(fresh_singleton))
    fresh_singleton._initialized = True
    fresh_singleton._register_service("redis_client", _RealClient())
    fresh_singleton.kb_qa_tool = object()
    fresh_singleton.settings = object()

    fresh_singleton.reset()

    assert set(vars(fresh_singleton)) == constructed
    assert fresh_singleton.settings is None
    assert fresh_singleton.is_initialized is False


def test_a_name_added_to_construction_is_kept_by_reset(monkeypatch):
    """Construction and ``reset()`` read one table (#2242 item 3). A name added
    to construction beside a second, hand-kept list of what reset keeps was
    deleted by every reset, and its next read failed."""
    probe = object()
    monkeypatch.setattr(
        DIContainer,
        "_CONSTRUCTED_STATE",
        DIContainer._CONSTRUCTED_STATE + (("_probe", lambda: probe),),
    )
    monkeypatch.setattr(DIContainer, "_instance", None)
    fresh = DIContainer()
    assert fresh._probe is probe
    fresh._register_service("redis_client", _RealClient())

    fresh.reset()

    assert fresh._probe is probe


@pytest.fixture
def ledger_restored(monkeypatch):
    """The process-global usage ledger, put back after: the teardown uninstalls it."""
    monkeypatch.setattr(
        usage_ledger, "_installed_ledger", usage_ledger.get_installed_usage_ledger()
    )


async def test_the_shutdown_ends_protection_then_composition_then_the_engine(
    monkeypatch, ledger_restored
):
    """Owned clients first, then composition (the container's shared client is
    closed there), then the engine: each middleware's own client before the
    container's, and both before the database engine is disposed."""
    from faultmaven.bootstrap import lifespan as lifespan_module

    order = []

    def _recording(name):
        async def _record(*_args, **_kwargs):
            order.append(name)

        return _record

    monkeypatch.setattr(
        "faultmaven.api.protection.shutdown_protection",
        _recording("shutdown_protection"),
    )
    monkeypatch.setattr(
        lifespan_module, "decompose_application", _recording("decompose_application")
    )
    monkeypatch.setattr(
        "faultmaven.infrastructure.persistence.database.close_database",
        _recording("close_database"),
    )

    await lifespan_module._stop_serving(FastAPI(), {})

    assert order == ["shutdown_protection", "decompose_application", "close_database"]


async def test_composition_is_unpublished_only_after_the_container_shut_down(
    container, monkeypatch, ledger_restored
):
    """The container closes its client while the services that hold it are
    still published, and only then are they taken off ``app.state``."""
    from faultmaven.bootstrap.composition import _ABSENT, decompose_application

    app = FastAPI()
    client = _RealClient()
    published_while_closing = []
    client.aclose.side_effect = lambda: published_while_closing.append(
        "redis_client" in app.state
    )
    container.redis_client = client
    app.state.redis_client = client
    monkeypatch.setattr("faultmaven.container.container", container)

    await decompose_application(app, {"redis_client": _ABSENT})

    assert published_while_closing == [True]
    assert "redis_client" not in app.state
