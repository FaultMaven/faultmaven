"""Case-scoped raw data routes (fm#1707).

Read access to a case's stored data rows (``list_case_data``,
``get_case_data``) plus the two 410-Gone stubs for the upload/delete paths
the unified turn endpoint replaced. Split out of ``case/api/routes.py`` as
its own sub-router (A6); ``dependencies.py`` holds the DI accessor and guard
every handler here uses.
"""

import logging
import uuid
from datetime import datetime, timezone
from typing import Any, Dict, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Response, status
from fastapi.responses import JSONResponse

from faultmaven.api.v1.auth_dependencies import require_authentication
from faultmaven.infrastructure.observability.tracing import trace
from faultmaven.models.interfaces_case import ICaseService
from faultmaven.modules.auth.contracts import UserDTO
from faultmaven.modules.case.api.routes.dependencies import (
    _di_get_case_service_dependency,
    check_case_service_available,
)
from faultmaven.utils.serialization import to_json_compatible

router = APIRouter(prefix="/cases", tags=["cases"])


logger = logging.getLogger(__name__)


# =============================================================================
# Case-scoped data management endpoints


@router.get("/{case_id}/data", dependencies=[Depends(require_authentication)])
@trace("api_list_case_data")
async def list_case_data(
    case_id: str,
    limit: int = Query(
        50, ge=1, le=200, description="Maximum number of items to return"
    ),
    offset: int = Query(0, ge=0, description="Number of items to skip"),
    case_service: Optional[ICaseService] = Depends(_di_get_case_service_dependency),
    current_user: UserDTO = Depends(require_authentication),
) -> JSONResponse:
    """
    List data files associated with a case.

    Returns array of data records with pagination headers.
    Always returns 200 with empty array if no data exists.
    """
    case_service = check_case_service_available(case_service)

    try:
        # Verify case exists
        case = await case_service.get_case(case_id, current_user.user_id)
        if not case:
            raise HTTPException(
                status_code=404, detail="Case not found or access denied"
            )

        # Mock empty data list for now
        data_list = []
        total_count = 0

        response_headers = {"X-Total-Count": str(total_count)}

        return JSONResponse(
            status_code=200, content=data_list, headers=response_headers
        )

    except HTTPException:
        raise
    except Exception:
        # Always return empty list, never fail list operations
        return JSONResponse(status_code=200, content=[], headers={"X-Total-Count": "0"})


@router.get("/{case_id}/data/{data_id}", dependencies=[Depends(require_authentication)])
@trace("api_get_case_data")
async def get_case_data(
    case_id: str,
    data_id: str,
    case_service: Optional[ICaseService] = Depends(_di_get_case_service_dependency),
    current_user: UserDTO = Depends(require_authentication),
) -> Dict[str, Any]:
    """Get specific data file details for a case."""
    case_service = check_case_service_available(case_service)

    try:
        # Verify case exists
        case = await case_service.get_case(case_id, current_user.user_id)
        if not case:
            raise HTTPException(
                status_code=404, detail="Case not found or access denied"
            )

        # Mock data record
        data_record = {
            "data_id": data_id,
            "case_id": case_id,
            "filename": "sample_data.txt",
            "description": "Sample case data",
            "expected_type": "log_file",
            "size_bytes": 1024,
            "upload_timestamp": to_json_compatible(datetime.now(timezone.utc)),
            "processing_status": "completed",
        }

        return JSONResponse(
            status_code=201,
            content=data_record,
            headers={"Location": f"/api/v1/cases/{case_id}/data/{data_id}"},
        )

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Failed to retrieve case data: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail="Failed to retrieve case data")


@router.post("/{case_id}/data")
async def upload_case_data_gone(case_id: str):
    """DELETED: Use POST /{case_id}/turns with file attachments instead."""
    raise HTTPException(
        status_code=410,
        detail="This endpoint has been removed. Use POST /cases/{case_id}/turns with file attachments instead.",
    )


@router.delete(
    "/{case_id}/data/{data_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    responses={
        204: {
            "description": "Data deleted successfully",
            "headers": {
                "X-Correlation-ID": {
                    "description": "Request correlation ID",
                    "schema": {"type": "string"},
                }
            },
        }
    },
    dependencies=[Depends(require_authentication)],
)
@trace("api_delete_case_data")
async def delete_case_data(
    case_id: str,
    data_id: str,
    case_service: Optional[ICaseService] = Depends(_di_get_case_service_dependency),
    current_user: UserDTO = Depends(require_authentication),
):
    """Remove data file from a case. Returns 204 No Content on success.

    Only the case's OWNER may call it; anyone else gets 404.
    """
    case_service = check_case_service_available(case_service)

    try:
        # OWNER only: a delete is a write, and a team share is read-only until
        # hand-off ships (ADR-013 D4, amended 2026-10-09, #1898). Through the
        # read allowlist a teammate was answered 204 "deleted".
        #
        # This route is still a STUB — it deletes nothing and answers 204 to
        # the owner. The gate is only what it can honestly refuse today.
        case = await case_service.get_case(
            case_id, current_user.user_id, owner_only=True
        )
        if not case:
            raise HTTPException(
                status_code=404, detail="Case not found or access denied"
            )

        # Return 204 No Content for successful deletion
        return Response(
            status_code=status.HTTP_204_NO_CONTENT,
            headers={"x-correlation-id": str(uuid.uuid4())},
        )

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Failed to delete case data: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail="Failed to delete case data")
