"""Contract-probe middleware: pagination-header detection + severity.

The probe flags a paginated list endpoint (GET with limit/offset) that omits
the canonical ``X-Total-Count`` header. That signal is useful, but it is an
advisory convention drift — the response body still carries the total — so it
must NOT be logged at ``error`` (which pages on-call and pollutes triage). It
is logged at ``warning`` instead, while genuine broken-contract violations
(missing Location / Retry-After) stay at ``error``.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from faultmaven.api.middleware.contract_probe import ContractProbeMiddleware


def _detect(*, headers: dict, query_params: dict, status_code: int = 200) -> list[str]:
    middleware = ContractProbeMiddleware(app=None)
    probe_data = {
        "status_code": status_code,
        "headers": headers,
        "had_auth_header": True,
    }
    return middleware._detect_violations(
        probe_data,
        path="/api/v1/cases/abc/messages",
        method="GET",
        query_params=query_params,
    )


@pytest.mark.unit
class TestPaginationHeaderDetection:
    def test_paginated_list_without_total_count_is_flagged(self):
        violations = _detect(headers={}, query_params={"limit": "50", "offset": "0"})
        assert any("MISSING_PAGINATION_HEADER" in v for v in violations)

    def test_paginated_list_with_total_count_is_clean(self):
        violations = _detect(
            headers={"X-Total-Count": "74"}, query_params={"limit": "50", "offset": "0"}
        )
        assert not any("MISSING_PAGINATION_HEADER" in v for v in violations)

    def test_non_paginated_get_is_not_treated_as_a_list(self):
        # No limit/offset → not a list endpoint → no pagination requirement.
        violations = _detect(headers={}, query_params={})
        assert not any("MISSING_PAGINATION_HEADER" in v for v in violations)


@pytest.mark.unit
class TestViolationSeverity:
    """``_log_contract_probe`` logs advisory-only violations at warning, and
    anything with a genuine broken-contract violation at error."""

    def _log(self, violations: list[str]):
        middleware = ContractProbeMiddleware(app=None)
        probe_data = {
            "status_code": 200,
            "path": "/api/v1/cases/abc/messages",
            "method": "GET",
            "correlation_id": "corr-1",
            "contract_violations": violations,
            "response_time_ms": 1.0,
        }
        request = MagicMock()
        request.headers = {}
        with patch("faultmaven.api.middleware.contract_probe.logger") as mock_logger:
            middleware._log_contract_probe(probe_data, request, MagicMock())
        return mock_logger

    def test_pagination_only_violation_logs_at_warning(self):
        mock_logger = self._log(
            ["MISSING_PAGINATION_HEADER: List endpoint missing X-Total-Count header"]
        )
        mock_logger.error.assert_not_called()
        mock_logger.warning.assert_called_once()

    def test_critical_violation_logs_at_error(self):
        mock_logger = self._log(
            ["MISSING_LOCATION_HEADER: 201 response missing Location header"]
        )
        mock_logger.error.assert_called_once()

    def test_mixed_violations_log_at_error(self):
        # An advisory + a critical together must escalate to error.
        mock_logger = self._log(
            [
                "MISSING_PAGINATION_HEADER: List endpoint missing X-Total-Count header",
                "MISSING_LOCATION_HEADER: 201 response missing Location header",
            ]
        )
        mock_logger.error.assert_called_once()
