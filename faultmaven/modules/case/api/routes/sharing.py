"""Case team-sharing routes (fm#1707).

Share and unshare a case with a Team via the polymorphic ``resource_shares``
table (ADR-013 §D4). Cloud-only: standalone has no teams. Split out of
``case/api/routes.py`` as its own sub-router (A6); ``dependencies.py`` holds
the DI accessor and guard every handler here uses.
"""

import logging
from typing import Optional

from fastapi import APIRouter, Body, Depends, HTTPException, Path, status

from faultmaven.api.v1.auth_dependencies import require_authentication
from faultmaven.exceptions import ValidationException
from faultmaven.models.interfaces_case import ICaseService
from faultmaven.modules.auth.contracts import UserDTO
from faultmaven.modules.case.api.routes.dependencies import (
    _di_get_case_service_dependency,
    check_case_service_available,
)

router = APIRouter(prefix="/cases", tags=["cases"])


logger = logging.getLogger(__name__)


# ============================================================================
# Case Team-Sharing Endpoints (ADR-013 §D4)
# ============================================================================
#
# A case is shared to a Team via the polymorphic ``resource_shares`` table; every
# member of that Team can then read the case (visibility is resolved in the read
# path, not stored on the case). These replace the retired pre-ADR-013 per-user
# participant endpoints (``/share``, ``/participants``, ``/access-check``), which
# had no client and referenced a ``case_participants`` table that never existed in
# the clean baseline. Cloud-only: standalone has no teams, so the service returns
# a clear "not available".


@router.post(
    "/{case_id}/team-shares",
    status_code=status.HTTP_201_CREATED,
    summary="Share Case With Team",
    description="Share a case with a Team (ADR-013 §D4). Owner-only; the Team must "
    "be one the caller belongs to. Idempotent.",
    dependencies=[Depends(require_authentication)],
)
async def share_case_with_team(
    case_id: str = Path(..., description="Case ID"),
    team_id: str = Body(..., embed=True, description="Team ID to share the case with"),
    case_service: Optional[ICaseService] = Depends(_di_get_case_service_dependency),
    current_user: UserDTO = Depends(require_authentication),
):
    """Share a case with a Team."""
    case_service = check_case_service_available(case_service)
    user_id = current_user.user_id

    try:
        await case_service.share_case_with_team(case_id, team_id, user_id)
        logger.info(f"Case {case_id} shared with team {team_id} by {user_id}")
        return {
            "message": "Case shared with team",
            "case_id": case_id,
            "team_id": team_id,
        }

    except ValidationException as e:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=str(e))
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error sharing case {case_id} with team: {e}", exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to share case with team",
        )


@router.delete(
    "/{case_id}/team-shares/{team_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Unshare Case From Team",
    description="Remove a case's share to a Team (ADR-013 §D4). Owner-only.",
    dependencies=[Depends(require_authentication)],
)
async def unshare_case_from_team(
    case_id: str = Path(..., description="Case ID"),
    team_id: str = Path(..., description="Team ID to unshare from"),
    case_service: Optional[ICaseService] = Depends(_di_get_case_service_dependency),
    current_user: UserDTO = Depends(require_authentication),
):
    """Unshare a case from a Team."""
    case_service = check_case_service_available(case_service)
    user_id = current_user.user_id

    try:
        removed = await case_service.unshare_case_from_team(case_id, team_id, user_id)

        if not removed:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Case {case_id} is not shared with team {team_id}",
            )

        logger.info(f"Case {case_id} unshared from team {team_id} by {user_id}")

    except ValidationException as e:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=str(e))
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error unsharing case {case_id} from team: {e}", exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to unshare case from team",
        )
