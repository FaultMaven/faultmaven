"""``generate_reports`` refuses a type the ``reports`` table cannot hold.

``ReportType`` is deliberately wider than ``reports_type_check`` — ``RUNBOOK``
is API/projection surface, not a ``reports`` row (see
``tests/unit/modules/case/test_report_vocabulary.py``). ``ReportGenerationRequest``
therefore accepts ``report_types: ["runbook"]``, which the OpenAPI schema
publishes as a valid request.

Before #520 that request travelled four layers down to the generator's dispatch
``else``, raised ``invalid_report_type`` there, and was then swallowed by the
per-type ``except Exception: continue`` in ``_generate_reports_locked`` — so the
caller was told ``report_generation_failed`` ("Failed to generate any reports").
That is a claim about the generation attempt, not about the request, and it is
the "misleading" half of #520's report arm. The refusal now happens up front,
screened against ``PERSISTED_REPORT_TYPES``, which is the same set the CHECK
pins.
"""

from unittest.mock import AsyncMock, MagicMock

import pytest

from faultmaven.exceptions import ValidationException
from faultmaven.modules.case.contracts import (
    PERSISTED_REPORT_TYPES,
    CaseReport,
    ReportType,
)
from faultmaven.modules.report.domain.services.report_generation_service import (
    ReportGenerationService,
)

pytestmark = pytest.mark.unit


def _service():
    service = ReportGenerationService(
        case_repository=AsyncMock(), lock_manager=None, pii_redactor=None
    )
    # A real count, not an AsyncMock: the regeneration-cap check compares it
    # with ``>=``. Left as a bare mock, removing the screen under test makes
    # these fail with a TypeError from the cap check instead of with the
    # assertion that the refusal did not happen — a failure that says nothing.
    service.case_repository.count_reports = AsyncMock(return_value=0)
    return service


def _case():
    case = MagicMock()
    case.case_id = "case_abc123def456"
    return case


def _a_report(report_type=ReportType.RESOLUTION_SUMMARY):
    """A real ``CaseReport``, not a mock.

    ``_generate_reports_locked`` feeds whatever the generator returns into
    ``ReportGenerationResponse``, so a bare mock makes the "screen removed"
    mutation fail on a pydantic type error rather than on ``DID NOT RAISE``.
    """
    return CaseReport(
        case_id="case_abc123def456",
        report_type=report_type,
        title="A sufficiently long report title",
        content="body",
        generation_status="completed",
        generation_time_ms=1,
    )


@pytest.mark.asyncio
async def test_runbook_is_refused_by_name_not_as_a_generation_failure():
    service = _service()
    service._validate_case_for_report_generation = MagicMock()
    service._generate_single_report = AsyncMock(return_value=_a_report())

    with pytest.raises(ValidationException) as exc:
        await service.generate_reports(_case(), [ReportType.RUNBOOK])

    # The code is what the route puts in the 400 body.
    assert str(exc.value) == "invalid_report_type"
    assert "runbook" in str(exc.value.details)
    # Refused before any work: the generator was never reached, so the failure
    # cannot be reported as "generation failed".
    service._generate_single_report.assert_not_awaited()


@pytest.mark.asyncio
async def test_one_unsupported_type_refuses_the_whole_request():
    """Not a partial success. A request naming an ungeneratable type is
    malformed, and answering it with "here are the two that worked" would leave
    the caller believing the third is merely flaky."""
    service = _service()
    service._validate_case_for_report_generation = MagicMock()
    service._generate_single_report = AsyncMock(return_value=_a_report())

    with pytest.raises(ValidationException):
        await service.generate_reports(
            _case(), [ReportType.RESOLUTION_SUMMARY, ReportType.RUNBOOK]
        )
    service._generate_single_report.assert_not_awaited()


@pytest.mark.asyncio
async def test_every_persistable_type_passes_the_screen():
    """CONTROL. Without it the screen could reject everything and the tests
    above would still pass."""
    for report_type in sorted(PERSISTED_REPORT_TYPES, key=lambda t: t.value):
        service = _service()
        service._validate_case_for_report_generation = MagicMock()
        service._generate_single_report = AsyncMock(return_value=_a_report(report_type))

        response = await service.generate_reports(_case(), [report_type])
        service._generate_single_report.assert_awaited_once()
        assert [r.report_type for r in response.reports] == [report_type]
