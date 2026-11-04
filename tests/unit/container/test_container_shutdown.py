"""The container closes what its compositions own, once, on the last holder's shutdown.

A provider records each resource it opens with ``container.own``. Before,
``shutdown()`` closed a hand-kept list naming only the Redis client, and the
rest stayed open until a symptom found it (#2490). A second lifespan's
``initialize()`` returns early and shares the container: each holds it, in its
caller's context, and only the shutdown that leaves no hold closes anything
(#1843).

Shutdown also resets the container. Left marked initialized, a second lifespan
in the same process returned early from ``initialize()`` and published no Redis
client at all. A reset over what is still owned is refused: nothing could close
it afterwards.
"""

from __future__ import annotations

import asyncio
import contextvars
import logging
import sys
from contextlib import contextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

import pytest
from fastapi import FastAPI

from faultmaven.container import DIContainer
from faultmaven.container.errors import ContainerError
from faultmaven.infrastructure.llm import usage_ledger
from tests.unit.container.conftest import make_private_container

pytestmark = pytest.mark.unit


def _constructed_names() -> set[str]:
    return {name for name, _factory in DIContainer._CONSTRUCTED_STATE}


@pytest.fixture
async def container():
    """A container of this test's own; the process singleton is untouched.

    Whatever the test left owned or held is ended after it."""
    instance = make_private_container()
    yield instance
    await instance.release_every_hold()


class _RealClient:
    """Stands in for ``redis.asyncio.Redis``: anything not from ``fakeredis``."""

    def __init__(self):
        self.aclose = AsyncMock()


def _own(container, client, what="the Redis client"):
    """Record ``client`` as the Redis registration does."""
    return container.own(client, close=lambda c: c.aclose(), what=what)


async def _hold(container, holders):
    """Take ``holders`` holds on ``container`` in this context, as lifespans
    nested in one coroutine do: each finds it initialized and shares it."""
    container._initialized = True
    for _ in range(holders):
        await container.initialize()


def _elsewhere(coroutine):
    """Run ``coroutine`` in a context that took no hold."""
    return asyncio.create_task(coroutine, context=contextvars.Context())


@contextmanager
def _composing(register_infrastructure):
    """``initialize()`` with its providers stubbed: ``register_infrastructure``
    is what a composition does."""
    with (
        patch(
            "faultmaven._container_impl.register_infrastructure",
            register_infrastructure,
        ),
        patch("faultmaven._container_impl.register_tools"),
        patch("faultmaven._container_impl.register_services"),
    ):
        yield


async def test_the_last_shutdown_closes_what_is_owned_newest_first(container):
    closed = []
    for what in ("first", "second", "third"):
        client = _own(container, _RealClient(), what)
        client.aclose.side_effect = lambda what=what: closed.append(what)

    assert await container.shutdown() is True

    assert closed == ["third", "second", "first"]
    assert container.is_initialized is False


async def test_shutdown_leaves_the_container_re_initializable(container):
    """``initialize()`` returns early while ``_initialized`` is set, so a
    container left marked initialized never composes a new client."""
    container._initialized = True
    container._initializing = False
    container.redis_client = _RealClient()

    await container.shutdown()

    assert container._initialized is False
    assert container._initializing is False


async def test_a_failed_close_is_logged_and_every_other_still_closes(container, caplog):
    """Through ``run_teardown``'s rules: a close that raises is that close's
    failure alone, and the container is still reset."""
    first, failing, last = _RealClient(), _RealClient(), _RealClient()
    _own(container, first, "the first client")
    _own(container, failing, "the Redis client")
    _own(container, last, "the last client")
    failing.aclose.side_effect = RuntimeError("the pool would not close")
    container._initialized = True

    with caplog.at_level(
        logging.WARNING, logger="faultmaven.infrastructure.concurrency.teardown"
    ):
        assert await container.shutdown() is True

    assert "Closing the Redis client failed" in caplog.text
    assert "the pool would not close" in caplog.text
    first.aclose.assert_awaited_once()
    last.aclose.assert_awaited_once()
    assert container._initialized is False


async def test_an_interrupted_close_still_closes_the_rest_and_resets(container):
    """The interruption is held until every close has run, then raised."""
    first, interrupted = _RealClient(), _RealClient()
    _own(container, first, "the first client")
    _own(container, interrupted, "the interrupted client")
    interrupted.aclose.side_effect = asyncio.CancelledError()
    container._initialized = True

    with pytest.raises(asyncio.CancelledError):
        await container.shutdown()

    first.aclose.assert_awaited_once()
    assert container._initialized is False
    container.reset()  # nothing is left owned


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
    client = _own(container, _RealClient())

    assert await container.shutdown() is True
    assert await container.shutdown() is True

    client.aclose.assert_awaited_once()


async def test_shutdown_without_a_client_does_nothing(container):
    assert await container.shutdown() is True

    assert getattr(container, "redis_client", None) is None


async def test_a_shutdown_that_is_not_the_last_closes_nothing(container):
    """#1843: two lifespans nested in one coroutine hold one container twice.
    The first to end closes nothing; the last closes everything, once."""
    await _hold(container, 2)
    client = _own(container, _RealClient())

    assert await container.shutdown() is False
    client.aclose.assert_not_awaited()
    assert container.is_initialized is True

    assert await container.shutdown() is True
    client.aclose.assert_awaited_once()
    assert container.is_initialized is False


async def test_a_shutdown_with_no_holder_leaves_none_owed(container):
    """At no holder a shutdown is a no-op, not a debt: two of them, then two
    holders, and the first holder's shutdown is still not the last."""
    assert await container.shutdown() is True
    assert await container.shutdown() is True
    await _hold(container, 2)
    client = _own(container, _RealClient())

    assert await container.shutdown() is False
    client.aclose.assert_not_awaited()


async def test_a_context_that_holds_nothing_closes_nothing_beside_a_holder(
    container,
):
    """A hold is its caller's context's. A composer that never initialized
    released another's, and closed what that one still served from."""
    await _hold(container, 1)
    client = _own(container, _RealClient())

    assert await _elsewhere(container.shutdown()) is False
    client.aclose.assert_not_awaited()
    assert await container.shutdown() is True
    client.aclose.assert_awaited_once()


@pytest.mark.parametrize("composes", [True, False], ids=["beside", "alone"])
async def test_a_call_that_finds_the_container_initializing_holds_nothing(
    container, composes
):
    """It gets an uninitialized container back. Beside the composition, once
    that holds it, its composer's shutdown closes nothing; if that composition
    fails, nothing holds, and it closes what that one opened."""
    composing, client = asyncio.Event(), _RealClient()

    async def _register(c):
        _own(c, client)
        await composing.wait()
        if not composes:
            raise ValueError("planted part-way failure")

    with _composing(_register):
        first = asyncio.create_task(container.initialize(allow_degraded=True))
        await asyncio.sleep(0)
        await container.initialize()
        assert container.is_initialized is False
        composing.set()
        await first

    assert await container.shutdown() is not composes
    assert client.aclose.await_count == (0 if composes else 1)


@pytest.mark.parametrize("beside", [True, False], ids=["beside", "alone"])
async def test_a_degraded_call_holds_nothing(container, beside):
    """The lenient path returns it uninitialized. Beside a composition that holds
    it, the degraded composer's shutdown closes nothing; alone, it closes what
    the degraded composition opened."""
    degraded, serving = _RealClient(), _RealClient()
    built = iter([degraded, serving])

    async def _register(c):
        if _own(c, next(built)) is degraded:
            raise ValueError("planted part-way failure")

    with _composing(_register):
        await _elsewhere(container.initialize(allow_degraded=True))
        if beside:
            await container.initialize()

    assert await _elsewhere(container.shutdown()) is not beside
    assert degraded.aclose.await_count == (0 if beside else 1)
    if beside:
        assert await container.shutdown() is True
        degraded.aclose.assert_awaited_once()
        serving.aclose.assert_awaited_once()


async def test_a_composition_begun_during_the_last_closes_keeps_its_hold(container):
    """The last release resets before it awaits its closes. Reset after them,
    a composition begun meanwhile shared clients being closed, and the reset
    erased its hold."""
    gate = asyncio.Event()

    async def _slow_close(_resource):
        await gate.wait()

    await _hold(container, 1)
    container.own(object(), close=_slow_close, what="a Redis pool draining")
    closing = asyncio.create_task(container.shutdown())
    await asyncio.sleep(0)
    assert container.is_initialized is False
    with _composing(AsyncMock()):
        await container.initialize()
    gate.set()

    assert await closing is True
    assert container.is_initialized is True
    assert await _elsewhere(container.shutdown()) is False


async def test_a_reset_voids_every_hold(container):
    await _hold(container, 1)
    container.reset()

    assert await _elsewhere(container.shutdown()) is True


async def test_a_private_containers_shutdown_leaves_the_singleton_alone(
    container, fresh_singleton
):
    """The record and the holds were the class's, so this closed the
    singleton's clients and zeroed its holders."""
    client = _own(fresh_singleton, _RealClient())
    await _hold(fresh_singleton, 1)

    assert await container.shutdown() is True
    client.aclose.assert_not_awaited()
    assert await fresh_singleton.shutdown() is True
    client.aclose.assert_awaited_once()


async def test_a_reset_is_refused_while_anything_is_owned(container, monkeypatch):
    """A reset drops what it is not handed back: the fixture setup that reset
    over an earlier test's open composition left its clients unclosed."""
    monkeypatch.setattr(DIContainer, "_instance", container)
    client = _own(container, _RealClient(), "the KB ChromaDB client")
    container._initialized = True

    with pytest.raises(ContainerError, match="the KB ChromaDB client"):
        container.reset()
    with pytest.raises(ContainerError, match="the KB ChromaDB client"):
        DIContainer.reset_singleton()

    assert container.is_initialized is True
    await container.shutdown()
    client.aclose.assert_awaited_once()
    container.reset()


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

    _own(container, client)
    app.state.redis_client = client
    monkeypatch.setattr("faultmaven.container.container", container)
    monkeypatch.setattr(
        "faultmaven.infrastructure.persistence.database.close_database",
        _close_database,
    )

    await decompose_application(app, {"redis_client": _ABSENT})

    assert order == [("container shutdown", True), ("close_database", False)]
    assert "redis_client" not in app.state


async def test_the_last_release_writes_what_was_metered_before_the_engine_goes(
    container, monkeypatch, ledger_restored
):
    """The ledger is the process's, installed until the last release, so a call
    metered after the lifespan's own drain schedules a write. The ledger is
    uninstalled, then drained, so nothing is written against a closed engine."""
    from faultmaven.bootstrap.composition import decompose_application

    order = []
    install = usage_ledger.install_usage_ledger
    monkeypatch.setattr(
        usage_ledger,
        "install_usage_ledger",
        lambda ledger: (order.append("uninstall"), install(ledger)),
    )

    async def _write():
        for _ in range(100):  # still writing when the steps before it end
            await asyncio.sleep(0)
        order.append("write")

    async def _close_database():
        order.append("close_database")

    monkeypatch.setattr(
        usage_ledger, "_pending_writes", {asyncio.create_task(_write())}
    )
    monkeypatch.setattr("faultmaven.container.container", container)
    monkeypatch.setattr(
        "faultmaven.infrastructure.persistence.database.close_database",
        _close_database,
    )

    await decompose_application(FastAPI(), {})

    assert order == ["uninstall", "write", "close_database"]


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
            "llm_usage_retention_task",
        )
    }
    for name, task in tasks.items():
        setattr(app.state, name, task)
    app.state.case_cleanup_scheduler = object()
    monkeypatch.setattr(
        "faultmaven.infrastructure.tasks.stop_case_cleanup_scheduler", Mock()
    )

    await stop_serving(app, {})

    assert all(task.cancelled() for task in tasks.values())
    left = [name for name in (*tasks, "case_cleanup_scheduler") if name in app.state]
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


#: The background tasks ``_start_serving`` puts on ``app.state`` and the
#: shutdown stops with ``cancel_and_wait``.
_METRICS_TASKS = ("funnel_metrics_task", "revocation_watermark_task", "loop_lag_task")


async def _cancel_the_shutdown_while_a_task_ends(stop_serving, handle):
    """Run the shutdown over a task, published as ``handle``, that is still
    ending when the shutdown itself is cancelled. Returns the app, the task
    and the shutdown's own task, once it has ended."""
    cancel_seen, release = asyncio.Event(), asyncio.Event()

    async def _slow_to_end():
        try:
            await asyncio.sleep(3600)
        except asyncio.CancelledError:
            cancel_seen.set()
            await release.wait()  # still ending when the shutdown is cancelled
            raise

    app = FastAPI()
    task = asyncio.create_task(_slow_to_end())
    setattr(app.state, handle, task)
    shutdown = asyncio.create_task(stop_serving(app, {}))
    await cancel_seen.wait()
    shutdown.cancel()
    release.set()
    await asyncio.wait({shutdown})
    await asyncio.wait({task})
    return app, task, shutdown


@pytest.mark.parametrize("handle", _METRICS_TASKS)
async def test_a_cancellation_while_a_metrics_task_ends_is_not_swallowed(
    stop_serving, handle
):
    """``await task`` under ``except CancelledError`` swallowed a cancellation of
    the shutdown itself together with the task's own: the shutdown is cancelled
    while the metrics task is still ending, and ends cancelled."""
    from faultmaven.bootstrap import lifespan as lifespan_module

    _app, task, shutdown = await _cancel_the_shutdown_while_a_task_ends(
        stop_serving, handle
    )

    assert shutdown.cancelled(), "the shutdown's cancellation was swallowed"
    lifespan_module.decompose_application.assert_awaited_once()
    assert task.cancelled()


@pytest.mark.parametrize("handle", _METRICS_TASKS)
async def test_a_cancellation_during_a_metrics_task_stop_leaves_no_handle(
    stop_serving, handle
):
    """The handle comes off ``app.state`` before the wait. Left on it, a later
    stop would be handed a task of this lifespan, whose loop may have closed."""
    app, _task, _shutdown = await _cancel_the_shutdown_while_a_task_ends(
        stop_serving, handle
    )

    assert handle not in app.state, f"{handle} left on app.state"


#: The shutdown's nine stops, in order: a background task by its handle on
#: ``app.state``, any other stop by what it stops.
_STOPS = (
    "protection",
    "ledger drain",
    *_METRICS_TASKS,
    "llm_config_watch_task",
    "case_cleanup_scheduler",
    "llm_usage_retention_task",
    "monitoring",
)
_TASK_HANDLES = (*_METRICS_TASKS, "llm_config_watch_task", "llm_usage_retention_task")
#: The stops with no await of their own: the cancellation is raised from them.
_RAISED_FROM = ("case_cleanup_scheduler",)


@pytest.mark.parametrize("cancelled_during", _STOPS)
async def test_a_cancellation_during_any_stop_still_runs_every_stop(
    monkeypatch, ledger_restored, cancelled_during
):
    """#2257: a cancellation during one stop skipped every stop after it, so the
    later tasks were left running with their handles on ``app.state``.

    Every stop is the real one. The shutdown is cancelled while the stop under
    test waits on what it stops; a stop with no await raises the cancellation
    instead. Every stop then runs, in order, and the cancellation propagates.
    A stop that swallows it fails here, as the retention stop did."""
    from faultmaven.api import protection
    from faultmaven.bootstrap import lifespan as lifespan_module
    from faultmaven.infrastructure import tasks as background_tasks
    from faultmaven.infrastructure.observability.apm_integration import (
        APMConfiguration,
        APMProvider,
        apm_integration,
    )

    ran = []
    in_progress, release = asyncio.Event(), asyncio.Event()

    async def _hangs(*_args):
        await asyncio.Event().wait()

    async def _background(handle):
        try:
            await asyncio.sleep(3600)
        except asyncio.CancelledError:
            ran.append(handle)
            if handle == cancelled_during:
                in_progress.set()
                await release.wait()  # still ending when the shutdown is cancelled
            raise

    def _record(owner, name, stop):
        """Record ``stop`` when ``owner.name`` is called, and call the real one."""
        real = getattr(owner, name)

        def _recording(*args, **kwargs):
            ran.append(stop)
            if stop != cancelled_during:
                return real(*args, **kwargs)
            in_progress.set()
            return real(*args, **kwargs)

        monkeypatch.setattr(owner, name, _recording)

    middleware = SimpleNamespace(shutdown=AsyncMock())
    if cancelled_during == "protection":
        middleware.shutdown = _hangs
    monkeypatch.setattr(
        protection, "live_protection_middleware", lambda _: [middleware]
    )
    writes = (
        {asyncio.create_task(_hangs())} if cancelled_during == "ledger drain" else set()
    )
    monkeypatch.setattr(usage_ledger, "_pending_writes", set(writes))
    usage_ledger.install_usage_ledger(usage_ledger.InMemoryUsageLedger())
    scheduler = Mock()
    if cancelled_during == "case_cleanup_scheduler":
        scheduler.shutdown.side_effect = asyncio.CancelledError()
    if cancelled_during == "monitoring":
        monkeypatch.setattr(apm_integration, "metrics_buffer", [object()])
        monkeypatch.setattr(
            apm_integration,
            "configurations",
            {APMProvider.GENERIC: APMConfiguration(provider=APMProvider.GENERIC)},
        )
        monkeypatch.setattr(
            apm_integration, "export_callbacks", {APMProvider.GENERIC: _hangs}
        )
    _record(protection, "shutdown_protection", "protection")
    _record(usage_ledger, "drain_pending_usage_writes", "ledger drain")
    _record(background_tasks, "stop_case_cleanup_scheduler", "case_cleanup_scheduler")
    _record(apm_integration, "flush_metrics", "monitoring")
    decompose = AsyncMock()
    monkeypatch.setattr(lifespan_module, "decompose_application", decompose)
    app = FastAPI()
    tasks = {
        handle: asyncio.create_task(_background(handle)) for handle in _TASK_HANDLES
    }
    for handle, task in tasks.items():
        setattr(app.state, handle, task)
    app.state.case_cleanup_scheduler = scheduler
    await asyncio.sleep(0)  # let the tasks start

    shutdown = asyncio.create_task(lifespan_module._stop_serving(app, {}))
    try:
        if cancelled_during not in _RAISED_FROM:
            reached = asyncio.ensure_future(in_progress.wait())
            await asyncio.wait({reached, shutdown}, return_when=asyncio.FIRST_COMPLETED)
            reached.cancel()
            shutdown.cancel()
            release.set()
        await asyncio.wait({shutdown})
        stopped = list(ran)
    finally:
        for task in (*tasks.values(), *writes):
            task.cancel()
        await asyncio.wait({*tasks.values(), *writes})

    assert shutdown.cancelled(), "the cancellation was swallowed"
    assert stopped == list(_STOPS), "a stop was skipped, or ran out of order"
    left = [h for h in (*_TASK_HANDLES, "case_cleanup_scheduler") if h in app.state]
    # A cancelled watcher stop leaves its handle for decompose_application's
    # own stop of it (``stop_config_watcher``), which is stubbed here.
    watcher = "llm_config_watch_task"
    watcher_left = [watcher] if cancelled_during == watcher else []
    assert left == watcher_left, f"still on app.state: {left}"
    scheduler.shutdown.assert_called_once()
    # The process's: the decomposition uninstalls it on the last release.
    assert usage_ledger.get_installed_usage_ledger() is not None
    decompose.assert_awaited_once()


#: Each module a serving stop imports, and the stops that need it.
_SERVING_STOP_IMPORTS = {
    "faultmaven.infrastructure.tasks": ("Stopping the case cleanup scheduler",),
    "faultmaven.infrastructure.llm.usage_ledger": ("LLM usage ledger drain",),
    "faultmaven.api.protection": ("Protection middleware shutdown",),
    "faultmaven.infrastructure.observability.apm_integration": (
        "Phase 2 monitoring cleanup",
    ),
}


@pytest.mark.parametrize("module", sorted(_SERVING_STOP_IMPORTS))
async def test_a_stop_whose_import_fails_fails_alone(
    module, stop_serving, monkeypatch, caplog
):
    """An import made while the stops were built, outside every step, would
    skip every stop if it failed (#2257, #2267). Each is that step's failure
    alone: logged, and every other stop still runs."""
    from faultmaven.bootstrap import lifespan as lifespan_module

    usage_ledger.install_usage_ledger(usage_ledger.InMemoryUsageLedger())
    monkeypatch.setitem(sys.modules, module, None)
    app = FastAPI()
    tasks = {h: asyncio.create_task(asyncio.sleep(3600)) for h in _TASK_HANDLES}
    for handle, task in tasks.items():
        setattr(app.state, handle, task)
    app.state.case_cleanup_scheduler = object()

    try:
        with caplog.at_level(
            logging.WARNING, logger="faultmaven.infrastructure.concurrency.teardown"
        ):
            await stop_serving(app, {})
        running = [handle for handle, task in tasks.items() if not task.cancelled()]
    finally:
        for task in tasks.values():
            task.cancel()
        await asyncio.wait(set(tasks.values()))

    failed = [
        line for line in caplog.text.splitlines() if "failed (non-critical)" in line
    ]
    for stop in _SERVING_STOP_IMPORTS[module]:
        assert any(f"{stop} failed" in line for line in failed), stop
    assert len(failed) == len(_SERVING_STOP_IMPORTS[module]), failed
    assert running == [], f"left running after the shutdown: {running}"
    left = [h for h in (*_TASK_HANDLES, "case_cleanup_scheduler") if h in app.state]
    # Unpublished before it is stopped, so a failed stop leaves no handle either.
    assert left == [], f"still on app.state: {left}"
    assert usage_ledger.get_installed_usage_ledger() is not None
    lifespan_module.decompose_application.assert_awaited_once()


#: Each module a decomposition step imports, and the steps that need it.
_DECOMPOSITION_IMPORTS = {
    "faultmaven.container": ("Container shutdown",),
    "faultmaven.infrastructure.llm.usage_ledger": (
        "LLM usage ledger uninstall",
        "LLM usage ledger drain",
    ),
    "faultmaven.infrastructure.observability.apm_integration": ("APM export stop",),
    "faultmaven.infrastructure.persistence.database": ("Database close",),
}


@pytest.mark.parametrize("module", sorted(_DECOMPOSITION_IMPORTS))
async def test_a_decomposition_step_whose_import_fails_fails_alone(
    module, container, monkeypatch, ledger_restored, caplog
):
    """``decompose_application`` imported the container, the ledger and the
    database before its steps, so one failed import would have skipped all of
    them, the engine disposal included (#2267)."""
    from faultmaven.bootstrap.composition import _ABSENT, decompose_application

    app = FastAPI()
    client = _RealClient()
    _own(container, client)
    app.state.redis_client = client
    close_database = AsyncMock()
    monkeypatch.setattr("faultmaven.container.container", container)
    monkeypatch.setattr(
        "faultmaven.infrastructure.persistence.database.close_database",
        close_database,
    )
    usage_ledger.install_usage_ledger(usage_ledger.InMemoryUsageLedger())
    monkeypatch.setitem(sys.modules, module, None)

    with caplog.at_level(
        logging.WARNING, logger="faultmaven.infrastructure.concurrency.teardown"
    ):
        await decompose_application(app, {"redis_client": _ABSENT})

    failed = [
        line for line in caplog.text.splitlines() if "failed (non-critical)" in line
    ]
    steps = _DECOMPOSITION_IMPORTS[module]
    assert len(failed) == len(steps), failed
    assert all(any(step in line for line in failed) for step in steps), failed
    assert "redis_client" not in app.state, "the composition is still published"
    shut_down = module != "faultmaven.container"
    assert client.aclose.await_count == (1 if shut_down else 0)
    uninstalled = module != "faultmaven.infrastructure.llm.usage_ledger"
    assert (usage_ledger.get_installed_usage_ledger() is None) is uninstalled
    disposed = module != "faultmaven.infrastructure.persistence.database"
    assert close_database.await_count == (1 if disposed else 0)


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
