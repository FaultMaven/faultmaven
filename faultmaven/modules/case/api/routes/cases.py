"""Case CRUD, listing, health, UI, title and analytics routes (fm#1707).

The core per-case resource surface: create/list/get/update/delete, the
UI-shaped read (``get_case_ui``), server-side auto-title generation
(``generate_case_title``), case search and case analytics, plus the case
service health probe. Split out of ``case/api/routes.py`` as its own
sub-router (A6); ``dependencies.py`` holds the DI accessors and guards every
handler here uses.
"""

import logging
import uuid
from datetime import datetime, timezone
from typing import Any, Dict, List, Literal, Optional

from fastapi import (
    APIRouter,
    Body,
    Depends,
    HTTPException,
    Query,
    Request,
    Response,
    status,
)
from fastapi.responses import JSONResponse

from faultmaven.api.v1.auth_dependencies import require_authentication
from faultmaven.exceptions import (
    AuthorizationError,
    FaultMavenException,
    NotFoundError,
    ServiceException,
    SessionException,
    ValidationException,
)
from faultmaven.infrastructure.observability.tracing import trace
from faultmaven.models.api import ErrorDetail, ErrorResponse, TitleResponse
from faultmaven.models.api_models import (
    CaseCreateRequest,
    CaseDetail,
    CaseListFilter,
    CaseListResponse,
    CaseSearchRequest,
    CaseSummary,
    CaseUpdateRequest,
    bound_to_utc,
)
from faultmaven.models.case_ui import CaseUIResponse
from faultmaven.models.interfaces_case import ICaseService
from faultmaven.modules.auth.contracts import ISessionService, UserDTO
from faultmaven.modules.case.api.routes.dependencies import (
    _di_get_case_service_dependency,
    _di_get_creator_service_channel,
    _di_get_session_service_dependency,
    _is_default_case_title,
    check_case_service_available,
    require_case_not_terminal,
)
from faultmaven.modules.case.api.title_generation import (
    MAX_TITLE_WORDS_DEFAULT,
    _generate_and_persist_title,
)
from faultmaven.modules.case.domain.models.lifecycle import CaseState
from faultmaven.modules.case.domain.services.case_ui_adapter import (
    transform_case_for_ui,
)
from faultmaven.utils.serialization import to_json_compatible

router = APIRouter(prefix="/cases", tags=["cases"])


logger = logging.getLogger(__name__)


@router.delete(
    "/{case_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    responses={
        204: {
            "description": "Case deleted successfully",
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
@trace("api_delete_case")
async def delete_case(
    case_id: str,
    case_service: Optional[ICaseService] = Depends(_di_get_case_service_dependency),
    current_user: UserDTO = Depends(require_authentication),
):
    """
    Permanently delete a case and all associated data.

    This endpoint provides hard delete functionality. Once deleted,
    the case and all associated data are permanently removed.

    The operation is idempotent - subsequent requests will return
    204 No Content even if the case has already been deleted, and so does a
    request naming a case the caller cannot see.

    Only the OWNER may delete. A teammate who can read the case through a team
    share is refused with 403 (ADR-017 D4: a share is read visibility, not
    ownership).

    Returns 204 No Content on success.
    """
    case_service = check_case_service_available(case_service)
    correlation_id = str(uuid.uuid4())

    try:
        # DELETE stays idempotent for a case the caller cannot see: the service
        # answers True there, and 204 is indistinguishable from "already gone",
        # which is the same refusal shape every other read on this surface uses.
        #
        # It answers **False** for exactly one situation — a case the caller CAN
        # see (a team share) but does not OWN. That is not an absence and must
        # not be reported as one: the caller demonstrably knows the case exists,
        # so 404 would be a lie they can detect, and 204 would be a lie about
        # what happened. The route used to discard this boolean and answer 204
        # to both, which said "deleted" about a row that is still there.
        deleted = await case_service.hard_delete_case(case_id, current_user.user_id)
        if not deleted:
            logger.warning(
                f"Refused delete of case {case_id}: caller is not the owner",
                extra={"correlation_id": correlation_id},
            )
            error_response = ErrorResponse(
                schema_version="3.1.0",
                error=ErrorDetail(
                    code="FORBIDDEN",
                    message="Only the owner of a case may delete it.",
                ),
            )
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=error_response.model_dump(),
                headers={"x-correlation-id": correlation_id},
            )

        # Success response with correlation header
        return Response(
            status_code=status.HTTP_204_NO_CONTENT,
            headers={"x-correlation-id": correlation_id},
        )
    except HTTPException:
        raise
    except AuthorizationError as e:
        # Authorization errors should not be treated as idempotent success
        logger.warning(
            f"Authorization error in delete_case: {e}",
            extra={"correlation_id": correlation_id},
        )
        error_response = ErrorResponse(
            schema_version="3.1.0", error=ErrorDetail(code="FORBIDDEN", message=str(e))
        )
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=error_response.model_dump(),
            headers={"x-correlation-id": correlation_id},
        )
    except NotFoundError:
        # NotFoundError is treated as success for idempotent DELETE
        # REST principle: DELETE is idempotent, resource deletion succeeds whether or not resource exists
        logger.info(
            f"Case not found in delete_case, treating as idempotent success: {case_id}",
            extra={"correlation_id": correlation_id},
        )
        return Response(
            status_code=status.HTTP_204_NO_CONTENT,
            headers={"x-correlation-id": correlation_id},
        )
    except Exception as e:
        logger.error(
            f"Unexpected error in delete_case: {e}",
            extra={"correlation_id": correlation_id},
        )
        error_response = ErrorResponse(
            schema_version="3.1.0",
            error=ErrorDetail(
                code="DELETE_CASE_ERROR", message="Failed to delete case"
            ),
        )
        raise HTTPException(
            status_code=500,
            detail=error_response.model_dump(),
            headers={"x-correlation-id": correlation_id},
        )


@router.post(
    "",
    response_model=CaseSummary,
    status_code=status.HTTP_201_CREATED,
    dependencies=[Depends(require_authentication)],
)
@trace("api_create_case")
async def create_case(
    request: CaseCreateRequest,
    response: Response,
    case_service: Optional[ICaseService] = Depends(_di_get_case_service_dependency),
    session_service: ISessionService = Depends(_di_get_session_service_dependency),
    current_user: UserDTO = Depends(require_authentication),
    creator_service_channel: Optional[str] = Depends(_di_get_creator_service_channel),
) -> CaseSummary:
    """
    Create a new troubleshooting case (v2.0 milestone-based)

    Creates a new case with milestone-based investigation tracking.
    Initial status is INQUIRY (problem definition phase).

    Returns CaseSummary with basic case info and milestone progress.
    """
    correlation_id = str(uuid.uuid4())
    case_service = check_case_service_available(case_service)

    try:
        # The session named in the BODY is an ARGUMENT, not a licence over
        # somebody else's (#1390, #1393, #1398).
        #
        # Without this, naming SOMEONE ELSE'S session id retargets their
        # `session:{id}:current_case_id` pointer at a case of the caller's
        # choosing: the owner's next turn either lands in the caller's case (if
        # they can reach it) or silently abandons the case they were working.
        # That is the identical defect fixed one route over on
        # `POST /cases/sessions/{id}/resume/{case_id}`, reached through a
        # different parameter — the session id arrives in the request body
        # here rather than in the path, which changes nothing about what it
        # buys. The route required a bearer all along and still asked only
        # whether the session EXISTED; existence is not ownership.
        #
        # ORDERING. The pointer write is inside `CaseService.create_case`, and
        # inside it BEFORE `repository.save`, so it lands ahead of every
        # failure path this route has — exactly as the pre-fix 404 on the
        # resume route never prevented the retarget there. The gate therefore
        # has to be resolved HERE, before the service is called at all, and
        # cannot be folded into a later check. Unlike the resume route this one
        # names a single resource — the case does not exist yet — so there is
        # no second gate to order against and no ambiguity about which refusal
        # a cross-tenant probe is exercising.
        if request.session_id:
            # `get_session` RAISES rather than returning None when it cannot
            # answer — `ServiceException("Session store not configured")` is
            # the shipped case. Treating it as a nullable return let that reach
            # this handler's own `except ServiceException` arm and answer 500
            # "Failed to create case" on a request the server simply could not
            # evaluate. An unevaluable gate is a 503, which is the rule
            # `faultmaven/api/routes/sessions.py` already states for its own:
            # "503 if the case service is unavailable (the gate cannot be
            # evaluated, so nothing is served)".
            try:
                session = await session_service.get_session(
                    request.session_id, validate=True
                )
            except (ServiceException, SessionException) as exc:
                # BOTH families. `ServiceException("Session store not
                # configured")` is the unconfigured case; a store that IS
                # configured and unreachable raises `SessionStoreException`,
                # which descends from `SessionException` and NOT from
                # `ServiceException`. Catching one made two spellings of "the
                # gate could not be evaluated" answer 503 and 500 respectively
                # — and here the second spelling escaped the handler entirely,
                # because `SessionException` has no exception handler
                # registered and nothing below catches it.
                logger.warning(
                    f"Cannot evaluate session ownership for {request.session_id}: {exc}",
                    extra={"correlation_id": correlation_id},
                )
                raise HTTPException(
                    status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                    detail="Session service unavailable",
                    headers={"x-correlation-id": correlation_id},
                )

            if session is None or session.user_id != current_user.user_id:
                # ONE answer for "no such session" and "not yours", for the
                # reason the resume route gives: naming another user's session
                # must not be distinguishable from naming one that does not
                # exist. The answer is this route's EXISTING 401
                # `SESSION_EXPIRED` rather than the resume route's 404,
                # because the first half of that pair is already published
                # here and the two have to stay indistinguishable. Its
                # remediation fits both: the session id the caller is holding
                # is no good to them and a fresh one will work, which is what
                # "refresh the page to continue" already tells them to get.
                #
                # The LOG distinguishes them, because an operator needs to see
                # an attempt and the log is not published to the caller.
                logger.warning(
                    "Refusing case creation on session %s for %s: %s",
                    request.session_id,
                    current_user.user_id,
                    (
                        "invalid or expired session"
                        if session is None
                        else "session belongs to another user"
                    ),
                    extra={"correlation_id": correlation_id},
                )
                error_response = ErrorResponse(
                    schema_version="3.1.0",
                    error=ErrorDetail(
                        code="SESSION_EXPIRED",
                        message="Your session has expired. Please refresh the page to continue.",
                    ),
                )
                raise HTTPException(
                    status_code=status.HTTP_401_UNAUTHORIZED,
                    detail=error_response.model_dump(),
                    headers={"x-correlation-id": correlation_id},
                )

        # Create case using new model. Origin is derived from the creating
        # account's SERVICE CHANNEL (ADR-017 D6): a service account serving
        # Slack → 'slack', otherwise 'copilot'. Server-derived, not
        # client-provided (not spoofable).
        source = "slack" if creator_service_channel == "slack" else "copilot"
        case_entity = await case_service.create_case(
            title=request.title,  # Pass None to trigger auto-generation in service
            description=request.description,
            owner_id=current_user.user_id,
            session_id=request.session_id,
            initial_message=request.initial_message,  # Restored from old implementation
            source=source,
        )

        # Set Location header
        response.headers["Location"] = f"/api/v1/cases/{case_entity.case_id}"
        response.headers["x-correlation-id"] = correlation_id

        # Return summary (v2.0 API model)
        return CaseSummary.from_case(case_entity)

    except ValidationException as e:
        logger.error(
            f"Validation error in create_case: {e}",
            extra={"correlation_id": correlation_id},
        )
        error_response = ErrorResponse(
            schema_version="3.1.0",
            error=ErrorDetail(code="VALIDATION_ERROR", message=str(e)),
        )
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=error_response.model_dump(),
            headers={"x-correlation-id": correlation_id},
        )
    except ServiceException as e:
        logger.error(
            f"Service error in create_case: {e}",
            extra={"correlation_id": correlation_id},
            exc_info=True,
        )
        error_response = ErrorResponse(
            schema_version="3.1.0",
            error=ErrorDetail(
                code="CASE_SERVICE_ERROR", message="Failed to create case"
            ),
        )
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=error_response.model_dump(),
            headers={"x-correlation-id": correlation_id},
        )


@router.get(
    "", response_model=CaseListResponse, dependencies=[Depends(require_authentication)]
)
@trace("api_list_cases")
async def list_cases(
    response: Response,
    case_service: Optional[ICaseService] = Depends(_di_get_case_service_dependency),
    current_user: UserDTO = Depends(require_authentication),
    state: Optional[CaseState] = Query(None, description="Filter by state"),
    source: Optional[Literal["copilot", "slack", "api"]] = Query(
        None, description="Filter by case source"
    ),
    team_id: Optional[str] = Query(
        None,
        description=(
            "Filter to cases shared with this Team (ADR-013 §D4). Only Teams the "
            "caller belongs to yield results; ignored in standalone (no teams)."
        ),
    ),
    created_after: Optional[datetime] = Query(
        None,
        description=(
            "Only cases created at or after this instant — INCLUSIVE. ISO-8601 "
            "with an offset; a value without one is read as UTC."
        ),
    ),
    created_before: Optional[datetime] = Query(
        None,
        description=(
            "Only cases created strictly before this instant — EXCLUSIVE. "
            "ISO-8601 with an offset; a value without one is read as UTC. To "
            "select a calendar day, pass that day's first instant as "
            "created_after and the FOLLOWING day's first instant here."
        ),
    ),
    limit: int = Query(50, ge=1, le=100, description="Items per page"),
    offset: int = Query(0, ge=0, description="Number of items to skip"),
    # Changed default to True - new cases should be visible immediately
    include_empty: bool = Query(
        True, description="Include cases with current_turn == 0 (newly created)"
    ),
):
    """
    List user's cases with pagination (v2.0 milestone-based)

    Returns CaseListResponse with:
    - List of CaseSummary objects (with milestone progress)
    - Total count for pagination
    - has_more flag

    Default Filtering Behavior:
    - INCLUDES empty cases (current_turn == 0) - newly created cases are visible
    - INCLUDES closed/resolved cases (the client categorizes by state)
    - Use include_empty=false to hide cases with no conversation yet
    - Use the `state` filter to further refine results

    Creation-date bounds:
    - The window is HALF-OPEN, `[created_after, created_before)`, and lives in
      the same WHERE clause as every other filter, so `total_count` describes
      the same set as the page.
    - Half-open because an inclusive upper bound is not expressible by a client
      whose clock stops at milliseconds — which is every browser — while
      `created_at` keeps microseconds. A day bounded at 23:59:59.999 silently
      drops a case created at 23:59:59.9997.
    - They are INSTANTS, not calendar days. To select one day, send that day's
      first instant and the FOLLOWING day's first instant, both resolved in the
      CLIENT's timezone: only the client knows which day the user meant.
    - Send an offset. A bare naive value is read as UTC, and any offset is
      normalized to UTC before it reaches the query, so two spellings of one
      instant always answer alike.
    """
    case_service = check_case_service_available(case_service)
    correlation_id = str(uuid.uuid4())
    response.headers["x-correlation-id"] = correlation_id

    # Prevent browser/extension caching to ensure title updates are visible immediately
    response.headers["Cache-Control"] = "no-cache, no-store, must-revalidate"
    response.headers["Pragma"] = "no-cache"
    response.headers["Expires"] = "0"

    # An inverted window is refused, not served as an empty list. It is
    # unsatisfiable by construction, so `created_after > created_before` comes
    # back as "you have no cases in that range" when the truth is "you swapped
    # the ends" — the same silence this endpoint's date bounds exist to end.
    # Checked here rather than left to CaseListFilter's own validator because a
    # ValidationError raised inside the try below lands in the generic handler
    # and would be served as a 500.
    if (
        created_after is not None
        and created_before is not None
        and bound_to_utc(created_after) > bound_to_utc(created_before)
    ):
        error_response = ErrorResponse(
            schema_version="3.1.0",
            error=ErrorDetail(
                code="VALIDATION_ERROR",
                message=(
                    "created_after must not be later than created_before "
                    "(the window is [created_after, created_before))"
                ),
            ),
        )
        raise HTTPException(
            # The non-deprecated spelling, as operator_grants.py already uses.
            # Same status code; `HTTP_422_UNPROCESSABLE_ENTITY` warns on access.
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=error_response.model_dump(),
            headers={"x-correlation-id": correlation_id},
        )

    try:
        # Build filter with restored filtering parameters
        # The principal is NOT passed here. ``list_user_cases`` takes it as its
        # own argument, and it is the only one either of them reads; a second
        # copy on the filter was a settable field that changed no answer.
        filters = CaseListFilter(
            state=state,
            source=source,
            team_id=team_id,
            created_after=created_after,
            created_before=created_before,
            limit=limit,
            offset=offset,
            include_empty=include_empty,
        )

        # Get case summaries (already converted by service). The service returns
        # the page plus the repository's true total match count so pagination
        # (total_count / has_more) is sound rather than derived from page length.
        case_summaries, total_count = await case_service.list_user_cases(
            current_user.user_id, filters
        )

        # DEFENSIVE: Ensure we actually have CaseSummary objects (validation check)
        from faultmaven.models.api_models import CaseSummary
        from faultmaven.modules.case.domain.models.case import Case as CaseEntity

        validated_summaries = []
        for item in case_summaries:
            if isinstance(item, CaseSummary):
                validated_summaries.append(item)
            elif hasattr(item, "case_id"):  # Duck typing for Case entity
                # logger.warning(f"Unexpected Case entity in list_cases response, converting: {item.case_id}")
                validated_summaries.append(CaseSummary.from_case(item))
            else:
                # logger.error(f"Unknown item type in list_cases: {type(item)}")
                pass

        case_summaries = validated_summaries

        # Build response. total_count is the repository's true match count (all
        # filters applied, before pagination); has_more is derived from the
        # offset + this page's length against that total.
        #
        # Known safe-direction divergence: total_count is a raw COUNT(*), while
        # the page can contain fewer rows than the LIMIT if a row fails to
        # hydrate (skipped in _row_to_case / from_case). total_count can then
        # slightly over-report, keeping has_more True at the true end — the
        # caller fetches one extra page that comes back empty. It never hides a
        # real page, so we accept the over-report rather than reconcile COUNT
        # against hydration on every list call.
        has_more = offset + len(case_summaries) < total_count
        list_response = CaseListResponse(
            cases=case_summaries,
            total_count=total_count,
            limit=limit,
            offset=offset,
            has_more=has_more,
        )

        # Set pagination headers
        response.headers["X-Total-Count"] = str(total_count)
        response.headers["x-correlation-id"] = correlation_id

        return list_response

    except ServiceException as e:
        # Service-level errors
        correlation_id = str(uuid.uuid4())
        logger = logging.getLogger(__name__)
        logger.error(
            f"Service error in list_cases: {e}",
            extra={"correlation_id": correlation_id},
            exc_info=True,
        )
        error_response = ErrorResponse(
            schema_version="3.1.0",
            error=ErrorDetail(
                code="CASE_SERVICE_ERROR", message="Case service unavailable"
            ),
        )
        return JSONResponse(
            status_code=503,
            content=error_response.model_dump(),
            headers={"x-correlation-id": correlation_id},
        )

    except Exception as e:
        # Unexpected errors
        correlation_id = str(uuid.uuid4())
        logger = logging.getLogger(__name__)
        logger.error(
            f"Unexpected error in list_cases: {e}",
            extra={"correlation_id": correlation_id},
        )
        error_response = ErrorResponse(
            schema_version="3.1.0",
            error=ErrorDetail(
                code="INTERNAL_ERROR", message="Failed to retrieve cases"
            ),
        )
        return JSONResponse(
            status_code=500,
            content=error_response.model_dump(),
            headers={"x-correlation-id": correlation_id},
        )


@router.get("/health", response_model=Dict[str, Any])
@trace("api_case_health")
async def get_case_service_health(
    case_service: ICaseService = Depends(_di_get_case_service_dependency),
) -> Dict[str, Any]:
    """
    Get case service health status

    Returns health information about the case persistence system,
    including connectivity and performance metrics.
    """
    try:
        # Try to get basic health information
        # This would typically call a health method on the case service
        return {
            "service": "case_management",
            "status": "healthy",
            "timestamp": to_json_compatible(datetime.now(timezone.utc)),
            "features": {
                "case_persistence": True,
                "case_sharing": True,
                "session_integration": True,
                "conversation_history": True,
            },
        }

    except Exception as e:
        logger.error(f"Case service health check failed: {e}", exc_info=True)
        return {
            "service": "case_management",
            "status": "unhealthy",
            "timestamp": to_json_compatible(datetime.now(timezone.utc)),
            "error": "Case service health check failed",
        }


@router.get(
    "/{case_id}",
    response_model=CaseDetail,
    dependencies=[Depends(require_authentication)],
)
@trace("api_get_case")
async def get_case(
    case_id: str,
    response: Response,
    case_service: Optional[ICaseService] = Depends(_di_get_case_service_dependency),
    current_user: UserDTO = Depends(require_authentication),
) -> CaseDetail:
    """
    Get a specific case by ID (v2.0 milestone-based)

    Returns full case details with milestone progress, investigation stage,
    and completion percentage.
    """
    correlation_id = str(uuid.uuid4())
    response.headers["x-correlation-id"] = correlation_id

    # Prevent browser/extension caching to ensure title updates are visible immediately
    response.headers["Cache-Control"] = "no-cache, no-store, must-revalidate"
    response.headers["Pragma"] = "no-cache"
    response.headers["Expires"] = "0"

    try:
        case = await case_service.get_case(case_id, current_user.user_id)
        if not case:
            # Restored from old implementation - proper error response format
            error_response = ErrorResponse(
                schema_version="3.1.0",
                error=ErrorDetail(
                    code="CASE_NOT_FOUND", message="Case not found or access denied"
                ),
            )
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=error_response.model_dump(),
                headers={"x-correlation-id": correlation_id},
            )

        # Convert to CaseDetail (v2.0 API model with milestones)
        detail = CaseDetail.from_case(case)
        # Enrich with team shares (ADR-013 §D4); empty in standalone.
        detail.shared_team_ids = await case_service.get_case_team_ids(case_id)
        return detail

    except HTTPException:
        raise
    except Exception as e:
        correlation_id = str(uuid.uuid4())
        logger.error(
            f"Unexpected error in get_case: {e}",
            extra={"correlation_id": correlation_id},
        )
        # Restored from old implementation - proper error response format
        error_response = ErrorResponse(
            schema_version="3.1.0",
            error=ErrorDetail(code="GET_CASE_ERROR", message="Failed to get case"),
        )
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=error_response.model_dump(),
            headers={"x-correlation-id": correlation_id},
        )


@router.get(
    "/{case_id}/ui",
    response_model=CaseUIResponse,
    dependencies=[Depends(require_authentication)],
)
@trace("api_get_case_ui")
async def get_case_ui(
    request: Request,
    case_id: str,
    response: Response,
    case_service: Optional[ICaseService] = Depends(_di_get_case_service_dependency),
    current_user: UserDTO = Depends(require_authentication),
) -> CaseUIResponse:
    """
    Get phase-adaptive UI-optimized case response.

    Returns different response schemas based on case status:
    - INQUIRY: Focus on problem understanding, clarifying questions
    - INVESTIGATING: Milestone progress, hypotheses, evidence, working conclusion
    - RESOLVED: Root cause, solution, verification, resolution summary

    This endpoint eliminates multiple API calls by returning all UI state
    in a single response optimized for the current investigation phase.
    """
    correlation_id = str(uuid.uuid4())
    response.headers["x-correlation-id"] = correlation_id

    try:
        case_service = check_case_service_available(case_service)

        # Get case from service
        case = await case_service.get_case(case_id, current_user.user_id)
        if not case:
            error_response = ErrorResponse(
                schema_version="3.1.0",
                error=ErrorDetail(
                    code="CASE_NOT_FOUND", message="Case not found or access denied"
                ),
            )
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=error_response.model_dump(),
                headers={"x-correlation-id": correlation_id},
            )

        # Transform to UI response based on phase
        ui_response = transform_case_for_ui(case)

        # Enrich the terminal-phase response with any case-linked runbook
        # drafts. The adapter is pure (no service deps); the case-ui route
        # owns the cross-module composition. The Artifacts strip in the
        # case header reads `reports_available` to render runbook badges.
        from faultmaven.modules.case.contracts import CaseState as _CS

        if case.state in (_CS.RESOLVED, _CS.CLOSED):
            conversion_service = getattr(request.app.state, "conversion_service", None)
            if conversion_service is not None:
                try:
                    drafts = await conversion_service.list_drafts_for_case(case_id)
                except Exception:
                    drafts = []
                if drafts:
                    from faultmaven.models.case_ui import ReportAvailability

                    for d in drafts:
                        ui_response.reports_available.append(
                            ReportAvailability(
                                report_type="runbook",
                                status=(
                                    "available"
                                    if d.get("knowledge_item_id")
                                    else "draft"
                                ),
                                reason=d.get("title"),
                            )
                        )

        return ui_response

    except HTTPException:
        raise
    except Exception as e:
        logger.error(
            f"Unexpected error in get_case_ui: {e}",
            extra={"correlation_id": correlation_id},
        )
        error_response = ErrorResponse(
            schema_version="3.1.0",
            error=ErrorDetail(
                code="GET_CASE_UI_ERROR", message="Failed to get case UI data"
            ),
        )
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=error_response.model_dump(),
            headers={"x-correlation-id": correlation_id},
        )


@router.put(
    "/{case_id}",
    status_code=status.HTTP_200_OK,
    dependencies=[Depends(require_authentication)],
)
@trace("api_update_case")
async def update_case(
    case_id: str,
    request: CaseUpdateRequest,
    response: Response,
    case_service: Optional[ICaseService] = Depends(_di_get_case_service_dependency),
    current_user: UserDTO = Depends(require_authentication),
):
    """
    Update case details

    Updates case metadata such as title, description, state, priority, and tags.
    Requires edit permissions on the case.
    """
    correlation_id = str(uuid.uuid4())
    response.headers["x-correlation-id"] = correlation_id

    try:
        # Reject writes on terminal or archived cases
        case = await case_service.get_case(case_id, current_user.user_id)
        if case:
            require_case_not_terminal(case)

        # Build updates dict from request (milestone-based model)
        updates = {}
        if request.title is not None:
            updates["title"] = request.title
        if request.description is not None:
            updates["description"] = request.description
        if request.state is not None:
            updates["state"] = request.state.value  # Convert enum to string value
        # Note: priority and tags removed - not in milestone-based model

        if not updates:
            # Restored from old implementation - proper error response format
            error_response = ErrorResponse(
                schema_version="3.1.0",
                error=ErrorDetail(code="NO_UPDATES", message="No updates provided"),
            )
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=error_response.model_dump(),
                headers={"x-correlation-id": correlation_id},
            )

        success = await case_service.update_case(case_id, updates, current_user.user_id)
        if not success:
            # Restored from old implementation - proper error response format
            error_response = ErrorResponse(
                schema_version="3.1.0",
                error=ErrorDetail(
                    code="CASE_NOT_FOUND", message="Case not found or access denied"
                ),
            )
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=error_response.model_dump(),
                headers={"x-correlation-id": correlation_id},
            )

        # Return successful update response as expected by tests
        return {
            "case_id": case_id,
            "success": True,
            "message": "Case updated successfully",
        }

    except HTTPException:
        raise
    except NotFoundError as e:
        logger.warning(
            f"Case not found in update_case: {e}",
            extra={"correlation_id": correlation_id},
        )
        error_response = ErrorResponse(
            schema_version="3.1.0",
            error=ErrorDetail(code="CASE_NOT_FOUND", message=str(e)),
        )
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=error_response.model_dump(),
            headers={"x-correlation-id": correlation_id},
        )
    except AuthorizationError as e:
        logger.warning(
            f"Authorization error in update_case: {e}",
            extra={"correlation_id": correlation_id},
        )
        error_response = ErrorResponse(
            schema_version="3.1.0", error=ErrorDetail(code="FORBIDDEN", message=str(e))
        )
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=error_response.model_dump(),
            headers={"x-correlation-id": correlation_id},
        )
    except ValidationException as e:
        logger.error(
            f"Validation error in update_case: {e}",
            extra={"correlation_id": correlation_id},
        )
        error_response = ErrorResponse(
            schema_version="3.1.0",
            error=ErrorDetail(code="VALIDATION_ERROR", message=str(e)),
        )
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=error_response.model_dump(),
            headers={"x-correlation-id": correlation_id},
        )
    except Exception as e:
        logger.error(
            f"Unexpected error in update_case: {e}",
            extra={"correlation_id": correlation_id},
            exc_info=True,
        )
        error_response = ErrorResponse(
            schema_version="3.1.0",
            error=ErrorDetail(
                code="UPDATE_CASE_ERROR", message="Failed to update case"
            ),
        )
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=error_response.model_dump(),
            headers={"x-correlation-id": correlation_id},
        )


@router.post(
    "/{case_id}/title",
    response_model=TitleResponse,
    dependencies=[Depends(require_authentication)],
)
@trace("api_generate_case_title")
async def generate_case_title(
    case_id: str,
    request: Request,
    response: Response,
    request_body: Optional[Dict[str, Any]] = Body(
        None, description="Optional request parameters"
    ),
    force: bool = Query(
        False, description="Only overwrite non-default titles when true"
    ),
    case_service: Optional[ICaseService] = Depends(_di_get_case_service_dependency),
    current_user: UserDTO = Depends(require_authentication),
) -> TitleResponse:
    """
    Generate a concise, case-specific title from case messages and metadata.

    **Request body (optional):**
    - `max_words`: integer (3–12, default 8) - Maximum words in generated title
    - `hint`: string - Optional hint to guide title generation
    - `force`: boolean (default false) - Only overwrite non-default titles when true

    **Returns:**
    - 200: TitleResponse with X-Correlation-ID header
    - 422: ValidationException body — see ``api/exception_handlers.py``
      and ``docs/architecture/specifications/exception-contract.md``.
      Raised when there is insufficient meaningful context to generate
      a title (pre-LLM length gate, or LLM + fallback both fail).
      Clients SHOULD keep the existing title unchanged and may retry
      later.
    """
    case_service = check_case_service_available(case_service)
    correlation_id = str(uuid.uuid4())
    response.headers["x-correlation-id"] = correlation_id

    try:
        logger = logging.getLogger(__name__)
        logger.info(
            f"🔍 Title generation started for case {case_id}",
            extra={"case_id": case_id, "force_query": force},
        )

        # Parse request body parameters (optional) - force can be in body or query
        max_words = MAX_TITLE_WORDS_DEFAULT  # default
        hint = None
        body_force = False
        if request_body:
            max_words = request_body.get("max_words", MAX_TITLE_WORDS_DEFAULT)
            hint = request_body.get("hint")
            body_force = request_body.get("force", False)

        # Use force from body if provided, otherwise from query parameter
        effective_force = body_force or force

        # Validate max_words (3–12, default 8)
        if not isinstance(max_words, int) or max_words < 3 or max_words > 12:
            max_words = MAX_TITLE_WORDS_DEFAULT

        logger.info(
            f"🔍 Effective parameters: max_words={max_words}, hint='{hint}', force={effective_force}",
            extra={
                "max_words": max_words,
                "hint": hint,
                "effective_force": effective_force,
            },
        )
        # Verify user has access to the case
        case = await case_service.get_case(case_id, current_user.user_id)
        if not case:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Case not found or access denied",
            )

        logger.info(
            f"🔍 Case retrieved: title='{case.title}', force={effective_force}",
            extra={"existing_title": case.title},
        )

        # A terminal case's title is final and is not regenerated — unless it was
        # never set at all. "Terminal" said nothing about whether the case was ever
        # named: a case that reaches RESOLVED still carrying the placeholder
        # ``Case-YYMMDD-N`` has no final title to protect, and it is the case a user
        # most needs named in their history. Naming it is the one write still owed.
        if case.is_terminal and not _is_default_case_title(case.title):
            return TitleResponse(title=case.title)

        # Idempotency check removed - allow free regeneration
        # Rationale:
        # 1. Hybrid approach makes regeneration nearly free (90% extractive, 1ms, $0)
        # 2. The substance gate below refuses before any LLM call, so a client that
        #    re-asks on a thin case costs reads, not tokens
        # 3. Duplicate request middleware provides rate limiting
        # 4. Better UX - users can regenerate as conversation evolves
        # 5. Titles improve as more context is revealed
        #
        # Previous: Blocked regeneration if title was "meaningful"
        # Now: Always regenerate (respects the substance gate + duplicate protection)

        generated_title, title_source, substance_length = (
            await _generate_and_persist_title(
                case=case,
                case_id=case_id,
                user_id=current_user.user_id,
                case_service=case_service,
                llm_provider=getattr(request.app.state, "llm_provider", None),
                max_words=max_words,
                hint=hint,
                correlation_id=correlation_id,
            )
        )

        # Persist success atomically and return X-Correlation-ID on all responses
        response.headers["x-correlation-id"] = correlation_id
        response.headers["x-title-source"] = (
            title_source  # Log source=llm vs fallback for telemetry
        )
        response.headers["x-content-length"] = str(substance_length)

        # Optional telemetry logging
        logger.info(
            f"Title generation completed successfully",
            extra={
                "case_id": case_id,
                "title_source": title_source,
                "title_length": len(generated_title),
            },
        )

        return TitleResponse(schema_version="3.1.0", title=generated_title)

    except HTTPException as he:
        # Ensure X-Correlation-ID on all error responses
        if "x-correlation-id" not in (he.headers or {}):
            he.headers = he.headers or {}
            he.headers["x-correlation-id"] = correlation_id
        raise
    except FaultMavenException:
        # Typed service exceptions (ValidationException, ConflictError,
        # NotFoundError, etc.) propagate to FastAPI's global handlers which
        # map them to 422/409/404. See api/exception_handlers.py. Without
        # this pass-through, the blanket `except Exception` below would
        # swallow them and re-wrap as 500.
        raise
    except Exception as e:
        logger.error(
            f"Unexpected error in generate_case_title: {e}",
            extra={"correlation_id": correlation_id},
        )
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to generate title",
            headers={"x-correlation-id": correlation_id},
        )


@router.post(
    "/search",
    response_model=List[CaseSummary],
    dependencies=[Depends(require_authentication)],
)
@trace("api_search_cases")
async def search_cases(
    request: CaseSearchRequest,
    case_service: Optional[ICaseService] = Depends(_di_get_case_service_dependency),
    current_user: UserDTO = Depends(require_authentication),
) -> List[CaseSummary]:
    """
    Search cases by content

    Searches case titles, descriptions, and optionally message content
    for the specified query terms.
    """
    try:
        cases = await case_service.search_cases(
            request, current_user.user_id if current_user else None
        )
        return cases

    except ValidationException as e:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(e))
    except Exception as e:
        logger.error(f"Case search failed: {e}", exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Search failed",
        )


@router.get(
    "/{case_id}/analytics",
    response_model=Dict[str, Any],
    dependencies=[Depends(require_authentication)],
)
@trace("api_get_case_analytics")
async def get_case_analytics(
    case_id: str,
    case_service: Optional[ICaseService] = Depends(_di_get_case_service_dependency),
    current_user: UserDTO = Depends(require_authentication),
) -> Dict[str, Any]:
    """
    Get case analytics and metrics

    Returns analytics data including message counts, participant activity,
    resolution time, and other case metrics.
    """
    try:
        # Verify user has access to the case
        case = await case_service.get_case(case_id, current_user.user_id)
        if not case:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Case not found or access denied",
            )

        analytics = await case_service.get_case_analytics(case_id)
        return analytics

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Failed to get case analytics: {e}", exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to get case analytics",
        )
