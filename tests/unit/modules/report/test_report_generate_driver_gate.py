"""``POST /reports/generate`` writes, so it resolves the case by its DRIVER.

The mutating report endpoints (generate, edit, delete, link-case) pass
``driver_only=True`` through ``authorize_case_access``: generation mints report
rows against the case and flips which one is current, an investigation write
that is the case's effective driver's (ADR-020 D2). Every other reader — a
teammate holding a share, or the creator while someone else drives — is
refused.
"""

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import HTTPException

from faultmaven.config.constants import STANDALONE_ENTERPRISE_ID
from faultmaven.config.tenant_context import set_current_enterprise_id
from faultmaven.modules.case.contracts import ReportGenerationRequest, ReportType
from faultmaven.modules.report.api.routes import generate_report

pytestmark = [pytest.mark.unit, pytest.mark.security]

ENTERPRISE = "22222222-2222-2222-2222-222222222222"
CASE_ID = "case_aaaabbbbcccc"
OWNER = "user_owner"
TEAMMATE = "user_teammate"
DRIVER = "user_driver"


@pytest.fixture(autouse=True)
def _bound_enterprise():
    set_current_enterprise_id(ENTERPRISE)
    yield
    set_current_enterprise_id(STANDALONE_ENTERPRISE_ID)


def _user(user_id: str) -> SimpleNamespace:
    return SimpleNamespace(
        user_id=user_id, email=f"{user_id}@example.com", enterprise_id=ENTERPRISE
    )


def _case_service(driver: str = OWNER) -> MagicMock:
    """Everyone here reads the case; ``driver_only`` admits ``driver`` alone."""
    service = MagicMock()

    async def get_case(case_id, user_id=None, *, driver_only=False, creator_only=False):
        if case_id != CASE_ID:
            return None
        if driver_only and user_id != driver:
            return None
        return SimpleNamespace(
            case_id=CASE_ID, user_id=OWNER, enterprise_id=ENTERPRISE, title="Outage"
        )

    service.get_case = AsyncMock(side_effect=get_case)
    return service


def _generation_service() -> MagicMock:
    service = MagicMock()
    service.generate_reports = AsyncMock(
        return_value=SimpleNamespace(reports=[], model_dump=lambda: {"reports": []})
    )
    return service


async def test_a_teammate_with_a_read_share_cannot_generate_reports():
    case_service = _case_service()
    generation = _generation_service()

    with pytest.raises(HTTPException) as exc:
        await generate_report(
            request=ReportGenerationRequest(report_types=[ReportType.CLOSURE_SUMMARY]),
            case_id=CASE_ID,
            case_service=case_service,
            generation_service=generation,
            current_user=_user(TEAMMATE),
        )

    assert exc.value.status_code == 404
    assert case_service.get_case.await_args.kwargs.get("driver_only") is True
    generation.generate_reports.assert_not_awaited()


async def test_the_owner_still_generates_reports():
    """The control: refusing everyone would satisfy the case above."""
    generation = _generation_service()

    response = await generate_report(
        request=ReportGenerationRequest(report_types=[ReportType.CLOSURE_SUMMARY]),
        case_id=CASE_ID,
        case_service=_case_service(),
        generation_service=generation,
        current_user=_user(OWNER),
    )

    assert response.reports == []
    generation.generate_reports.assert_awaited_once()


async def test_the_creator_cannot_generate_while_another_drives():
    generation = _generation_service()

    with pytest.raises(HTTPException) as exc:
        await generate_report(
            request=ReportGenerationRequest(report_types=[ReportType.CLOSURE_SUMMARY]),
            case_id=CASE_ID,
            case_service=_case_service(driver=DRIVER),
            generation_service=generation,
            current_user=_user(OWNER),
        )

    assert exc.value.status_code == 404
    generation.generate_reports.assert_not_awaited()


async def test_the_assigned_driver_generates_reports():
    generation = _generation_service()

    response = await generate_report(
        request=ReportGenerationRequest(report_types=[ReportType.CLOSURE_SUMMARY]),
        case_id=CASE_ID,
        case_service=_case_service(driver=DRIVER),
        generation_service=generation,
        current_user=_user(DRIVER),
    )

    assert response.reports == []
    generation.generate_reports.assert_awaited_once()
