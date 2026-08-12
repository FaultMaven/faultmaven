"""Session routes scope on the case, not only on the organization (#1044).

``APIInvestigationSessionService`` authorizes every session operation against
``case.organization_id`` alone, while the rest of the case surface is
owner ∪ shared-to-my-teams. Under that asymmetry any member of an organization
could read another member's ``session_goal``, ``findings_summary`` and token
usage, and pause, resume, update or complete their sessions — with the case
itself unreadable to them.

Every route in this router is nested under ``/cases/{case_id}``, so the canonical
single-case gate applies whole. It is declared as a router-level dependency: a
route added later inherits it rather than having to remember it. These tests
assert both halves — that the gate denies a stranger, and that it is attached to
every route rather than to the handful in the table.
"""

from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import HTTPException
from fastapi.routing import APIRoute

from faultmaven.api.routes.sessions import require_case_access, router
from faultmaven.exceptions import NotFoundError
from faultmaven.modules.case.domain.models import Case
from faultmaven.modules.case.domain.services.case_service import CaseService

SHARED_ORG = "org_beta_group"
VICTIM = "user_victim"
ATTACKER = "user_attacker"
CASE_ID = "case_aaaabbbbcccc"


def _case_service_holding_victims_case() -> CaseService:
    """A real CaseService, so the denial comes from the access gate itself."""
    case = Case(
        case_id=CASE_ID,
        user_id=VICTIM,
        organization_id=SHARED_ORG,
        title="Checkout latency spike",
    )
    repository = MagicMock()
    repository.get = AsyncMock(return_value=case)
    return CaseService(case_repository=repository)


def _user(user_id: str) -> MagicMock:
    user = MagicMock()
    user.user_id = user_id
    user.organization_id = SHARED_ORG
    return user


@pytest.mark.asyncio
@pytest.mark.security
async def test_stranger_in_the_same_org_is_denied():
    with pytest.raises(NotFoundError):
        await require_case_access(
            case_id=CASE_ID,
            current_user=_user(ATTACKER),
            case_service=_case_service_holding_victims_case(),
        )


@pytest.mark.asyncio
async def test_owner_passes():
    await require_case_access(
        case_id=CASE_ID,
        current_user=_user(VICTIM),
        case_service=_case_service_holding_victims_case(),
    )


@pytest.mark.asyncio
async def test_teammate_with_a_share_passes():
    """The gate is owner ∪ shared-to-my-teams, not owner-only."""
    case_service = _case_service_holding_victims_case()
    case_service._resolve_shared_case_ids = AsyncMock(return_value=[CASE_ID])

    await require_case_access(
        case_id=CASE_ID,
        current_user=_user(ATTACKER),
        case_service=case_service,
    )


@pytest.mark.asyncio
@pytest.mark.security
async def test_missing_case_service_fails_closed():
    with pytest.raises(HTTPException) as exc:
        await require_case_access(
            case_id=CASE_ID,
            current_user=_user(ATTACKER),
            case_service=None,
        )

    assert exc.value.status_code == 503


@pytest.mark.security
def test_every_session_route_carries_the_gate():
    """Router-level, so this holds for routes that do not exist yet."""
    routes = [route for route in router.routes if isinstance(route, APIRoute)]
    assert routes, "no session routes found — the assertion below would be vacuous"

    for route in routes:
        names = {
            getattr(dependency.call, "__name__", "")
            for dependency in route.dependant.dependencies
        }
        assert "require_case_access" in names, f"{route.methods} {route.path}"
