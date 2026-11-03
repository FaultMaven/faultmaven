"""A ChromaDB client registered with ``closed_at_teardown`` is closed when its test ends.

chromadb keeps a local client's System, with its SQLite pool, HNSW segment
files and Rust runtime, in a process-wide cache until the client is closed.
Unclosed, the clients of a serial run held 1,130 descriptors and failed every
later test that opened a file under a soft ``ulimit -n`` of 1024 with EMFILE
(``tests/chroma_clients.py``).

A test cannot watch its own teardown, so a two-test suite runs in a child
process with the root conftest loaded, as ``test_reset_container_fixture.py``
does. The first test opens a real client under its ``tmp_path`` and checks
that it holds descriptors there. The child's own conftest records what is
still held once that test's teardown has finished, which also catches a
close deferred to the next test's setup. The second test asserts what was
released.

``close()`` stops the System and releases the HNSW segment files at once.
chromadb's two SQLite handles go when the closed client's objects are
garbage-collected, so they are bounded and do not pile up. Before
``close()`` the collector cannot reach any of it.
"""

from __future__ import annotations

import os
import pathlib
import sys
import textwrap

import pytest

from tests.chroma_clients import close_the_clients_opened, closed_at_teardown

pytest_plugins = ["pytester"]

pytestmark = pytest.mark.unit

REPO_ROOT = pathlib.Path(__file__).resolve().parents[2]

_SUITE = """
import os

from faultmaven.infrastructure.persistence.chromadb_store import (
    create_persistent_client,
)
from tests.chroma_clients import closed_at_teardown

OPENED = {}
HELD_AFTER_TEARDOWN = {}


def _descriptors_under(path):
    held = []
    for fd in os.listdir("/proc/self/fd"):
        try:
            target = os.readlink(f"/proc/self/fd/{fd}")
        except OSError:
            continue
        if target.startswith(path):
            held.append(target)
    return held


def test_a_test_opens_a_client_and_registers_it(tmp_path):
    path = str(tmp_path / "chroma")
    client = closed_at_teardown(create_persistent_client(path))
    client.get_or_create_collection("kb-probe").add(
        ids=["a"], embeddings=[[0.1, 0.2]]
    )
    OPENED["path"] = path
    # Positive control: the open client holds files under its path.
    assert _descriptors_under(path)


def test_the_first_tests_teardown_released_its_client():
    at_teardown = HELD_AFTER_TEARDOWN["at_teardown"]
    assert [f for f in at_teardown if not f.endswith("/chroma.sqlite3")] == []
    assert HELD_AFTER_TEARDOWN["after_gc"] == []
"""

# Runs after the whole of a test's teardown, the root conftest's autouse close
# included, so a close deferred to the next test's setup is caught too.
_CONFTEST = """
import gc

import pytest


@pytest.hookimpl(wrapper=True)
def pytest_runtest_teardown(item, nextitem):
    result = yield
    path = item.module.OPENED.get("path")
    held = item.module.HELD_AFTER_TEARDOWN
    if path is not None and not held:
        held["at_teardown"] = item.module._descriptors_under(path)
        gc.collect()
        held["after_gc"] = item.module._descriptors_under(path)
    return result
"""


@pytest.mark.skipif(not sys.platform.startswith("linux"), reason="reads /proc/self/fd")
def test_a_registered_client_releases_its_descriptors_at_teardown(
    pytester, monkeypatch
):
    monkeypatch.setenv(
        "PYTHONPATH",
        os.pathsep.join(filter(None, [str(REPO_ROOT), os.environ.get("PYTHONPATH")])),
    )
    pytester.makeconftest(textwrap.dedent(_CONFTEST))
    pytester.makepyfile(textwrap.dedent(_SUITE))

    result = pytester.runpytest_subprocess(
        "-p",
        "no:cacheprovider",
        "-p",
        "tests.conftest",
        timeout=600,
    )

    result.assert_outcomes(passed=2)


class _Client:
    def __init__(self, name: str, closed: list[str], error: Exception | None = None):
        self.name, self.closed, self.error = name, closed, error

    def close(self) -> None:
        self.closed.append(self.name)
        if self.error is not None:
            raise self.error


def test_every_client_is_closed_newest_first_and_the_registry_emptied():
    closed: list[str] = []
    first = closed_at_teardown(_Client("first", closed))
    closed_at_teardown(_Client("second", closed))

    close_the_clients_opened()
    close_the_clients_opened()

    assert closed == ["second", "first"]
    assert isinstance(first, _Client)  # handed back unchanged


def test_a_close_that_raises_still_closes_the_rest_and_is_raised():
    closed: list[str] = []
    closed_at_teardown(_Client("first", closed, RuntimeError("first failed")))
    closed_at_teardown(_Client("second", closed, ValueError("second failed")))
    closed_at_teardown(_Client("third", closed))

    with pytest.raises(ValueError, match="second failed"):
        close_the_clients_opened()

    assert closed == ["third", "second", "first"]
    close_the_clients_opened()  # nothing left for the next test's teardown
    assert closed == ["third", "second", "first"]
