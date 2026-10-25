"""The test session's logging is set once, and no test changes it for the next (#1944).

``DataSanitizer()`` was the first thing in a ``tests/performance`` run to ask
for a logger, so it built the logging singleton, which lowered the ROOT logger
to the zero-config ``local`` preset's DEBUG for the rest of the process. The
turn-commit benchmark that ran after it then paid for 87,546 aiosqlite DEBUG
records, and its p50 tripled.

``tests/conftest.py`` now pins ``LOG_LEVEL`` and configures the singleton at
import, and gives each test the root logger's level and handlers back as it
found them. It does the same for structlog's configuration and for the
singleton itself (#1972): ``tests/infrastructure/logging/test_config.py``'s
teardowns call ``structlog.reset_defaults()`` and drop the singleton, and every
logger the app bound at import (the middleware's) then wrote to structlog's
default ``PrintLogger`` on stdout for the rest of that xdist worker, so a later
test's ``caplog`` assertion saw nothing. This runs a child session against that
real conftest, in the environment the leak needed (no Redis, no LLM key, no
``ENVIRONMENT``: the zero-config preset applies, whichever CI profile runs
this), and checks the state between tests:

* after the sanitizer runs, the root level and handlers are unchanged and
  ``aiosqlite`` is not enabled for DEBUG;
* after a test that lowers the root and adds a handler without restoring
  either, the next test still finds the session's level and handlers.
* a singleton rebuilt mid-session from freshly read settings, as the
  logging config tests leave it to be, still lands on the session's level:
  ``clean_test_environment`` keeps ``LOG_LEVEL`` pinned past session start;
* after a test that resets structlog and drops the singleton, a logger bound
  at import reaches ``caplog`` and not stdout, structlog's configuration is
  the session's, and the singleton is the one the session built;
* after a test that resets structlog under a live singleton, the real
  middleware logger still reaches ``caplog`` and not stdout.
"""

from __future__ import annotations

import os
import pathlib

import pytest

from faultmaven.infrastructure.llm.providers.registry import (
    PROVIDER_SCHEMA,
    configuration_env_var,
)

pytest_plugins = ["pytester"]

pytestmark = pytest.mark.unit

#: Put on the child's path so ``tests.conftest`` and ``faultmaven`` import from
#: this checkout.
REPO_ROOT = pathlib.Path(__file__).resolve().parents[2]

#: Importing ``tests.conftest`` runs it: the ``LOG_LEVEL`` pin and the logging
#: configuration happen at import. The restore is a hook and the session pin is
#: a fixture, so both are re-exported by name for the child's pytest to
#: register: without ``clean_test_environment`` the child would never run the
#: code that keeps ``LOG_LEVEL`` pinned for the rest of the session.
_CHILD_CONFTEST = """
from tests.conftest import (  # noqa: F401
    clean_test_environment,
    pytest_runtest_protocol,
)
"""

_CHILD_TESTS = """
import logging

import structlog

import faultmaven.infrastructure.logging.config as logging_config

# The loggers under test are kept in here, never as module-level names of this
# file: pytest's collection probes every attribute of a test module for
# ``__test__``, which resolves a lazy structlog proxy and, under the session's
# ``cache_logger_on_first_use``, caches it for good, so a proxy held in a test
# module could never see a reset and the guard would pass on a conftest that
# does not restore anything.
STATE = {}


def _root_state():
    root = logging.getLogger()
    return logging.getLevelName(root.level), tuple(root.handlers)


def _assert_as_the_session_set_it():
    assert _root_state() == STATE["session"]
    assert not logging.getLogger("aiosqlite").isEnabledFor(logging.DEBUG)


def _reaches_caplog_and_not_stdout(logger, event, caplog, capsys):
    caplog.set_level(logging.INFO)
    logger.info(event)
    assert [r for r in caplog.records if event in r.getMessage()], (
        "the line did not go through stdlib logging"
    )
    assert event not in capsys.readouterr().out, (
        "the line went to structlog's default PrintLogger on stdout"
    )


def test_0_the_session_runs_at_info():
    STATE["session"] = _root_state()
    STATE["structlog"] = structlog.get_config()
    STATE["singleton"] = logging_config._logger_config
    assert STATE["session"][0] == "INFO"
    assert STATE["singleton"] is not None


def test_0b_the_loggers_are_bound_as_the_app_binds_them():
    # Bound now and not called until the test that reads each. A lazy proxy
    # resolves structlog's configuration when it is first called, so it is the
    # test that follows a reset that decides where its lines go.
    from faultmaven.api.middleware.logging import logger as middleware_logger

    STATE["module_level"] = logging_config.get_logger("child.module_level_logger")
    STATE["middleware"] = middleware_logger


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


def test_5_a_rebuilt_singleton_lands_on_the_session_level():
    # What tests/infrastructure/logging/test_config.py's teardowns leave
    # behind: no singleton, so the next get_logger builds one from settings
    # read afresh. With the zero-config preset applied, only the session's
    # LOG_LEVEL pin keeps that rebuild off DEBUG.
    import faultmaven.infrastructure.logging.config as logging_config
    from faultmaven.config.settings import reset_settings

    reset_settings()
    logging_config._logger_config = None
    try:
        logging_config.get_logger(__name__)
        assert logging.getLevelName(logging.getLogger().level) == STATE["session"][0]
    finally:
        reset_settings()


def test_6_a_test_resets_structlog_and_drops_the_singleton():
    # What TestGetLogger.setup_method/teardown_method in
    # tests/infrastructure/logging/test_config.py leave behind.
    structlog.reset_defaults()
    logging_config._logger_config = None


def test_7_a_logger_bound_at_import_still_reaches_caplog(caplog, capsys):
    _reaches_caplog_and_not_stdout(
        STATE["module_level"], "child module level event", caplog, capsys
    )


def test_8_structlog_is_configured_as_the_session_configured_it():
    assert structlog.get_config() == STATE["structlog"]


def test_9_the_singleton_is_the_one_the_session_built():
    assert logging_config._logger_config is STATE["singleton"]


def test_10_a_test_resets_structlog_under_a_live_singleton():
    # What TestFaultMavenLogger.setup_method/teardown_method leave behind:
    # get_logger rebuilds the configuration only while the singleton is None,
    # so nothing ever reconfigures structlog after this.
    assert logging_config._logger_config is not None
    structlog.reset_defaults()


def test_11_the_middleware_logger_still_reaches_caplog(caplog, capsys):
    # The logger the #1972 failures used: faultmaven.api.middleware.logging
    # binds it at import, so it resolves structlog's configuration here.
    _reaches_caplog_and_not_stdout(
        STATE["middleware"], "Request completed: child -> 200", caplog, capsys
    )
"""

#: Removed from the child's environment so the zero-config ``local`` preset
#: (``LOG_LEVEL=DEBUG``) applies there, as it did in Test Standalone:
#:
#: * ``LOG_LEVEL`` and ``CONFIG_PRESET`` because the child must start from
#:   neither pin nor preset of the parent's: the preset never overrides a
#:   ``LOG_LEVEL`` already set, and a set ``CONFIG_PRESET`` applies that
#:   preset instead of the detected one, except ``none``, which applies none
#:   (#2243);
#: * the rest because ``detect_zero_config_preset`` declines the preset when
#:   any of them is set (``ENVIRONMENT`` only when it names something other
#:   than ``development``). Test Cloud's suite declined the preset (then with
#:   ``REDIS_HOST``; ``CONFIG_PRESET=none`` since #2243), which is why the
#:   leak only showed in Test Standalone.
_PRESET_DECIDERS = (
    "LOG_LEVEL",
    "CONFIG_PRESET",
    "ENVIRONMENT",
    "REDIS_HOST",
    "CHROMADB_URL",
    # Every provider the registry knows: the preset check asks the registry,
    # not a list of five keys (#1984), so this must too.
    *(configuration_env_var(provider) for provider in PROVIDER_SCHEMA),
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

    result.assert_outcomes(passed=13)
