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
import os
import re
import time
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Union

from fastapi import (
    APIRouter,
    BackgroundTasks,
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
    is_quota_exhausted_service_error,
    quota_exhausted_http_exception,
)
from faultmaven.api.v1.auth_dependencies import (
    get_current_user_id,
    get_current_user_optional,
    require_authentication,
)
from faultmaven.api.v1.dependencies import (
    get_case_repository,  # TD-001: use case_repository for reports
    get_case_service,
    get_case_vector_store,
    get_data_service,
    get_investigation_service,  # V2.0 milestone-based
    get_preprocessing_service,
    get_session_id,
    get_session_service,
)
from faultmaven.core.investigation.schemas import Attachment, TurnPayload
from faultmaven.exceptions import (
    AuthorizationError,
    FaultMavenException,
    NotFoundError,
    PermissionDeniedException,
    ServiceException,
    ValidationException,
)
from faultmaven.infrastructure.observability.tracing import trace

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
)
from faultmaven.models.case_ui import CaseUIResponse
from faultmaven.models.interfaces_case import ICaseService

# Cross-module imports via contracts (Principle 2: Vertical Modules with Contracts)
from faultmaven.modules.auth.contracts import ISessionService, UserDTO
from faultmaven.modules.case.domain.models import Case as CaseEntity
from faultmaven.modules.case.domain.models import CaseState
from faultmaven.modules.case.domain.services.case_converter import CaseConverter
from faultmaven.modules.case.domain.services.case_ui_adapter import (
    transform_case_for_ui,
)
from faultmaven.modules.case.exceptions import StaleCaseException
from faultmaven.modules.case.infrastructure.case_repository import CaseRepository
from faultmaven.utils.serialization import to_json_compatible

# Create router
router = APIRouter(prefix="/cases", tags=["cases"])

# Include Replay Router
from faultmaven.modules.case.api.replay import router as replay_router

router.include_router(replay_router)

# Set up logging
logger = logging.getLogger(__name__)


# Helper function to safely extract enum values
def _safe_enum_value(value):
    """Safely extract enum value, return string if already string."""
    if hasattr(value, "value"):
        return value.value
    return str(value)


def _resolve_agent_timeout(settings) -> tuple[float, str]:
    """Resolve the per-provider agent-level timeout for the active CHAT_PROVIDER.

    Mirrors the LLM-router's ``_resolve_timeout`` shape (ISS-054) but applies to
    the agent-level (turn-wide) ceiling enforced via ``asyncio.wait_for``.

    Returns a ``(timeout_seconds, provider_name_for_logging)`` tuple. The
    returned name is the resolved provider string (or ``"default"`` when the
    setting is missing entirely) so log lines can attribute timeouts.

    See ISS-058.
    """
    provider_name = getattr(settings.llm, "chat_provider", None) or os.getenv(
        "CHAT_PROVIDER"
    )
    if provider_name is not None and not isinstance(provider_name, str):
        provider_name = getattr(provider_name, "value", str(provider_name))
    timeout = float(settings.agent.timeout_for_provider(provider_name))
    return timeout, provider_name or "default"


# Configurable banned words list - minimal but extensible
BANNED_GENERIC_WORDS = [
    "new case",
    "untitled",
    "troubleshooting",
    "conversation",
    "discussion",
    "issue",
    "problem",
    "help",
    "assistance",
    "user query",
    "support request",
    "technical issue",
]

# =============================================================================
# Title Generation Constants
# =============================================================================

# Incomplete ending detection - words that indicate mid-sentence title cuts
# These words should NEVER be the last word in a title as they indicate truncation
INCOMPLETE_ENDINGS = {
    # Auxiliary verbs
    "have",
    "has",
    "is",
    "are",
    "was",
    "were",
    "been",
    # Modal verbs
    "will",
    "would",
    "should",
    "could",
    "can",
    "may",
    "might",
    # Articles
    "the",
    "a",
    "an",
    # Possessive adjectives
    "my",
    "our",
    "their",
    "your",
    "his",
    "her",
    "its",
    # Demonstratives
    "this",
    "that",
    "these",
    "those",
    # Personal pronouns (subject)
    "i",
    "you",
    "he",
    "she",
    "it",
    "we",
    "they",
    # Prepositions
    "with",
    "about",
    "from",
    "into",
    "to",
    "for",
    "of",
    "in",
    "on",
    "at",
    "after",
    "before",
    "during",
    "between",
    "through",
    "without",
    "within",
    "upon",
    "under",
    "over",
    "across",
    "against",
    # Conjunctions
    "and",
    "or",
    "but",
    "by",
    "so",
    "if",
    "when",
    "while",
}

# Conversational filler patterns (ordered longest-first for greedy matching)
# Only strip COMPLETE conversational phrases, not single words that might be part of content
CONVERSATIONAL_FILLER = [
    "i was wondering if you could help me with",
    "could you assist me with",
    "can you help me with",
    "i need help with",
    "i have a question about",
    "could you assist with",
    "i'm having trouble with",
    "i'm experiencing",
    "i am experiencing",
    "i am having",
    "i noticed",  # "I noticed our API..."
    "by the way,",  # "By the way, can..."
    "hello,",  # Only strip if followed by comma
    "hi,",  # Only strip if followed by comma
    "hey,",  # Only strip if followed by comma
]

# Title casing exceptions - keep these words lowercase in the middle of titles
TITLE_CASE_LOWERCASE_WORDS = {
    "a",
    "an",
    "the",
    "in",
    "on",
    "at",
    "to",
    "for",
    "of",
    "with",
}

# =============================================================================
# Title Generation Thresholds and Settings
# =============================================================================

# Turn threshold - minimum user conversation turns required for title generation
MIN_TURNS_FOR_TITLE_GENERATION = 5  # Require meaningful conversation depth

# Content length thresholds
MIN_CONTENT_LENGTH_FOR_TITLE = 200  # Minimum chars of user content after extraction
EXTRACTIVE_MAX_CONTENT_LENGTH = (
    300  # Use fast extractive for simple, short conversations
)

# Title validation constraints
MIN_TITLE_WORDS = 2  # Minimum words in valid title ("API Error" is valid)
MIN_TITLE_LENGTH = 5  # Minimum characters in valid title
MAX_TITLE_WORDS_DEFAULT = 8  # Default maximum words in generated title
MIN_EXTRACTIVE_WORDS = (
    3  # Extractive path requires more words than validation (more conservative)
)

# LLM generation settings (optimized for title quality)
LLM_TITLE_MAX_TOKENS = (
    128  # Prevent truncation (Gemini may emit reasoning tokens before the title)
)
LLM_TITLE_TEMPERATURE = 0.2  # More deterministic generation
LLM_TITLE_TOP_P = 0.9  # Focused sampling

# Context extraction settings
MAX_USER_MESSAGES_FOR_CONTEXT = 12  # Cap message count to reduce noise
MIN_MESSAGE_WORD_COUNT = 3  # Filter out very short messages like "ok", "thanks"
CONTEXT_MESSAGE_LIMIT = 10  # Number of recent messages to fetch for context

# =============================================================================
# Helper Functions for Title Generation
# =============================================================================


def is_title_valid(title: str, check_banned_words: bool = True) -> bool:
    """Validate generated title meets quality standards.

    Args:
        title: Generated title string
        check_banned_words: Whether to check against banned generic words

    Returns:
        True if title passes all validation gates
    """
    if not title:
        return False

    words = title.split()
    # Length/word-count guards (language-agnostic)
    # Reduced from 3 to 2 words - many valid titles are 2 words:
    # "Database Timeout", "API Slowness", "Memory Leak", "Redis Error"
    if len(words) < MIN_TITLE_WORDS or len(title.strip()) < MIN_TITLE_LENGTH:
        return False

    # Check for incomplete endings (titles ending mid-sentence)
    # These indicate truncated or low-quality titles
    last_word = words[-1].lower().strip(".,!?;:")
    if last_word in INCOMPLETE_ENDINGS:
        return False

    # Catch titles truncated mid-token (e.g., "Database I/" from "I/O")
    # A valid title should end with an alphanumeric character
    last_char = title.rstrip()[-1]
    if not last_char.isalnum() and last_char not in ")]}":
        return False

    # Optional banned words check (English-centric, configurable)
    if check_banned_words:
        title_lower = title.lower().strip()
        return not (
            title_lower in BANNED_GENERIC_WORDS
            or any(generic in title_lower for generic in BANNED_GENERIC_WORDS)
        )

    return True


def apply_title_case(title: str) -> str:
    """Apply title case formatting to generated title.

    Capitalizes first letter of each word except common articles/prepositions
    in the middle of the title.

    Args:
        title: Raw title string

    Returns:
        Title-cased string (e.g., "Database Connection Timeout")
    """
    words = title.split()
    title_cased = []
    for i, word in enumerate(words):
        # Always capitalize first word, otherwise check exceptions
        if i == 0 or word.lower() not in TITLE_CASE_LOWERCASE_WORDS:
            title_cased.append(word.capitalize())
        else:
            title_cased.append(word.lower())
    return " ".join(title_cased)


def get_extractive_fallback_title(
    user_signals: Optional[str],
    context_text: str,
    case,
    max_words: int = MAX_TITLE_WORDS_DEFAULT,
) -> Optional[str]:
    """Generate fallback title using extractive logic.

    Tries multiple sources in order of reliability:
    1. Pre-extracted user signals
    2. Re-extract from context
    3. Case description

    Args:
        user_signals: Pre-extracted user content
        context_text: Full conversation context
        case: Case object
        max_words: Maximum words in title

    Returns:
        Extracted title or None if insufficient content
    """

    def _clean_fallback_candidate(text: str) -> Optional[str]:
        """Clean and validate a fallback title candidate."""
        words = text.strip().split()[:max_words]
        candidate = " ".join(words)
        # Strip trailing punctuation (consistent with smart extractive path)
        candidate = candidate.strip(".,!?;:")
        # Strip punctuation from last word before checking incomplete endings
        if words:
            last_word = words[-1].lower().strip(".,!?;:")
            if last_word in INCOMPLETE_ENDINGS:
                return None
        if is_title_valid(candidate, check_banned_words=False):
            return apply_title_case(candidate)
        return None

    # First try the pre-extracted user signals (most reliable)
    if user_signals and user_signals.strip():
        result = _clean_fallback_candidate(user_signals)
        if result:
            return result

    # Fallback to re-extracting from context if user_signals not provided
    extracted_signals = _extract_user_signals_from_context(context_text)
    if extracted_signals:
        result = _clean_fallback_candidate(extracted_signals)
        if result:
            return result

    # Final fallback: try case description if available and meaningful
    if (
        hasattr(case, "description")
        and case.description
        and case.description.strip()
        and case.description != "No description"
    ):
        result = _clean_fallback_candidate(case.description)
        if result:
            return result

    # Skip case title fallback entirely - it's likely to be generic
    # If no meaningful content found, this should trigger 422 instead
    return None


async def _di_get_case_service_dependency(request: Request) -> Optional[ICaseService]:
    """Runtime wrapper so patched dependency is honored in tests."""
    # Import inside to resolve the patched function at call time
    from faultmaven.api.v1.dependencies import get_case_service as _getter

    return await _getter(request)


# Legacy dependency functions removed - using new auth_dependencies directly


async def _di_get_session_id_dependency(request: Request) -> Optional[str]:
    """Runtime wrapper so patched dependency is honored in tests."""
    from faultmaven.api.v1.dependencies import get_session_id as _get_session_id

    return await _get_session_id(request)


async def _di_get_session_service_dependency(request: Request) -> ISessionService:
    """Runtime wrapper so patched dependency is honored in tests."""
    from faultmaven.api.v1.dependencies import get_session_service as _getter

    return await _getter(request)


async def _di_get_vector_store_dependency(request: Request):
    """Get the DI-provided ChromaDB vector store for report services."""
    try:
        container = request.app.extra.get("di_container")
        if container:
            return getattr(container, "vector_store", None)
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
    204 No Content even if the case has already been deleted.

    Returns 204 No Content on success.
    """
    case_service = check_case_service_available(case_service)
    correlation_id = str(uuid.uuid4())

    try:
        # Proceed to hard delete via service if supported; otherwise emulate success
        # DELETE is idempotent - always returns 204 No Content regardless of whether case existed
        await case_service.hard_delete_case(case_id, current_user.user_id)
        # Service layer handles the deletion and cascade behavior
        # Idempotent: No error even if case doesn't exist

        # Success response with correlation header (always 204 for idempotent behavior)
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


@router.post("", response_model=CaseSummary, status_code=status.HTTP_201_CREATED)
@trace("api_create_case")
async def create_case(
    request: CaseCreateRequest,
    response: Response,
    case_service: Optional[ICaseService] = Depends(_di_get_case_service_dependency),
    session_service: ISessionService = Depends(_di_get_session_service_dependency),
    current_user: UserDTO = Depends(require_authentication),
) -> CaseSummary:
    """
    Create a new troubleshooting case (v2.0 milestone-based)

    Creates a new case with milestone-based investigation tracking.
    Initial status is INQUIRY (problem definition phase).

    Returns CaseSummary with basic case info and milestone progress.
    """
    correlation_id = str(uuid.uuid4())
    print(
        f"DEBUG: create_case entered! Request title: {request.title}, User: {current_user.user_id if current_user else 'None'}"
    )
    case_service = check_case_service_available(case_service)

    try:
        # Validate session if provided (restored from old implementation)
        if request.session_id:
            session = await session_service.get_session(
                request.session_id, validate=True
            )
            if not session:
                logger.warning(
                    f"Invalid or expired session: {request.session_id}",
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

        # Create case using new model
        case_entity = await case_service.create_case(
            title=request.title,  # Pass None to trigger auto-generation in service
            description=request.description,
            owner_id=current_user.user_id,
            session_id=request.session_id,
            initial_message=request.initial_message,  # Restored from old implementation
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
        )
        error_response = ErrorResponse(
            schema_version="3.1.0",
            error=ErrorDetail(code="CASE_SERVICE_ERROR", message=str(e)),
        )
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=error_response.model_dump(),
            headers={"x-correlation-id": correlation_id},
        )


@router.get("", response_model=CaseListResponse)
@trace("api_list_cases")
async def list_cases(
    response: Response,
    case_service: Optional[ICaseService] = Depends(_di_get_case_service_dependency),
    current_user: UserDTO = Depends(require_authentication),
    state: Optional[CaseState] = Query(None, description="Filter by state"),
    limit: int = Query(50, ge=1, le=100, description="Items per page"),
    offset: int = Query(0, ge=0, description="Number of items to skip"),
    # Changed default to True - new cases should be visible immediately
    include_empty: bool = Query(
        True, description="Include cases with current_turn == 0 (newly created)"
    ),
    include_archived: bool = Query(False, description="Include archived/closed cases"),
):
    """
    List user's cases with pagination (v2.0 milestone-based)

    Returns CaseListResponse with:
    - List of CaseSummary objects (with milestone progress)
    - Total count for pagination
    - has_more flag

    Default Filtering Behavior:
    - INCLUDES empty cases (current_turn == 0) - newly created cases are visible
    - INCLUDES closed/resolved cases (frontend categorizes by status)
    - Use include_empty=false to hide cases with no conversation yet
    - Use status filter to further refine results
    """
    case_service = check_case_service_available(case_service)
    correlation_id = str(uuid.uuid4())
    response.headers["x-correlation-id"] = correlation_id

    # Prevent browser/extension caching to ensure title updates are visible immediately
    response.headers["Cache-Control"] = "no-cache, no-store, must-revalidate"
    response.headers["Pragma"] = "no-cache"
    response.headers["Expires"] = "0"

    try:
        # Build filter with restored filtering parameters
        filters = CaseListFilter(
            user_id=current_user.user_id,
            state=state,
            limit=limit,
            offset=offset,
            include_empty=include_empty,
            include_archived=include_archived,
        )

        # Get case summaries (already converted by service)
        case_summaries = await case_service.list_user_cases(
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

        # Build response
        total_count = len(case_summaries)  # TODO: Get actual total from repository
        list_response = CaseListResponse(
            cases=case_summaries,
            total_count=total_count,
            limit=limit,
            offset=offset,
            has_more=len(case_summaries) == limit,
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
        )
        error_response = ErrorResponse(
            schema_version="3.1.0",
            error=ErrorDetail(code="CASE_SERVICE_ERROR", message=str(e)),
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
        return {
            "service": "case_management",
            "status": "unhealthy",
            "timestamp": to_json_compatible(datetime.now(timezone.utc)),
            "error": str(e),
        }


@router.get("/{case_id}", response_model=CaseDetail)
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
        return CaseDetail.from_case(case)

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


@router.get("/{case_id}/ui", response_model=CaseUIResponse)
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


@router.put("/{case_id}", status_code=status.HTTP_200_OK)
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
        )
        error_response = ErrorResponse(
            schema_version="3.1.0",
            error=ErrorDetail(
                code="UPDATE_CASE_ERROR", message=f"Failed to update case: {str(e)}"
            ),
        )
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=error_response.model_dump(),
            headers={"x-correlation-id": correlation_id},
        )


@router.post("/{case_id}/title", response_model=TitleResponse)
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

        # Terminal cases already have a final title — skip regeneration
        if case.is_terminal:
            return TitleResponse(title=case.title)

        # Idempotency check removed - allow free regeneration
        # Rationale:
        # 1. Hybrid approach makes regeneration nearly free (90% extractive, 1ms, $0)
        # 2. Turn threshold (5+ turns) already prevents abuse
        # 3. Duplicate request middleware provides rate limiting
        # 4. Better UX - users can regenerate as conversation evolves
        # 5. Titles improve as more context is revealed
        #
        # Previous: Blocked regeneration if title was "meaningful"
        # Now: Always regenerate (respects turn threshold + duplicate protection)

        # Get messages to check turn count
        # Use repository directly (same as get_case_conversation_context does)
        try:
            messages = await case_service.repository.get_messages(case_id, limit=50)
        except Exception as e:
            logger.warning(
                f"Failed to get messages for case {case_id}: {str(e)}",
                extra={
                    "case_id": case_id,
                    "error": str(e),
                    "error_type": type(e).__name__,
                    "correlation_id": correlation_id,
                },
            )
            messages = []

        # Count user turns (user role messages only)
        # Messages from repository.get_messages() are dicts with "role" field
        # role can be "user" or "agent"
        user_turn_count = len([m for m in messages if m.get("role") == "user"])

        logger.info(
            f"Title generation: checking turn threshold (case_id={case_id}, turns={user_turn_count}, threshold={MIN_TURNS_FOR_TITLE_GENERATION})",
            extra={
                "case_id": case_id,
                "user_turn_count": user_turn_count,
                "threshold": MIN_TURNS_FOR_TITLE_GENERATION,
            },
        )

        # Check if we have enough turns for title generation
        if user_turn_count < MIN_TURNS_FOR_TITLE_GENERATION:
            # Insufficient turns - return user-friendly error
            logger.info(
                f"Skipping title generation: insufficient turns (case_id={case_id}, turns={user_turn_count})",
                extra={
                    "case_id": case_id,
                    "user_turn_count": user_turn_count,
                    "threshold": MIN_TURNS_FOR_TITLE_GENERATION,
                },
            )
            raise ValidationException(
                f"Need at least {MIN_TURNS_FOR_TITLE_GENERATION} conversation "
                f"turns to generate a meaningful title. Continue discussing "
                f"your issue (currently {user_turn_count} turns), then try "
                f"again."
            )

        # Get conversation context for LLM prompt
        context_text = ""
        try:
            context_text = await case_service.get_case_conversation_context(
                case_id, limit=CONTEXT_MESSAGE_LIMIT
            )
        except Exception as e:
            logger.warning(
                f"Failed to get conversation context for case {case_id}, using fallback: {str(e)}",
                extra={
                    "case_id": case_id,
                    "error": str(e),
                    "error_type": type(e).__name__,
                    "correlation_id": correlation_id,
                },
            )
            context_text = f"Case: {case.title}\nDescription: {case.description or 'No description'}"

        # Extract meaningful content for the title prompt + gate. The gate's
        # purpose is a quality guard — don't ask the LLM to title a case with
        # too little substance. Substance is NOT just user chat: an upload- or
        # capture-driven investigation can have a confirmed problem statement,
        # evidence, and file summaries while the user typed almost nothing. We
        # measure the richest available signal (problem statement → evidence/
        # file summaries → user chat) so the guard fires on genuinely empty
        # cases without blocking content-rich ones. See _titleable_substance.
        user_signals = _extract_user_signals_from_context(context_text)
        user_message_content = _titleable_substance(case, user_signals)

        # Debug logging to diagnose empty extraction
        logger.info(
            f"Title generation: Extracted user signals",
            extra={
                "case_id": case_id,
                "context_length": len(context_text) if context_text else 0,
                "user_signals_length": (
                    len(user_message_content) if user_message_content else 0
                ),
                "context_preview": context_text[:300] if context_text else None,
                "user_signals_preview": (
                    user_message_content[:200] if user_message_content else None
                ),
            },
        )

        # Content gate. A confirmed/proposed problem statement is, by itself,
        # a title-grade summary of the case — when one exists the case is
        # titleable regardless of how little the user typed. Otherwise require
        # MIN_CONTENT_LENGTH_FOR_TITLE chars of substance (evidence/file
        # summaries + chat) so we don't ask the LLM to title an empty case.
        has_problem_statement = _has_problem_statement(case)
        if (
            not has_problem_statement
            and len(user_message_content) < MIN_CONTENT_LENGTH_FOR_TITLE
        ):
            logger.info(
                f"Skipping title generation: insufficient content (case_id={case_id}, length={len(user_message_content)})",
                extra={
                    "case_id": case_id,
                    "content_length": len(user_message_content),
                    "threshold": MIN_CONTENT_LENGTH_FOR_TITLE,
                    "has_problem_statement": has_problem_statement,
                },
            )
            raise ValidationException(
                f"Need at least {MIN_CONTENT_LENGTH_FOR_TITLE} characters of "
                f"conversation content to generate a meaningful title "
                f"(currently {len(user_message_content)} characters). "
                f"Continue discussing your issue, then try again."
            )

        # Generate title using LLM with fallback logic
        title_source = "unknown"
        llm_provider = getattr(request.app.state, "llm_provider", None)
        try:
            generated_title, title_source = await _generate_title_with_llm(
                context_text,
                case,
                max_words,
                hint,
                user_message_content,
                llm_provider,
                # Cost routing keys off the user's CHAT length (the original
                # "simple single-issue conversation" signal), not the larger
                # substance blob — otherwise every substance-rich case would
                # always take the LLM path. Content/extractive still use the
                # richer substance (user_message_content).
                routing_signal=user_signals,
            )
        except ValueError:
            raise ValidationException(
                "Cannot generate meaningful title from available context"
            )

        # Persist the generated title to database (Approach 1: Generate AND persist)
        try:
            success = await case_service.update_case(
                case_id, {"title": generated_title}, current_user.user_id
            )
            if not success:
                logger.error(
                    f"Failed to persist generated title for case {case_id}",
                    extra={"case_id": case_id, "generated_title": generated_title},
                )
                raise HTTPException(
                    status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                    detail="Failed to persist generated title",
                    headers={"x-correlation-id": correlation_id},
                )

            # Verify persistence by re-fetching the case from database
            verification_case = await case_service.get_case(
                case_id, current_user.user_id
            )
            if verification_case and verification_case.title != generated_title:
                logger.error(
                    f"Title persistence verification failed for case {case_id}: expected '{generated_title}', got '{verification_case.title}'",
                    extra={
                        "case_id": case_id,
                        "expected_title": generated_title,
                        "actual_title": verification_case.title,
                    },
                )
                raise HTTPException(
                    status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                    detail="Title saved but verification failed - possible database issue",
                    headers={"x-correlation-id": correlation_id},
                )

            logger.info(
                f"Title persistence verified for case {case_id}",
                extra={"case_id": case_id, "title": generated_title},
            )
        except HTTPException:
            # Re-raise HTTPException without modification to preserve original error
            raise
        except ServiceException as e:
            # Handle service-level exceptions with proper error detail
            logger.error(
                f"Service error persisting generated title: {e}",
                extra={"case_id": case_id, "correlation_id": correlation_id},
            )
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail=f"Failed to persist generated title: {str(e)}",
                headers={"x-correlation-id": correlation_id},
            )
        except Exception as e:
            # Handle unexpected exceptions
            logger.error(
                f"Unexpected error persisting generated title: {e}",
                extra={"case_id": case_id, "correlation_id": correlation_id},
            )
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail=f"Failed to persist generated title: {str(e)}",
                headers={"x-correlation-id": correlation_id},
            )

        # Persist success atomically and return X-Correlation-ID on all responses
        response.headers["x-correlation-id"] = correlation_id
        response.headers["x-title-source"] = (
            title_source  # Log source=llm vs fallback for telemetry
        )
        response.headers["x-content-length"] = str(
            len(user_message_content) if user_message_content else 0
        )

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
            detail=f"Failed to generate title: {str(e)}",
            headers={"x-correlation-id": correlation_id},
        )


def _sanitize_title_content(content: str) -> str:
    """Sanitize content for title generation - remove PII, profanity, etc."""
    if not content:
        return ""

    # Basic content hygiene - remove common PII patterns
    # Remove email addresses
    content = re.sub(
        r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Z|a-z]{2,}\b", "[email]", content
    )

    # Remove phone numbers (basic patterns)
    content = re.sub(r"\b\d{3}[-.]?\d{3}[-.]?\d{4}\b", "[phone]", content)
    content = re.sub(r"\b\(\d{3}\)\s*\d{3}[-.]?\d{4}\b", "[phone]", content)

    # Remove IP addresses
    content = re.sub(r"\b\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3}\b", "[ip]", content)

    # Remove URLs
    content = re.sub(r"https?://[^\s]+", "[url]", content)

    # Remove file paths (basic patterns)
    content = re.sub(r"[A-Za-z]:\\[^\s]+", "[path]", content)
    content = re.sub(r"/[^\s]+/", "[path]", content)

    return content.strip()


def _extract_user_signals_from_context(context_text: str) -> str:
    """Extract meaningful user content from conversation context for title generation.

    Focuses only on user messages, filtering out system/agent responses.
    Dedupes near-identical lines and caps to last 8-12 meaningful user messages.
    Returns the most relevant user content for title generation.
    """
    if not context_text or not context_text.strip():
        return ""

    lines = context_text.strip().split("\n")
    user_messages = []
    seen_messages = set()  # For deduplication

    for line in lines:
        line = line.strip()
        if not line:
            continue

        # Skip system headers and metadata
        skip_patterns = [
            "Previous conversation",
            "Case status:",
            "Created:",
            "Last updated:",
            "Message count:",
            "Current query:",
            "Description: No description",
            "Case: New Case",
            "Case: Untitled",
            "] Assistant:",  # Skip assistant responses
            "] System:",  # Skip system messages
        ]

        if any(pattern in line for pattern in skip_patterns):
            continue

        # Extract user messages specifically (only user lines)
        user_content = None
        if "] User:" in line:
            # Extract content after "User:"
            user_content = line.split("] User:", 1)[-1].strip()
        elif "User:" in line and not line.startswith("["):
            # Handle simpler "User:" format
            user_content = line.split("User:", 1)[-1].strip()
        elif line.startswith("Description:") and "No description" not in line:
            # Extract meaningful description as user content
            user_content = line.split("Description:", 1)[-1].strip()

        # Validate and dedupe user content
        if (
            user_content
            and len(user_content.split())
            >= MIN_MESSAGE_WORD_COUNT  # Filter short messages
            and user_content.lower() not in seen_messages
        ):  # Dedupe

            seen_messages.add(user_content.lower())
            user_messages.append(user_content)

            # Cap to last N meaningful user messages to reduce noise
            if len(user_messages) > MAX_USER_MESSAGES_FOR_CONTEXT:
                user_messages = user_messages[-MAX_USER_MESSAGES_FOR_CONTEXT:]

    # Return ALL user messages concatenated for accurate length measurement
    # This ensures the threshold check considers total conversation depth,
    # not just the last message (which could be short like "thanks")
    if user_messages:
        # Join all user messages with space separator
        all_content = " ".join(user_messages)
        return _sanitize_title_content(all_content)

    return ""


def _case_problem_statement(case) -> str:
    """Confirmed (problem_verification) or proposed (inquiry) problem statement."""
    pv = getattr(case, "problem_verification", None)
    statement = getattr(pv, "symptom_statement", None) if pv else None
    if not statement:
        inquiry = getattr(case, "inquiry", None)
        statement = (
            getattr(inquiry, "proposed_problem_statement", None) if inquiry else None
        )
    return (statement or "").strip()


# A statement shorter than this is too thin to be a meaningful title on its own.
_MIN_PROBLEM_STATEMENT_LEN_FOR_TITLE = 20


def _has_problem_statement(case) -> bool:
    """True when the case carries a non-trivial problem statement (title-grade)."""
    return len(_case_problem_statement(case)) >= _MIN_PROBLEM_STATEMENT_LEN_FOR_TITLE


def _titleable_substance(case, user_signals: str) -> str:
    """Richest titleable content for a case, for the title gate + prompt.

    The previous gate measured only sanitized user chat from the last few
    messages, so upload/capture-driven investigations — where the human types
    little but the case carries a confirmed problem statement, evidence, and
    file summaries — were permanently blocked from titling. This measures the
    real substance in priority order:

    1. Confirmed/proposed problem statement (the ideal title source).
    2. Evidence summaries (what the investigation established).
    3. Uploaded-file summaries (what data the user provided).
    4. User chat (their own framing) — always appended as fallback.

    The guard still fires on a genuinely empty case (no problem statement, no
    evidence, no files, a one-line "hi") because none of 1–3 contribute and the
    chat is below threshold — preserving the gate's original intent.
    """
    parts: list[str] = []

    # 1. Problem statement — confirmed (problem_verification) or proposed (inquiry).
    statement = _case_problem_statement(case)
    if statement:
        parts.append(statement)

    # 2. Evidence summaries (cap to a few — enough to establish substance).
    for ev in (getattr(case, "evidence", None) or [])[:5]:
        summary = getattr(ev, "summary", None)
        if summary and summary.strip():
            parts.append(summary.strip())

    # 3. Uploaded-file summaries.
    for uf in (getattr(case, "uploaded_files", None) or [])[:5]:
        summary = getattr(uf, "summary", None)
        if summary and summary.strip():
            parts.append(summary.strip())

    # 4. User chat framing (fallback / always included).
    if user_signals and user_signals.strip():
        parts.append(user_signals.strip())

    # Dedupe case-insensitively, preserve order.
    seen: set[str] = set()
    unique: list[str] = []
    for part in parts:
        key = part.lower()
        if key not in seen:
            seen.add(key)
            unique.append(part)

    return _sanitize_title_content(" ".join(unique))


def _generate_smart_extractive_title(
    user_signals: str, max_words: int = MAX_TITLE_WORDS_DEFAULT
) -> Optional[str]:
    """Generate title using smart extractive logic (no LLM).

    Strips conversational filler and extracts meaningful technical content.

    Args:
        user_signals: Pre-extracted user content from conversation
        max_words: Maximum words in generated title

    Returns:
        Extracted title or None if insufficient content
    """
    if not user_signals or not user_signals.strip():
        return None

    # Clean and tokenize
    content = user_signals.strip()
    content_lower = content.lower()

    # Strip filler from beginning (try longest patterns first, repeat until no match)
    stripped_any = True
    while stripped_any:
        stripped_any = False
        for filler in CONVERSATIONAL_FILLER:
            if content_lower.startswith(filler):
                # Remove the filler and any trailing whitespace/punctuation
                content = content[len(filler) :].strip().strip(",").strip()
                content_lower = content.lower()
                stripped_any = True
                break  # Start over with longest patterns first

    # Tokenize the cleaned content
    words = content.split()

    # Extract meaningful words (up to max_words)
    meaningful_words = words[:max_words]

    # Extractive path requires more words (3) than general validation (2)
    # This is intentional - extractive titles from longer content are more reliable
    if len(meaningful_words) < MIN_EXTRACTIVE_WORDS:
        return None

    # Join and clean up
    title = " ".join(meaningful_words)
    title = title.strip(".,!?;:")

    # Check for incomplete endings - reject if title ends mid-sentence
    last_word = meaningful_words[-1].lower().strip(".,!?;:")
    if last_word in INCOMPLETE_ENDINGS:
        # Title ends with incomplete phrase, reject it and fall through to LLM
        return None

    # Apply title casing and return
    return apply_title_case(title)


async def _generate_title_with_llm(
    context_text: str,
    case,
    max_words: int = 8,
    hint: Optional[str] = None,
    user_signals: Optional[str] = None,
    llm_provider=None,
    routing_signal: Optional[str] = None,
) -> tuple[str, str]:
    """Generate title using hybrid approach: smart extractive for simple cases, LLM for complex.

    Strategy:
    - Simple cases (< 300 chars): Fast smart extractive (1ms, $0, no API)
    - Complex cases (>= 300 chars): LLM synthesis (500-1200ms, ~$0.0002, API call)

    Smart extractive strips conversational filler and extracts meaningful terms.
    LLM path is used for multi-topic or long conversations requiring synthesis.

    Args:
        context_text: Conversation context text
        case: Case object
        max_words: Maximum words in generated title
        hint: Optional hint to guide title generation
        user_signals: Pre-extracted user signals from conversation
        llm_provider: LLM provider from app.state (Composition Root)

    Returns:
        Tuple of (title, source) where source is "extractive", "llm", or "fallback"
    """
    try:
        # LLM provider passed from app.state (Composition Root)
        if not llm_provider:
            fallback = get_extractive_fallback_title(
                user_signals, context_text, case, max_words
            )
            if not fallback:
                raise ValueError("Insufficient context for title generation")
            return fallback, "extractive"

        # HYBRID APPROACH: Use smart extractive for simple cases, LLM for complex ones
        # Complexity heuristics:
        # 1. Content length: < EXTRACTIVE_MAX_CONTENT_LENGTH chars = simple, single-issue conversation
        # 2. No user_signals means insufficient extraction (rare edge case)

        # Route on the user's chat length (routing_signal) when provided, so a
        # large synthesized substance blob doesn't force every case onto the LLM
        # path; fall back to user_signals when no separate routing signal given.
        routing_text = routing_signal if routing_signal is not None else user_signals
        use_smart_extractive = False
        if user_signals and user_signals.strip():
            content_length = len(routing_text or "")
            # Simple conversation: short content that likely describes a single issue
            if content_length < EXTRACTIVE_MAX_CONTENT_LENGTH:
                use_smart_extractive = True
                logger.info(
                    f"Title generation: Using smart extractive (content_length={content_length})",
                    extra={"content_length": content_length, "decision": "extractive"},
                )

        if use_smart_extractive:
            # Fast path: Smart extractive title generation (1ms, $0, no API call)
            extractive_title = _generate_smart_extractive_title(user_signals, max_words)
            if extractive_title and is_title_valid(extractive_title):
                logger.info(
                    "Title generation: Smart extractive success",
                    extra={"extractive_title": extractive_title},
                )
                return extractive_title, "extractive"
            else:
                # Extractive failed (rare), fall through to LLM
                logger.info(
                    "Title generation: Smart extractive insufficient, using LLM",
                    extra={"extractive_attempt": extractive_title},
                )

        # Slow path: LLM-based title generation (500-1200ms, $0.0001-0.0003, API call)
        # Used for: complex conversations, long content, multi-topic discussions
        logger.info(
            f"Title generation: Using LLM (content_length={len(user_signals) if user_signals else 0})",
            extra={
                "content_length": len(user_signals) if user_signals else 0,
                "decision": "llm",
            },
        )

        # Use extracted user signals if available, otherwise fall back to full context
        # User signals are already cleaned, deduplicated, and focused on user content
        prompt_content = (
            user_signals if user_signals and user_signals.strip() else context_text
        )

        # Simple, clear prompt focused on the task
        hint_text = f" {hint}" if hint else ""
        prompt = (
            f"Generate a concise, descriptive title (maximum {max_words} words) for this technical support conversation.\n\n"
            f"User's messages:\n{prompt_content}\n\n"
            f"Requirements:\n"
            f"- Maximum {max_words} words\n"
            f"- Use specific technical terms from the conversation\n"
            f"- Title Case format (e.g., 'PostgreSQL Connection Timeout')\n"
            f"- Avoid generic words: Issue, Problem, Troubleshooting, Conversation\n"
            f"- Return ONLY the title, no quotes or explanations{hint_text}\n\n"
            f"Title:"
        )

        # Generate title using LLM with optimized settings
        response = await llm_provider.generate(
            prompt=prompt,
            max_tokens=LLM_TITLE_MAX_TOKENS,
            temperature=LLM_TITLE_TEMPERATURE,
            top_p=LLM_TITLE_TOP_P,
        )

        if response and response.content and response.content.strip():
            # Strip quotes/punctuation; collapse whitespace
            generated_title = response.content.strip().strip('"').strip("'").strip()

            # Check for error placeholder strings from LLM providers
            error_placeholders = [
                "[Response truncated due to token limit]",
                "[Content blocked by safety filters]",
                "[Response blocked]",
                "[Error]",
            ]
            if any(
                placeholder.lower() in generated_title.lower()
                for placeholder in error_placeholders
            ):
                logger.warning(
                    "Title generation: LLM returned error placeholder",
                    extra={"error_placeholder": generated_title},
                )
                raise ValueError(f"LLM returned error placeholder: {generated_title}")

            generated_title = re.sub(
                r"\s+", " ", generated_title
            )  # Collapse whitespace
            generated_title = generated_title.rstrip(
                ".,!?;:"
            )  # Remove trailing punctuation

            # Remove common LLM prefixes/suffixes
            prefixes_to_remove = [
                "Title:",
                "title:",
                "Here is a title:",
                "Here's a title:",
            ]
            for prefix in prefixes_to_remove:
                if generated_title.lower().startswith(prefix.lower()):
                    generated_title = generated_title[len(prefix) :].strip()

            # Check if LLM returned NONE token (deterministic escape hatch)
            if generated_title.upper() == "NONE":
                logger.info("Title generation: LLM returned NONE token")
                raise ValueError("LLM determined no compliant title possible")

            # Lightweight guards: length ≤ max_words, ≥3 words, no banned generics, basic validation
            words = generated_title.split()
            if len(words) > max_words:
                generated_title = " ".join(words[:max_words])
                words = words[:max_words]  # Update words array to match truncated title

            # Run lightweight validation guards
            if not is_title_valid(generated_title):
                logger.info(
                    "Title generation: LLM output failed validation guards",
                    extra={"invalid_title": generated_title},
                )

                # Minimal deterministic fallback behind flag for resiliency (optional but prudent)
                from faultmaven.config.settings import get_settings

                use_fallback = get_settings().case.title_generation_use_fallback
                if use_fallback:
                    fallback = get_extractive_fallback_title(
                        user_signals, context_text, case, max_words
                    )
                    if fallback and is_title_valid(
                        fallback, check_banned_words=False
                    ):  # Don't block non-English fallbacks
                        logger.info(
                            "Title generation: Using extractive fallback for resiliency",
                            extra={"fallback_title": fallback},
                        )
                        return fallback, "fallback"

                # If no fallback or fallback fails, return 422
                raise ValueError(
                    "Generated title failed validation guards and fallback insufficient"
                )

            logger.info(
                "Title generation: LLM success",
                extra={"generated_title": generated_title},
            )
            return generated_title, "llm"
        else:
            fallback = get_extractive_fallback_title(
                user_signals, context_text, case, max_words
            )
            if not fallback:
                raise ValueError("LLM failed and insufficient fallback context")
            logger.info(
                f"Title generation: LLM empty response, using fallback",
                extra={"fallback_title": fallback},
            )
            return fallback, "fallback"

    except Exception as e:
        logger.warning(f"LLM title generation failed, trying fallback: {e}")
        fallback = get_extractive_fallback_title(
            user_signals, context_text, case, max_words
        )
        if not fallback:
            raise ValueError("Both LLM and fallback title generation failed")
        logger.info(
            f"Title generation: LLM exception, using fallback",
            extra={"error": str(e), "fallback_title": fallback},
        )
        return fallback, "fallback"


@router.post("/search", response_model=List[CaseSummary])
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
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Search failed: {str(e)}",
        )


@router.get("/{case_id}/analytics", response_model=Dict[str, Any])
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
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to get case analytics: {str(e)}",
        )


# Conversation thread retrieval (messages)
@router.get("/{case_id}/messages", response_model=CaseMessagesResponse)
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
            detail=f"Failed to get messages: {str(e)}",
            headers={"x-correlation-id": correlation_id},
        )


# Session-case integration endpoints


@router.post("/sessions/{session_id}/case", response_model=Dict[str, Any])
@trace("api_create_case_for_session")
async def create_case_for_session(
    session_id: str,
    request: Request,
    title: Optional[str] = Query(
        None, description="Case title (optional, auto-generated if not provided)"
    ),
    force_new: bool = Query(False, description="Force creation of new case"),
    case_service: Optional[ICaseService] = Depends(_di_get_case_service_dependency),
    session_service: ISessionService = Depends(_di_get_session_service_dependency),
    current_user: Optional[UserDTO] = Depends(get_current_user_optional),
) -> Dict[str, Any]:
    """
    Create or get case for a session

    Associates a case with the given session. If no case exists, creates a new one.
    If force_new is true, always creates a new case.

    **Title Auto-Generation**: If title is not provided or empty, the backend
    automatically generates a unique title in the format: Case-MMDD-N
    (e.g., Case-1028-1, Case-1028-2). The sequence counter resets daily.

    Supports idempotency via 'idempotency-key' header to prevent duplicate case
    creation on retry when using force_new=true.
    """
    try:
        # Validate session and derive user if not authenticated
        session = await session_service.get_session(session_id, validate=True)
        if not session:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Invalid or expired session",
            )

        # Get user_id from auth or session
        user_id = current_user.user_id if current_user else session.user_id

        # Check for idempotency key (prevents duplicate case creation on retry)
        idempotency_key = request.headers.get("idempotency-key")

        if idempotency_key and force_new:
            # Check if we already processed this request
            existing_result = await case_service.check_idempotency_key(idempotency_key)
            if existing_result:
                logger.info(
                    f"Returning cached result for idempotency key: {idempotency_key}"
                )
                return existing_result.get("content", existing_result)

        # Create or get case for session
        case_id = await case_service.get_or_create_case_for_session(
            session_id=session_id, user_id=user_id, force_new=force_new, title=title
        )

        if not case_id:
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail="Failed to create case for session",
            )

        result = {"case_id": case_id, "created_new": force_new, "success": True}

        # Store idempotency result if key provided (only for force_new to prevent duplicates)
        if idempotency_key and force_new:
            await case_service.store_idempotency_result(
                idempotency_key, 200, result, {}
            )

        return result

    except ValidationException as e:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(e))
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to manage session case: {str(e)}",
        )


@router.post("/sessions/{session_id}/resume/{case_id}", response_model=Dict[str, Any])
@trace("api_resume_case_in_session")
async def resume_case_in_session(
    session_id: str,
    case_id: str,
    case_service: Optional[ICaseService] = Depends(_di_get_case_service_dependency),
    current_user: UserDTO = Depends(require_authentication),
) -> Dict[str, Any]:
    """
    Resume an existing case in a session

    Links the session to an existing case, allowing the user to continue
    a previous troubleshooting conversation.
    """
    try:
        success = await case_service.resume_case_in_session(case_id, session_id)

        if not success:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Case not found or resume not permitted",
            )

        return {
            "case_id": case_id,
            "success": True,
            "message": "Case resumed in session",
        }

    except HTTPException:
        raise
    except ValidationException as e:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(e))
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to resume case: {str(e)}",
        )


# ============================================================
# Unified Turn Endpoint (v4.1)
# ============================================================


@router.post("/{case_id}/turns", response_model=TurnResponse)
@trace("api_submit_turn")
async def submit_turn(
    case_id: str,
    fastapi_request: Request,
    query: Optional[str] = Form(None),
    files: List[UploadFile] = File(default=[]),
    pasted_content: Optional[str] = Form(None),
    intent_type: Optional[str] = Form(None),
    intent_data: Optional[str] = Form(None),
    input_type: Optional[str] = Form(None),
    source_url: Optional[str] = Form(None),
    case_service: Optional[ICaseService] = Depends(_di_get_case_service_dependency),
    investigation_service=Depends(get_investigation_service),
    current_user: UserDTO = Depends(require_authentication),
) -> TurnResponse:
    """Submit a turn to a case investigation.

    A turn consists of an optional query and/or optional attachments.
    Attachments are preprocessed through Tier 0+1 before the LLM sees them.
    If no query is provided with attachments, an implicit query is generated.
    """
    import json

    case_service = check_case_service_available(case_service)
    correlation_id = str(uuid.uuid4())

    try:
        # Validate at least one input provided
        if not query and not files and not pasted_content:
            raise HTTPException(
                status_code=400,
                detail="At least one of query, files, or pasted_content must be provided",
                headers={"x-correlation-id": correlation_id},
            )

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
                    detail="Cannot submit evidence to a closed case. Only questions about the case are allowed.",
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
            if input_type == "page_capture":
                source_meta = {"source_type": "page_capture"}
                if source_url:
                    source_meta["source_url"] = source_url
                filename = f"page-capture-{ts}.txt"
            else:
                source_meta = {"source_type": "text_paste"}
                filename = f"pasted-content-{ts}.txt"

            attachments.append(
                Attachment(
                    content=pasted_content.encode("utf-8"),
                    filename=filename,
                    content_type="text/plain",
                    source_metadata=source_meta,
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
                )
            try:
                intent = QueryIntent(type=parsed_intent_type, **data)
            except (ValidationError, ValueError) as e:
                raise HTTPException(
                    status_code=422,
                    detail=f"Invalid fields for intent_type={intent_type!r}: {e}",
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
            response = await asyncio.wait_for(
                investigation_service.process_turn(
                    case_id=case_id, user_id=current_user.user_id, payload=payload
                ),
                timeout=agent_timeout,
            )

            # Store idempotency result if key provided
            idempotency_key = fastapi_request.headers.get("idempotency-key")
            if idempotency_key:
                await case_service.store_idempotency_result(
                    idempotency_key,
                    200,
                    response.model_dump(),
                    {"x-correlation-id": correlation_id},
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
        error_msg = str(e)
        # Billing / quota exhaustion → 402 via the shared mapper (one contract
        # across all LLM-calling endpoints). error_code is threaded through the
        # engine/service wrap, so the typed check alone is reliable.
        if is_quota_exhausted_service_error(e):
            raise quota_exhausted_http_exception(correlation_id)

        if "over capacity" in error_msg.lower() or "503" in error_msg:
            detail = "AI service temporarily unavailable due to high demand. Please try again."
            status_code = 503
            error_code = "LLM_OVER_CAPACITY"
            retry_after = "60"
        elif "rate limit" in error_msg.lower() or "429" in error_msg:
            detail = "Rate limit exceeded. Please wait before sending another message."
            status_code = 429
            error_code = "RATE_LIMIT_EXCEEDED"
            retry_after = "60"
        elif "timeout" in error_msg.lower():
            detail = "Request timed out. Please try again."
            status_code = 504
            error_code = "LLM_TIMEOUT"
            retry_after = "30"
        else:
            detail = f"Unable to process your message: {error_msg[:200]}"
            status_code = 500
            error_code = "SERVICE_ERROR"
            retry_after = "10"

        headers = {
            "x-correlation-id": correlation_id,
            "x-error-code": error_code,
        }
        # No Retry-After for billing: retrying won't help until an operator acts.
        if retry_after is not None:
            headers["Retry-After"] = retry_after

        raise HTTPException(
            status_code=status_code,
            detail=detail,
            headers=headers,
        )
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
    - ``422`` — invalid or missing ``data_type`` (``ValidationException``).
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


@router.get("/{case_id}/data")
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


@router.get("/{case_id}/data/{data_id}")
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
        raise HTTPException(
            status_code=500, detail=f"Failed to retrieve case data: {str(e)}"
        )


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
        raise HTTPException(
            status_code=500, detail=f"Failed to delete case data: {str(e)}"
        )


# =============================================================================
# Document Generation and Closure Endpoints
# =============================================================================


@router.get("/{case_id}/report-recommendations")
@trace("api_get_report_recommendations")
async def get_report_recommendations(
    case_id: str,
    case_service: Optional[ICaseService] = Depends(_di_get_case_service_dependency),
    current_user: UserDTO = Depends(require_authentication),
    vector_store=Depends(_di_get_vector_store_dependency),
):
    """
    Get intelligent report recommendations for a resolved case.

    Returns recommendations for which reports to generate, including
    intelligent runbook suggestions based on similarity search of existing
    runbooks (both incident-driven and document-driven sources).

    Recommendation Logic:
    - Always available: Incident Report, Post-Mortem (unique per incident)
    - Conditional: Runbook (based on similarity search)
        - ≥85% similarity: Recommend reuse existing runbook
        - 70-84% similarity: Offer both review OR generate options
        - <70% similarity: Recommend generate new runbook

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
    from faultmaven.infrastructure.knowledge.runbook_kb import RunbookKnowledgeBase
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

        if vector_store is None:
            raise HTTPException(
                status_code=503,
                detail="Vector store not available. Check ChromaDB configuration.",
            )
        runbook_kb = RunbookKnowledgeBase(vector_store=vector_store)
        recommendation_service = ReportRecommendationService(runbook_kb=runbook_kb)

        # Get intelligent recommendations
        recommendations = await recommendation_service.get_available_report_types(
            case=case
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
    except Exception as e:
        logger.error(
            f"Failed to get report recommendations for case {case_id}: {e}",
            exc_info=True,
        )
        raise HTTPException(
            status_code=500, detail=f"Failed to get report recommendations: {str(e)}"
        )


@router.post("/{case_id}/reports")
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
    )

    case_service = check_case_service_available(case_service)

    try:
        case = await case_service.get_case(case_id, current_user.user_id)
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
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/{case_id}/reports")
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
        raise HTTPException(status_code=500, detail=str(e))


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


@router.get("/{case_id}/reports/{report_id}/download")
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
        raise HTTPException(status_code=500, detail=str(e))


# ============================================================
# V2.0 Milestone-Based Investigation Endpoints
# ============================================================


@router.post("/{case_id}/close")
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

    Marks all latest reports as linked to case closure and transitions
    case to CLOSED state.

    Returns:
        CaseClosureResponse with list of archived reports
    """
    from faultmaven.modules.case.contracts import CaseClosureResponse

    case_service = check_case_service_available(case_service)

    try:
        case = await case_service.get_case(case_id, current_user.user_id)
        if not case:
            raise HTTPException(status_code=404, detail="Case not found")

        # Validate state — a case may be closed from any state except an
        # already-closed one (CLOSED is terminal and unconditional; the only
        # invalid close is re-closing). RESOLVED, INVESTIGATING and INQUIRY all
        # close legitimately. (The prior list referenced CaseState.SOLVED /
        # DOCUMENTING, which do not exist on the enum — every call raised
        # AttributeError 500 before reaching this check.)
        if case.state == CaseState.CLOSED:
            raise HTTPException(
                status_code=400,
                detail="Case is already closed",
            )

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
                    f"Failed to link reports to closure, continuing with case close: {e}",
                    extra={"case_id": case_id},
                )
                # Continue closing case even if report linking fails

        # Close case
        closed_at = datetime.now(timezone.utc)
        case.state = CaseState.CLOSED
        await case_service.update_case_status(
            case_id, CaseState.CLOSED, current_user.user_id
        )

        logger.info(
            f"Case closed successfully",
            extra={"case_id": case_id, "archived_report_count": len(archived_reports)},
        )

        response = CaseClosureResponse(
            case_id=case_id,
            closed_at=to_json_compatible(closed_at),
            archived_reports=archived_reports,
            download_available_until=(closed_at + timedelta(days=90)).isoformat() + "Z",
        )

        return response.model_dump()

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Case closure failed: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))


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
        files = [UploadedFileMetadata.from_uploaded_file(f) for f in paginated_files]

        # Set pagination header (required by API contract)
        response.headers["X-Total-Count"] = str(total_count)

        return UploadedFilesList(
            files=files, total_count=total_count, limit=limit, offset=offset
        )

    except NotFoundError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except PermissionDeniedException as e:
        raise HTTPException(status_code=403, detail=str(e))
    except Exception as e:
        logger.error(f"Failed to list uploaded files: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))


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
        raise HTTPException(status_code=500, detail=str(e))


def _build_evidence_response(case, evidence, case_id: str) -> EvidenceDetailsResponse:
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

        evidence_items = [
            _build_evidence_response(case, evidence, case_id)
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
        raise HTTPException(status_code=500, detail=str(e))


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
        raise HTTPException(status_code=500, detail=str(e))


# ============================================================================
# Case Sharing Endpoints
# ============================================================================


@router.post(
    "/{case_id}/share",
    status_code=status.HTTP_201_CREATED,
    summary="Share Case",
    description="Share a case with another user. Requires owner or collaborator permission.",
)
async def share_case(
    case_id: str = Path(..., description="Case ID"),
    target_user_id: str = Body(..., embed=True, description="User ID to share with"),
    role: str = Body(
        "viewer",
        embed=True,
        description="Participant role: owner, collaborator, viewer",
    ),
    case_service: ICaseService = Depends(get_case_service),
    auth: tuple = Depends(require_authentication),
):
    """Share a case with another user."""
    session_id, user_id = auth

    try:
        # Validate role
        valid_roles = ["owner", "collaborator", "viewer"]
        if role not in valid_roles:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"Invalid role. Must be one of: {', '.join(valid_roles)}",
            )

        # Share the case
        success = await case_service.share_case(
            case_id=case_id,
            target_user_id=target_user_id,
            role=role,
            sharer_user_id=user_id,
        )

        if not success:
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail="Failed to share case",
            )

        logger.info(
            f"Case {case_id} shared with user {target_user_id} as {role} by {user_id}"
        )

        return {
            "message": "Case shared successfully",
            "case_id": case_id,
            "shared_with": target_user_id,
            "role": role,
        }

    except ValidationException as e:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=str(e))
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error sharing case {case_id}: {e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=str(e)
        )


@router.delete(
    "/{case_id}/share/{target_user_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Unshare Case",
    description="Unshare a case from a user. Requires owner permission.",
)
async def unshare_case(
    case_id: str = Path(..., description="Case ID"),
    target_user_id: str = Path(..., description="User ID to unshare from"),
    case_service: ICaseService = Depends(get_case_service),
    auth: tuple = Depends(require_authentication),
):
    """Unshare a case from a user."""
    session_id, user_id = auth

    try:
        success = await case_service.unshare_case(
            case_id=case_id, target_user_id=target_user_id, unsharer_user_id=user_id
        )

        if not success:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"User {target_user_id} not found in case {case_id} participants",
            )

        logger.info(f"Case {case_id} unshared from user {target_user_id} by {user_id}")

    except ValidationException as e:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=str(e))
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error unsharing case {case_id}: {e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=str(e)
        )


@router.get(
    "/{case_id}/participants",
    response_model=List[Dict[str, Any]],
    summary="Get Case Participants",
    description="Get all participants who have access to this case.",
)
async def get_case_participants(
    case_id: str = Path(..., description="Case ID"),
    case_service: ICaseService = Depends(get_case_service),
    auth: tuple = Depends(require_authentication),
) -> List[Dict[str, Any]]:
    """Get all participants for a case."""
    session_id, user_id = auth

    try:
        # Verify user has access to the case
        case = await case_service.get_case(case_id)
        if not case:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Case {case_id} not found",
            )

        # Get participants
        participants = await case_service.get_case_participants(case_id)

        return participants

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error getting participants for case {case_id}: {e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=str(e)
        )


@router.get(
    "/{case_id}/access-check",
    response_model=Dict[str, bool],
    summary="Check Case Access",
    description="Check if current user has access to this case.",
)
async def check_case_access(
    case_id: str = Path(..., description="Case ID"),
    case_service: ICaseService = Depends(get_case_service),
    auth: tuple = Depends(require_authentication),
) -> Dict[str, bool]:
    """Check if user has access to case."""
    session_id, user_id = auth

    try:
        has_access = await case_service.user_can_access_case(user_id, case_id)

        return {"has_access": has_access, "user_id": user_id, "case_id": case_id}

    except Exception as e:
        logger.error(f"Error checking access for case {case_id}: {e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=str(e)
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
    request: Request = None,
    case_service: ICaseService = Depends(get_case_service),
    current_user: UserDTO = Depends(require_authentication),
) -> Dict[str, Any]:
    """
    Extract knowledge from a case conversation into a suggestion.

    This endpoint uses LLM to analyze the case's messages and evidence,
    then generates a reusable knowledge article (runbook, troubleshooting guide).

    The suggestion is automatically scanned for PII and placed in a
    "pending_review" state for admin approval in the Dashboard Review Inbox.

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
        # Verify case exists and user has access
        case = await case_service.get_case(case_id, current_user.user_id)
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

        # Get suggestion service from app state
        suggestion_service = None
        if request and hasattr(request.app, "state"):
            suggestion_service = getattr(request.app.state, "suggestion_service", None)

        if not suggestion_service:
            # Create a temporary service for extraction
            from faultmaven.modules.knowledge.domain.services.suggestion_service import (
                SuggestionService,
            )

            suggestion_service = SuggestionService()

        # Extract knowledge
        suggestion = await suggestion_service.extract_knowledge_from_case(
            case_id=case_id,
            organization_id=getattr(case, "organization_id", "default"),
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
            "extracted_from": {
                "case_title": suggestion.source_case_title,
                "message_count": suggestion.message_count,
                "evidence_count": suggestion.evidence_count,
            },
            "created_at": to_json_compatible(suggestion.created_at),
        }

    except HTTPException:
        raise
    except Exception as e:
        logger.error(
            f"Knowledge extraction failed for case {case_id}: {e}", exc_info=True
        )
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Knowledge extraction failed: {str(e)}",
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
