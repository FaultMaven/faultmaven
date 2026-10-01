"""The runtime check, driven in a child pytest run (fm#1628).

Two rules in ``tests/conftest.py`` fail a test at SETUP, where the test itself
cannot catch the failure:

* an entry on ``faultmaven.main.app`` from a fixture scoped wider than one test
  fails, whether or not its module borrows the shared boot;
* a test that requests both ``booted_app_client`` and ``unshared_app_boot``
  fails.

So each of those controls runs a small suite in a child process with
``pytester.runpytest_subprocess``, and asserts the child's outcome and message.
The child's conftest re-exports the real fixtures from ``tests.conftest``, the
way ``tests/integration/conftest.py`` does. Importing it runs the real
``tests/conftest.py``, which installs the real guard, so what refuses there is
the code under test and not a copy. Each is refused before a lifespan starts,
so none pays a boot.

Two more run a child session for what only a session shows. One runs in this
process, to check that ``_app_boot_guard`` hands back the context it replaced.
The last runs a ``-k`` subset of ``test_app_boot_runtime_guard.py`` itself, by
path, the way a developer would. Selected without the module's other tests,
its controls must still pass and leave no lifespan live.
"""

from __future__ import annotations

import os
import pathlib

import pytest

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
    _real_app_boot,
    booted_app_client,
    unshared_app_boot,
)
"""

_MODULE_SCOPED_BOOT = """
import pytest
from fastapi.testclient import TestClient


@pytest.fixture(scope="module")
def module_scoped_boot():
    from faultmaven.main import app

    with TestClient(app) as client:
        yield client


def test_uses_the_module_scoped_boot(module_scoped_boot):
    pass
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


def test_a_session_run_inside_a_test_hands_the_context_back(pytester, request):
    """``_app_boot_guard`` restores the context it replaced, not a cleared one.

    A pytest session run in this process records its own test over this one's
    and must hand this one back when it ends, or the rest of this test would
    meet the guard as if it were outside any test.
    """
    from tests.conftest import _APP_BOOT_CONTEXT as context

    pytester.makeconftest(_CHILD_CONFTEST)
    pytester.makepyfile(test_inner="def test_inner():\n    pass\n")
    before = (context.test, context.borrows, context.declares_unshared)

    result = pytester.runpytest_inprocess(
        "-p", "no:cacheprovider", "--import-mode=importlib"
    )

    result.assert_outcomes(passed=1)
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
