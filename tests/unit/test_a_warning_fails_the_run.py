"""A run whose warnings summary is not empty fails; its tests keep their outcomes.

``tests/conftest.py``'s ``pytest_sessionfinish`` is the gate. A
``ResourceWarning`` or a thread's unhandled exception surfaces at garbage
collection, inside whichever test is running then, so failing a test would
blame the wrong one: the exit status carries it instead. Under xdist the
controller decides, from the warnings its workers relayed.

Each case runs a small suite in a child process with the root conftest loaded,
the way ``tests/unit/test_network_guard.py`` drives its guard.
"""

from __future__ import annotations

import os
import pathlib
import textwrap

import pytest

pytestmark = pytest.mark.unit

REPO_ROOT = pathlib.Path(__file__).resolve().parents[2]

_WARNS = """
import warnings


def test_passes_and_warns():
    warnings.warn("left behind", UserWarning)


def test_passes_quietly():
    assert True
"""

_WARNS_FROM_AN_INSTALLED_PACKAGE = """
import warnings


def test_passes_and_a_dependency_warns():
    warnings.warn_explicit(
        "deprecated in the dependency",
        UserWarning,
        "/venv/lib/python3.11/site-packages/somelib/api.py",
        12,
    )
"""

_QUIET = """
def test_passes_quietly():
    assert True
"""


def _run(pytester, monkeypatch, source, *args):
    monkeypatch.setenv(
        "PYTHONPATH",
        os.pathsep.join(filter(None, [str(REPO_ROOT), os.environ.get("PYTHONPATH")])),
    )
    pytester.makepyfile(textwrap.dedent(source))
    return pytester.runpytest_subprocess(
        "-p", "no:cacheprovider", "-p", "tests.conftest", *args, timeout=600
    )


@pytest.mark.parametrize("workers", [[], ["-n", "2"]], ids=["serial", "xdist"])
def test_a_warning_fails_the_run_and_no_test(pytester, monkeypatch, workers):
    result = _run(pytester, monkeypatch, _WARNS, *workers)

    result.assert_outcomes(passed=2, warnings=1)
    assert result.ret == pytest.ExitCode.TESTS_FAILED
    result.stdout.fnmatch_lines(
        ["*FAILED: the run raised 1 warning(s); see the warnings summary above.*"]
    )
    result.stdout.no_fnmatch_line("*sync-venv*")


def test_a_warning_from_an_installed_package_points_at_the_lockfile(
    pytester, monkeypatch
):
    result = _run(pytester, monkeypatch, _WARNS_FROM_AN_INSTALLED_PACKAGE)

    assert result.ret == pytest.ExitCode.TESTS_FAILED
    result.stdout.fnmatch_lines(["*./scripts/sync-venv.sh*"])


@pytest.mark.parametrize("workers", [[], ["-n", "2"]], ids=["serial", "xdist"])
def test_a_quiet_run_passes(pytester, monkeypatch, workers):
    result = _run(pytester, monkeypatch, _QUIET, *workers)

    result.assert_outcomes(passed=1)
    assert result.ret == pytest.ExitCode.OK
    result.stdout.no_fnmatch_line("*FAILED: the run raised*")


def test_a_failing_test_keeps_its_own_status(pytester, monkeypatch):
    result = _run(
        pytester,
        monkeypatch,
        _WARNS + "\n\ndef test_fails():\n    assert False\n",
    )

    result.assert_outcomes(passed=2, failed=1, warnings=1)
    assert result.ret == pytest.ExitCode.TESTS_FAILED
