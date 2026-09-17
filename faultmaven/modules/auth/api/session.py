"""sessions.py

Purpose: Session management endpoints

Requirements:
--------------------------------------------------------------------------------
• Handle session creation, retrieval, and management
• Support user ID association with sessions
• Provide session listing and cleanup

Key Components:
--------------------------------------------------------------------------------
  router = APIRouter()
  @router.post('/sessions')
  @router.get('/sessions/{session_id}')

Technology Stack:
--------------------------------------------------------------------------------
FastAPI, Pydantic

Core Design Principles:
--------------------------------------------------------------------------------
• Privacy-First: Sanitize all external-bound data
• Resilience: Implement retries and fallbacks
• Extensibility: Use interfaces for pluggable components
• Observability: Add tracing spans for key operations
"""

import logging
import time
from datetime import datetime, timedelta, timezone
from typing import Optional

from fastapi import APIRouter, Body, Depends, HTTPException, Query, Response
from pydantic import BaseModel, Field

from faultmaven.api.v1.auth_dependencies import (
    get_current_user_optional,
    require_authentication,
)
from faultmaven.api.v1.dependencies import get_session_service
from faultmaven.config.settings import get_settings
from faultmaven.exceptions import ValidationException
from faultmaven.infrastructure.observability.tracing import trace
from faultmaven.models.api import (
    AuthSessionStatus,
    ErrorDetail,
    SessionErrorCode,
    SessionResponse,
)
from faultmaven.modules.auth.domain.models.auth import DevUser
from faultmaven.modules.auth.domain.services.auth_session_service import (
    AuthSessionService,
)
from faultmaven.utils.serialization import to_json_compatible

router = APIRouter(prefix="/sessions", tags=["session_management"])

# Use standard logger to avoid infrastructure imports
logger = logging.getLogger(__name__)

# Rate-limited logging for repeated session not found errors
_session_not_found_log_tracker = {}
_SESSION_NOT_FOUND_LOG_INTERVAL = 30  # Log every 30 seconds per session_id


def _get_session_timeout_bounds() -> tuple[int, int, int]:
    """Get session timeout bounds from settings.

    Returns:
        Tuple of (min_timeout, max_timeout, default_timeout) in minutes
    """
    settings = get_settings()
    return (
        settings.session.min_timeout_minutes,
        settings.session.max_timeout_minutes,
        settings.session.default_timeout_minutes,
    )


def validate_session_timeout(timeout_minutes: Optional[int]) -> int:
    """
    Validate and clamp session timeout parameter to safe ranges.

    Frontend crash recovery requires specific timeout behavior:
    - Min: 60 minutes (1 hour) - prevents too-frequent expiration
    - Max: 480 minutes (8 hours) - prevents indefinite sessions
    - Default: 180 minutes (3 hours) - good balance for troubleshooting sessions

    Args:
        timeout_minutes: Requested timeout in minutes

    Returns:
        Validated timeout in minutes, clamped to safe range
    """
    min_timeout, max_timeout, default_timeout = _get_session_timeout_bounds()

    if not timeout_minutes or timeout_minutes <= 0:
        logger.debug(f"Using default session timeout: {default_timeout} minutes")
        return default_timeout

    original_timeout = timeout_minutes
    # Clamp to safe range
    validated_timeout = max(min_timeout, min(max_timeout, timeout_minutes))

    if validated_timeout != original_timeout:
        logger.info(
            f"Session timeout clamped from {original_timeout} to {validated_timeout} minutes"
        )

    return validated_timeout


def _safe_datetime_to_utc_string(dt: datetime) -> str:
    """
    Safely convert a datetime object to UTC string with Z suffix.

    Handles both timezone-aware and timezone-naive datetime objects.
    Assumes timezone-naive datetimes are already in UTC.

    Args:
        dt: datetime object to convert

    Returns:
        UTC timestamp string with Z suffix (e.g., "2025-01-15T10:30:00.123Z")
    """
    if dt.tzinfo is not None:
        # Timezone-aware - convert to UTC and make naive
        dt_utc = dt.utctimetuple()
        dt_naive = datetime(*dt_utc[:6], microsecond=dt.microsecond)
        return to_json_compatible(dt_naive)
    else:
        # Timezone-naive - assume it's already UTC
        return to_json_compatible(dt)


def _log_session_not_found_rate_limited(session_id: str) -> None:
    """
    Log session not found error with rate limiting to prevent log spam.

    Only logs once per 30 seconds per session_id to reduce noise from
    frontend clients that repeatedly send heartbeats for expired sessions.

    Args:
        session_id: The session ID that was not found
    """
    current_time = time.time()
    last_logged = _session_not_found_log_tracker.get(session_id, 0)

    if current_time - last_logged >= _SESSION_NOT_FOUND_LOG_INTERVAL:
        # Log with count if this is a repeated occurrence
        if last_logged > 0:
            logger.warning(
                f"Session not found for heartbeat: {session_id} "
                f"(repeated attempts - last logged {int((current_time - last_logged))}s ago)"
            )
        else:
            logger.warning(f"Session not found for heartbeat: {session_id}")

        _session_not_found_log_tracker[session_id] = current_time

        # Clean up old entries to prevent memory leak
        cutoff_time = current_time - (2 * _SESSION_NOT_FOUND_LOG_INTERVAL)
        for sid, logged_time in list(_session_not_found_log_tracker.items()):
            if logged_time < cutoff_time:
                del _session_not_found_log_tracker[sid]


class AuthSessionCreateRequest(BaseModel):
    """Request model for authentication session creation.

    This schema is for auth sessions (user authentication), not investigation sessions.
    Investigation sessions use a different schema in the case module.
    See: docs/architecture/case-and-session-concepts.md for the three-tier architecture.
    """

    timeout_minutes: Optional[int] = Field(
        default=180,
        ge=60,
        le=480,
        description="Session timeout in minutes. Min: 60 (1 hour), Max: 480 (8 hours), Default: 180 (3 hours)",
        examples=[180, 240, 360],
    )
    session_type: Optional[str] = Field(default="troubleshooting", min_length=1)
    metadata: Optional[dict] = None
    client_id: Optional[str] = Field(
        None,
        min_length=1,
        max_length=255,
        description="Client/device identifier for session resumption. If provided, existing session for this client will be resumed.",
        examples=["550e8400-e29b-41d4-a716-446655440000"],
    )


@router.post(
    "",
    status_code=201,
    responses={
        201: {
            "description": "Session created or resumed successfully",
            "content": {
                "application/json": {
                    "examples": {
                        "new_session": {
                            "summary": "New session created",
                            "value": {
                                "session_id": "550e8400-e29b-41d4-a716-446655440000",
                                "user_id": "user_123",
                                "client_id": "browser-client-abc123",
                                "created_at": "2025-01-15T10:00:00Z",
                                "expires_at": "2025-01-15T13:00:00Z",
                                "status": "inquiry",
                                "session_type": "troubleshooting",
                                "session_resumed": False,
                                "timeout_minutes": 180,
                                "message": "Session created successfully",
                            },
                        },
                        "resumed_session": {
                            "summary": "Existing session resumed",
                            "value": {
                                "session_id": "550e8400-e29b-41d4-a716-446655440000",
                                "user_id": "user_123",
                                "client_id": "browser-client-abc123",
                                "created_at": "2025-01-15T09:30:00Z",
                                "expires_at": "2025-01-15T14:30:00Z",
                                "status": "inquiry",
                                "session_type": "troubleshooting",
                                "session_resumed": True,
                                "timeout_minutes": 300,
                                "message": "Session resumed successfully",
                            },
                        },
                    }
                }
            },
        },
        404: {
            "description": "Session expired or not found (when resuming with client_id)",
            "content": {
                "application/json": {
                    "examples": {
                        "session_expired": {
                            "summary": "Session expired",
                            "value": {
                                "detail": "Session expired after 180 minutes of inactivity"
                            },
                        }
                    }
                }
            },
        },
        410: {
            "description": "Session gone (alternative to 404 for expired sessions)",
            "content": {
                "application/json": {
                    "examples": {
                        "session_gone": {
                            "summary": "Session no longer available",
                            "value": {"detail": "Session not found"},
                        }
                    }
                }
            },
        },
        422: {
            "description": "Validation error (invalid timeout_minutes)",
            "content": {
                "application/json": {
                    "examples": {
                        "invalid_timeout": {
                            "summary": "Invalid timeout value",
                            "value": {
                                "detail": "timeout_minutes must be between 60 and 480 minutes"
                            },
                        }
                    }
                }
            },
        },
    },
)
@trace("api_create_session")
async def create_session(
    request: Optional[AuthSessionCreateRequest] = Body(None),
    session_service: AuthSessionService = Depends(get_session_service),
    response: Response = Response(),
    current_user: Optional["DevUser"] = Depends(get_current_user_optional),
):
    """
    Create or resume a troubleshooting session.

    **Session Creation & Resumption:**
    - If `client_id` is provided and matches an active session, that session is resumed
    - If `client_id` matches an expired session, returns 404/410 error (frontend creates new session)
    - If `client_id` is new or not provided, creates fresh session

    **User ID Resolution:**
    - Authenticated user from the JWT token, when one is presented
    - Auto-generated anonymous user otherwise (development/unauthenticated only)

    The identity minted is the server's answer, never the caller's. A request
    cannot name the user its session is bound to; see the note on the
    resolution below.

    **Session Timeout:**
    - Sessions automatically expire after `timeout_minutes` of inactivity
    - Default timeout: 180 minutes (3 hours)
    - Min timeout: 60 minutes, Max timeout: 480 minutes
    - Expired sessions cannot be resumed and return 404/410 errors

    **Frontend Crash Recovery:**
    - Browser crashes: Session resumes if within timeout window
    - Extended downtime: Session expires, new session created automatically

    Args:
        request: Session creation parameters including optional client_id and timeout
        current_user: Optional authenticated user from JWT token

    Returns:
        Session creation/resumption response with expiration information
    """
    try:
        # Prepare metadata from request with validated timeout
        metadata = {}
        validated_timeout_minutes = validate_session_timeout(
            request.timeout_minutes if request else None
        )

        if request:
            if request.session_type:
                metadata["session_type"] = request.session_type
            if request.metadata:
                metadata.update(request.metadata)

        # Always set validated timeout in metadata
        metadata["timeout_minutes"] = validated_timeout_minutes

        # WHOSE identity this session is minted for is the SERVER's answer.
        #
        # It used to be the caller's: a `user_id` query parameter took priority
        # over the bearer token, so a request could name any identity and the
        # authenticated user was never consulted or compared. That parameter is
        # gone (contract 6.0.0). There are two answers left and no way for a
        # request to choose between them:
        #
        #   1. the bearer token's subject, when one is presented;
        #   2. a freshly generated anonymous id, when none is.
        #
        # The order matters more than it looks, because a session id is still
        # accepted as proof of this identity in places (see the note in
        # `api/middleware/idempotency.py`). Minting is therefore a credential
        # operation, and a credential operation must not take dictation.
        if current_user:
            # Use authenticated user's ID from JWT token
            user_id = current_user.user_id
            logger.info(
                f"Using authenticated user_id for session: {user_id} (from JWT)"
            )
        else:
            # Auto-generate anonymous user_id for development
            import uuid

            user_id = f"user_{str(uuid.uuid4())[:8]}"
            logger.warning(
                f"⚠️ Creating anonymous session (user_id={user_id}) without JWT token. "
                f"If user just logged in, frontend should pass JWT token in Authorization header. "
                f"This will cause cases to be invisible after re-login. "
                f"Client ID: {request.client_id if request else 'none'}"
            )

        # Create session with metadata and client_id
        session_result = await session_service.create_session(
            user_id,
            metadata=metadata if metadata else None,
            client_id=request.client_id if request else None,
        )

        # Handle both new session and resumed session scenarios
        if isinstance(session_result, tuple):
            session, was_resumed = session_result
        else:
            session, was_resumed = session_result, False

        action_verb = "resumed" if was_resumed else "created"

        # Enhanced logging for session lifecycle tracking
        log_details = {
            "session_id": session.session_id,
            "user_id": user_id,
            "client_id": request.client_id if request else None,
            "timeout_minutes": validated_timeout_minutes,
            "session_type": metadata.get("session_type", "troubleshooting"),
            "was_resumed": was_resumed,
        }
        logger.info(
            f"Session {action_verb} successfully: {session.session_id} (timeout: {validated_timeout_minutes}min, client: {request.client_id if request else 'none'})"
        )

        # Set Location header for REST compliance
        response.headers["Location"] = f"/api/v1/sessions/{session.session_id}"

        # Calculate expires_at timestamp
        expires_at = session.created_at + timedelta(minutes=validated_timeout_minutes)

        return {
            "session_id": session.session_id,
            "user_id": session.user_id,
            "client_id": request.client_id if request else None,
            "created_at": _safe_datetime_to_utc_string(session.created_at),
            "expires_at": _safe_datetime_to_utc_string(
                expires_at
            ),  # NEW: Session expiration time
            "status": AuthSessionStatus.ACTIVE.value,
            "session_type": metadata.get("session_type", "troubleshooting"),
            "session_resumed": was_resumed,
            "timeout_minutes": validated_timeout_minutes,  # Return validated timeout to frontend
            "message": f"Session {action_verb} successfully",
        }
    except Exception as e:
        logger.error(f"Failed to create session: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail="Failed to create session")


def _caller_scope(current_user: DevUser) -> str:
    """The id to scope a query to, or a refusal — never a falsy value.

    Layer two of the #1447-review fix, and it is deliberately not the only one.
    ``get_current_user_optional`` now refuses a token whose ``sub`` names no
    subject, so in a correct system this never fires; it fires if any future
    identity path hands a route a principal with a blank id. That matters here
    more than elsewhere because the shape these services expose is a FILTER —
    ``if user_id:`` in both ``AuthSessionService.list_sessions`` and the
    minimal stand-in — and a filter that is skipped returns everything rather
    than nothing.

    It REFUSES; it does not NORMALISE. The returned id is the principal's own
    bytes, unstripped, because the four sibling ownership checks on this router
    compare ``session.user_id != current_user.user_id`` unstripped — and a
    session minted under a padded subject is stored under the padded one. A
    scope that stripped while the comparisons did not would answer a padded
    caller an empty listing on this route while its own per-session routes kept
    working, which is a disagreement rather than a defence. Normalisation, if it
    is ever wanted, belongs at the identity layer where the principal is built,
    once, for every reader (#1447 review).

    The refusal is byte-identical to ``require_authentication``'s, on purpose:
    a caller presenting a subject-less credential learns "not authenticated",
    not "your token's shape was the problem".
    """
    user_id = current_user.user_id
    if not isinstance(user_id, str) or not user_id.strip():
        logger.warning(
            "Refusing a session query: the authenticated principal has no subject"
        )
        raise HTTPException(
            status_code=401,
            detail="Authentication required. Please log in to access this resource.",
            headers={"WWW-Authenticate": "Bearer"},
        )
    return user_id


# The auth gate is a ROUTE-LEVEL dependency, not only a handler parameter, and
# that is the difference between a refusal and a 500. FastAPI solves a
# dependant's dependencies in DECLARATION ORDER, and every route on this router
# declared ``session_service`` before ``current_user`` — so on a deployment
# whose Composition Root did not populate ``app.state.session_service``,
# ``get_session_service``'s bare attribute read raised first and an
# unauthenticated caller got 500 before the 401 ever ran. A pre-auth
# availability oracle, on the routes #1447 exists to close. Measured: with the
# slot absent, anonymous ``GET /api/v1/sessions`` answered
# ``500 Internal Server Error``.
#
# Route-level dependencies are solved BEFORE the handler's own parameters, so
# the refusal is unconditional and cannot be undone by a future parameter edit.
# The ``current_user`` parameter stays where a handler needs the principal —
# FastAPI caches a dependency per request, so ``require_authentication`` still
# runs once. ``tests/integration/security/test_unauthenticated_session_surface.py``
# asserts the ordering on a service-less app, because "declared in the right
# order" is not a property anything would otherwise check (#1447 review).
@router.get(
    "/{session_id}",
    response_model=SessionResponse,
    dependencies=[Depends(require_authentication)],
)
async def get_session(
    session_id: str,
    session_service: AuthSessionService = Depends(get_session_service),
    current_user: DevUser = Depends(require_authentication),
) -> SessionResponse:
    """
    Retrieve a specific session by ID.

    Args:
        session_id: Session identifier

    Returns:
        Session details

    Raises:
        404: Session not found
        403: User not authorized to read this session
    """
    try:
        session = await session_service.get_session(session_id)
        if not session:
            _log_session_not_found_rate_limited(session_id)
            raise HTTPException(
                status_code=404,
                detail=ErrorDetail(
                    code=SessionErrorCode.SESSION_NOT_FOUND.value,
                    message=f"Session not found: {session_id}",
                    session_id=session_id,
                ).model_dump(),
            )

        # Authorization check - users can only read their own sessions
        if session.user_id != current_user.user_id:
            raise HTTPException(
                status_code=403, detail="Not authorized to read this session"
            )

        return SessionResponse(
            session_id=session.session_id,
            user_id=session.user_id,
            status=AuthSessionStatus.ACTIVE,
            created_at=_safe_datetime_to_utc_string(session.created_at),
            # `last_activity` only. `data_uploads_count` and
            # `case_history_count` counted lists NOTHING EVER APPENDS TO, so
            # both were always 0 — the same always-zero reporting `/stats` was
            # deleted for, on an endpoint that survives. A client reading
            # `metadata.case_history_count` draws exactly the conclusion the
            # removal was meant to stop it drawing.
            metadata={
                "last_activity": _safe_datetime_to_utc_string(session.last_activity),
            },
        )
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Failed to get session {session_id}: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail="Failed to get session")


# WHOSE sessions this lists is the server's answer, not the request's. The
# route used to take no auth dependency at all AND a `user_id` query FILTER, so
# an anonymous caller could name a user and read back their session ids — and,
# unfiltered, every session id with the identity it is bound to (#1447 §1,
# measured against MinimalSessionService, which ships a working list_sessions
# and which create_session_service installs whenever the real service cannot be
# constructed).
#
# The filter is REMOVED rather than ignored, for the argument contract 6.0.0
# made about the identical parameter on `POST /api/v1/sessions`: once the
# caller's own id is the only legal value, every accepted request carries
# redundant information and every rejected one wrong information, so the
# parameter can no longer say anything.
@router.get("", dependencies=[Depends(require_authentication)])
async def list_sessions(
    session_type: Optional[str] = Query(None),
    limit: int = Query(50, ge=1, le=100),
    offset: int = Query(0, ge=0),
    session_service: AuthSessionService = Depends(get_session_service),
    current_user: DevUser = Depends(require_authentication),
):
    """
    List the caller's own sessions.

    Whose sessions are listed is not a request parameter: the route answers
    with the authenticated caller's own sessions and nothing else. The
    `user_id` filter this route used to accept was removed in contract 7.0.0.

    Args:
        session_type: Optional session type filter
        limit: Maximum number of sessions to return
        offset: Number of sessions to skip

    Returns:
        List of the authenticated caller's sessions
    """
    # OUTSIDE the try, and that is load-bearing: this handler's only exception
    # arm is a bare ``except Exception`` answering 500, with no ``except
    # HTTPException: raise`` before it, so a refusal raised inside the block is
    # swallowed and republished as "Failed to list sessions". Caught by this
    # change's own test — a 500 where a 401 was asserted.
    scope = _caller_scope(current_user)

    try:
        # Get sessions from SessionManager and apply filters/pagination
        all_sessions = await session_service.list_sessions(user_id=scope)

        # Apply session type filtering
        if session_type:
            filtered_sessions = []
            for session in all_sessions:
                try:
                    # Check session metadata for session_type or usage_type
                    session_data = None
                    if (
                        hasattr(session_service, "session_manager")
                        and hasattr(session_service.session_manager, "session_store")
                        and hasattr(
                            session_service.session_manager.session_store, "get"
                        )
                    ):
                        session_data = (
                            await session_service.session_manager.session_store.get(
                                session.session_id
                            )
                        )

                    if session_data:
                        metadata_type = (
                            session_data.get("session_type") or "troubleshooting"
                        )
                        if metadata_type == session_type:
                            filtered_sessions.append(session)
                    elif session_type == "troubleshooting":  # Default type
                        filtered_sessions.append(session)
                except Exception as e:
                    logger.warning(
                        f"Failed to get session metadata for {session.session_id}: {e}"
                    )
                    # Include session if we can't determine its type and filter is for default type
                    if session_type == "troubleshooting":
                        filtered_sessions.append(session)
            all_sessions = filtered_sessions

        # Apply pagination
        total = len(all_sessions)
        paginated_sessions = all_sessions[offset : offset + limit]

        # Format response
        sessions_response = []
        for session in paginated_sessions:
            # Get session metadata for display
            session_type_val = "troubleshooting"  # default
            try:
                session_data = None
                if (
                    hasattr(session_service, "session_manager")
                    and hasattr(session_service.session_manager, "session_store")
                    and hasattr(session_service.session_manager.session_store, "get")
                ):
                    session_data = (
                        await session_service.session_manager.session_store.get(
                            session.session_id
                        )
                    )

                if session_data:
                    session_type_val = (
                        session_data.get("session_type") or "troubleshooting"
                    )
            except Exception as e:
                logger.warning(
                    f"Failed to get session metadata for display {session.session_id}: {e}"
                )

            sessions_response.append(
                {
                    "session_id": session.session_id,
                    "user_id": session.user_id,
                    "created_at": _safe_datetime_to_utc_string(session.created_at),
                    "last_activity": _safe_datetime_to_utc_string(
                        session.last_activity
                    ),
                    "status": "inquiry",
                    "session_type": session_type_val,
                    # The same two always-zero counters as the single-session
                    # read above, removed for the same reason.
                }
            )

        return {
            "sessions": sessions_response,
            "total_count": total,
            "limit": limit,
            "offset": offset,
        }
    except Exception as e:
        logger.error(f"Failed to list sessions: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail="Failed to list sessions")


@router.delete(
    "/{session_id}",
    status_code=204,
    dependencies=[Depends(require_authentication)],
)
async def delete_session(
    session_id: str,
    session_service: AuthSessionService = Depends(get_session_service),
    current_user: DevUser = Depends(require_authentication),
):
    """
    Delete a specific session.

    Args:
        session_id: Session identifier

    Returns:
        Deletion confirmation

    Raises:
        404: Session not found
        403: User not authorized to delete this session
    """
    try:
        # Check if session exists first
        session = await session_service.get_session(session_id)
        if not session:
            _log_session_not_found_rate_limited(session_id)
            raise HTTPException(
                status_code=404,
                detail=ErrorDetail(
                    code=SessionErrorCode.SESSION_NOT_FOUND.value,
                    message=f"Session not found: {session_id}",
                    session_id=session_id,
                ).model_dump(),
            )

        # Authorization check - users can only delete their own sessions
        if session.user_id != current_user.user_id:
            raise HTTPException(
                status_code=403, detail="Not authorized to delete this session"
            )

        # Delete session
        success = await session_service.delete_session(session_id)
        if not success:
            raise HTTPException(status_code=500, detail="Failed to delete session")

        # Return no content for 204 status code
        return None
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Failed to delete session {session_id}: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail="Failed to delete session")


# TAKES NO AUTH DEPENDENCY, and that is a decision rather than the omission its
# three siblings were (#1447 §2). It is the one session route whose closure is
# coupled to the mint, which stays anonymous-capable until the coordinated
# two-repository release tracked by #1460: while `POST /sessions` mints
# `user_id = f"user_{uuid4()[:8]}"` for a caller with no bearer, anonymously
# minted sessions exist and require_authentication would refuse their owner.
#
# The evidence that decided it, gathered across both sibling frontends. The only
# production caller is `heartbeatSession`
# (faultmaven-copilot/packages/copilot-ui/lib/api/services/session-service.ts,
# which the Dashboard runs too, via @faultmaven/copilot-ui), and it deliberately
# does NOT route through `authenticatedFetch`: a failure is swallowed by
# `session-slice.ts` as non-fatal. So a 401 here would neither loop nor sign
# anyone out — the mint's failure mode does not reach this route. What does
# reach it is worse for being quiet: `CopilotPanel` mints unconditionally at
# mount, sign-IN does not re-mint, and a signed-in user holding an anonymously
# minted session would be refused 403 forever with no signal, losing the
# keep-alive this route exists to provide.
#
# It is listed in PUBLIC_OPERATIONS
# (tests/integration/api/test_no_unauthenticated_operations.py) carrying #1460,
# and that guard fails on a stale entry — so closing this route forces the entry
# out rather than leaving a green test asserting nothing.
@router.post("/{session_id}/heartbeat")
async def session_heartbeat(
    session_id: str,
    session_service: AuthSessionService = Depends(get_session_service),
):
    """
    Update session activity timestamp (heartbeat).

    Args:
        session_id: Session identifier

    Returns:
        Heartbeat confirmation
    """
    # Input validation
    if not session_id or not session_id.strip():
        raise HTTPException(status_code=400, detail="Session ID cannot be empty")

    try:
        # Check if session service is available
        if not session_service:
            logger.error("Session service is not available")
            raise HTTPException(status_code=503, detail="Session service unavailable")

        # Update session activity with specific error handling
        try:
            result = await session_service.update_last_activity(session_id)
        except FileNotFoundError:
            _log_session_not_found_rate_limited(session_id)
            raise HTTPException(
                status_code=404,
                detail=ErrorDetail(
                    code=SessionErrorCode.SESSION_NOT_FOUND.value,
                    message=f"Session not found or expired: {session_id}",
                    session_id=session_id,
                ).model_dump(),
            )
        except RuntimeError as e:
            # Handle specific runtime errors from session service
            if "Session store unavailable" in str(e):
                logger.error(
                    f"Session store connection issue during heartbeat for {session_id}: {e}"
                )
                raise HTTPException(
                    status_code=503, detail="Session store temporarily unavailable"
                )
            elif "Activity update operation failed" in str(e):
                logger.error(f"Session activity update failed for {session_id}: {e}")
                raise HTTPException(
                    status_code=500, detail="Failed to update session activity"
                )
            else:
                logger.error(
                    f"Unexpected runtime error during heartbeat for {session_id}: {e}"
                )
                raise HTTPException(status_code=500, detail="Internal server error")
        except Exception as e:
            logger.error(f"Unexpected error during heartbeat for {session_id}: {e}")
            raise HTTPException(status_code=500, detail="Internal server error")

        if not result:
            _log_session_not_found_rate_limited(session_id)
            raise HTTPException(status_code=404, detail="Session not found or expired")

        # No heartbeat "history record" is built here any more. It assembled a
        # dict with a `datetime.now()` + `to_json_compatible` round trip and
        # handed it to `session_manager.add_case_history` — a method that does
        # not exist anywhere in this repository, so the `hasattr` guard has
        # always been False and the record has always been discarded. The
        # identical block went with `/stats`; this is its twin, on the
        # highest-frequency route on the router.

        # Get updated session to return current last_activity (best effort)
        last_activity = to_json_compatible(datetime.now(timezone.utc))  # fallback
        try:
            session = await session_service.get_session(session_id, validate=False)
            if session and session.last_activity:
                last_activity = _safe_datetime_to_utc_string(session.last_activity)
        except Exception as e:
            logger.warning(f"Failed to get updated session info for {session_id}: {e}")

        return {
            "session_id": session_id,
            "status": "inquiry",
            "last_activity": last_activity,
            "message": "Session heartbeat updated",
        }
    except HTTPException:
        # Re-raise HTTP exceptions as-is
        raise
    except Exception as e:
        # Catch-all for unexpected errors
        logger.error(f"Unexpected error in heartbeat for session {session_id}: {e}")
        raise HTTPException(
            status_code=500, detail="Internal server error during heartbeat operation"
        )


# =============================================================================
# Microservices Parity Endpoints (Phase 3 Week 19-20)
# =============================================================================


@router.put("/{session_id}", dependencies=[Depends(require_authentication)])
async def update_session(
    session_id: str,
    updates: dict = Body(...),
    session_service: AuthSessionService = Depends(get_session_service),
    current_user: DevUser = Depends(require_authentication),
):
    """
    Update session metadata.

    Implements microservices parity with fm-session-service.
    Updates authentication-related metadata only (not case data).

    Args:
        session_id: Session identifier
        updates: Dict of fields to update (metadata, timeout_minutes, etc.)

    Returns:
        Updated session information

    Raises:
        404: Session not found
        403: User not authorized to update this session
        400: Invalid update fields (trying to update case data)
    """
    try:
        # Get session to check ownership
        session = await session_service.get_session(session_id)
        if not session:
            raise HTTPException(status_code=404, detail="Session not found")

        # Authorization check - users can only update their own sessions
        if session.user_id != current_user.user_id:
            raise HTTPException(
                status_code=403, detail="Not authorized to update this session"
            )

        # Update session
        success = await session_service.update_session(session_id, updates)
        if not success:
            raise HTTPException(status_code=500, detail="Failed to update session")

        # Get updated session
        updated_session = await session_service.get_session(session_id)

        logger.info(f"Updated session {session_id} for user {current_user.user_id}")

        return {
            "session_id": updated_session.session_id,
            "user_id": updated_session.user_id,
            "created_at": _safe_datetime_to_utc_string(updated_session.created_at),
            "updated_at": _safe_datetime_to_utc_string(updated_session.updated_at),
            "last_activity": _safe_datetime_to_utc_string(
                updated_session.last_activity
            ),
            "metadata": updated_session.metadata,
            "message": "Session updated successfully",
        }

    except HTTPException:
        raise
    except ValidationException as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        logger.error(f"Failed to update session {session_id}: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail="Failed to update session")


@router.post("/search", dependencies=[Depends(require_authentication)])
async def search_sessions(
    search_params: dict = Body(...),
    session_service: AuthSessionService = Depends(get_session_service),
    current_user: DevUser = Depends(require_authentication),
):
    """
    Search user's sessions with filters.

    Implements microservices parity with fm-session-service.
    Searches only the authenticated user's sessions.

    Request body:
        {
            "query": "optional text search",
            "status": "optional status filter (active, archived)",
            "limit": 50
        }

    Returns:
        {
            "sessions": [...],
            "total": int
        }
    """
    # Outside the try for the same reason as the listing above: this handler
    # also catches ``Exception`` and answers 500 with no re-raise arm.
    scope = _caller_scope(current_user)

    try:
        query = search_params.get("query")
        status = search_params.get("status")
        limit = search_params.get("limit", 50)

        # Search sessions
        # Same scope-not-filter rule as the listing: ``search_sessions``
        # forwards this straight to ``list_sessions``, so a blank caller id
        # would search every session rather than none.
        sessions = await session_service.search_sessions(
            user_id=scope, query=query, status=status, limit=limit
        )

        # Format response
        sessions_response = []
        for session in sessions:
            sessions_response.append(
                {
                    "session_id": session.session_id,
                    "user_id": session.user_id,
                    "created_at": _safe_datetime_to_utc_string(session.created_at),
                    "last_activity": _safe_datetime_to_utc_string(
                        session.last_activity
                    ),
                    "status": session.metadata.get("status", "active"),
                    "metadata": session.metadata,
                }
            )

        logger.info(
            f"Search returned {len(sessions)} sessions for user {current_user.user_id} "
            f"(query={query}, status={status})"
        )

        return {"sessions": sessions_response, "total": len(sessions)}

    except Exception as e:
        logger.error(f"Failed to search sessions: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail="Failed to search sessions")


@router.post(
    "/{session_id}/archive",
    dependencies=[Depends(require_authentication)],
)
async def archive_session(
    session_id: str,
    session_service: AuthSessionService = Depends(get_session_service),
    current_user: DevUser = Depends(require_authentication),
):
    """
    Archive a session.

    Implements microservices parity with fm-session-service.
    Sets session status to 'archived' while preserving all data.

    Args:
        session_id: Session identifier

    Returns:
        {
            "session_id": str,
            "status": "archived",
            "message": "Session archived successfully"
        }

    Raises:
        404: Session not found
        403: User not authorized to archive this session
    """
    try:
        # Get session to check ownership
        session = await session_service.get_session(session_id)
        if not session:
            raise HTTPException(status_code=404, detail="Session not found")

        # Authorization check
        if session.user_id != current_user.user_id:
            raise HTTPException(
                status_code=403, detail="Not authorized to archive this session"
            )

        # Archive session
        success = await session_service.archive_session(session_id)
        if not success:
            raise HTTPException(status_code=500, detail="Failed to archive session")

        logger.info(f"Archived session {session_id} for user {current_user.user_id}")

        return {
            "session_id": session_id,
            "status": "archived",
            "message": "Session archived successfully",
        }

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Failed to archive session {session_id}: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail="Failed to archive session")
