"""The runtime check on a second lifespan beside the shared boot (fm#1628).

``tests/conftest.py`` wraps ``TestClient.__enter__`` and ``__exit__``. An entry
whose application resolves to ``faultmaven.main.app`` meets three rules, in
order, and the first that applies fails it before the lifespan starts:

* (a) scope: not from a fixture scoped wider than one test. Those controls
  fail at setup, so they run in a child process, in
  ``test_app_boot_runtime_guard_setup_errors.py``;
* (b) declaration: in a module that borrows the shared boot
  (``booted_app_client``), only a test that takes ``unshared_app_boot``;
* (c) live count: never while another lifespan on the app is live.

These controls drive the check through the path that runs it: the real
conftest, the real shared boot and the real application. Each expects the
message of the one rule that applies to it, so disabling a rule fails that
rule's controls and no others.

Each control stands alone, so any ``-k`` subset behaves:

* a control that expects (b) takes ``booted_app_client`` itself, which makes
  this module a borrower whatever else is selected;
* a control whose guard might not fire exits what it entered, so a regression
  fails that control and cannot leave a lifespan open for the next test.

``test_app_boot_runtime_guard_no_borrower.py`` is the module that names no
borrow.
"""

from __future__ import annotations

import contextlib
import re

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

pytestmark = pytest.mark.unit

#: What each rule's failure says, as ``tests/conftest.py`` words it.
DECLARATION = "without taking `unshared_app_boot`"
OVERLAP = "while another lifespan on it is live"


def _caught(request, rule):
    """Expect the guard's failure under ``rule``, naming this test."""
    return pytest.raises(
        pytest.fail.Exception,
        match=re.escape(request.node.nodeid) + ".*" + re.escape(rule),
    )


class _Wrap:
    """ASGI middleware in miniature: the real app behind an ``.app`` attribute."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        await self.app(scope, receive, send)


# -- allowed ----------------------------------------------------------------


def test_the_brokers_own_entry_is_allowed(booted_app_client):
    """The one sanctioned entry, made by ``_RealAppBoot`` under its flag."""
    import faultmaven.main

    assert booted_app_client.app is faultmaven.main.app


def test_a_local_app_named_like_the_real_one_is_not_caught():
    """The census's false positive: a scratch application bound to ``app``."""
    app = FastAPI()

    with TestClient(app) as client:
        assert client.app is app


class _RaisingApp:
    """A scratch ASGI app whose ``.app`` raises, as one needing a context can."""

    def __init__(self):
        self._inner = FastAPI()

    @property
    def app(self):
        raise RuntimeError("no application context")

    async def __call__(self, scope, receive, send):
        await self._inner(scope, receive, send)


def test_a_scratch_app_whose_app_attribute_raises_is_not_caught():
    """A hop that raises is no route to the real app, and must not crash."""
    with TestClient(_RaisingApp()) as client:
        assert isinstance(client.app, _RaisingApp)


# -- (b) the declaration ----------------------------------------------------


def test_a_rebinding_is_caught(request, booted_app_client):
    """``real = app`` hid a real boot from the census; identity ignores names."""
    from faultmaven.main import app

    real = app
    with _caught(request, DECLARATION):
        with TestClient(real):
            pass


def test_a_manual_enter_is_caught(request, booted_app_client):
    import faultmaven.main as m

    client = TestClient(m.app)
    with _caught(request, DECLARATION):
        client.__enter__()
        # Reached only if the guard let the entry through.
        client.__exit__(None, None, None)


def test_an_exit_stack_entry_is_caught(request, booted_app_client):
    import faultmaven.main as m

    with _caught(request, DECLARATION):
        with contextlib.ExitStack() as stack:
            stack.enter_context(TestClient(m.app))


def test_the_real_app_inside_middleware_is_caught(request, booted_app_client):
    """Resolved through ``.app``, so a wrapper does not hide the real app."""
    from faultmaven.main import app

    with _caught(request, DECLARATION):
        with TestClient(_Wrap(app)):
            pass


def test_re_entering_the_lent_client_is_caught(request, booted_app_client):
    """The lent client carries no exemption: the broker's is a flag, not a mark."""
    with _caught(request, DECLARATION):
        with booted_app_client:
            pass


def test_an_undeclared_boot_is_caught_while_no_shared_boot_is_live(
    request, booted_app_client, _real_app_boot
):
    """The rule keys on the module borrowing, not on a shared boot being live.

    With the shared boot stood down, nothing is live for the count to see, so
    only the declaration rule catches this entry. That is the case a test
    placed before its module's first borrower is in.
    """
    from faultmaven.main import app

    _real_app_boot.release()
    with _caught(request, DECLARATION):
        with TestClient(app):
            pass


# -- (c) the live count -----------------------------------------------------


@pytest.fixture
def entered_before_the_stand_down(request):
    """Enter the real app before ``unshared_app_boot`` has run.

    The module's shared boot is live, as it is after any earlier borrower here;
    ``getfixturevalue`` makes it so for this test alone. The test declares, so
    only the count can catch the overlap, and the failure is caught here,
    during setup, where it happens.
    """
    from faultmaven.main import app

    request.getfixturevalue("booted_app_client")
    with pytest.raises(pytest.fail.Exception) as caught:
        with TestClient(app):
            pass
    return caught


def test_a_fixture_entering_before_unshared_app_boot_is_caught(
    request, entered_before_the_stand_down, unshared_app_boot
):
    message = str(entered_before_the_stand_down.value)
    assert message.startswith(request.node.nodeid)
    assert OVERLAP in message
    assert "opened by the shared boot" in message


def test_a_declared_unshared_boot_is_allowed(unshared_app_boot):
    """Allowed: it is declared, and the only lifespan live on the app."""
    from faultmaven.main import app

    with TestClient(app) as client:
        assert client.app is app


def test_a_second_lifespan_inside_a_declared_one_is_caught(request, unshared_app_boot):
    from faultmaven.main import app

    with TestClient(app):
        with _caught(request, OVERLAP):
            with TestClient(app):
                pass


def test_a_lifespan_left_open_fails_the_next_shared_boot(request, unshared_app_boot):
    """The broker is counted too: it may not boot beside a leaked lifespan."""
    from faultmaven.main import app

    leaked = TestClient(app)
    leaked.__enter__()
    try:
        with pytest.raises(pytest.fail.Exception) as caught:
            request.getfixturevalue("booted_app_client")
    finally:
        leaked.__exit__(None, None, None)
    message = str(caught.value)
    assert message.startswith(f"the shared boot for {request.node.nodeid}")
    assert f"{OVERLAP}, opened by {request.node.nodeid}." in message
