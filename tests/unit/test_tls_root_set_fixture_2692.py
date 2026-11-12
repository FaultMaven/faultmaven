"""``_no_inherited_tls_root_set`` leaves no TLS root variable after a test,
even one that patched an owned variable after applying the set (#2692).

``monkeypatch`` is set up before that fixture, so it is torn down after it. A
test that applied the set and then patched an owned variable had monkeypatch
record the applied value and put it back once the fixture had restored, and the
next module-scoped fixture or subprocess saw it. A test cannot watch its own
teardown, and the next test's fixture clears at setup, so a small suite runs in
a child process with the root conftest loaded: the module-scoped fixture of its
second test reads the environment before any function-scoped fixture runs.
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
import tempfile

import pytest

from faultmaven.config.tls_root_set import (
    EXTRA_CA_FILE_SETTING,
    OWNED_VARIABLES,
    apply_root_set,
)

OWNED = {name.upper() for name in (*OWNED_VARIABLES, EXTRA_CA_FILE_SETTING)}


def test_apply_then_patch_an_owned_variable(monkeypatch, tmp_path):
    monkeypatch.setenv("FAULTMAVEN_SKIP_DOTENV", "1")
    monkeypatch.setattr(tempfile, "tempdir", str(tmp_path))
    apply_root_set()
    monkeypatch.setenv("REQUESTS_CA_BUNDLE", str(tmp_path / "copy.pem"))


@pytest.fixture(scope="module")
def left_behind():
    return sorted(key for key in os.environ if key.upper() in OWNED)


def test_no_owned_variable_is_left_behind(left_behind):
    assert left_behind == []
"""


def test_a_patched_owned_variable_does_not_outlive_its_test(pytester, monkeypatch):
    monkeypatch.setenv(
        "PYTHONPATH",
        os.pathsep.join(filter(None, [str(REPO_ROOT), os.environ.get("PYTHONPATH")])),
    )
    pytester.makepyfile(textwrap.dedent(_SUITE))

    result = pytester.runpytest_subprocess(
        "-p",
        "no:cacheprovider",
        "-p",
        "no:randomly",
        "-p",
        "tests.conftest",
        "-o",
        "asyncio_mode=auto",
        timeout=600,
    )

    result.assert_outcomes(passed=2)
