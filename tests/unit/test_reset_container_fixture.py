"""``reset_container`` gives the next test its own environment back (#2283).

The fixture wrote ``os.environ["SKIP_SERVICE_CHECKS"] = "true"`` and never
restored it, so every later test in the process saw the flag, whatever its CI
job set. Nor did the write reach a settings object already cached, so the
container it handed out could compose with service checks on.

A test cannot watch its own teardown, so a small suite runs in a child process
with the root conftest loaded, the way ``test_a_warning_fails_the_run.py`` runs
its suites: the first test takes the fixture, the second runs after it.
"""

from __future__ import annotations

import os
import pathlib
import textwrap

import pytest

pytest_plugins = ["pytester"]

pytestmark = pytest.mark.unit

REPO_ROOT = pathlib.Path(__file__).resolve().parents[2]

_SUITE = """
import os

from faultmaven.config.settings import get_settings

# Settings cached before the fixture runs, with service checks on: the fixture
# must reset them for its environment to count.
assert get_settings().server.skip_service_checks is False


def test_the_fixture_composes_with_service_checks_skipped(reset_container):
    assert os.environ["SKIP_SERVICE_CHECKS"] == "true"
    assert get_settings().server.skip_service_checks is True


def test_the_next_test_has_its_own_environment():
    assert os.environ["SKIP_SERVICE_CHECKS"] == "false"
    assert get_settings().server.skip_service_checks is False
"""


def test_the_fixture_restores_what_it_sets(pytester, monkeypatch):
    monkeypatch.setenv(
        "PYTHONPATH",
        os.pathsep.join(filter(None, [str(REPO_ROOT), os.environ.get("PYTHONPATH")])),
    )
    monkeypatch.setenv("SKIP_SERVICE_CHECKS", "false")
    pytester.makepyfile(textwrap.dedent(_SUITE))

    result = pytester.runpytest_subprocess(
        "-p",
        "no:cacheprovider",
        "-p",
        "tests.conftest",
        "-o",
        "asyncio_mode=auto",
        timeout=600,
    )

    result.assert_outcomes(passed=2)
