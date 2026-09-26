"""Per-call cost of the observability and security shims when they are OFF.

Moved here from ``tests/integration/test_shims_integration.py`` (#1579),
where each asserted a raw wall-clock total in both required gates — 1000
calls in under 100 ms for the no-op ``track`` decorator, under 50 ms for the
``PIIRedactor`` passthrough. Those are product targets for a code path every
request crosses when tracing and redaction are disabled, so they are kept as
the ``product_target`` of a calibrated budget in ``budgets.py`` and asserted
raw in the ``FM_BENCHMARK_ABSOLUTE`` nightly; each pull request asserts a
regression anchor 2-3x the measured cost instead.

‼ Every comparison goes through ``assert_latency_within`` (#1557).
"""

import os
import time
from unittest.mock import patch

import pytest

from tests.wallclock import assert_latency_within

from .budgets import NOOP_TRACK_CALL, PII_PASSTHROUGH_CALL

CALLS = 1000
SAMPLES = 5


def _fastest_per_call(call) -> float:
    call()  # warm-up
    best = float("inf")
    for _ in range(SAMPLES):
        started = time.perf_counter()
        for _ in range(CALLS):
            call()
        best = min(best, time.perf_counter() - started)
    return best / CALLS


@pytest.mark.performance
def test_noop_decorator_minimal_overhead():
    """``@track`` with tracing off costs one extra Python frame, no more."""
    with patch.dict(os.environ, {"ENABLE_TRACING": "false"}):
        from faultmaven.infrastructure.shims import track

        @track("fast_operation")
        def fast_function():
            return 42

        per_call = _fastest_per_call(fast_function)

    print(f"\nNo-op track decorator: {per_call * 1e6:.3f}us per call")
    assert_latency_within(per_call, NOOP_TRACK_CALL, "no-op @track call")


@pytest.mark.performance
def test_pii_redactor_passthrough_minimal_overhead():
    """``PIIRedactor.redact`` with redaction off returns its input untouched."""
    with patch.dict(os.environ, {"ENABLE_PII_REDACTION": "false"}):
        from faultmaven.infrastructure.shims import PIIRedactor

        redactor = PIIRedactor()
        text = "Some text to process"
        assert redactor.redact(text) is text

        per_call = _fastest_per_call(lambda: redactor.redact(text))

    print(f"\nPIIRedactor passthrough: {per_call * 1e6:.3f}us per call")
    assert_latency_within(per_call, PII_PASSTHROUGH_CALL, "PIIRedactor passthrough")
