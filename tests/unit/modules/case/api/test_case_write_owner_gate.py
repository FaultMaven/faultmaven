"""A share grants READ, not WRITE — on every case-addressed mutation (ADR-017 D4).

The single-case gate has two halves and one flag: ``CaseService.get_case``
resolves through ``owner ∪ shared-to-my-teams`` by default and through ownership
alone under ``owner_only=True``. Every route that WRITES owes the second half,
and three of them were still asking for the first: the report regeneration
endpoint (which flips ``is_current`` on the owner's reports), the
knowledge-extraction endpoint (which mints a suggestion attributed to the case)
and the delete endpoint (which threw away the service's refusal and answered 204).

Inside one enterprise there is nothing else standing between a teammate and the
owner: RLS admits both rows, so the flag is the whole of the boundary. These
cases call the handlers directly, because the property is about which resolver
the handler asks for — not about routing.
"""

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import HTTPException

from faultmaven.modules.case.api.routes import (
    delete_case,
    extract_knowledge_from_case,
    generate_case_reports,
)

pytestmark = [pytest.mark.unit, pytest.mark.security]

CASE_ID = "case_aaaabbbbcccc"
OWNER = "user_owner"
TEAMMATE = "user_teammate"


def _user(user_id: str) -> SimpleNamespace:
    return SimpleNamespace(user_id=user_id, enterprise_id="ent_one")


def _case_service_that_refuses_non_owners() -> MagicMock:
    """A stand-in for the real resolver's two answers.

    ``owner_only=True`` is the ONLY thing that separates the teammate from the
    owner here, so a handler that omits it gets a case back and the assertion
    fails — which is the point.
    """
    service = MagicMock()

    async def get_case(case_id, user_id=None, *, owner_only=False):
        if case_id != CASE_ID:
            return None
        if owner_only and user_id != OWNER:
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
    case_service = _case_service_that_refuses_non_owners()
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
    assert case_service.get_case.await_args.kwargs.get("owner_only") is True


async def test_report_regeneration_still_serves_the_owner():
    """The control: refusing everyone would satisfy the case above."""
    case_service = _case_service_that_refuses_non_owners()
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


# ---------------------------------------------------------------------------
# POST /cases/{case_id}/extract-knowledge — mints a suggestion from the owner's
# transcript and evidence.
# ---------------------------------------------------------------------------


async def test_knowledge_extraction_refuses_a_teammate_with_a_read_share():
    case_service = _case_service_that_refuses_non_owners()
    suggestion_service = MagicMock()
    suggestion_service.extract_knowledge_from_case = AsyncMock()

    with pytest.raises(HTTPException) as exc:
        await extract_knowledge_from_case(
            case_id=CASE_ID,
            request_body=None,
            case_service=case_service,
            suggestion_service=suggestion_service,
            current_user=_user(TEAMMATE),
        )

    assert exc.value.status_code == 404
    assert case_service.get_case.await_args.kwargs.get("owner_only") is True
    suggestion_service.extract_knowledge_from_case.assert_not_awaited()


# ---------------------------------------------------------------------------
# DELETE /cases/{case_id} — the service already refuses a non-owner; the route
# threw the answer away.
# ---------------------------------------------------------------------------


async def test_a_refused_delete_is_not_reported_as_success():
    """``hard_delete_case`` answers False for a visible case the caller does not own."""
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
