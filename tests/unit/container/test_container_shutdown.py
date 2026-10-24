"""The container closes the Redis client it opened, only that one, and resets.

``register_infrastructure`` builds the shared Redis client, so the container
owns it. A real client's pool is bound to the event loop it was opened on, and
nothing else closed it: the API lifespan, the jobs runner and every test that
initialized a container left it open, which surfaced on the Test Cloud job's
real Redis as ``ResourceWarning: unclosed transport``. The FakeRedis stand-in is
the event loop's client, shared beyond any one container, so shutdown leaves it
alone.

Shutdown also resets the container. Left marked initialized, a second lifespan
in the same process returned early from ``initialize()`` and published no Redis
client at all.
"""

from __future__ import annotations

import asyncio
import logging
from unittest.mock import AsyncMock, Mock

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


def test_reset_keeps_and_rebuilds_every_name_construction_lists(monkeypatch):
    """Construction and ``reset()`` read one table (#2242 item 3). A name added
    to construction beside a second, hand-kept list of what reset keeps was
    deleted by every reset, and its next read failed. Each kept name is built
    again from its factory, so a reset leaves no value of the run before."""
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
    fresh._probe = object()  # what the run before left in it

    fresh.reset()

    assert fresh._probe is probe


@pytest.fixture
def ledger_restored(monkeypatch):
    """The process-global usage ledger, put back after: the teardown uninstalls it."""
    monkeypatch.setattr(
        usage_ledger, "_installed_ledger", usage_ledger.get_installed_usage_ledger()
    )


@pytest.fixture
def stop_serving(monkeypatch, ledger_restored):
    """``_stop_serving``, with protection and composition stubbed out."""
    from faultmaven.bootstrap import lifespan as lifespan_module

    monkeypatch.setattr("faultmaven.api.protection.shutdown_protection", AsyncMock())
    monkeypatch.setattr(lifespan_module, "decompose_application", AsyncMock())
    return lifespan_module._stop_serving


async def test_the_shutdown_ends_protection_then_composition(
    monkeypatch, ledger_restored
):
    """Owned clients first, then composition: each middleware's own client
    before the container's shared one. The database engine is composition's
    to dispose, last (``decompose_application``), not a step after it."""
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

    assert order == ["shutdown_protection", "decompose_application"]


async def test_decompose_closes_the_client_then_unpublishes_then_disposes_the_engine(
    container, monkeypatch, ledger_restored
):
    """The container closes its client while the services that hold it are
    still published; they are taken off ``app.state``; the database engine is
    disposed last, a refused composition's included."""
    from faultmaven.bootstrap.composition import _ABSENT, decompose_application

    app = FastAPI()
    client = _RealClient()
    order = []
    client.aclose.side_effect = lambda: order.append(
        ("container shutdown", "redis_client" in app.state)
    )

    async def _close_database():
        order.append(("close_database", "redis_client" in app.state))

    container.redis_client = client
    app.state.redis_client = client
    monkeypatch.setattr("faultmaven.container.container", container)
    monkeypatch.setattr(
        "faultmaven.infrastructure.persistence.database.close_database",
        _close_database,
    )

    await decompose_application(app, {"redis_client": _ABSENT})

    assert order == [("container shutdown", True), ("close_database", False)]
    assert "redis_client" not in app.state


async def test_the_shutdown_stops_the_config_watcher_before_the_schedulers(
    stop_serving, monkeypatch
):
    """A config reload must not land mid-shutdown, so the watcher is stopped
    before the schedulers are, not later inside the composition's teardown."""
    app = FastAPI()
    watcher = asyncio.create_task(asyncio.sleep(3600))
    app.state.llm_config_watch_task = watcher
    app.state.case_cleanup_scheduler = object()
    watcher_ended_first = []
    monkeypatch.setattr(
        "faultmaven.infrastructure.tasks.stop_case_cleanup_scheduler",
        lambda _scheduler: watcher_ended_first.append(watcher.done()),
    )

    try:
        await stop_serving(app, {})
    finally:
        watcher.cancel()
        await asyncio.wait({watcher})

    assert watcher_ended_first == [True]


async def test_the_shutdown_removes_every_background_handle_it_stops(
    stop_serving, monkeypatch
):
    """A handle left on ``app.state`` names a task or scheduler that is gone,
    and the next lifespan on the app would find it there."""
    app = FastAPI()
    tasks = {
        name: asyncio.create_task(asyncio.sleep(3600))
        for name in (
            "funnel_metrics_task",
            "revocation_watermark_task",
            "loop_lag_task",
        )
    }
    for name, task in tasks.items():
        setattr(app.state, name, task)
    app.state.case_cleanup_scheduler = object()
    app.state.llm_usage_retention_task = object()
    monkeypatch.setattr(
        "faultmaven.infrastructure.tasks.stop_case_cleanup_scheduler", Mock()
    )
    monkeypatch.setattr(
        "faultmaven.infrastructure.tasks.llm_usage_retention."
        "stop_llm_usage_retention_scheduler",
        AsyncMock(),
    )

    await stop_serving(app, {})

    assert all(task.cancelled() for task in tasks.values())
    left = [
        name
        for name in (*tasks, "case_cleanup_scheduler", "llm_usage_retention_task")
        if name in app.state
    ]
    assert left == [], f"stopped, but still on app.state: {left}"


@pytest.mark.parametrize(
    "cancelled_at",
    [
        "faultmaven.api.protection.shutdown_protection",
        "faultmaven.bootstrap.lifespan.stop_config_watcher",
    ],
    ids=["protection shutdown", "early config-watcher stop"],
)
async def test_a_cancellation_during_the_shutdown_still_decomposes_then_propagates(
    stop_serving, monkeypatch, cancelled_at
):
    """A cancellation at any step of the shutdown skipped the composition's
    teardown, leaving the container initialized and its client open."""
    from faultmaven.bootstrap import lifespan as lifespan_module

    monkeypatch.setattr(cancelled_at, AsyncMock(side_effect=asyncio.CancelledError()))

    with pytest.raises(asyncio.CancelledError):
        await stop_serving(FastAPI(), {})

    lifespan_module.decompose_application.assert_awaited_once()


async def test_a_cancellation_while_a_metrics_task_ends_is_not_swallowed(stop_serving):
    """``await task`` under ``except CancelledError`` swallowed a cancellation of
    the shutdown itself together with the task's own: the shutdown is cancelled
    while the funnel collector is still ending, and ends cancelled."""
    from faultmaven.bootstrap import lifespan as lifespan_module

    cancel_seen, release = asyncio.Event(), asyncio.Event()

    async def _slow_to_end():
        try:
            await asyncio.sleep(3600)
        except asyncio.CancelledError:
            cancel_seen.set()
            await release.wait()  # still ending when the shutdown is cancelled
            raise

    app = FastAPI()
    funnel = asyncio.create_task(_slow_to_end())
    app.state.funnel_metrics_task = funnel
    shutdown = asyncio.create_task(stop_serving(app, {}))
    await cancel_seen.wait()
    shutdown.cancel()
    release.set()

    with pytest.raises(asyncio.CancelledError):
        await shutdown

    lifespan_module.decompose_application.assert_awaited_once()
    await asyncio.wait({funnel})
    assert funnel.cancelled()


async def test_the_config_watcher_is_stopped_once_and_taken_off_app_state(caplog):
    """Both the shutdown and the composition's teardown stop the watcher. The
    first takes it off ``app.state``, so the second is a no-op: an error the
    watcher ended with is reported once, not once per stop."""
    from faultmaven.bootstrap.composition import stop_config_watcher

    async def _fails():
        raise ValueError("the watcher blew up")

    app = FastAPI()
    watcher = asyncio.create_task(_fails())
    await asyncio.wait({watcher})
    app.state.llm_config_watch_task = watcher

    with caplog.at_level(logging.WARNING, logger="faultmaven.bootstrap.composition"):
        await stop_config_watcher(app)
        await stop_config_watcher(app)

    assert "llm_config_watch_task" not in app.state
    assert caplog.text.count("the watcher blew up") == 1
