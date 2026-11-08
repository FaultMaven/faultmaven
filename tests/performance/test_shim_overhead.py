"""Per-call cost of the security shim when it is OFF.

Moved here from ``tests/integration/test_shims_integration.py`` (#1579),
where it asserted a raw wall-clock total in both required gates — 1000
calls of the ``PIIRedactor`` passthrough in under 50 ms. That is a product
target for a code path every request crosses when redaction is disabled, so it
is kept as the ``product_target`` of a calibrated budget in ``budgets.py`` and
asserted raw in the ``FM_BENCHMARK_ABSOLUTE`` nightly; each pull request asserts
a regression anchor 2-3x the measured cost instead. The observability shim's
no-op ``track`` went with the shim (#2616 part c).

‼ Every comparison goes through ``assert_latency_within`` (#1557).
"""

import os
from unittest.mock import patch

import pytest

from tests.wallclock import assert_latency_within
from tests.wallclock.sampling import fastest_of_windows

from .budgets import PII_PASSTHROUGH_CALL

CALLS = 1000
SAMPLES = 5


def _fastest_per_call(call) -> float:
    """Seconds per call: the fastest of ``SAMPLES`` windows of ``CALLS``."""
    call()  # warm-up

    def calls():
        for _ in range(CALLS):
            call()

    return fastest_of_windows(lambda window: window.seconds(calls), SAMPLES) / CALLS


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
