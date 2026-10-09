"""Case driver routes (ADR-020 D4): reassign the driver, list the candidates.

A case has a creator (``user_id``) and a driver (``driver_id``): every reader
views the case, the driver holds the investigation writes, the creator holds
governance. These two routes move the driver. Both are open to the case's
creator and its effective driver only; refusals come from the service:

- a caller who cannot read the case: 404, as on every case route;
- a reader who is neither creator nor driver: 403;
- a target who is not a candidate: 422;
- a terminal case: 409 ``CASE_TERMINAL``; a lost version race: 409
  ``CASE_VERSION_CONFLICT``.

Standalone gets no refusal of its own: it has no teams, so the creator is the
only candidate and any other target is the same 422.
"""

import logging
from typing import Optional

from fastapi import APIRouter, Depends, Path

from faultmaven.api.v1.auth_dependencies import require_authentication
from faultmaven.models.api_models import (
    CaseDriverCandidate,
    CaseDriverCandidateList,
    CaseDriverUpdateRequest,
    CaseSummary,
)
from faultmaven.models.interfaces_case import ICaseService
from faultmaven.modules.auth.contracts import UserDTO
from faultmaven.modules.case.api.routes.dependencies import (
    _di_get_case_service_dependency,
    check_case_service_available,
)

router = APIRouter(prefix="/cases", tags=["cases"])

logger = logging.getLogger(__name__)

_REFUSALS = {
    403: {"description": "The caller reads the case but neither created nor drives it"},
    404: {"description": "No such case, or the caller cannot read it"},
}


@router.get(
    "/{case_id}/driver-candidates",
    response_model=CaseDriverCandidateList,
    summary="List Case Driver Candidates",
    description=(
        "Who the case's driver may be handed to (ADR-020 D4): the creator, "
        "then the active individual members of the teams the case is shared "
        "with, in the case's enterprise. Display names only, never email "
        "addresses. Readable by the case's creator and its current driver."
    ),
    responses=_REFUSALS,
    dependencies=[Depends(require_authentication)],
)
async def list_driver_candidates(
    case_id: str = Path(..., description="Case ID"),
    case_service: Optional[ICaseService] = Depends(_di_get_case_service_dependency),
    current_user: UserDTO = Depends(require_authentication),
) -> CaseDriverCandidateList:
    """The accounts this case's driver may be handed to."""
    case_service = check_case_service_available(case_service)
    candidates = await case_service.list_driver_candidates(
        case_id, current_user.user_id
    )
    return CaseDriverCandidateList(
        candidates=[
            CaseDriverCandidate(user_id=c.user_id, display_name=c.display_name)
            for c in candidates
        ]
    )


@router.put(
    "/{case_id}/driver",
    response_model=CaseSummary,
    summary="Reassign Case Driver",
    description=(
        "Hand the case's investigation writes to another account (ADR-020 "
        "D4). The caller must be the case's creator or its current driver; "
        "the target must be one of `GET /cases/{case_id}/driver-candidates`. "
        "Naming the creator hands the case back to them; naming the current "
        "driver changes nothing. A change bumps the case's version, so a turn "
        "in flight fails with 409 `CASE_VERSION_CONFLICT`, and is recorded in "
        "the audit log."
    ),
    responses={
        **_REFUSALS,
        409: {
            "description": (
                "`CASE_TERMINAL`: the case is resolved or closed. "
                "`CASE_VERSION_CONFLICT`: the case kept changing; reload and "
                "retry"
            )
        },
        422: {"description": "The target is not a candidate for this case"},
    },
    dependencies=[Depends(require_authentication)],
)
async def reassign_case_driver(
    request: CaseDriverUpdateRequest,
    case_id: str = Path(..., description="Case ID"),
    case_service: Optional[ICaseService] = Depends(_di_get_case_service_dependency),
    current_user: UserDTO = Depends(require_authentication),
) -> CaseSummary:
    """Reassign the case's driver; answers the case as it now stands."""
    case_service = check_case_service_available(case_service)
    case = await case_service.reassign_driver(
        case_id, current_user.user_id, request.driver_id
    )
    summary = CaseSummary.from_case(case)
    summary.shared_team_ids = await case_service.get_case_team_ids(case_id)
    await case_service.fill_display_names([summary])
    return summary
