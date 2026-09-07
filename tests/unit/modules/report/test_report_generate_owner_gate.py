"""``POST /reports/generate`` writes, so it resolves the case by OWNERSHIP.

The three mutating report endpoints (edit, delete, link-case) already pass
``owner_only=True`` through ``authorize_case_access``. Generation did not, and it
is a write too: it mints report rows against the owner's case and flips which
one is current. A teammate holding a read share on the case could therefore
overwrite the owner's report set through this route while being refused on every
other one — a share is read visibility, not ownership (ADR-017 D4).
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


@pytest.fixture(autouse=True)
def _bound_enterprise():
    set_current_enterprise_id(ENTERPRISE)
    yield
    set_current_enterprise_id(STANDALONE_ENTERPRISE_ID)


def _user(user_id: str) -> SimpleNamespace:
    return SimpleNamespace(
        user_id=user_id, email=f"{user_id}@example.com", enterprise_id=ENTERPRISE
    )


def _case_service() -> MagicMock:
    """Owner ∪ shared by default; owner alone under ``owner_only``."""
    service = MagicMock()

    async def get_case(case_id, user_id=None, *, owner_only=False):
        if case_id != CASE_ID:
            return None
        if owner_only and user_id != OWNER:
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
    assert case_service.get_case.await_args.kwargs.get("owner_only") is True
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
