"""The runtime check on a second lifespan beside the shared boot (fm#1628).

``tests/conftest.py`` wraps ``TestClient.__enter__``. In a module that borrows
the shared boot (``booted_app_client``), a test that enters the lifespan of
``faultmaven.main.app`` without taking ``unshared_app_boot`` fails there, before
the lifespan starts. The client's application is compared with the app object
by identity, so no spelling of the argument can hide a boot and no local name
``app`` can invent one.

These controls drive the check through the path that runs it: the real
conftest, the real shared boot and the real application. This module borrows
(``test_the_brokers_own_entry_is_allowed``), so every entry here falls under
the rule. ``test_app_boot_runtime_guard_no_borrower.py`` is the module that does
not borrow.
"""

from __future__ import annotations

import contextlib
import re

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

pytestmark = pytest.mark.unit


def _caught(request):
    """Expect the guard's failure, and that it names this test."""
    return pytest.raises(
        pytest.fail.Exception,
        match=re.escape(request.node.nodeid) + r".*`unshared_app_boot`",
    )


def test_an_undeclared_boot_placed_before_any_borrower_is_caught(request):
    """The rule keys on the module borrowing, not on a shared boot being live.

    In file order nothing has borrowed yet when this runs, so a check of "is a
    shared boot live now" would let it through. Run it with its module: the
    borrowers are read off the session, and selecting this test alone leaves
    none.
    """
    from faultmaven.main import app

    with _caught(request):
        with TestClient(app):
            pass


def test_the_brokers_own_entry_is_allowed(booted_app_client):
    """The one sanctioned entry: ``_RealAppBoot`` marks the client it starts."""
    import faultmaven.main

    assert booted_app_client.app is faultmaven.main.app


def test_a_rebinding_is_caught(request, booted_app_client):
    """``real = app`` hid a real boot from the census; identity ignores names."""
    from faultmaven.main import app

    real = app
    with _caught(request):
        with TestClient(real):
            pass


def test_a_manual_enter_is_caught(request):
    import faultmaven.main as m

    with _caught(request):
        TestClient(m.app).__enter__()


def test_an_exit_stack_entry_is_caught(request):
    import faultmaven.main as m

    with _caught(request):
        with contextlib.ExitStack() as stack:
            stack.enter_context(TestClient(m.app))


def test_a_local_app_named_like_the_real_one_is_not_caught():
    """The census's false positive: a scratch application bound to ``app``."""
    app = FastAPI()

    with TestClient(app) as client:
        assert client.app is app


def test_a_declared_unshared_boot_is_allowed(unshared_app_boot):
    from faultmaven.main import app

    with TestClient(app) as client:
        assert client.app is app
