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

import logging
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi import FastAPI

from faultmaven.bootstrap.composition import compose_application
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
def composition(container, monkeypatch):
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


def _assert_nothing_of_composition_remains(app, container, client):
    assert container.is_initialized is False, "the container is still initialized"
    client.aclose.assert_awaited_once()
    _assert_nothing_published_remains(app)


async def test_a_startup_refused_after_composition_is_undone_every_time(
    composition, container, monkeypatch, unshared_app_boot
):
    """The issue's probe: the route-policy gate refuses, twice, on one app.

    The refusal runs the lifespan's whole shutdown, ``close_database()``
    included, so this takes ``unshared_app_boot``.
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
            app, container, composition.clients[entry]
        )


async def test_a_refusal_inside_composition_is_undone(composition, container):
    """The bootstrap failure is raised after ``initialize()`` built the client."""
    composition.refusal = RuntimeError("Critical bootstrap failure: planted")
    app = FastAPI()

    with pytest.raises(RuntimeError) as refused:
        await compose_application(app, object())  # the fake reads no settings

    assert refused.value is composition.refusal
    _assert_nothing_of_composition_remains(app, container, composition.clients[0])


async def test_a_failing_teardown_step_neither_replaces_the_refusal_nor_skips_the_rest(
    composition, container, caplog
):
    """A shutdown that raises is logged: the refusal propagates as the same
    object, and the steps after it still run."""
    composition.refusal = RuntimeError("Critical bootstrap failure: planted")
    composition.close_failure = ConnectionError("the pool would not close")
    app = FastAPI()

    with caplog.at_level(logging.WARNING, logger="faultmaven.bootstrap.composition"):
        with pytest.raises(RuntimeError) as refused:
            await compose_application(app, object())

    assert refused.value is composition.refusal
    assert "the pool would not close" in caplog.text
    _assert_nothing_of_composition_remains(app, container, composition.clients[0])


async def test_a_refused_composition_leaves_a_container_it_did_not_initialize(
    composition, container
):
    """Whoever initializes the container shuts it down. Composition that found
    it already initialized takes back what it installed, and not the container."""
    owner_client = _RealClient()
    container._initialized = True
    container._register_service("redis_client", owner_client)
    container.redis_client = owner_client
    composition.refusal = RuntimeError("Critical bootstrap failure: planted")
    app = FastAPI()

    with pytest.raises(RuntimeError) as refused:
        await compose_application(app, object())

    assert refused.value is composition.refusal
    assert container.is_initialized is True
    owner_client.aclose.assert_not_awaited()
    _assert_nothing_published_remains(app)
