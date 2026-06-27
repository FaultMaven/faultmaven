"""Billing/quota exhaustion handling in report generation.

Regression for tracking issue #547: report generation generates each report
type in a loop and *swallows* per-report failures (`continue`) so a partial
failure still returns the reports that succeeded. But a billing/quota
exhaustion is permanent and affects every type — swallowing it surfaced a
misleading "no reports generated" 400 instead of telling the user the AI
provider is out of credits. The service must now abort and propagate billing as
a ServiceException carrying `error_code=QUOTA_EXHAUSTED`, which the route maps to
402 (same contract as the /turns endpoint).
"""

from unittest.mock import AsyncMock, MagicMock

import pytest

from faultmaven.exceptions import (
    QUOTA_EXHAUSTED,
    LLMException,
    ServiceException,
    ValidationException,
)
from faultmaven.modules.case.domain.owned_models.report import ReportType
from faultmaven.modules.report.domain.services.report_generation_service import (
    ReportGenerationService,
)


def _make_service():
    # lock_manager=None → generate path runs the loop without lock; the
    # per-report loop in _generate_reports_locked is what we exercise.
    return ReportGenerationService(
        case_repository=AsyncMock(),
        lock_manager=None,
        pii_redactor=None,
    )


def _make_case():
    case = MagicMock()
    case.case_id = "case_abc123def456"
    return case


@pytest.mark.asyncio
async def test_billing_aborts_and_propagates_quota_exhausted():
    """A billing error on the first report type aborts the loop and raises a
    ServiceException tagged QUOTA_EXHAUSTED — it is NOT swallowed."""
    service = _make_service()
    service._generate_single_report = AsyncMock(
        side_effect=LLMException(
            "You exceeded your current quota, please check your plan and billing details",
            status_code=429,
        )
    )

    with pytest.raises(ServiceException) as exc:
        await service._generate_reports_locked(
            _make_case(), [ReportType.RESOLUTION_SUMMARY, ReportType.RUNBOOK]
        )

    assert (exc.value.details or {}).get("error_code") == QUOTA_EXHAUSTED
    # Aborted on the first type — did not try the second against a dead provider.
    assert service._generate_single_report.await_count == 1


@pytest.mark.asyncio
async def test_non_billing_error_is_still_swallowed_then_validation_error():
    """A transient/non-billing failure keeps the existing partial-failure
    behavior: swallow-and-continue, and if nothing was produced raise a
    ValidationException (→ 400), not a billing 402."""
    service = _make_service()
    service._generate_single_report = AsyncMock(
        side_effect=RuntimeError("transient glitch")
    )

    with pytest.raises(ValidationException):
        await service._generate_reports_locked(
            _make_case(), [ReportType.RESOLUTION_SUMMARY, ReportType.RUNBOOK]
        )

    # Both types were attempted (swallow-and-continue), unlike the billing abort.
    assert service._generate_single_report.await_count == 2


class TestGenerateReportRouteMapsBillingTo402:
    """The generate_report route maps a QUOTA_EXHAUSTED ServiceException to 402
    Payment Required (x-error-code: QUOTA_EXHAUSTED), same as /turns."""

    @staticmethod
    async def _call(service_error):
        from fastapi import HTTPException  # noqa: F401

        from faultmaven.modules.report.api.routes import generate_report

        request = MagicMock()
        request.report_types = [MagicMock(value="resolution_summary")]
        case_service = MagicMock()
        case_service.get_case = AsyncMock(return_value=_make_case())
        generation_service = MagicMock()
        generation_service.generate_reports = AsyncMock(side_effect=service_error)
        current_user = MagicMock()
        current_user.user_id = "test-user-123"

        return await generate_report(
            request=request,
            case_id="case_abc123def456",
            current_user=current_user,
            tenant_provider=None,
            case_service=case_service,
            generation_service=generation_service,
        )

    @pytest.mark.asyncio
    async def test_billing_maps_to_402(self):
        from fastapi import HTTPException

        billing = ServiceException(
            "Report generation failed: AI provider is out of quota or credits",
            details={"error_code": QUOTA_EXHAUSTED},
        )
        with pytest.raises(HTTPException) as exc:
            await self._call(billing)

        assert exc.value.status_code == 402
        assert exc.value.headers["x-error-code"] == QUOTA_EXHAUSTED
        assert "Retry-After" not in exc.value.headers

    @pytest.mark.asyncio
    async def test_generic_service_error_still_500(self):
        from fastapi import HTTPException

        with pytest.raises(HTTPException) as exc:
            await self._call(ServiceException("disk on fire"))

        assert exc.value.status_code == 500
