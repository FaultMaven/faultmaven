"""The test session's logging is set once, and no test changes it for the next (#1944).

``DataSanitizer()`` was the first thing in a ``tests/performance`` run to ask
for a logger, so it built the logging singleton, which lowered the ROOT logger
to the zero-config ``local`` preset's DEBUG for the rest of the process. The
turn-commit benchmark that ran after it then paid for 87,546 aiosqlite DEBUG
records, and its p50 tripled.

``tests/conftest.py`` now pins ``LOG_LEVEL`` and configures the singleton at
import, and gives each test the root logger's level and handlers back as it
found them. This runs a child session against that real conftest, in the
environment the leak needed (no Redis, no LLM key, no ``ENVIRONMENT``: the
zero-config preset applies, whichever CI profile runs this), and checks the
state between tests:

* after the sanitizer runs, the root level and handlers are unchanged and
  ``aiosqlite`` is not enabled for DEBUG;
* after a test that lowers the root and adds a handler without restoring
  either, the next test still finds the session's level and handlers.
"""

from __future__ import annotations

import os
import pathlib

import pytest

pytest_plugins = ["pytester"]

pytestmark = pytest.mark.unit

#: Put on the child's path so ``tests.conftest`` and ``faultmaven`` import from
#: this checkout.
REPO_ROOT = pathlib.Path(__file__).resolve().parents[2]

#: Importing ``tests.conftest`` runs it: the ``LOG_LEVEL`` pin and the logging
#: configuration happen at import. The restore is a hook, so it is re-exported
#: by name for the child's pytest to register.
_CHILD_CONFTEST = """
from tests.conftest import pytest_runtest_protocol  # noqa: F401
"""

_CHILD_TESTS = """
import logging

STATE = {}


def _root_state():
    root = logging.getLogger()
    return logging.getLevelName(root.level), tuple(root.handlers)


def _assert_as_the_session_set_it():
    assert _root_state() == STATE["session"]
    assert not logging.getLogger("aiosqlite").isEnabledFor(logging.DEBUG)


def test_0_the_session_runs_at_info():
    STATE["session"] = _root_state()
    assert STATE["session"][0] == "INFO"


def test_1_the_sanitizer_runs():
    from faultmaven.infrastructure.security.redaction import DataSanitizer

    DataSanitizer().sanitize("contact user@example.com from 192.168.1.10")


def test_2_the_sanitizer_left_logging_as_it_found_it():
    _assert_as_the_session_set_it()


def test_3_a_test_lowers_the_root_and_adds_a_handler():
    root = logging.getLogger()
    root.setLevel(logging.DEBUG)
    root.addHandler(logging.NullHandler())


def test_4_the_next_test_finds_the_session_level_and_handlers():
    _assert_as_the_session_set_it()
"""

#: What makes the zero-config ``local`` preset (``LOG_LEVEL=DEBUG``) apply, or
#: not: any of these set and it declines. The Cloud job sets ``REDIS_HOST``,
#: which is why the leak only showed in Test Standalone.
_PRESET_DECIDERS = (
    "LOG_LEVEL",
    "CONFIG_PRESET",
    "ENVIRONMENT",
    "REDIS_HOST",
    "CHROMADB_URL",
    "OPENAI_API_KEY",
    "ANTHROPIC_API_KEY",
    "FIREWORKS_API_KEY",
    "GROQ_API_KEY",
    "COHERE_API_KEY",
)


def test_no_test_changes_the_logging_the_next_one_runs_under(pytester, monkeypatch):
    monkeypatch.setenv(
        "PYTHONPATH",
        os.pathsep.join(
            filter(
                None,
                [str(REPO_ROOT), str(pytester.path), os.environ.get("PYTHONPATH")],
            )
        ),
    )
    # A serial run of its own, with a data root of its own (fm#1772).
    for name in ("PYTEST_XDIST_WORKER", "DATABASE_URL", *_PRESET_DECIDERS):
        monkeypatch.delenv(name, raising=False)
    pytester.makeconftest(_CHILD_CONFTEST)
    pytester.makepyfile(test_child=_CHILD_TESTS)

    result = pytester.runpytest_subprocess("-p", "no:cacheprovider", timeout=600)

    result.assert_outcomes(passed=5)
