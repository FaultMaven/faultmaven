"""Replay & Debugging API Routes.

This module provides endpoints for "Time Travel" debugging:
- Inspecting case state at specific turns.
- Diffing state between turns.
"""

from typing import Any, Dict

from fastapi import APIRouter, Depends, HTTPException, Query, status

from faultmaven.api.v1.auth_dependencies import require_authentication
from faultmaven.api.v1.dependencies import get_case_repository
from faultmaven.exceptions import FaultMavenException
from faultmaven.models.api import ErrorDetail, ErrorResponse
from faultmaven.modules.auth.contracts import UserDTO
from faultmaven.modules.case.domain.models import Case
from faultmaven.modules.case.domain.services.replayer import CaseReplayer
from faultmaven.modules.case.infrastructure.case_repository import CaseRepository

router = APIRouter(tags=["replay"])


@router.get(
    "/{case_id}/snapshot/{turn_number}",
    response_model=Case,
    summary="Get case snapshot at specific turn",
    operation_id="get_case_snapshot",
)
async def get_case_snapshot(
    case_id: str,
    turn_number: int,
    case_repo: CaseRepository = Depends(get_case_repository),
    current_user: UserDTO = Depends(require_authentication),
) -> Case:
    """
    Get the full state of a case at a specific turn number.

    This is a read-only operation that reconstructs the case from the checkpoint.
    """
    replayer = CaseReplayer(case_repo)
    try:
        # Permission check could be added here (e.g. check if user owns case)
        # For now, relying on obscure ID and internal safeguards
        return await replayer.restore_at_turn(case_id, turn_number)
    except FaultMavenException:
        # Typed service exceptions (NotFoundError on missing checkpoint, etc.)
        # propagate to the global handlers in api/exception_handlers.py for
        # canonical translation (404 / 422 / 409 / etc.). See
        # docs/architecture/specifications/exception-contract.md.
        raise
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=ErrorResponse(
                schema_version="3.1.0",
                error=ErrorDetail(
                    code="REPLAY_ERROR", message=f"Failed to restore snapshot: {e}"
                ),
            ).model_dump(),
        )


@router.get(
    "/{case_id}/diff",
    response_model=Dict[str, Any],
    summary="Diff two case states",
    operation_id="diff_case_turns",
)
async def diff_case_turns(
    case_id: str,
    turn_from: int = Query(..., alias="from", description="Start turn number"),
    turn_to: int = Query(..., alias="to", description="End turn number"),
    case_repo: CaseRepository = Depends(get_case_repository),
    current_user: UserDTO = Depends(require_authentication),
) -> Dict[str, Any]:
    """
    Compute the semantic difference between two turns of a case.

    Returns a dictionary describing added, removed, and modified fields.
    """
    replayer = CaseReplayer(case_repo)
    try:
        diff = await replayer.diff_turns(case_id, turn_from, turn_to)
        return diff if diff else {"message": "No differences found"}
    except FaultMavenException:
        # NotFoundError from missing checkpoint propagates to the global
        # handler (404). See replay handler above for rationale.
        raise
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=ErrorResponse(
                schema_version="3.1.0",
                error=ErrorDetail(
                    code="DIFF_ERROR", message=f"Failed to compute diff: {e}"
                ),
            ).model_dump(),
        )
