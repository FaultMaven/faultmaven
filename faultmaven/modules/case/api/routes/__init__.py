"""Case Management API Routes

Purpose: REST API endpoints for case persistence and management

This module provides REST API endpoints for managing troubleshooting cases,
enabling case persistence across sessions, case sharing, and conversation
history management.

Key Endpoints:
- Case CRUD operations
- Case sharing and collaboration
- Case search and filtering
- Session-case association
- Conversation history retrieval
"""

import asyncio
import hashlib
import logging
import re
import time
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Literal, Optional, Sequence, Union

from fastapi import (
    APIRouter,
    Body,
    Depends,
    File,
    Form,
    HTTPException,
    Path,
    Query,
    Request,
    Response,
    UploadFile,
    status,
)
from fastapi.responses import JSONResponse
from pydantic import ValidationError

from faultmaven.api.exception_handlers import (
    llm_service_error_http_exception,
)
from faultmaven.api.v1.auth_dependencies import (
    get_current_user_id,
    get_current_user_optional,
    require_actor_enterprise,
    require_authentication,
)
from faultmaven.api.v1.dependencies import (
    SUGGESTION_QUEUE_FULL,
    get_case_repository,  # TD-001: use case_repository for reports
    get_case_service,
    get_case_vector_store,
    get_data_service,
    get_investigation_service,  # V2.0 milestone-based
    get_preprocessing_service,
    get_session_service,
    get_suggestion_service,
)
from faultmaven.config.tenant_context import get_current_enterprise_id
from faultmaven.core.investigation.schemas import Attachment, TurnPayload
from faultmaven.core.investigation.turn_budget import bind_turn_deadline
from faultmaven.exceptions import (
    AuthorizationError,
    FaultMavenException,
    NotFoundError,
    PermissionDeniedException,
    ServiceException,
    ServiceUnavailableException,
    SessionException,
    ValidationException,
)
from faultmaven.infrastructure.base_client import CircuitBreakerError
from faultmaven.infrastructure.knowledge.runbook_kb import RESULTS_UNREADABLE_CODE
from faultmaven.infrastructure.llm.router import resolve_chat_provider_name
from faultmaven.infrastructure.observability.tracing import trace
from faultmaven.infrastructure.protection.tenant_turn_cap import (
    TenantTurnCapExceeded,
    TenantTurnCapUnavailable,
)

# TD-001: IReportStore removed - reports now accessed via CaseRepository
from faultmaven.models.api import (
    AgentResponse,
    Case,
    CaseMessagesResponse,
    CaseResponse,
    DataType,
    ErrorDetail,
    ErrorResponse,
    Message,
    ProcessingStatus,
    QueryJobStatus,
    QueryRequest,
    ResponseType,
    TitleGenerateResponse,
    TitleResponse,
    User,
    ViewState,
)
from faultmaven.models.api_models import (  # Phase 2: Evidence-to-File Linkage
    AttachmentResult,
    CaseCreateRequest,
    CaseDetail,
    CaseEvidenceListResponse,
    CaseListFilter,
    CaseListResponse,
    CaseMessage,
    CaseParticipant,
    CaseSearchRequest,
    CaseSummary,
    CaseUpdateRequest,
    DerivedEvidenceSummary,
    EvidenceDetailsResponse,
    IntentType,
    QueryIntent,
    RelatedHypothesis,
    SourceFileReference,
    TurnResponse,
    UploadedFileDetails,
    UploadedFileDetailsResponse,
    UploadedFileMetadata,
    UploadedFilesList,
    bound_to_utc,
)
from faultmaven.models.case_ui import CaseUIResponse
from faultmaven.models.exceptions import KnowledgeBaseError
from faultmaven.models.interfaces_case import ICaseService

# Cross-module imports via contracts (Principle 2: Vertical Modules with Contracts)
from faultmaven.modules.auth.contracts import ISessionService, UserDTO
from faultmaven.modules.case.domain.models import Case as CaseEntity
from faultmaven.modules.case.domain.models import CaseState, is_default_case_title
from faultmaven.modules.case.domain.services.case_converter import CaseConverter
from faultmaven.modules.case.domain.services.case_ui_adapter import (
    transform_case_for_ui,
)
from faultmaven.modules.case.exceptions import StaleCaseException
from faultmaven.modules.case.infrastructure.case_repository import CaseRepository
from faultmaven.utils.serialization import to_json_compatible

# fm#1707: the old module's only direct use of `is_default_case_title` (the
# `_is_default_case_title` alias) moved into title_generation.py, so this
# module-level binding is otherwise unread until `generate_case_reports`'s
# unmoved local `from faultmaven.modules.case.contracts import
# is_default_case_title` shadows it -- which ruff's F811 reads as this import
# being unused before it is redefined. It stays imported here (facade parity:
# `from faultmaven.modules.case.api.routes import is_default_case_title` must
# keep working, same as on origin/main) with one no-op read to tell ruff
# otherwise.
_ = is_default_case_title

from .title_generation import (
    _MIN_PROBLEM_STATEMENT_LEN_FOR_TITLE,
    _TITLE_EDGE_PUNCT,
    _TITLE_TRAILING_REPAIR,
    AUTO_TITLE_TIMEOUT_SECONDS,
    BANNED_GENERIC_WORDS,
    CONTEXT_MESSAGE_LIMIT,
    CONVERSATIONAL_FILLER,
    EXTRACTIVE_MAX_CONTENT_LENGTH,
    INCOMPLETE_ENDINGS,
    LLM_TITLE_MAX_TOKENS,
    LLM_TITLE_TEMPERATURE,
    LLM_TITLE_TOP_P,
    LY_NOT_ADVERB,
    MAX_TITLE_WORDS_DEFAULT,
    MAX_USER_MESSAGES_FOR_CONTEXT,
    MIN_CONTENT_LENGTH_FOR_TITLE,
    MIN_EXTRACTIVE_WORDS,
    MIN_MESSAGE_WORD_COUNT,
    MIN_TITLE_LENGTH,
    MIN_TITLE_WORDS,
    TITLE_CASE_LOWERCASE_WORDS,
    _auto_title_case_if_default,
    _case_problem_statement,
    _extract_user_signals_from_context,
    _generate_and_persist_title,
    _generate_smart_extractive_title,
    _generate_title_with_llm,
    _has_problem_statement,
    _is_default_case_title,
    _is_manner_adverb,
    _sanitize_title_content,
    _titleable_substance,
    _TitleSubstanceTooThin,
    _word_can_end_title,
    apply_title_case,
    get_extractive_fallback_title,
    is_title_valid,
    truncate_title_at_phrase_boundary,
)

# Create router
router = APIRouter(prefix="/cases", tags=["cases"])

# Include Replay Router


# Set up logging
logger = logging.getLogger(__name__)


# Helper function to safely extract enum values
def _safe_enum_value(value):
    """Safely extract enum value, return string if already string."""
    if hasattr(value, "value"):
        return value.value
    return str(value)


def resolve_paste_source_meta(
    input_type: Optional[str], source_url: Optional[str]
) -> tuple[dict, str]:
    """Source metadata + filename prefix for a ``pasted_content`` attachment.

    Returns ``(source_meta, filename_prefix)``. The origin discrimination feeds
    the classifier's confidence boost downstream:

    - ``page_capture`` — the browser extension captured a web page
    - ``text_paste``   — raw text pasted by a user, or relayed by an agent

    ``source_url`` stays scoped to ``page_capture``. It means the URL the
    CONTENT came from — the classifier consults it at Priority 3 (0.88-0.94),
    ahead of the content rules, precisely because for a capture the page IS the
    content's origin. A relay link is a different thing: the Slack agent's
    permalink points at the message that forwarded an alert, not at where the
    alert's text came from, and feeding it to a content classifier would let a
    pasted excerpt be typed by whatever page someone happened to copy it out of.
    Recording relay provenance is worth doing, but it needs its own field and a
    consumer; overloading this one is not the way.

    This is a module-level function rather than inline branching because the
    test suite previously kept its own hand-copied mirror of the logic, which
    meant a change to the route left the mirror stale and the tests green.
    Tests import THIS.
    """

    if input_type == "page_capture":
        meta = {"source_type": "page_capture"}
        prefix = "page-capture-"
        if source_url:
            meta["source_url"] = source_url
    else:
        meta = {"source_type": "text_paste"}
        prefix = "pasted-content-"
    return meta, prefix


def _parse_observed_at(raw: Optional[str], correlation_id: str) -> Optional[datetime]:
    """Parse the caller-supplied ``observed_at`` into an aware UTC instant.

    Fails to ``None`` — never to "now" and never to a 4xx. This field is a
    voluntary provenance hint from a forwarding caller; a client that sends a
    malformed one should still get its turn processed, just without a claim
    about when the content was observed. Substituting the current time would
    manufacture the exact false currency the field exists to prevent.

    A naive timestamp is read as UTC (the wire contract is UTC) and a future
    one is rejected: content cannot have been observed after it was submitted,
    so a future value means a broken clock or a bad conversion, and trusting it
    would make stale evidence look fresher than it is — the unsafe direction.
    """

    if not raw:
        return None
    try:
        parsed = datetime.fromisoformat(raw.strip().replace("Z", "+00:00"))
    except (ValueError, AttributeError):
        logger.warning(
            "Ignoring un-parseable observed_at %r (correlation_id=%s)",
            raw[:64],
            correlation_id,
        )
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    else:
        parsed = parsed.astimezone(timezone.utc)
    # Small tolerance so ordinary clock skew between the caller and this host
    # doesn't discard a legitimate just-now observation.
    if parsed > datetime.now(timezone.utc) + timedelta(minutes=5):
        logger.warning(
            "Ignoring future observed_at %s (correlation_id=%s)",
            parsed.isoformat(),
            correlation_id,
        )
        return None
    return parsed


def _resolve_agent_timeout(settings) -> tuple[float, str]:
    """Resolve the per-provider agent-level timeout for the active CHAT_PROVIDER.

    Mirrors the LLM-router's ``_resolve_timeout`` shape (ISS-054) but applies to
    the agent-level (turn-wide) ceiling enforced via ``asyncio.wait_for``.

    Returns a ``(timeout_seconds, provider_name_for_logging)`` tuple. The
    returned name is the resolved provider string (or ``"default"`` when the
    setting is missing entirely) so log lines can attribute timeouts.

    See ISS-058.
    """
    # Resolved by the SAME helper the LLM router uses for its own per-provider
    # timeout lookup. The two sides of the turn budget must agree on which
    # provider they are talking about, or a comparison between them compares
    # two different providers' timeouts.
    provider_name = resolve_chat_provider_name(settings)
    timeout = float(settings.agent.timeout_for_provider(provider_name))
    return timeout, provider_name or "default"


async def _di_get_case_service_dependency(request: Request) -> Optional[ICaseService]:
    """Runtime wrapper so patched dependency is honored in tests."""
    # Import inside to resolve the patched function at call time
    from faultmaven.api.v1.dependencies import get_case_service as _getter

    return await _getter(request)


# Legacy dependency functions removed - using new auth_dependencies directly


async def _di_get_session_service_dependency(request: Request) -> ISessionService:
    """Runtime wrapper so patched dependency is honored in tests."""
    from faultmaven.api.v1.dependencies import get_session_service as _getter

    return await _getter(request)


async def _di_get_runbook_kb_dependency(request: Request):
    """Get the DI-provided runbook-dedup KB (fm#1030).

    The container binds this reader to the SAME ChromaDB collection the KB
    writer writes (``create_runbook_dedup_kb``). The route must not build its
    own over ``container.vector_store`` — that store binds the
    settings-derived collection name, which diverges from the production
    writer's hardcoded ``KB_COLLECTION`` the moment ``CHROMADB_COLLECTION``
    is overridden, silently reinstating the empty-result dedup.
    """
    try:
        container = request.app.extra.get("di_container")
        if container:
            return getattr(container, "runbook_kb", None)
        return None
    except Exception:
        return None


def check_case_service_available(case_service: Optional[ICaseService]) -> ICaseService:
    """Check if case service is available and raise appropriate error if not"""
    if case_service is None:
        # For protected endpoints that require authentication, return 401 instead of 500
        # This prevents pre-auth calls from getting 500 errors
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Authentication required - case service unavailable",
        )
    return case_service


def require_case_not_terminal(case) -> None:
    """Reject write operations on terminal (RESOLVED/CLOSED) cases."""
    if case.is_terminal:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Case is in terminal state and read-only. No further modifications allowed.",
        )


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


async def _di_get_creator_service_channel(
    raw_request: Request,
    current_user: UserDTO = Depends(require_authentication),
) -> Optional[str]:
    """Resolve the creating account's ``service_channel`` for source stamping.

    The **channel**, not the kind (ADR-017 D6): there are exactly two kinds of
    account — a human and a service — and which integration a service account
    serves is a separate attribute, so a second integration is a new value here
    rather than a third account kind. Reading the kind would answer 'service'
    for every integration and could no longer say which one.

    Best-effort: falls back to ``None`` if the user service is unavailable or
    the lookup fails, so case creation never depends on it.
    """
    user_service = getattr(raw_request.app.state, "user_service", None)
    if user_service is None:
        return None
    try:
        user = await user_service.get_user(current_user.user_id)
        return getattr(user, "service_channel", None) if user else None
    except Exception as e:
        # Don't fail case creation on this — but do NOT swallow silently: a
        # Slack case mislabeled 'copilot' (source is immutable) is otherwise
        # undetectable.
        logger.warning(
            "Could not resolve service_channel for user %s; case source will "
            "default to 'copilot': %s",
            getattr(current_user, "user_id", "?"),
            e,
        )
        return None


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
        from faultmaven.modules.case.domain.models import Case as CaseEntity

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


# Health and status endpoints


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


# Conversation thread retrieval (messages)
@router.get(
    "/{case_id}/messages",
    response_model=CaseMessagesResponse,
    dependencies=[Depends(require_authentication)],
)
@trace("api_get_case_messages_enhanced")
async def get_case_messages_enhanced(
    case_id: str,
    response: Response,
    limit: int = Query(
        50, le=100, ge=1, description="Maximum number of messages to return"
    ),
    offset: int = Query(0, ge=0, description="Offset for pagination"),
    include_debug: bool = Query(
        False, description="Include debug information for troubleshooting"
    ),
    case_service: Optional[ICaseService] = Depends(_di_get_case_service_dependency),
    current_user: UserDTO = Depends(require_authentication),
) -> CaseMessagesResponse:
    """
    Retrieve conversation messages for a case with enhanced debugging info.
    Supports pagination and includes metadata about message retrieval status.
    """
    case_service = check_case_service_available(case_service)
    correlation_id = str(uuid.uuid4())
    response.headers["x-correlation-id"] = correlation_id

    try:
        # Verify user has access to the case
        case = await case_service.get_case(case_id, current_user.user_id)
        if not case:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Case not found or access denied",
            )

        # Use the enhanced message retrieval method
        message_response = await case_service.get_case_messages_enhanced(
            case_id=case_id, limit=limit, offset=offset, include_debug=include_debug
        )

        # Add headers for metadata. X-Total-Count is the canonical pagination
        # header used by every other list endpoint (and expected by the contract
        # probe / any generic paginating client); X-Message-Count is kept for
        # backward compatibility.
        response.headers["X-Total-Count"] = str(message_response.total_count)
        response.headers["X-Message-Count"] = str(message_response.total_count)
        response.headers["X-Retrieved-Count"] = str(message_response.retrieved_count)

        # Determine storage status
        storage_status = "success"
        if message_response.debug_info and message_response.debug_info.storage_errors:
            storage_status = (
                "error" if message_response.retrieved_count == 0 else "partial"
            )
        response.headers["X-Storage-Status"] = storage_status

        return message_response

    except HTTPException:
        raise
    except Exception as e:
        logger.error(
            f"Unexpected error in get_case_messages_enhanced: {e}",
            extra={"correlation_id": correlation_id},
        )
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to get messages",
            headers={"x-correlation-id": correlation_id},
        )


# Session-case integration endpoints


@router.post(
    "/sessions/{session_id}/resume/{case_id}",
    response_model=Dict[str, Any],
    dependencies=[Depends(require_authentication)],
)
@trace("api_resume_case_in_session")
async def resume_case_in_session(
    session_id: str,
    case_id: str,
    case_service: Optional[ICaseService] = Depends(_di_get_case_service_dependency),
    session_service: ISessionService = Depends(_di_get_session_service_dependency),
    current_user: UserDTO = Depends(require_authentication),
) -> Dict[str, Any]:
    """
    Resume an existing case in a session

    Links the session to an existing case, allowing the user to continue
    a previous troubleshooting conversation.
    """
    case_service = check_case_service_available(case_service)

    try:
        # TWO gates, because this route names two resources (#1390, #1393).
        # `faultmaven/api/routes/sessions.py` states the rule its own surface
        # follows — the case gate "covers the case named in the path and
        # nothing else … Both halves are needed; neither is sufficient" — and
        # this route had neither.
        #
        # ── 1. The case ──────────────────────────────────────────────────
        # Enforced in BOTH places, deliberately (#1398).
        #
        # The authoritative gate is inside `link_session_to_case`: it is on
        # `ICaseService`, reachable by another route, the Slack agent or an
        # `fm-*` CLI, and a gate that lives only in this handler protects only
        # this handler. That one also folds in the existence check, so the
        # member asks one question of one load.
        #
        # This early check is a SECOND resolution of the same row, and it is
        # kept for ordering rather than for safety. The session check below
        # cannot always be evaluated — where no session store is configured it
        # answers 503 — and if it ran first it would answer BOTH parties the
        # same way, which is how `test_two_enterprise_surface_probe` stops
        # exercising the case boundary on this route at all ("a parametrisation
        # that never reaches the tenant check asserts nothing"). Refusing here
        # keeps the cross-tenant refusal attributable to the case, and makes it
        # cheap: no session lookup for a caller who was never going to pass.
        #
        # A case the caller cannot reach raises `NotFoundError` from the
        # service, which the app's handler answers as 404; a link that FAILS
        # returns False and is a 500 below. Keeping those apart is the point —
        # conflating them reported a working resume as an absence (#1390).
        #
        # Owner ∪ shared-to-my-teams, matching `submit_turn` and the service's
        # own gate. That is a deliberate departure from the method-based rule
        # in `sessions.py`, which picks `owner_only` from the HTTP method
        # because "a share grants read visibility, not the right to write"
        # (ADR-017 D4) — and this is a POST that writes `cases.last_activity`
        # through `update_activity_timestamp`.
        #
        # The exception is bounded: a teammate who may POST a turn into a
        # shared case already writes messages, turn history AND that same
        # activity stamp, so refusing the resume while admitting the turn would
        # leave the extension able to read and write a case it cannot open. The
        # only row this path touches that a read share does not already cover
        # is `last_activity`, which is bookkeeping about access rather than
        # case content, and the teammate's own turn bumps it moments later.
        case = await case_service.get_case(case_id, current_user.user_id)
        if case is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Case not found or resume not permitted",
            )

        # ── 2. The session ───────────────────────────────────────────────
        # Without this, naming SOMEONE ELSE'S session id retargets their
        # `session:{id}:current_case_id` pointer at a case of the caller's
        # choosing: the owner's next turn either lands in the caller's case (if
        # they can reach it) or silently abandons the case they were working —
        # and the write lands before any of this route's failure paths, so the
        # pre-fix 404 never prevented it.
        #
        # Ordered AFTER the case gate on purpose. Both answer 404, so neither
        # ordering leaks anything; but the two-enterprise probe drives this
        # route with a session id belonging to nobody, so checking the session
        # first would refuse BOTH parties there and the case half would stop
        # being exercised — the "parametrisation that never reaches the tenant
        # check asserts nothing" failure that probe's own docstring warns about.
        #
        # `get_session` RAISES rather than returning None when it cannot
        # answer — `ServiceException("Session store not configured")` is the
        # shipped case. Treating it as a nullable return let that escape to the
        # handler's bare `except` and answer 500 on a request the server simply
        # could not evaluate. An unevaluable gate is a 503, which is the rule
        # `faultmaven/api/routes/sessions.py` already states for its own:
        # "503 if the case service is unavailable (the gate cannot be
        # evaluated, so nothing is served)".
        try:
            session = await session_service.get_session(session_id, validate=True)
        except (ServiceException, SessionException) as exc:
            # BOTH families. `ServiceException("Session store not configured")`
            # is the unconfigured case; a store that IS configured and
            # unreachable raises `SessionStoreException`, which descends from
            # `SessionException` and NOT from `ServiceException`. Catching one
            # made two spellings of "the gate could not be evaluated" answer
            # 503 and 500 respectively — the inconsistency this is fixing.
            logger.warning(f"Cannot evaluate session ownership for {session_id}: {exc}")
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="Session service unavailable",
            )

        if session is None or session.user_id != current_user.user_id:
            # One answer for "no such session" and "not yours": naming
            # another user's session must not be distinguishable from naming
            # one that does not exist. 404 rather than 401 — the caller IS
            # authenticated, and 401 would tell a client to re-authenticate
            # for a request that will never succeed.
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Session not found or resume not permitted",
            )

        success = await case_service.resume_case_in_session(
            case_id, session_id, current_user.user_id
        )

        if not success:
            # NOT a 404. A case the caller cannot reach raised `NotFoundError`
            # inside the service and never got here, and the session is theirs
            # — so a falsy result is the link itself failing (a repository
            # error, a session store that refused the write), the server's
            # problem.
            # Reporting it as "not found or not permitted" is the same
            # half-success-as-absence shape this endpoint was fixed for: the
            # client abandons a case that is fine instead of retrying.
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail="Failed to resume case",
            )

        return {
            "case_id": case_id,
            "success": True,
            "message": "Case resumed in session",
        }

    except HTTPException:
        raise
    except NotFoundError:
        # The access verdict from `link_session_to_case`. Re-raised so the
        # app's handler answers 404; the bare handler below would make it a
        # 500 and tell the caller the server broke on a request it was simply
        # not allowed to make.
        raise
    except ValidationException as e:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(e))
    except Exception as e:
        logger.error(f"Failed to resume case: {e}", exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to resume case (unexpected error)",
        )


# ============================================================
# Unified Turn Endpoint (v4.1)
# ============================================================


@router.post(
    "/{case_id}/turns",
    response_model=TurnResponse,
    dependencies=[Depends(require_authentication)],
)
@trace("api_submit_turn")
async def submit_turn(
    case_id: str,
    request: Request,
    query: Optional[str] = Form(None),
    # One file per turn (fm#694), enforced by the framework rather than by hand:
    # `max_length=1` both rejects a second file with the app's normalized 422
    # AND publishes `maxItems: 1` into the OpenAPI schema, so the narrowing is
    # visible to clients and to scripts/check_contract_version.py. A hand-rolled
    # `len(files) > 1` raise enforced the same rule invisibly.
    #
    # The cap is on `files` ALONE. `pasted_content` is a separate form field
    # that legitimately rides alongside a file as a second attachment, and that
    # is a shipped path — counting it here would break paste+file turns.
    #
    # One thing this does NOT do, stated so nobody reads more into it: it is
    # correctness-only, not a cost control. Starlette parses and spools the
    # ENTIRE multipart body (main.py patches Request.form with max_files=1000)
    # before pydantic validates, so a 50-file request is fully read off the
    # wire and written to temp files before the 422.
    #
    # It is also not what bounds the clarification emitter. A one-file turn
    # still carries two attachments when a paste rides along, and both can
    # fail classification; the emitter clarifies every failure rather than
    # reasoning from this cap (#1222).
    files: List[UploadFile] = File(
        default=[],
        max_length=1,
        description=(
            "At most ONE file per turn. Submit each additional file as its own "
            "turn. `pasted_content` is a separate field and does not count "
            "toward this limit, so a turn may carry one file *and* a paste."
        ),
    ),
    pasted_content: Optional[str] = Form(None),
    intent_type: Optional[str] = Form(None),
    intent_data: Optional[str] = Form(None),
    input_type: Optional[str] = Form(None),
    source_url: Optional[str] = Form(None),
    observed_at: Optional[str] = Form(None),
    case_service: Optional[ICaseService] = Depends(_di_get_case_service_dependency),
    investigation_service=Depends(get_investigation_service),
    current_user: UserDTO = Depends(require_authentication),
) -> TurnResponse:
    """Submit a turn to a case investigation.

    A turn consists of an optional query and/or optional attachments.
    Attachments are preprocessed through Tier 0+1 before the LLM sees them.
    If no query is provided with attachments, an implicit query is generated.

    **One file per turn.** `files` accepts at most one item (`maxItems: 1`);
    more than one is rejected with 422. Submit each additional file as its own
    turn. `pasted_content` is a separate field and does not count against this
    limit, so a turn may legitimately carry one file *and* a paste.

    **Auto-titling:** a case still carrying its auto-generated `Case-YYMMDD-N`
    placeholder is named from its own content as part of processing the turn, if
    it now has enough substance to name — so the name is already in place when
    this responds. Clients do not need to call `POST /cases/{case_id}/title` for
    this; that endpoint remains for user-initiated (re)naming. A case is
    auto-titled at most once — the moment a real title lands, later turns leave
    it alone. Naming is best-effort and time-bounded: it can never fail or
    delay the turn itself.
    """
    import json

    case_service = check_case_service_available(case_service)
    correlation_id = str(uuid.uuid4())

    try:
        # An EMPTY turn — no query, no file, no paste — is accepted: it is a
        # bare @mention in Slack, and the service answers it with a state-aware
        # orientation (where the case stands, what to do next) rather than the
        # 400 the client used to have to swallow.

        # Validate case_id
        if not case_id or case_id.strip() in ("", "undefined", "null"):
            raise HTTPException(
                status_code=400,
                detail="Valid case_id is required",
                headers={"x-correlation-id": correlation_id},
            )

        # Size cap on text-shaped form fields. Multipart files are bounded by
        # Starlette via `_upload_max_bytes`, but `query` and `pasted_content`
        # are raw form fields — without this guard a 50MB paste would be
        # accepted, decoded, classified, and copied through the pipeline
        # before any extractor's own cap fired. Trivial DoS surface otherwise.
        from faultmaven.config.settings import get_settings as _get_settings

        _max_text_bytes = _get_settings().upload.max_upload_size_mb * 1024 * 1024
        if pasted_content and len(pasted_content.encode("utf-8")) > _max_text_bytes:
            raise HTTPException(
                status_code=413,
                detail=(
                    f"pasted_content exceeds the {_max_text_bytes // (1024 * 1024)}MB "
                    f"limit. Upload as a file instead."
                ),
                headers={"x-correlation-id": correlation_id},
            )
        if query and len(query.encode("utf-8")) > _max_text_bytes:
            raise HTTPException(
                status_code=413,
                detail=(
                    f"query exceeds the {_max_text_bytes // (1024 * 1024)}MB limit."
                ),
                headers={"x-correlation-id": correlation_id},
            )

        # Per-attachment size cap. Starlette >= 1.1 bounds only non-file form
        # fields via max_part_size (see main.py); file parts reach the route
        # unbounded, so the same MAX_UPLOAD_SIZE_MB limit is enforced here.
        for upload in files:
            if upload.size is not None and upload.size > _max_text_bytes:
                raise HTTPException(
                    status_code=413,
                    detail=(
                        f"{upload.filename or 'attachment'} exceeds the "
                        f"{_max_text_bytes // (1024 * 1024)}MB upload limit."
                    ),
                    headers={"x-correlation-id": correlation_id},
                )

        # Verify case exists and user has access
        case = await case_service.get_case(case_id, current_user.user_id)
        if not case:
            raise HTTPException(
                status_code=404,
                detail="Case not found or access denied",
                headers={"x-correlation-id": correlation_id},
            )

        # Terminal cases: allow text-only Q&A, block evidence and state transitions
        if case.is_terminal:
            if files or pasted_content:
                raise HTTPException(
                    status_code=status.HTTP_409_CONFLICT,
                    detail="Cannot submit new data to a closed case. Only questions about the case are allowed.",
                    headers={"x-correlation-id": correlation_id},
                )
            if intent_type == "status_transition":
                raise HTTPException(
                    status_code=status.HTTP_409_CONFLICT,
                    detail="Cannot change status of a closed case.",
                    headers={"x-correlation-id": correlation_id},
                )
            if intent_type == "file_reclassification":
                raise HTTPException(
                    status_code=status.HTTP_409_CONFLICT,
                    detail="Cannot reclassify files on a closed case.",
                    headers={"x-correlation-id": correlation_id},
                )

        # Build attachments list
        # Every attachment carries source_metadata so the classifier knows the
        # input origin and can apply the correct confidence boosts:
        #   file_upload  → user selected a local OS file
        #   text_paste   → user pasted raw text into the scratchpad
        #   page_capture → browser extension captured a web page (has source URL)
        attachments = []
        for f in files:
            content = await f.read()
            attachments.append(
                Attachment(
                    content=content,
                    filename=f.filename or "unnamed_file",
                    content_type=f.content_type or "application/octet-stream",
                    source_metadata={"source_type": "file_upload"},
                )
            )
        if pasted_content:
            ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S")

            # When the caller says the content was OBSERVED. Distinct from the
            # `ts` above, which is ingestion time and is all the synthetic
            # filename can ever carry. A caller forwarding something it did not
            # just witness (the Slack agent relaying an alert posted hours ago)
            # is the only party that knows this, so an unparseable value is
            # dropped to None rather than guessed at: unknown is honest, and
            # "now" would be the exact false claim this field exists to stop.
            observed_at_dt = _parse_observed_at(observed_at, correlation_id)

            # Determine source type from the explicit `input_type` form field.
            # The frontend (UnifiedInputBar.tsx) always sets this since the
            # text_paste pathway shipped — see faultmaven-copilot:
            # src/shared/ui/components/UnifiedInputBar.tsx:226-256.
            #
            # The legacy `--- Page Content (URL) ---` body header is no
            # longer recognized as a page-capture signal: it was a write-
            # around (a paste shaped that way bypassed Tier-1 extraction by
            # entering the page-capture passthrough), and the explicit
            # `input_type` field has fully replaced it.
            source_meta, filename_prefix = resolve_paste_source_meta(
                input_type, source_url
            )
            filename = f"{filename_prefix}{ts}.txt"

            attachments.append(
                Attachment(
                    content=pasted_content.encode("utf-8"),
                    filename=filename,
                    content_type="text/plain",
                    source_metadata=source_meta,
                    observed_at=observed_at_dt,
                )
            )

        # Build intent
        intent = None
        if intent_type:
            data = json.loads(intent_data) if intent_data else {}
            # Remove 'type' from data to avoid conflict with explicit type= arg
            data.pop("type", None)
            # Defensive: a malformed intent from a client must not crash the turn
            # with an unhandled error -> 500. Two failure modes, both -> 422:
            #   (a) an unrecognized intent_type (e.g. a suggestion action_type
            #       like 'free_speech') -> IntentType() ValueError;
            #   (b) a valid intent_type with invalid/missing fields (e.g. a
            #       'status_transition' without to_state) -> QueryIntent
            #       ValidationError.
            try:
                parsed_intent_type = IntentType(intent_type)
            except ValueError:
                raise HTTPException(
                    status_code=422,
                    detail=(
                        f"Unknown intent_type {intent_type!r}. Valid values: "
                        + ", ".join(t.value for t in IntentType)
                        + "."
                    ),
                    headers={"x-correlation-id": correlation_id},
                )
            try:
                intent = QueryIntent(type=parsed_intent_type, **data)
            except (ValidationError, ValueError) as e:
                raise HTTPException(
                    status_code=422,
                    detail=f"Invalid fields for intent_type={intent_type!r}: {e}",
                    headers={"x-correlation-id": correlation_id},
                )

        payload = TurnPayload(query=query, attachments=attachments, intent=intent)

        # Process turn with configurable timeout (provider-aware — see ISS-058).
        try:
            from faultmaven.config.settings import get_settings

            agent_timeout, provider_name = _resolve_agent_timeout(get_settings())
            logger.info(
                f"Processing turn for case {case_id} with {agent_timeout}s timeout "
                f"(provider={provider_name})"
            )
            # Bind the same ceiling as a DEADLINE for the duration of the turn,
            # so the LLM retry ladder inside can budget against the cancellation
            # that would otherwise cut it mid-attempt (#1278, #1292). This is the
            # only site that knows both the ceiling and the instant it starts;
            # deriving either independently downstream is exactly the drift the
            # two settings already have between them. Scoped to the wait_for and
            # nothing else — auto-titling below has its own timeout and must not
            # be charged to the turn budget.
            with bind_turn_deadline(agent_timeout):
                response = await asyncio.wait_for(
                    investigation_service.process_turn(
                        case_id=case_id, user_id=current_user.user_id, payload=payload
                    ),
                    timeout=agent_timeout,
                )

            # Name the case from its own content. Called unconditionally: whether
            # the case is *titleable* is decided inside, against the case as it
            # stands after this turn, by the same gate the title endpoint uses.
            #
            # Awaited BEFORE returning rather than deferred to a background task,
            # which orders it ahead of the next turn's load and so keeps that
            # turn's full-row save from writing the placeholder back over the
            # generated title. It costs the turn ~1ms on the extractive path and
            # up to AUTO_TITLE_TIMEOUT_SECONDS in the worst case, once per case.
            # See _auto_title_case_if_default — the placement is load-bearing.
            await _auto_title_case_if_default(
                case_id=case_id,
                user_id=current_user.user_id,
                case_service=case_service,
                llm_provider=getattr(request.app.state, "llm_provider", None),
            )

            return response

        except asyncio.TimeoutError:
            from faultmaven.config.settings import get_settings

            agent_timeout, provider_name = _resolve_agent_timeout(get_settings())
            logger.error(
                f"Turn processing timed out for case {case_id} after {agent_timeout}s "
                f"(provider={provider_name})"
            )
            raise HTTPException(
                status_code=504,
                detail="Request timeout - processing is taking longer than expected. Please try again.",
                headers={
                    "x-correlation-id": correlation_id,
                    "x-error-code": "REQUEST_TIMEOUT",
                    "Retry-After": "30",
                },
            )

    except StaleCaseException as e:
        # OCC conflict — another writer updated the case while this turn
        # was in flight. We deliberately do NOT silently retry: LLM turns
        # are expensive and non-idempotent (tool calls trigger external
        # side effects, tokens get spent). Surface the conflict to the
        # client so it can reload and decide whether to re-submit.
        logger.warning(
            f"Stale case on turn submission for {case_id}: "
            f"expected v{e.expected_version}, db v{e.actual_version}"
        )
        raise HTTPException(
            status_code=409,
            detail=(
                "Case state changed while processing this turn. "
                "Reload the case and resubmit if still applicable."
            ),
            headers={
                "x-correlation-id": correlation_id,
                "x-error-code": "CASE_VERSION_CONFLICT",
                "x-expected-version": str(e.expected_version),
                "x-actual-version": str(e.actual_version),
            },
        )
    except TenantTurnCapExceeded as capped:
        # The per-tenant daily cap (ADR-016 D5.3). Raised from
        # ``InvestigationService.process_turn`` once the request is known to be
        # a real turn on a case this caller may write to; the route's job is
        # only to render it. 429 with a distinct ``x-error-code`` because a
        # rate-limit 429 and a cap 429 want opposite reactions from a client:
        # one means slow down, this one means come back tomorrow.
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail=capped.user_message,
            headers={
                "x-correlation-id": correlation_id,
                "x-error-code": "TENANT_TURN_CAP_EXCEEDED",
                "Retry-After": str(capped.retry_after_seconds),
            },
        )
    except TenantTurnCapUnavailable as unavailable:
        # Fail closed, but do not claim the caller spent an allowance they did
        # not: telling somebody their day is gone when the ledger merely failed
        # to write is a false statement about their own account, and it sends
        # them away until midnight for a fault that may clear in seconds.
        logger.error(
            "Turn refused: the tenant turn cap could not be applied: %s",
            unavailable,
            extra={"correlation_id": correlation_id},
        )
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=(
                "Your usage allowance could not be checked just now. "
                "Please try again in a moment."
            ),
            headers={
                "x-correlation-id": correlation_id,
                "x-error-code": "TENANT_TURN_CAP_UNAVAILABLE",
                "Retry-After": "10",
            },
        )
    except NotFoundError as e:
        raise HTTPException(
            status_code=404,
            detail=str(e),
            headers={"x-correlation-id": correlation_id},
        )
    except PermissionDeniedException as e:
        raise HTTPException(
            status_code=403,
            detail=str(e),
            headers={"x-correlation-id": correlation_id},
        )
    except HTTPException:
        raise
    except ServiceException as e:
        logger.error(
            f"Turn processing failed: {e}",
            extra={"correlation_id": correlation_id},
        )
        # Classify off the typed provider metadata (LLMException.status_code /
        # retryable on the __cause__ chain), not the message string — see
        # llm_service_error_http_exception. Covers billing (→402), rate limits,
        # over-capacity, timeouts, provider 4xx/5xx, and schema-parse failures.
        raise llm_service_error_http_exception(e, correlation_id)
    except Exception as e:
        logger.error(
            f"Unexpected error processing turn: {e}",
            exc_info=True,
            extra={"correlation_id": correlation_id},
        )
        raise HTTPException(
            status_code=500,
            detail=f"An unexpected error occurred. Error ID: {correlation_id}",
            headers={
                "x-correlation-id": correlation_id,
                "x-error-code": "UNEXPECTED_ERROR",
                "Retry-After": "10",
            },
        )


# ============================================================
# Legacy Endpoints (DELETED - replaced by /turns)
# ============================================================

# NOTE: The following endpoints have been deleted as part of the
# Unified Ingestion Pipeline (v4.1):
# - POST /{case_id}/queries → use POST /{case_id}/turns
# - POST /{case_id}/data → use POST /{case_id}/turns


@router.post("/{case_id}/queries")
async def submit_case_query_gone(case_id: str):
    """DELETED: Use POST /{case_id}/turns instead."""
    raise HTTPException(
        status_code=410,
        detail="This endpoint has been removed. Use POST /cases/{case_id}/turns instead.",
    )


# Phase 1.5 — Evidence reclassification endpoint
# =============================================================================


@router.patch("/{case_id}/evidence/{evidence_id}/classification")
@trace("api_reclassify_evidence")
async def reclassify_evidence(
    case_id: str,
    evidence_id: str,
    body: Dict[str, Any] = Body(
        ...,
        description=(
            "Request body: {'data_type': '<DataType value>'}. The data_type "
            "must be one of the enum values in faultmaven.models.api.DataType "
            "(e.g. 'logs_and_errors', 'structured_config')."
        ),
    ),
    investigation_service=Depends(get_investigation_service),
    current_user: UserDTO = Depends(require_authentication),
):
    """Reclassify an existing evidence row under a user-specified data type.

    Phase 1.5 — the escape hatch for "the classifier was confidently
    wrong". Re-runs the preprocessing pipeline on the stored raw file
    with ``user_override=data_type``, overwrites the evidence's
    structural index, and appends to its extractor.attempts history.

    Gated by ``FAULTMAVEN_RECLASSIFY_ENABLED``. Returns 404 when the
    flag is off so the endpoint is invisible in production by default.

    Error responses (dispatched by ``api/exception_handlers.py``):

    - ``404`` — feature disabled, case not found, or evidence not in case
      (``NotFoundError``).
    - ``409`` — evidence has no backing file (``ConflictError`` with
      ``conflict_reason="no_backing_file"``).
    - ``403`` — caller does not own the case (``AuthorizationError``).
    - ``422`` — invalid or missing ``data_type``, OR the case is terminal
      (both ``ValidationException``). A closed or resolved investigation
      accepts questions, not mutation; the terminal refusal is raised after
      the evidence lookup, so a missing evidence id is still a ``404``.
    - ``500`` — storage/preprocessing failure (``ServiceException``).
    """
    from faultmaven.config.settings import get_settings

    settings = get_settings()
    if not settings.preprocessing.reclassify_enabled:
        raise NotFoundError(message="Reclassification endpoint is not enabled")

    data_type_raw = body.get("data_type") if isinstance(body, dict) else None
    if not data_type_raw or not isinstance(data_type_raw, str):
        raise ValidationException("Request body must include 'data_type' (string)")

    try:
        data_type = DataType(data_type_raw)
    except ValueError:
        valid = ", ".join(t.value for t in DataType)
        raise ValidationException(
            f"Unknown data_type '{data_type_raw}'. Valid: {valid}"
        )

    updated_evidence = await investigation_service.reclassify_evidence(
        case_id=case_id,
        evidence_id=evidence_id,
        user_id=current_user.user_id,
        data_type=data_type,
        trigger="api",
    )

    return {
        "evidence_id": updated_evidence.evidence_id,
        "source_type": (
            updated_evidence.source_type.value
            if hasattr(updated_evidence.source_type, "value")
            else str(updated_evidence.source_type)
        ),
        "summary": updated_evidence.summary,
        "metadata": updated_evidence.metadata,
    }


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
    """Remove data file from a case. Returns 204 No Content on success."""
    case_service = check_case_service_available(case_service)

    try:
        # Verify case exists
        case = await case_service.get_case(case_id, current_user.user_id)
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


# =============================================================================
# Document Generation and Closure Endpoints
# =============================================================================


def _recommendation_unavailable_detail(unreadable: bool) -> str:
    """The 503 body for a runbook-recommendation refusal, chosen by cause.

    The remediation differs, so the message must too. Almost everything that
    reaches the handler is a transient unavailability that retrying clears;
    ``RUNBOOK_RESULTS_UNREADABLE`` is deterministic — the knowledge base is up
    and answering, and the offending rows fail identically on every attempt
    until someone re-indexes them. Telling that operator to "retry once the
    knowledge base is available" points them at a subsystem that is not broken
    and at an action that cannot work (#912).

    A module-level function rather than an inline branch so the wording is
    reachable from a test: the branch it replaced was the only part of that
    fix nothing exercised.

    Takes a **bool**, not the exception. The caller classifies and passes the
    verdict, so no exception object ever reaches the response-building path —
    which is the invariant ``test_no_500_site_interpolates_the_exception``
    exists to keep. Handing the exception to a helper looks harmless and is not:
    the guard cannot see inside the call, so it must assume the text travels,
    and one edit to this function would make that assumption true.
    """
    if unreadable:
        # "The closest", not "only". The refusal fires whenever an unreadable
        # runbook outranks every readable one, which includes mixed result sets
        # where other runbooks were read perfectly well — so "found only
        # runbooks it could not read" would be false in exactly the case the
        # guard was widened to cover.
        return (
            "The closest-matching runbooks could not be read, so duplicate "
            "runbooks cannot be ruled out. The knowledge base is available; "
            "the affected runbooks need re-indexing. Retrying will not clear "
            "this."
        )
    return (
        "Runbook similarity search is unavailable, so duplicate runbooks "
        "cannot be ruled out. Retry once the knowledge base is available."
    )


@router.get(
    "/{case_id}/report-recommendations", dependencies=[Depends(require_authentication)]
)
@trace("api_get_report_recommendations")
async def get_report_recommendations(
    case_id: str,
    request: Request,
    case_service: Optional[ICaseService] = Depends(_di_get_case_service_dependency),
    current_user: UserDTO = Depends(require_authentication),
    runbook_kb=Depends(_di_get_runbook_kb_dependency),
):
    """
    Get intelligent report recommendations for a resolved case.

    Returns recommendations for which reports to generate, including runbook
    suggestions based on a similarity search of the published runbooks the
    REQUESTER can read (global ∪ their own ∪ shared to their teams). The
    requester is the principal who will act on the answer, so their scope
    governs here; the engine's terminal-turn dedup scopes on the case owner
    (fm#1030).

    Recommendation Logic:
    - Always available: the case's terminal summary
    - Runbook: a ≥70% best-chunk match is surfaced by title and score for the
      user to judge; below that, generation is recommended. There is no
      auto-"reuse" verdict.

    Args:
        case_id: Case identifier
        case_service: Injected case service
        current_user: Authenticated user

    Returns:
        ReportRecommendation with available types and runbook suggestion

    Raises:
        400: Case not in resolved state
        404: Case not found or access denied
        500: Internal server error
    """
    from faultmaven.modules.case.contracts import ReportRecommendation
    from faultmaven.modules.report.domain.services.report_recommendation_service import (
        ReportRecommendationService,
    )

    case_service = check_case_service_available(case_service)

    try:
        # Verify case exists and user has access
        case = await case_service.get_case(case_id, current_user.user_id)
        if not case:
            raise HTTPException(
                status_code=404, detail="Case not found or access denied"
            )

        # Validate case is in resolved state (terminal state with solution)
        # Note: Only RESOLVED is valid - CLOSED is terminal without solution
        if case.state != CaseState.RESOLVED:
            raise HTTPException(
                status_code=400,
                detail={
                    "error": "invalid_case_state",
                    "message": f"Cannot get report recommendations for case in {case.state.value} state. Case must be RESOLVED.",
                    "current_state": case.state.value,
                    "required_state": CaseState.RESOLVED.value,
                },
            )

        if runbook_kb is None:
            raise HTTPException(
                status_code=503,
                detail="Vector store not available. Check ChromaDB configuration.",
            )
        # Requester-scope dependencies: None in standalone (no teams exist —
        # the team arm is empty by construction, not a narrowed search).
        recommendation_service = ReportRecommendationService(
            runbook_kb=runbook_kb,
            team_service=getattr(request.app.state, "team_service", None),
            share_repository=getattr(request.app.state, "share_repository", None),
        )

        # Get intelligent recommendations, scoped to the requester.
        #
        # The tenant term here is the ENTERPRISE the request is bound to, not the
        # caller's organization claim. The team arm matches the share row's own
        # ``enterprise_id`` (ADR-017 D1/D4), so feeding it an organization made
        # the arm empty in every configuration — absent in standalone and for a
        # cloud account in no organization, and a billing id where present — and
        # a runbook shared to a common team stopped counting as a duplicate.
        recommendations = await recommendation_service.get_available_report_types(
            case=case,
            requester_user_id=current_user.user_id,
            requester_enterprise_id=get_current_enterprise_id(),
        )

        logger.info(
            f"Report recommendations generated for case {case_id}",
            extra={
                "case_id": case_id,
                "runbook_action": recommendations.runbook_recommendation.action,
                "available_types": [
                    t.value for t in recommendations.available_for_generation
                ],
            },
        )

        # Return recommendations
        return recommendations.model_dump()

    except HTTPException:
        raise
    except (KnowledgeBaseError, TimeoutError, CircuitBreakerError) as e:
        # Refuse rather than recommend. The runbook recommendation IS a claim
        # about what the knowledge base holds, so when the similarity search
        # cannot run there is no honest recommendation to give — answering
        # anyway is what produced a permanent "generate" and let duplicate
        # runbooks accumulate (#944). 503 matches the sibling refusal above
        # for a missing vector store, and keeps the response contract intact.
        #
        # TimeoutError and CircuitBreakerError are caught alongside the typed
        # error because call_external raises those two unwrapped — they are
        # the commonest unavailability modes, and routing them to the generic
        # handler below would answer a generic 500 instead. (Its body is static
        # — no text leaks — but the refusal is erased: the caller loses both the
        # 503 and the remediation this handler chooses below.)
        logger.warning(
            f"Report recommendations unavailable for case {case_id}: {e}",
            extra={"case_id": case_id},
            exc_info=True,
        )
        # The comparison, not the exception, is what crosses into the response.
        # ``ast.Compare`` yields a bool, so no exception text can travel through
        # it — the same shape the leak guard recognises elsewhere in the app.
        results_unreadable = getattr(e, "error_code", None) == RESULTS_UNREADABLE_CODE
        raise HTTPException(
            status_code=503,
            detail=_recommendation_unavailable_detail(results_unreadable),
        )
    except Exception as e:
        logger.error(
            f"Failed to get report recommendations for case {case_id}: {e}",
            exc_info=True,
        )
        raise HTTPException(
            status_code=500, detail="Failed to get report recommendations"
        )


@router.post("/{case_id}/reports", dependencies=[Depends(require_authentication)])
@trace("api_generate_case_reports")
async def generate_case_reports(
    case_id: str,
    fastapi_request: Request,
    request_body: Dict[str, Any] = Body(...),
    case_service: Optional[ICaseService] = Depends(_di_get_case_service_dependency),
    current_user: UserDTO = Depends(require_authentication),
):
    """Regenerate reports for a terminal case.

    Reports are auto-generated when a case reaches terminal state.
    This endpoint allows regeneration if the original was missing or needs refresh.
    """
    from faultmaven.modules.case.contracts import (
        ReportGenerationRequest,
        ReportType,
        is_default_case_title,
    )

    case_service = check_case_service_available(case_service)

    try:
        # ``owner_only``: regeneration flips ``is_current`` across the case's
        # reports, so it is a WRITE on rows a read share never covered (ADR-017
        # D4). Inside one enterprise nothing else separates a teammate from the
        # owner, so this flag is the whole of the boundary here.
        case = await case_service.get_case(
            case_id, current_user.user_id, owner_only=True
        )
        if not case:
            raise HTTPException(status_code=404, detail="Case not found")

        # Only terminal cases can have reports
        if not case.is_terminal:
            raise HTTPException(
                status_code=400,
                detail="Reports can only be generated for resolved or closed cases",
            )

        # Get the report service from app.state (Composition Root)
        report_service = getattr(
            fastapi_request.app.state, "report_generation_service", None
        )
        if not report_service:
            raise HTTPException(
                status_code=503,
                detail="Report generation service not available",
            )

        request = ReportGenerationRequest(
            report_types=[ReportType(t) for t in request_body["report_types"]]
        )
        response = await report_service.generate_reports(case, request.report_types)
        return response.model_dump()

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Report generation failed: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail="Failed to generate reports")


@router.get("/{case_id}/reports", dependencies=[Depends(require_authentication)])
@trace("api_get_case_reports")
async def get_case_reports(
    request: Request,
    case_id: str,
    include_history: bool = Query(default=False),
    report_type: Optional[str] = Query(default=None),
    case_service: Optional[ICaseService] = Depends(_di_get_case_service_dependency),
    case_repository: Optional[CaseRepository] = Depends(get_case_repository),
    current_user: UserDTO = Depends(require_authentication),
):
    """
    Retrieve generated reports for a case.

    Composite response: includes both the SQL-persisted ``reports`` rows
    (resolution_summary / closure_summary) and a projected view of any
    conversion drafts linked to this case (returned as synthetic
    ``report_type=runbook`` entries). The case Report tab uses the
    runbook entries to drive the KB-link banner; the underlying runbook
    content lives in ``conversion_drafts`` (the KB Drafts editor), not
    ``reports``.

    Args:
        case_id: Case identifier
        include_history: If True, return all report versions; if False, only current
        report_type: Optional filter by report type (resolution_summary, closure_summary, runbook)

    Returns:
        List of CaseReport objects (summaries plus any case-linked runbook drafts)
    """
    case_service = check_case_service_available(case_service)

    try:
        # Verify case exists and user has access
        case = await case_service.get_case(case_id, current_user.user_id)
        if not case:
            raise HTTPException(status_code=404, detail="Case not found")

        # Check if case_repository is available
        if not case_repository:
            logger.warning("Case repository not available - returning empty list")
            return []

        # Retrieve reports from storage via CaseRepository (TD-001)
        from faultmaven.modules.case.contracts import ReportType

        filter_type = ReportType(report_type) if report_type else None
        reports = await case_repository.get_reports(
            case_id=case_id, include_history=include_history, report_type=filter_type
        )

        # Project case-linked conversion drafts into the same shape so the
        # Report tab's runbook banner has something to count. Skipped when
        # the conversion service isn't wired (e.g., minimal deployments) or
        # the caller filtered to summary-only types.
        if filter_type is None or filter_type == ReportType.RUNBOOK:
            conversion_service = getattr(request.app.state, "conversion_service", None)
            if conversion_service is not None:
                draft_rows = await conversion_service.list_drafts_for_case(case_id)
                reports = list(reports) + [
                    _draft_to_runbook_report(case_id, d) for d in draft_rows
                ]

        logger.info(
            f"Retrieved {len(reports)} reports for case",
            extra={
                "case_id": case_id,
                "include_history": include_history,
                "report_count": len(reports),
            },
        )

        return reports

    except HTTPException:
        raise
    except Exception as e:
        logger.error(
            f"Failed to retrieve reports for case {case_id}: {e}", exc_info=True
        )
        raise HTTPException(status_code=500, detail="Failed to retrieve reports")


def _draft_to_runbook_report(case_id: str, draft: dict) -> dict:
    """Project a conversion-draft row into the CaseReport shape used by the
    Report tab. Returns a dict (not a CaseReport instance) because some draft
    titles violate the model's min_length=10 constraint — the projection is a
    presentation-layer concern, not a domain artifact.

    Only the fields the frontend reads are populated; everything else is left
    out so this stays an unambiguous projection rather than a fake report row.
    """
    return {
        "report_id": draft["draft_id"],
        "case_id": case_id,
        "report_type": "runbook",
        "title": draft["title"],
        "content": "",
        "format": "markdown",
        "generation_status": (
            "completed" if draft.get("knowledge_item_id") else "draft"
        ),
        "generated_at": draft.get("created_at"),
        "is_current": True,
        "version": 1,
        # Pass-through fields used by the deep-link banner.
        "draft_id": draft["draft_id"],
        "conversion_id": draft["conversion_id"],
        "runbook_id": draft["runbook_id"],
        "scope": draft.get("scope"),
        "knowledge_item_id": draft.get("knowledge_item_id"),
    }


@router.get(
    "/{case_id}/reports/{report_id}/download",
    dependencies=[Depends(require_authentication)],
)
@trace("api_download_case_report")
async def download_case_report(
    case_id: str,
    report_id: str,
    format: str = Query(default="markdown"),
    case_service: Optional[ICaseService] = Depends(_di_get_case_service_dependency),
    case_repository: Optional[CaseRepository] = Depends(get_case_repository),
    current_user: UserDTO = Depends(require_authentication),
):
    """
    Download case report in specified format.

    Args:
        case_id: Case identifier
        report_id: Report identifier
        format: Output format (markdown or pdf) - currently only markdown supported

    Returns:
        File response with report content
    """
    from fastapi.responses import Response

    case_service = check_case_service_available(case_service)

    try:
        # Verify case exists and user has access
        case = await case_service.get_case(case_id, current_user.user_id)
        if not case:
            raise HTTPException(status_code=404, detail="Case not found")

        # Check if case_repository is available
        if not case_repository:
            raise HTTPException(
                status_code=503,
                detail="Report storage not available (case repository not initialized)",
            )

        # Retrieve report from storage via CaseRepository (TD-001)
        report = await case_repository.get_report(report_id)

        if not report:
            raise HTTPException(status_code=404, detail="Report not found")

        # Verify report belongs to this case
        if report.case_id != case_id:
            raise HTTPException(
                status_code=403, detail="Report does not belong to this case"
            )

        # Determine content type and filename
        if format == "pdf":
            # TODO: PDF conversion not implemented yet
            raise HTTPException(
                status_code=501,
                detail="PDF format not yet supported - use markdown format",
            )
        else:
            # Return markdown format
            content_type = "text/markdown"
            filename = f"{report.report_type.value}_{case_id}_{report.version}.md"

        logger.info(
            f"Serving report download: {filename}",
            extra={
                "case_id": case_id,
                "report_id": report_id,
                "download_format": format,
            },
        )

        return Response(
            content=report.content,
            media_type=content_type,
            headers={"Content-Disposition": f"attachment; filename={filename}"},
        )

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Failed to download report {report_id}: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail="Failed to download report")


# ============================================================
# V2.0 Milestone-Based Investigation Endpoints
# ============================================================


@router.post("/{case_id}/close", dependencies=[Depends(require_authentication)])
@trace("api_close_case")
async def close_case(
    case_id: str,
    request_body: Optional[Dict[str, Any]] = Body(default=None),
    case_service: Optional[ICaseService] = Depends(_di_get_case_service_dependency),
    case_repository: Optional[CaseRepository] = Depends(get_case_repository),
    current_user: UserDTO = Depends(require_authentication),
):
    """
    Close case and archive with reports.

    Transitions the case to CLOSED through the engine's terminal executor
    (CaseService.close_case → execute_user_closure): closure_reason is
    engine-derived, closed_at is stamped, and an action-history entry is
    recorded — the same closure rule as the chat-confirmed flow (#915; the
    previous body set ``case.state`` directly, which the terminal-state
    validator rejects, and then called a service method that didn't exist).

    Then marks all latest reports as linked to the closure. Close-first
    ordering: a refused close (404 unknown/not-owner, 409 already
    terminal — mapped by the global exception handlers) must never mark
    reports as closure-linked.

    Returns:
        CaseClosureResponse with list of archived reports
    """
    from faultmaven.modules.case.contracts import CaseClosureResponse

    case_service = check_case_service_available(case_service)

    closed_case = await case_service.close_case(case_id, current_user.user_id)

    # Get current reports for closure (TD-001: via CaseRepository)
    archived_reports = []
    if case_repository:
        try:
            # Get only current reports (latest version of each type)
            latest_reports = await case_repository.get_reports(
                case_id=case_id,
                only_current=True,  # Only current reports (latest version per type)
            )

            if latest_reports:
                # Mark each report as linked to closure
                for report in latest_reports:
                    # Update report to mark as linked to closure
                    updated_report = report.model_copy(
                        update={"linked_to_closure": True}
                    )
                    await case_repository.update_report(updated_report)

                    # The linked CaseReport is the closure response payload
                    # (CaseClosureResponse.archived_reports is List[CaseReport]).
                    archived_reports.append(updated_report)

                logger.info(
                    f"Linked {len(latest_reports)} reports to case closure",
                    extra={"case_id": case_id, "report_count": len(latest_reports)},
                )
            else:
                logger.info(
                    f"No reports to link for case closure",
                    extra={"case_id": case_id},
                )

        except Exception as e:
            logger.warning(
                f"Failed to link reports to closure, case is already closed: {e}",
                extra={"case_id": case_id},
            )
            # The close itself committed; report linking is best-effort.

    logger.info(
        f"Case closed successfully",
        extra={"case_id": case_id, "archived_report_count": len(archived_reports)},
    )

    response = CaseClosureResponse(
        case_id=case_id,
        closed_at=to_json_compatible(closed_case.closed_at),
        archived_reports=archived_reports,
        download_available_until=(
            closed_case.closed_at + timedelta(days=90)
        ).isoformat()
        + "Z",
    )

    return response.model_dump()


# Case archive endpoints removed in storage redesign 2026-05.
# Postpone path: the columns and feature are gone from the schema until
# archive UX is wired up as a deliberate epic (with retention policy,
# scheduled archival, list-view filter UI, etc.). Reintroduce the routes
# at that point, not before.


# ============================================================
# Uploaded Files / Evidence Endpoints
# ============================================================


@router.get(
    "/{case_id}/uploaded-files",
    response_model=UploadedFilesList,
    operation_id="list_uploaded_files",
)
@trace("api_list_uploaded_files")
async def list_uploaded_files(
    case_id: str,
    response: Response,
    limit: int = Query(
        50, ge=1, le=100, description="Maximum number of files to return"
    ),
    offset: int = Query(
        0, ge=0, description="Number of files to skip (for pagination)"
    ),
    sort_by: str = Query(
        "uploaded_at_turn", description="Sort field: uploaded_at_turn | filename | size"
    ),
    sort_order: str = Query("desc", description="Sort direction: asc | desc"),
    case_service=Depends(get_case_service),
    current_user: UserDTO = Depends(require_authentication),
):
    """
    List uploaded files for a case with pagination.

    Returns:
        Paginated list of file metadata with AI analysis status
    """
    try:
        # Get case with access control
        case = await case_service.get_case(case_id, current_user.user_id)
        if not case:
            raise HTTPException(status_code=404, detail="Case not found")

        # Get uploaded files list (not evidence - files exist in ALL phases)
        uploaded_files_list = case.uploaded_files

        # Sort uploaded files
        reverse = sort_order == "desc"
        if sort_by == "uploaded_at_turn":
            uploaded_files_list = sorted(
                uploaded_files_list, key=lambda f: f.uploaded_at_turn, reverse=reverse
            )
        elif sort_by == "filename":
            uploaded_files_list = sorted(
                uploaded_files_list, key=lambda f: f.filename, reverse=reverse
            )
        elif sort_by == "size":
            uploaded_files_list = sorted(
                uploaded_files_list, key=lambda f: f.size_bytes, reverse=reverse
            )

        # Paginate
        total_count = len(uploaded_files_list)
        paginated_files = uploaded_files_list[offset : offset + limit]

        # Convert to response models
        # One `out_of_band_turns` for the whole page rather than per row: the
        # formula lives on the Case and bisects this list, and rebuilding it per
        # file is the only cost that scales with the page size.
        asides = case.out_of_band_turns
        files = [
            UploadedFileMetadata.from_uploaded_file(
                f,
                investigation_turn=case.investigation_turn_at(
                    f.uploaded_at_turn, asides=asides
                ),
            )
            for f in paginated_files
        ]

        # Set pagination header (required by API contract)
        response.headers["X-Total-Count"] = str(total_count)

        return UploadedFilesList(
            files=files, total_count=total_count, limit=limit, offset=offset
        )

    except HTTPException:
        raise
    except NotFoundError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except PermissionDeniedException as e:
        raise HTTPException(status_code=403, detail=str(e))
    except Exception as e:
        logger.error(f"Failed to list uploaded files: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail="Failed to list uploaded files")


# ============================================================
# Phase 2: Evidence-to-File Linkage APIs
# ============================================================


@router.get(
    "/{case_id}/uploaded-files/{file_id}",
    response_model=UploadedFileDetailsResponse,
    summary="Get uploaded file details with derived evidence",
    description="Retrieve detailed information about an uploaded file including all evidence derived from it and hypothesis linkage.",
    operation_id="get_uploaded_file_details",
)
async def get_uploaded_file_details(
    case_id: str = Path(..., description="Case ID"),
    file_id: str = Path(..., description="File ID"),
    current_user: UserDTO = Depends(require_authentication),
    case_service: Optional[ICaseService] = Depends(_di_get_case_service_dependency),
):
    """
    GET /api/v1/cases/{case_id}/uploaded-files/{file_id}

    Returns comprehensive file details including:
    - File metadata (name, size, upload time)
    - List of evidence derived from this file
    - Hypothesis linkage for each evidence piece
    """
    case_service = check_case_service_available(case_service)
    user_id = current_user.user_id

    try:
        # ICaseService.get_case applies ownership-based access control and
        # returns None for both "not found" and "not owned" — both surface as 404.
        case = await case_service.get_case(case_id, user_id)
        if not case:
            raise HTTPException(status_code=404, detail=f"Case {case_id} not found")

        # Find the uploaded file
        uploaded_file = next(
            (f for f in case.uploaded_files if f.file_id == file_id), None
        )
        if not uploaded_file:
            raise HTTPException(
                status_code=404, detail=f"File {file_id} not found in case {case_id}"
            )

        # Find all evidence derived from this file via the canonical FK
        # (Evidence.source_file_id → UploadedFile.file_id). The pre-redesign
        # `content_ref == content_ref` matching was a polymorphism workaround
        # eliminated by the schema redesign.
        derived_evidence = []
        first_summary: Optional[str] = None
        # Built once for the loop below, for the same reason as the file list.
        asides = case.out_of_band_turns
        for evidence in case.evidence:
            if evidence.source_file_id != uploaded_file.file_id:
                continue
            # Find hypotheses related to this evidence (junction list)
            related_hypothesis_ids = [
                hyp.hypothesis_id
                for hyp in case.hypotheses.values()
                if any(
                    link.evidence_id == evidence.evidence_id
                    for link in hyp.evidence_links
                )
            ]
            derived_evidence.append(
                DerivedEvidenceSummary(
                    evidence_id=evidence.evidence_id,
                    summary=evidence.summary,
                    category=_safe_enum_value(evidence.category),
                    collected_at_turn=evidence.collected_at_turn,
                    investigation_turn=case.investigation_turn_at(
                        evidence.collected_at_turn, asides=asides
                    ),
                    source_type=_safe_enum_value(evidence.source_type),
                    primary_purpose=evidence.primary_purpose,
                    related_hypothesis_ids=related_hypothesis_ids,
                )
            )
            if first_summary is None:
                first_summary = evidence.summary

        # Format file size for display
        size_bytes = uploaded_file.size_bytes
        if size_bytes < 1024:
            size_display = f"{size_bytes} B"
        elif size_bytes < 1024 * 1024:
            size_display = f"{size_bytes / 1024:.1f} KB"
        else:
            size_display = f"{size_bytes / (1024 * 1024):.1f} MB"

        return UploadedFileDetailsResponse(
            file_id=uploaded_file.file_id,
            filename=uploaded_file.filename,
            size_bytes=uploaded_file.size_bytes,
            size_display=size_display,
            content_type=uploaded_file.content_type,
            content_hash=uploaded_file.content_hash,
            uploaded_at_turn=uploaded_file.uploaded_at_turn,
            investigation_turn=case.investigation_turn_at(
                uploaded_file.uploaded_at_turn, asides=asides
            ),
            uploaded_at=uploaded_file.uploaded_at,
            upload_source=uploaded_file.upload_source,
            summary=first_summary,
            derived_evidence=derived_evidence,
            evidence_count=len(derived_evidence),
        )

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Failed to get file details: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail="Failed to get file details")


def _build_evidence_response(
    case, evidence, case_id: str, asides: Optional[Sequence[int]] = None
) -> EvidenceDetailsResponse:
    """Build an EvidenceDetailsResponse from a domain Evidence + its parent Case.

    Resolves the source-file reference via the canonical FK and walks the
    hypothesis-evidence junction once to collect every related hypothesis.
    Shared by ``list_case_evidence`` and ``get_evidence_details`` so both
    endpoints produce identical row shapes.
    """
    matched_file = case.find_uploaded_file(evidence.source_file_id)
    source_file = (
        SourceFileReference(
            file_id=matched_file.file_id,
            filename=matched_file.filename,
            uploaded_at_turn=matched_file.uploaded_at_turn,
            investigation_turn=case.investigation_turn_at(
                matched_file.uploaded_at_turn, asides=asides
            ),
        )
        if matched_file
        else None
    )

    related_hypotheses = []
    for hypothesis in case.hypotheses.values():
        for link in hypothesis.evidence_links:
            if link.evidence_id != evidence.evidence_id:
                continue
            related_hypotheses.append(
                RelatedHypothesis(
                    hypothesis_id=hypothesis.hypothesis_id,
                    statement=hypothesis.statement,
                    stance=(
                        link.stance.value
                        if hasattr(link.stance, "value")
                        else str(link.stance)
                    ),
                )
            )

    return EvidenceDetailsResponse(
        evidence_id=evidence.evidence_id,
        case_id=case_id,
        summary=evidence.summary,
        category=_safe_enum_value(evidence.category),
        primary_purpose=evidence.primary_purpose,
        collected_at_turn=evidence.collected_at_turn,
        investigation_turn=case.investigation_turn_at(
            evidence.collected_at_turn, asides=asides
        ),
        collected_at=evidence.collected_at,
        collected_by=evidence.collected_by,
        source_file=source_file,
        related_hypotheses=related_hypotheses,
        extract=evidence.extract,
        analysis=evidence.analysis,
    )


@router.get(
    "/{case_id}/evidence",
    response_model=CaseEvidenceListResponse,
    summary="List all evidence for a case",
    description="Retrieve all evidence records for a case, each with source-file reference and hypothesis linkage.",
    operation_id="list_case_evidence",
)
async def list_case_evidence(
    case_id: str = Path(..., description="Case ID"),
    current_user: UserDTO = Depends(require_authentication),
    case_service: Optional[ICaseService] = Depends(_di_get_case_service_dependency),
):
    """
    GET /api/v1/cases/{case_id}/evidence

    Returns the full evidence list for a case. Each item carries the
    same shape as the single-evidence endpoint so the UI can render a
    list view and a detail panel from one payload.
    """
    case_service = check_case_service_available(case_service)
    user_id = current_user.user_id

    try:
        case = await case_service.get_case(case_id, user_id)
        if not case:
            raise HTTPException(status_code=404, detail=f"Case {case_id} not found")

        # Built ONCE for the whole list. This endpoint does not paginate, so a
        # case with many evidence rows would otherwise walk and sort the turn
        # history twice per row — the cost the two paginated endpoints already
        # hoist out, skipped on the one where it actually scales.
        asides = case.out_of_band_turns
        evidence_items = [
            _build_evidence_response(case, evidence, case_id, asides=asides)
            for evidence in case.evidence
        ]

        return CaseEvidenceListResponse(
            case_id=case_id,
            total_count=len(evidence_items),
            evidence=evidence_items,
        )

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Failed to list evidence: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail="Failed to list evidence")


@router.get(
    "/{case_id}/evidence/{evidence_id}",
    response_model=EvidenceDetailsResponse,
    summary="Get evidence details with source file",
    description="Retrieve detailed evidence information including source file reference and hypothesis linkage.",
)
async def get_evidence_details(
    case_id: str = Path(..., description="Case ID"),
    evidence_id: str = Path(..., description="Evidence ID"),
    current_user: UserDTO = Depends(require_authentication),
    case_service: Optional[ICaseService] = Depends(_di_get_case_service_dependency),
):
    """
    GET /api/v1/cases/{case_id}/evidence/{evidence_id}

    Returns comprehensive evidence details including:
    - Evidence metadata and content
    - Source file reference (if derived from upload)
    - Related hypotheses with stance (SUPPORTS/REFUTES/NEUTRAL)
    """
    case_service = check_case_service_available(case_service)
    user_id = current_user.user_id

    try:
        case = await case_service.get_case(case_id, user_id)
        if not case:
            raise HTTPException(status_code=404, detail=f"Case {case_id} not found")

        evidence = next(
            (e for e in case.evidence if e.evidence_id == evidence_id), None
        )
        if not evidence:
            raise HTTPException(
                status_code=404,
                detail=f"Evidence {evidence_id} not found in case {case_id}",
            )

        return _build_evidence_response(case, evidence, case_id)

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Failed to get evidence details: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail="Failed to get evidence details")


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


# ============================================================
# Knowledge Extraction Endpoint
# ============================================================


@router.post(
    "/{case_id}/extract-knowledge",
    response_model=Dict[str, Any],
    summary="Extract Knowledge from Case",
    description="Extract reusable knowledge from a case into a suggestion for the knowledge base.",
    status_code=status.HTTP_201_CREATED,
)
@trace("api_extract_knowledge")
async def extract_knowledge_from_case(
    case_id: str = Path(..., description="Case ID to extract knowledge from"),
    request_body: Optional[Dict[str, Any]] = Body(default=None),
    case_service: ICaseService = Depends(get_case_service),
    suggestion_service=Depends(get_suggestion_service),
    current_user: UserDTO = Depends(require_authentication),
) -> Dict[str, Any]:
    """
    Extract knowledge from a case conversation into a suggestion.

    This endpoint uses LLM to analyze the case's messages and evidence,
    then generates a reusable knowledge article (runbook, troubleshooting guide).

    The suggestion is automatically scanned for PII and placed in a
    "pending_review" state for admin approval in the Dashboard Review Inbox.

    Answers 503 when no suggestion service is composed for this process — the
    extraction cannot be stored, and a suggestion id that resolves to nothing is
    worse than a refusal.

    Args:
        case_id: Case to extract knowledge from
        request_body: Optional configuration:
            - include_messages: Include case conversation (default: true)
            - include_evidence: Include evidence summaries (default: true)
            - title_suggestion: Optional title for the suggestion

    Returns:
        KnowledgeExtractionResponse with suggestion details
    """
    from faultmaven.models.api import KnowledgeExtractionResponse
    from faultmaven.utils.serialization import to_json_compatible

    try:
        # Verify case exists and the caller OWNS it. Extraction mints a
        # knowledge suggestion out of the case's transcript and evidence and
        # attributes it to the extractor, so it is a write on the owner's
        # material: a read share does not authorise it (ADR-017 D4).
        case = await case_service.get_case(
            case_id, current_user.user_id, owner_only=True
        )
        if not case:
            raise HTTPException(status_code=404, detail="Case not found")

        # Parse request body
        include_messages = True
        include_evidence = True
        title_suggestion = None
        if request_body:
            include_messages = request_body.get("include_messages", True)
            include_evidence = request_body.get("include_evidence", True)
            title_suggestion = request_body.get("title_suggestion")

        # ``suggestion_service`` arrives via Depends(get_suggestion_service) —
        # the SAME shared dependency the knowledge-side review routes use, which
        # answers 503 when the composition root produced no singleton (#1214).
        #
        # It used to be looked up inline here, guarded by a
        # ``request: Request = None`` default FastAPI never actually passes, and
        # falling back to a fresh ``SuggestionService()``: the suggestion went
        # into that throwaway instance's private dict, the instance died with
        # the request, and the approve that followed could never find it (404).
        # As a dependency the 503 policy lives in one place and the route is
        # overridable in tests like every sibling.

        # THE TENANT THIS SUGGESTION IS STORED UNDER (#1227).
        #
        # It was ``getattr(case, "organization_id", "default")``. Two things
        # were wrong with that, and the store change turns the second from
        # cosmetic into fatal:
        #
        # 1. The suggestion has to be stamped with the SAME tenant the review
        #    routes scope by, or the reviewer never sees it. Every suggestion
        #    route — list, get, update, approve, reject, remediate — resolves
        #    its predicate with ``require_actor_enterprise``, so that is the
        #    value the write side owes them. The case's own enterprise is the
        #    same value on the success path (the case was just fetched through
        #    the caller's own scoped read), which is exactly why reading it off
        #    the case was never the SOURCE of the answer.
        # 2. ``"default"`` is not a tenant id. It was a silent placeholder while
        #    the store was a dict keyed by nothing;
        #    ``knowledge_suggestions.enterprise_id`` is a NOT NULL FK to
        #    ``enterprises`` with ``PRAGMA foreign_keys=ON``, so the same
        #    fallback fails the INSERT outright — and under PostgreSQL RLS it
        #    would fail the policy's WITH CHECK as well, because the value would
        #    not match the session's ``app.current_enterprise_id``.
        #
        # ``require_actor_enterprise`` refuses with 403 rather than handing back
        # a value to degrade with, which is the right answer: a request that
        # owns no tenant has nowhere to put the extraction.
        enterprise_id = require_actor_enterprise(current_user)

        # Extract knowledge
        suggestion = await suggestion_service.extract_knowledge_from_case(
            case_id=case_id,
            enterprise_id=enterprise_id,
            extracted_by=current_user.user_id,
            include_messages=include_messages,
            include_evidence=include_evidence,
            title_suggestion=title_suggestion,
        )

        # Build response
        return {
            "suggestion_id": suggestion.suggestion_id,
            "case_id": case_id,
            "status": suggestion.status.value,
            "suggested_title": suggestion.suggested_title,
            "suggested_content": suggestion.suggested_content,
            "pii_scan_status": suggestion.pii_scan_status.value,
            # The runbook quality gate's verdict on what was just extracted
            # (#1226). This response is the FIRST surface a reviewer sees, and
            # it is the one the extraction flow actually lands on — omitting the
            # verdict here meant they learned the draft was unpublishable only
            # by pressing approve and reading a 422, which is the whole failure
            # this issue is about. ``passed: null`` means not yet evaluated;
            # never read it as "fine".
            "validation": {
                "passed": suggestion.validation_passed,
                "errors": list(suggestion.validation_errors),
                "warnings": list(suggestion.validation_warnings),
            },
            "extracted_from": {
                "case_title": suggestion.source_case_title,
                "message_count": suggestion.message_count,
                "evidence_count": suggestion.evidence_count,
            },
            "created_at": to_json_compatible(suggestion.created_at),
        }

    except HTTPException:
        raise
    except ServiceUnavailableException as unavailable:
        # This organization's review queue is at its ceiling, and the store
        # refuses to add to it rather than deleting a decided suggestion
        # (#1227). 503 rather than 500: nothing is broken, the queue is full,
        # and the fix is to review it.
        # The static constant, never str(unavailable): this module's AST guard
        # forbids carrying a caught exception into any 5xx body, and that rule
        # does not get an exemption for a message that happens to be ours today.
        # The exception's own text goes to the log instead.
        logger.warning(
            "Knowledge extraction refused for case %s: %s", case_id, unavailable
        )
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=SUGGESTION_QUEUE_FULL,
        ) from unavailable
    except Exception as e:
        logger.error(
            f"Knowledge extraction failed for case {case_id}: {e}", exc_info=True
        )
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Knowledge extraction failed",
        )


# ============================================================
# REMOVED ENDPOINTS: Download and Delete
# ============================================================
# Rationale: Each file upload is a conversational turn. Downloading files users
# already have is an anti-pattern, and deleting would break conversation history
# integrity (similar to deleting individual chat messages).
# Only "View Analysis" feature remains for transparency and troubleshooting.
#
# Removed endpoints (cleaned up 2025-01-XX):
# - GET /{case_id}/uploaded-files/{file_id}/download
# - DELETE /{case_id}/uploaded-files/{file_id}
# ============================================================
