"""Reading a case is not writing it: the investigation writes are the DRIVER's,
governance is the CREATOR's (ADR-020 D2).

The single-case gate resolves through ``creator ∪ shared-to-my-teams`` by
default, through the effective driver under ``driver_only=True`` and through
the creator under ``creator_only=True``. The report regeneration endpoint
(which flips ``is_current`` on the case's reports) is an investigation write;
delete is governance, refused by the service for anyone but the creator.

Inside one enterprise there is nothing else standing between two readers: RLS
admits both rows, so the flag is the whole of the boundary. These cases call
the handlers directly, because the property is about which resolver the
handler asks for — not about routing.
"""

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import HTTPException

from faultmaven.modules.case.api.routes.cases import delete_case
from faultmaven.modules.case.api.routes.reports import generate_case_reports

pytestmark = [pytest.mark.unit, pytest.mark.security]

CASE_ID = "case_aaaabbbbcccc"
OWNER = "user_owner"
TEAMMATE = "user_teammate"
DRIVER = "user_driver"


def _user(user_id: str) -> SimpleNamespace:
    return SimpleNamespace(user_id=user_id, enterprise_id="ent_one")


def _case_service_that_refuses_non_drivers(driver: str = OWNER) -> MagicMock:
    """A stand-in for the real resolver's answers: everyone here reads the
    case; ``driver_only=True`` admits ``driver`` alone.

    ``driver_only=True`` is the ONLY thing that separates a reader from the
    driver here, so a handler that omits it gets a case back and the assertion
    fails — which is the point.
    """
    service = MagicMock()

    async def get_case(case_id, user_id=None, *, driver_only=False, creator_only=False):
        if case_id != CASE_ID:
            return None
        if driver_only and user_id != driver:
            return None
        if creator_only and user_id != OWNER:
            return None
        return SimpleNamespace(
            case_id=CASE_ID,
            user_id=OWNER,
            enterprise_id="ent_one",
            is_terminal=True,
            title="Checkout latency spike",
        )

    service.get_case = AsyncMock(side_effect=get_case)
    return service


# ---------------------------------------------------------------------------
# POST /cases/{case_id}/reports — regeneration flips is_current on the owner's
# reports, so it is a write on rows a read share never covered.
# ---------------------------------------------------------------------------


async def test_report_regeneration_refuses_a_teammate_with_a_read_share():
    case_service = _case_service_that_refuses_non_drivers()
    fastapi_request = MagicMock()
    fastapi_request.app.state.report_generation_service = MagicMock()

    with pytest.raises(HTTPException) as exc:
        await generate_case_reports(
            case_id=CASE_ID,
            fastapi_request=fastapi_request,
            request_body={"report_types": ["closure_summary"]},
            case_service=case_service,
            current_user=_user(TEAMMATE),
        )

    assert exc.value.status_code == 404
    assert case_service.get_case.await_args.kwargs.get("driver_only") is True


async def test_report_regeneration_still_serves_the_owner():
    """The control: refusing everyone would satisfy the case above."""
    case_service = _case_service_that_refuses_non_drivers()
    generation = MagicMock()
    generation.generate_reports = AsyncMock(
        return_value=SimpleNamespace(model_dump=lambda: {"reports": []})
    )
    fastapi_request = MagicMock()
    fastapi_request.app.state.report_generation_service = generation

    result = await generate_case_reports(
        case_id=CASE_ID,
        fastapi_request=fastapi_request,
        request_body={"report_types": ["closure_summary"]},
        case_service=case_service,
        current_user=_user(OWNER),
    )

    assert result == {"reports": []}


async def test_report_regeneration_refuses_the_creator_while_another_drives():
    """The split itself: once the case is handed to DRIVER, its creator reads
    it but no longer holds the investigation writes."""
    case_service = _case_service_that_refuses_non_drivers(driver=DRIVER)
    fastapi_request = MagicMock()
    fastapi_request.app.state.report_generation_service = MagicMock()

    with pytest.raises(HTTPException) as exc:
        await generate_case_reports(
            case_id=CASE_ID,
            fastapi_request=fastapi_request,
            request_body={"report_types": ["closure_summary"]},
            case_service=case_service,
            current_user=_user(OWNER),
        )

    assert exc.value.status_code == 404


async def test_report_regeneration_serves_the_assigned_driver():
    case_service = _case_service_that_refuses_non_drivers(driver=DRIVER)
    generation = MagicMock()
    generation.generate_reports = AsyncMock(
        return_value=SimpleNamespace(model_dump=lambda: {"reports": []})
    )
    fastapi_request = MagicMock()
    fastapi_request.app.state.report_generation_service = generation

    result = await generate_case_reports(
        case_id=CASE_ID,
        fastapi_request=fastapi_request,
        request_body={"report_types": ["closure_summary"]},
        case_service=case_service,
        current_user=_user(DRIVER),
    )

    assert result == {"reports": []}


# ---------------------------------------------------------------------------
# DELETE /cases/{case_id} — the service already refuses a non-owner; the route
# threw the answer away.
# ---------------------------------------------------------------------------


async def test_a_refused_delete_is_not_reported_as_success():
    """``hard_delete_case`` answers False for a visible case the caller did not
    create — its driver included (delete is governance, ADR-020 D2)."""
    case_service = MagicMock()
    case_service.hard_delete_case = AsyncMock(return_value=False)

    with pytest.raises(HTTPException) as exc:
        await delete_case(
            case_id=CASE_ID,
            case_service=case_service,
            current_user=_user(TEAMMATE),
        )

    assert exc.value.status_code == 403


async def test_a_delete_the_service_performed_still_answers_204():
    """The control, and the idempotent shape: an absent case answers True too."""
    case_service = MagicMock()
    case_service.hard_delete_case = AsyncMock(return_value=True)

    response = await delete_case(
        case_id=CASE_ID,
        case_service=case_service,
        current_user=_user(OWNER),
    )

    assert response.status_code == 204
