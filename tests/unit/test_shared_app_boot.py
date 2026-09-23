"""The shared application boot, and the leak it must not introduce (fm#1569).

``booted_app_client`` hands the same started application to every test in a
module. That removes 12 of the 29 lifespans the suite used to run, and buys the
saving with a risk: one test's mutation of ``app.state`` reaching the next one.
A timeout is loud; a leak is a flake attributed to whichever branch happened to
run, which is strictly worse than the cost it removes.

So the three ways state leaks — a key replaced, a key added, a key deleted —
are each exercised here, against a throwaway application with a REAL lifespan
driven through a REAL ``TestClient``. The broker takes an app factory for
exactly this; nothing else passes one.

The fixture bodies are driven as generators rather than re-implemented, so what
runs here is the code in ``tests/conftest.py`` and not a copy of it.
"""

from __future__ import annotations

import pytest
from fastapi import FastAPI

from tests.conftest import _RealAppBoot, booted_app_client, unshared_app_boot

pytestmark = pytest.mark.unit


def _probe_app_factory():
    """A one-route app whose lifespan writes to ``app.state``, and counts."""
    boots = {"n": 0}

    def build() -> FastAPI:
        from contextlib import asynccontextmanager

        @asynccontextmanager
        async def lifespan(app: FastAPI):
            boots["n"] += 1
            app.state.wired_by_the_lifespan = f"boot-{boots['n']}"
            yield

        app = FastAPI(lifespan=lifespan)

        @app.get("/probe")
        async def probe():
            return {"ok": True}

        return app

    app = build()
    return (lambda: app), boots, app


def _run(fixture, *args):
    """Drive a pytest fixture's body as the generator it is.

    Returns ``(value, finish)`` where calling ``finish()`` runs the teardown.
    """
    gen = fixture.__wrapped__(*args)
    value = next(gen)
    return value, lambda: next(gen, None)


def test_one_lifespan_serves_every_borrower():
    factory, boots, _app = _probe_app_factory()
    broker = _RealAppBoot(factory)
    try:
        first, finish_first = _run(booted_app_client, broker)
        assert first.get("/probe").status_code == 200
        finish_first()

        second, finish_second = _run(booted_app_client, broker)
        finish_second()

        assert first is second, "the module's borrowers got different clients"
        assert boots["n"] == 1, f"the lifespan ran {boots['n']} times, not once"
    finally:
        broker.release()


def test_a_replaced_state_key_does_not_reach_the_next_borrower():
    factory, _boots, app = _probe_app_factory()
    broker = _RealAppBoot(factory)
    try:
        _client, finish = _run(booted_app_client, broker)
        app.state.wired_by_the_lifespan = "clobbered"
        finish()
        assert app.state.wired_by_the_lifespan == "boot-1"
    finally:
        broker.release()


def test_an_added_state_key_does_not_reach_the_next_borrower():
    factory, _boots, app = _probe_app_factory()
    broker = _RealAppBoot(factory)
    try:
        _client, finish = _run(booted_app_client, broker)
        app.state.invented_by_the_test = object()
        finish()
        assert not hasattr(app.state, "invented_by_the_test")
    finally:
        broker.release()


def test_a_deleted_state_key_is_put_back_for_the_next_borrower():
    factory, _boots, app = _probe_app_factory()
    broker = _RealAppBoot(factory)
    try:
        _client, finish = _run(booted_app_client, broker)
        del app.state.wired_by_the_lifespan
        finish()
        assert app.state.wired_by_the_lifespan == "boot-1"
    finally:
        broker.release()


def test_an_unshared_boot_stands_the_shared_one_down_and_leaves_it_down():
    """The declaration a lifespan-subject test makes.

    Two lifespans on one app object is the failure this prevents: the second
    re-composes ``app.state`` onto its own event loop, and its shutdown disposes
    resources the first client is still serving from. So the shared boot must be
    gone while the declaring test runs, and the next borrower must get a fresh
    one rather than the corpse of the old.
    """
    factory, boots, app = _probe_app_factory()
    broker = _RealAppBoot(factory)
    try:
        _client, finish = _run(booted_app_client, broker)
        finish()
        assert boots["n"] == 1

        _none, finish_unshared = _run(unshared_app_boot, broker)
        assert broker._client is None, "the shared boot was still live"
        finish_unshared()
        assert broker._client is None, "the shared boot was left standing"

        _client2, finish2 = _run(booted_app_client, broker)
        finish2()
        assert boots["n"] == 2, "the next borrower did not get a fresh lifespan"
        assert app.state.wired_by_the_lifespan == "boot-2"
    finally:
        broker.release()


def test_release_is_idempotent_and_safe_before_any_boot():
    """``unshared_app_boot`` releases twice, and may run before anything booted."""
    factory, boots, _app = _probe_app_factory()
    broker = _RealAppBoot(factory)
    broker.release()
    broker.release()
    assert boots["n"] == 0
    broker.restore_state()  # nothing snapshotted yet: must not raise
