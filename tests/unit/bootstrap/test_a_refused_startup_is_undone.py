"""A startup refused after the container was initialized leaves nothing of composition behind (#2242).

The lifespan undid composition only in its post-``yield`` shutdown. A refusal
after ``container.initialize()`` never reached it: inside composition (the
auth-store gate, the bootstrap failure, the RLS role gate) or after it (the
route-policy gate, a cancellation at any later await). The container stayed
initialized with its Redis client open, the usage ledger stayed installed, and
``app.state`` kept what composition published. A second lifespan on the same
app then found ``initialize()`` returning early and published the same
objects, so its identity diff recorded none of them, and its shutdown closed the
client while those names still held it.

The composition root is stubbed here: a fake ``_wire_composition_root``
initializes a container singleton of the test's own (a recording Redis client,
unless it is already initialized), installs a ledger and publishes three names, which is what the
real one does first. The real refused boot runs in
``tests/integration/test_a_refused_boot_is_undone.py``.
"""

from __future__ import annotations

import asyncio
import logging
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi import FastAPI

from faultmaven.bootstrap.composition import (
    _ABSENT,
    compose_application,
    decompose_application,
)
from faultmaven.bootstrap.lifespan import lifespan
from faultmaven.container import DIContainer
from faultmaven.infrastructure.llm import usage_ledger

pytestmark = pytest.mark.unit

#: The names the fake composition publishes, each holding the Redis client
#: directly or as a service built over it would.
_PUBLISHED = ("redis_client", "job_service", "case_service")


class _RealClient:
    """Stands in for ``redis.asyncio.Redis``: anything not from ``fakeredis``."""

    def __init__(self):
        self.aclose = AsyncMock()


@pytest.fixture
def container(monkeypatch):
    """A singleton of this test's own, built by the real ``__new__``; the
    process one is put back after. Composition resolves it through the
    ``container`` proxy, as it resolves the real one."""
    monkeypatch.setattr(DIContainer, "_instance", None)
    instance = DIContainer()
    # The teardown uninstalls the process-global ledger; put back what was there.
    monkeypatch.setattr(
        usage_ledger, "_installed_ledger", usage_ledger.get_installed_usage_ledger()
    )
    return instance


@pytest.fixture
def database(monkeypatch):
    """``close_database``, recorded: the teardown's last step disposes the
    process-global engine, which these stubbed compositions never opened."""
    close_database = AsyncMock()
    monkeypatch.setattr(
        "faultmaven.infrastructure.persistence.database.close_database",
        close_database,
    )
    return close_database


@pytest.fixture
def composition(container, database, monkeypatch):
    """Replace the composition root with one that does what the real one does
    first. ``refusal``, when set, is raised once that is done; ``close_failure``
    is what the client it opens raises when closed."""
    wiring = SimpleNamespace(refusal=None, close_failure=None, clients=[])

    async def _wire(app, settings):
        if not container.is_initialized:  # ``initialize()`` returns early otherwise
            container._initialized = True
            client = _RealClient()
            client.aclose.side_effect = wiring.close_failure
            container._register_service("redis_client", client)
            container.redis_client = client
            wiring.clients.append(client)
        usage_ledger.install_usage_ledger(usage_ledger.InMemoryUsageLedger())
        app.state.redis_client = container.redis_client
        app.state.job_service = object()
        app.state.case_service = object()
        if wiring.refusal is not None:
            raise wiring.refusal

    monkeypatch.setattr(
        "faultmaven.bootstrap.composition._wire_composition_root", _wire
    )
    return wiring


def _assert_nothing_published_remains(app):
    left = [name for name in _PUBLISHED if name in app.state]
    assert left == [], f"composition's names are still published: {left}"
    assert usage_ledger.get_installed_usage_ledger() is None, "the ledger is installed"


def _assert_nothing_of_composition_remains(app, container, client, database):
    assert container.is_initialized is False, "the container is still initialized"
    client.aclose.assert_awaited_once()
    _assert_nothing_published_remains(app)
    assert database.await_count >= 1, "the database engine was not disposed"


async def test_a_startup_refused_after_composition_is_undone_every_time(
    composition, container, database, monkeypatch, unshared_app_boot
):
    """The issue's probe: the route-policy gate refuses, twice, on one app.

    The refusal runs the lifespan's whole shutdown, so this takes
    ``unshared_app_boot``.
    """
    monkeypatch.setattr(
        "faultmaven.api.middleware.route_policy.assert_policy_coherent",
        lambda app: "planted incoherent policy",
    )
    app = FastAPI()

    for entry in range(2):
        with pytest.raises(RuntimeError, match="planted incoherent policy"):
            async with lifespan(app):
                pytest.fail("the lifespan must not yield after a refusal")

        assert (
            len(composition.clients) == entry + 1
        ), "the container was not re-initialized"
        _assert_nothing_of_composition_remains(
            app, container, composition.clients[entry], database
        )
        assert database.await_count == entry + 1


async def test_a_refusal_inside_composition_is_undone(composition, container, database):
    """The bootstrap failure is raised after ``initialize()`` built the client.
    The database engine the bootstrap opened is disposed too."""
    composition.refusal = RuntimeError("Critical bootstrap failure: planted")
    app = FastAPI()

    with pytest.raises(RuntimeError) as refused:
        await compose_application(app, object())  # the fake reads no settings

    assert refused.value is composition.refusal
    _assert_nothing_of_composition_remains(
        app, container, composition.clients[0], database
    )


async def test_a_failing_teardown_step_neither_replaces_the_refusal_nor_skips_the_rest(
    composition, container, database, caplog
):
    """A shutdown that raises is logged: the refusal propagates as the same
    object, and the steps after it still run."""
    composition.refusal = RuntimeError("Critical bootstrap failure: planted")
    composition.close_failure = ConnectionError("the pool would not close")
    app = FastAPI()

    with caplog.at_level(
        logging.WARNING, logger="faultmaven.infrastructure.concurrency.teardown"
    ):
        with pytest.raises(RuntimeError) as refused:
            await compose_application(app, object())

    assert refused.value is composition.refusal
    assert "the pool would not close" in caplog.text
    _assert_nothing_of_composition_remains(
        app, container, composition.clients[0], database
    )


async def test_a_refused_composition_shuts_down_a_container_it_found_initialized(
    composition, container, database
):
    """One owner rule: whoever composes for a run shuts the container down at the
    run's end, on every path. A refused composition shuts it down even when it
    found it already initialized, as the end of a lifespan always has."""
    owner_client = _RealClient()
    container._initialized = True
    container._register_service("redis_client", owner_client)
    container.redis_client = owner_client
    composition.refusal = RuntimeError("Critical bootstrap failure: planted")
    app = FastAPI()

    with pytest.raises(RuntimeError) as refused:
        await compose_application(app, object())

    assert refused.value is composition.refusal
    _assert_nothing_of_composition_remains(app, container, owner_client, database)


async def test_a_cancelled_teardown_step_lets_the_rest_finish_then_propagates(
    composition, container, database
):
    """The container's shutdown is cancelled: the ledger, ``app.state`` and the
    engine are still taken back, and the cancellation is raised after them."""
    composition.refusal = RuntimeError("Critical bootstrap failure: planted")
    composition.close_failure = asyncio.CancelledError()
    app = FastAPI()

    with pytest.raises(asyncio.CancelledError):
        await compose_application(app, object())

    _assert_nothing_of_composition_remains(
        app, container, composition.clients[0], database
    )


async def test_a_cancellation_while_the_config_watcher_ends_is_not_swallowed(
    container, database
):
    """The teardown is itself cancelled while it waits for the watcher to end.
    The watcher's own cancellation is not the teardown's, and is not mistaken
    for it: the rest of the teardown runs, then the teardown ends cancelled."""
    cancel_seen, release = asyncio.Event(), asyncio.Event()

    async def _watch():
        try:
            await asyncio.sleep(3600)
        except asyncio.CancelledError:
            cancel_seen.set()
            await release.wait()  # still ending when the teardown is cancelled
            raise

    app = FastAPI()
    watcher = asyncio.create_task(_watch())
    app.state.llm_config_watch_task = watcher
    app.state.redis_client = object()
    teardown = asyncio.create_task(
        decompose_application(
            app, {"llm_config_watch_task": _ABSENT, "redis_client": _ABSENT}
        )
    )
    await cancel_seen.wait()
    teardown.cancel()
    release.set()

    with pytest.raises(asyncio.CancelledError):
        await teardown

    assert "redis_client" not in app.state, "the teardown stopped at the watcher"
    database.assert_awaited_once()
    await asyncio.wait({watcher})
    assert watcher.cancelled()
