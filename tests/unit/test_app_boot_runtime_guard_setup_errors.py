"""The runtime check, driven in a child pytest run (fm#1628).

Some rules in ``tests/conftest.py`` fail a test at SETUP or TEARDOWN, where the
test itself cannot catch the failure:

* an entry on ``faultmaven.main.app`` from a fixture scoped wider than one test
  fails at setup, whether or not its module borrows the shared boot;
* a test that requests both ``booted_app_client`` and ``unshared_app_boot``
  fails at setup;
* a test that leaves a lifespan it opened still live fails at teardown, whether
  it leaked a client or pulled a wider-scoped boot in by
  ``request.getfixturevalue``.

So each of those controls runs a small suite in a child process with
``pytester.runpytest_subprocess``, and asserts the child's outcome and message.
The child's conftest re-exports the real fixtures from ``tests.conftest``, the
way ``tests/integration/conftest.py`` does. Importing it runs the real
``tests/conftest.py``, which installs the real guard, so what refuses there is
the code under test and not a copy. A refusal at setup comes before a lifespan
starts, so it pays no boot; a leak pays one, in the child.

Two more run a child session for what only a session shows. One runs in this
process: a session started inside a test must record no test of ours, refuse
its own module-scoped boot, and give our test back when it ends. The last runs
a ``-k`` subset of ``test_app_boot_runtime_guard.py`` itself, by path, the way a
developer would. Selected without the module's other tests, its controls must
still pass and leave no lifespan live.
"""

from __future__ import annotations

import contextlib
import os
import pathlib

import pytest

# Imported here, before ``pytester`` snapshots ``sys.modules`` for a test. The
# in-process child session imports it otherwise, the snapshot rolls that import
# back, and the next import builds a second copy of the application's
# SQLAlchemy registry ("Type <class 'object'> is already registered").
import faultmaven.main
from tests.conftest import WORKER_DATABASE_URL_ENV

pytest_plugins = ["pytester"]

pytestmark = pytest.mark.unit

#: Put on the child's path so ``tests.conftest`` and ``faultmaven`` import from
#: this checkout.
REPO_ROOT = pathlib.Path(__file__).resolve().parents[2]

#: The guard's in-process controls, run in a child as a ``-k`` subset.
GUARD_CONTROLS = REPO_ROOT / "tests" / "unit" / "test_app_boot_runtime_guard.py"

_CHILD_CONFTEST = """
from tests.conftest import (  # noqa: F401
    _app_boot_guard,
    _app_boot_session,
    _real_app_boot,
    booted_app_client,
    unshared_app_boot,
)
"""

_MODULE_SCOPED_FIXTURE = """
import pytest
from fastapi.testclient import TestClient


@pytest.fixture(scope="module")
def module_scoped_boot():
    from faultmaven.main import app

    with TestClient(app) as client:
        yield client
"""

_USES_IT = """

def test_uses_the_module_scoped_boot(module_scoped_boot):
    pass
"""

_MODULE_SCOPED_BOOT = _MODULE_SCOPED_FIXTURE + _USES_IT

# Runs first, so that the module-scoped boot is set up BETWEEN two tests of the
# child session, where a context handed back to the outer test would still be
# recorded.
_A_TEST_BEFORE = """

def test_runs_first():
    pass
"""

# Pulls the module-scoped boot in from inside the test, where the scope rule
# sees a test running; the lifespan still outlives the test.
_PULLS_IT_BY_GETFIXTUREVALUE = """

def test_pulls_it(request):
    request.getfixturevalue("module_scoped_boot")
"""

# Enters by hand and never exits. The module-scoped fixture exits the leak
# after the guard has reported it, so the child ends with nothing live.
_LEAKS = """
import pytest
from fastapi.testclient import TestClient

LEAKED = []


@pytest.fixture(scope="module", autouse=True)
def exit_the_leak_once_reported():
    yield
    for client in LEAKED:
        client.__exit__(None, None, None)


def test_leaks():
    from faultmaven.main import app

    client = TestClient(app)
    LEAKED.append(client)
    client.__enter__()
"""

# Collected, so the module borrows the shared boot; skipped before its
# fixtures are set up, so the child pays no boot for it.
_A_BORROWER = """

@pytest.mark.skip(reason="makes the module a borrower without booting")
def test_borrows(booted_app_client):
    pass
"""

_BOTH = """
def test_requests_both(booted_app_client, unshared_app_boot):
    pass
"""

_OUTSIDE_A_TEST = (
    "*the lifespan of faultmaven.main.app is entered outside any test's "
    "function scope (during test_child.py::test_uses_the_module_scoped_boot "
    "(setup))*"
)


#: Loaded into the ``-k`` child with ``-p``: after every fixture is torn down,
#: it records how many lifespans on the real app the guard still counts live.
_LIVE_AT_EXIT = """
import pathlib


def pytest_sessionfinish(session):
    from starlette.testclient import TestClient

    live = TestClient.__enter__._fm_app_boot_context.live
    pathlib.Path("live_at_exit.txt").write_text(str(len(live)))
"""


def _isolate_child(pytester, monkeypatch):
    monkeypatch.setenv(
        "PYTHONPATH",
        os.pathsep.join(
            filter(
                None,
                [str(REPO_ROOT), str(pytester.path), os.environ.get("PYTHONPATH")],
            )
        ),
    )
    # The child is a serial run of its own, whatever worker runs this test, and
    # keeps every store under its own directory: pytester runs it there, and
    # the database would otherwise follow the worker's inherited DATABASE_URL.
    # A boot in the child must not share the parent's database or ./data
    # (fm#1772).
    for name in ("PYTEST_XDIST_WORKER", "DATABASE_URL", WORKER_DATABASE_URL_ENV):
        monkeypatch.delenv(name, raising=False)


def _run_child(pytester, monkeypatch, source):
    _isolate_child(pytester, monkeypatch)
    pytester.makeconftest(_CHILD_CONFTEST)
    pytester.makepyfile(test_child=source)
    return pytester.runpytest_subprocess("-p", "no:cacheprovider", timeout=600)


def test_a_module_scoped_boot_in_a_borrowing_module_fails_at_setup(
    pytester, monkeypatch
):
    result = _run_child(pytester, monkeypatch, _MODULE_SCOPED_BOOT + _A_BORROWER)

    result.assert_outcomes(errors=1, skipped=1)
    result.stdout.fnmatch_lines(
        ["*ERROR at setup of test_uses_the_module_scoped_boot*", _OUTSIDE_A_TEST]
    )


def test_a_module_scoped_boot_in_a_module_that_never_borrows_fails_at_setup(
    pytester, monkeypatch
):
    """Not keyed on borrowing: a wider-scoped lifespan outlives its test."""
    result = _run_child(pytester, monkeypatch, _MODULE_SCOPED_BOOT)

    result.assert_outcomes(errors=1)
    result.stdout.fnmatch_lines(
        ["*ERROR at setup of test_uses_the_module_scoped_boot*", _OUTSIDE_A_TEST]
    )


def test_a_test_requesting_both_fixtures_fails_at_setup(pytester, monkeypatch):
    result = _run_child(pytester, monkeypatch, _BOTH)

    result.assert_outcomes(errors=1)
    result.stdout.fnmatch_lines(
        [
            "*ERROR at setup of test_requests_both*",
            "*test_child.py::test_requests_both requests both `booted_app_client` "
            "and `unshared_app_boot`*",
        ]
    )


def test_a_test_that_leaks_a_lifespan_fails_at_its_own_teardown(pytester, monkeypatch):
    result = _run_child(pytester, monkeypatch, _LEAKS)

    result.assert_outcomes(passed=1, errors=1)
    result.stdout.fnmatch_lines(
        [
            "*ERROR at teardown of test_leaks*",
            "*test_child.py::test_leaks leaves a lifespan on faultmaven.main.app "
            "open*",
        ]
    )


def test_a_wider_boot_pulled_in_by_getfixturevalue_fails_its_test(
    pytester, monkeypatch
):
    """The scope rule sees a test running; the lifespan outlives it all the same."""
    result = _run_child(
        pytester, monkeypatch, _MODULE_SCOPED_FIXTURE + _PULLS_IT_BY_GETFIXTUREVALUE
    )

    result.assert_outcomes(passed=1, errors=1)
    result.stdout.fnmatch_lines(
        [
            "*ERROR at teardown of test_pulls_it*",
            "*test_child.py::test_pulls_it leaves a lifespan on faultmaven.main.app "
            "open*",
        ]
    )


@contextlib.asynccontextmanager
async def _empty_lifespan(app):
    yield


def test_a_session_run_inside_a_test_refuses_its_module_scoped_boot(
    pytester, request, monkeypatch
):
    """A session run in-process records no test of ours, and gives ours back.

    Its module-scoped boot is set up between its own tests, and must meet the
    scope rule there rather than pass as if it ran inside this test. When the
    session ends, this test is recorded again.
    """
    from tests.conftest import _APP_BOOT_CONTEXT as context

    # The guard decides by identity at ``TestClient.__enter__``, before any
    # lifespan runs, so an empty lifespan leaves the path under test real. It
    # matters only if the guard regresses: the child's boot then runs in THIS
    # process, under pytester's working directory, and a real one there grew a
    # store past 400 MB (fm#1772). A regression must fail this control on its
    # outcome, not boot in-process.
    monkeypatch.setattr(faultmaven.main.app.router, "lifespan_context", _empty_lifespan)
    pytester.makeconftest(_CHILD_CONFTEST)
    pytester.makepyfile(test_child=_MODULE_SCOPED_FIXTURE + _A_TEST_BEFORE + _USES_IT)
    before = (context.test, context.borrows, context.declares_unshared)

    result = pytester.runpytest_inprocess(
        "-p", "no:cacheprovider", "--import-mode=importlib"
    )

    result.assert_outcomes(passed=1, errors=1)
    result.stdout.fnmatch_lines(
        ["*ERROR at setup of test_uses_the_module_scoped_boot*", _OUTSIDE_A_TEST]
    )
    assert before[0] == request.node.nodeid
    assert (context.test, context.borrows, context.declares_unshared) == before


def test_a_k_subset_of_the_controls_stands_alone(pytester, monkeypatch):
    """Two controls that once passed only beside a borrower, selected alone.

    Each takes ``booted_app_client`` itself now, so the module borrows whatever
    ``-k`` selects, and nothing either entered is live when the run ends.
    """
    _isolate_child(pytester, monkeypatch)
    pytester.makepyfile(fm_live_at_exit=_LIVE_AT_EXIT)
    result = pytester.runpytest_subprocess(
        str(GUARD_CONTROLS),
        "-p",
        "no:cacheprovider",
        "-p",
        "fm_live_at_exit",
        "-k",
        "manual_enter or exit_stack",
        timeout=600,
    )

    result.assert_outcomes(passed=2)
    assert (pytester.path / "live_at_exit.txt").read_text() == "0"
