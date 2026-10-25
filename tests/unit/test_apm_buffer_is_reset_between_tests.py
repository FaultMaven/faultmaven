"""One test's buffered APM metrics never reach another test's lifespan (#2296).

``apm_integration`` buffers a metric for every request through the performance
middleware, and a live app flushes the buffer (a background loop its lifespan
starts, and its shutdown) to the Prometheus pushgateway (localhost:9091) when
``PROMETHEUS_ENABLED`` is on, as in Test Cloud. A test that left metrics behind
had them flushed by whichever app boot ran next on its worker, and the network
guard failed that other test.

The root conftest's ``_reset_observability_singletons`` clears the buffer after
every test. A test cannot watch its own teardown, so a two-test suite runs in a
child process with the root conftest loaded: the first records a metric, the
second must find the buffer empty.
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
from faultmaven.infrastructure.observability.apm_integration import apm_integration


def test_a_request_leaves_a_metric():
    apm_integration.record_operation(operation_name="probe", duration_ms=1.0)
    assert apm_integration.metrics_buffer


def test_the_next_test_finds_none():
    assert apm_integration.metrics_buffer == []
"""


def test_the_buffer_is_cleared_after_each_test(pytester, monkeypatch):
    monkeypatch.setenv(
        "PYTHONPATH",
        os.pathsep.join(filter(None, [str(REPO_ROOT), os.environ.get("PYTHONPATH")])),
    )
    pytester.makepyfile(textwrap.dedent(_SUITE))

    result = pytester.runpytest_subprocess(
        "-p", "no:cacheprovider", "-p", "tests.conftest", timeout=600
    )

    result.assert_outcomes(passed=2)
