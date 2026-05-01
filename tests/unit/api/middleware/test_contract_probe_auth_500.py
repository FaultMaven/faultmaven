"""Contract-probe middleware: AUTH_500_VIOLATION rule precision tests.

The rule used to flag *every* 500 on /cases or /sessions paths as an auth
violation, regardless of whether auth had succeeded. That conflated genuine
auth-contract bugs (unauthenticated request → 500 instead of 401) with
downstream errors after auth had already passed (e.g. milestone-engine
MilestoneEngineError on the turns endpoint, surfaced by the 2026-05-01
DeepSeek run).

After the tightening on 2026-05-01, the rule requires the request to lack
an Authorization header before flagging a 500 as an auth-contract violation.
"""

from __future__ import annotations

import pytest

from faultmaven.api.middleware.contract_probe import ContractProbeMiddleware


def _detect(
    *,
    status_code: int,
    path: str,
    had_auth_header: bool,
) -> list[str]:
    """Drive ``_detect_violations`` directly with a synthetic probe_data dict."""
    middleware = ContractProbeMiddleware(app=None)
    probe_data = {
        "status_code": status_code,
        "headers": {},
        "had_auth_header": had_auth_header,
    }
    return middleware._detect_violations(
        probe_data,
        path=path,
        method="POST",
        query_params={},
    )


@pytest.mark.unit
class TestAuth500ViolationPrecision:
    """Pin the post-2026-05-01 contract: the rule fires only when the
    request was unauthenticated."""

    def test_500_unauthenticated_on_protected_path_fires(self):
        """Before-fix and after-fix behaviour agree here: an unauthenticated
        request to /cases that returns 500 IS an auth-contract bug."""
        violations = _detect(
            status_code=500, path="/api/v1/cases/abc/turns", had_auth_header=False
        )
        assert any("AUTH_500_VIOLATION" in v for v in violations)

    def test_500_authenticated_on_protected_path_does_not_fire(self):
        """The point of the tightening: an authenticated request that hits
        a downstream error (e.g. milestone-engine bug) returning 500 is
        a real bug — but it isn't an *auth-contract* violation."""
        violations = _detect(
            status_code=500, path="/api/v1/cases/abc/turns", had_auth_header=True
        )
        assert not any("AUTH_500_VIOLATION" in v for v in violations)

    def test_500_on_unprotected_path_does_not_fire_either_way(self):
        """The rule scopes to /cases and /sessions; other 500s aren't
        misattributed to auth."""
        for had_auth in (False, True):
            violations = _detect(
                status_code=500, path="/api/v1/health", had_auth_header=had_auth
            )
            assert not any("AUTH_500_VIOLATION" in v for v in violations)

    def test_non_500_status_does_not_fire(self):
        """Non-500 responses on protected paths never produce the violation."""
        for status in (200, 400, 401, 403, 404, 502):
            violations = _detect(
                status_code=status,
                path="/api/v1/cases/abc/turns",
                had_auth_header=False,
            )
            assert not any("AUTH_500_VIOLATION" in v for v in violations)

    def test_violation_message_documents_the_unauthenticated_case(self):
        """The new message names the specific scenario it flags so an
        oncall reading it doesn't conflate it with downstream-500 bugs."""
        violations = _detect(
            status_code=500, path="/api/v1/sessions", had_auth_header=False
        )
        message = next(v for v in violations if "AUTH_500_VIOLATION" in v)
        assert "unauthenticated" in message.lower()
