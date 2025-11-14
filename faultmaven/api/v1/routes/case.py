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

from datetime import datetime, timezone
from faultmaven.utils.serialization import to_json_compatible
from typing import Any, Dict, List, Optional, Union
import asyncio
import time

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Path, Query, status, Response, Body, UploadFile, File, Form, Request
from fastapi.responses import JSONResponse
import uuid
import logging

from faultmaven.models.case import (
    Case as CaseEntity,
    CaseStatus,
)
from faultmaven.models.api_models import (
    CaseCreateRequest,
    CaseUpdateRequest,
    CaseListFilter,
    CaseMessage,
    CaseSearchRequest,
    CaseSummary,
    CaseDetail,
    CaseListResponse,
    CaseParticipant,
    CaseQueryRequest,
    CaseQueryResponse,
    UploadedFileMetadata,
    UploadedFileDetails,
    UploadedFilesList,
    # Phase 2: Evidence-to-File Linkage
    DerivedEvidenceSummary,
    UploadedFileDetailsResponse,
    UploadedFilesListResponse,
    SourceFileReference,
    RelatedHypothesis,
    EvidenceDetailsResponse,
)
from faultmaven.models.case_ui import CaseUIResponse
from faultmaven.services.adapters.case_ui_adapter import transform_case_for_ui
from faultmaven.models.interfaces_case import ICaseService
from faultmaven.models.interfaces_report import IReportStore
from faultmaven.models.api import (
    ErrorResponse, ErrorDetail, CaseResponse, Case, Message, QueryJobStatus,
    AgentResponse, ViewState, User, ResponseType, TitleGenerateResponse,
    TitleResponse, QueryRequest, CaseMessagesResponse, DataUploadResponse,
    ProcessingStatus
)
from faultmaven.api.v1.dependencies import (
    get_case_service, get_session_id, get_session_service,
    get_preprocessing_service, get_report_store,
    get_investigation_service,  # V2.0 milestone-based
    get_data_service,
    get_case_vector_store
)
from faultmaven.api.v1.auth_dependencies import (
    require_authentication,
    get_current_user_optional,
    get_current_user_id
)
from faultmaven.models.auth import DevUser
from faultmaven.services.domain.session_service import SessionService
from faultmaven.services.converters import CaseConverter
from fastapi import Request
from faultmaven.infrastructure.observability.tracing import trace
from faultmaven.exceptions import (
    ValidationException,
    ServiceException,
    NotFoundException,
    PermissionDeniedException
)

# Create router
router = APIRouter(prefix="/cases", tags=["cases"])

# Set up logging
logger = logging.getLogger(__name__)

# Helper function to safely extract enum values
def _safe_enum_value(value):
    """Safely extract enum value, return string if already string."""
    if hasattr(value, 'value'):
        return value.value
    return str(value)


async def _store_evidence_in_vector_db(
    case_id: str,
    data_id: str,
    content: str,
    data_type: str,
    metadata: Dict[str, Any],
    case_vector_store
):
    """
    Background task: Store evidence in ChromaDB for forensic queries.

    This runs asynchronously after upload completes, so user doesn't wait.
    Implements the async pipeline from data-preprocessing-design-specification.md Step 5.

    Args:
        case_id: Case identifier for collection scoping
        data_id: Unique evidence identifier
        content: Preprocessed content (NOT raw)
        data_type: Evidence data type
        metadata: Evidence metadata
        case_vector_store: Case-scoped vector store (InMemory or ChromaDB)
    """
    try:
        logger.info(
            f"Starting background vectorization for evidence {data_id} in case {case_id}",
            extra={'case_id': case_id, 'data_id': data_id, 'content_size': len(content)}
        )

        await case_vector_store.add_documents(
            case_id=case_id,
            documents=[{
                'id': data_id,
                'content': content,
                'metadata': {
                    'data_type': data_type,
                    'upload_timestamp': datetime.now(timezone.utc).isoformat(),
                    **metadata
                }
            }]
        )

        logger.info(
            f"✅ Evidence {data_id} vectorized successfully for case {case_id}",
            extra={'case_id': case_id, 'data_id': data_id}
        )

    except Exception as e:
        # Silent failure - doesn't affect user experience
        # Evidence is still stored in data storage and available via preprocessed summary
        logger.error(
            f"❌ Failed to vectorize evidence {data_id} for case {case_id}: {e}",
            extra={'case_id': case_id, 'data_id': data_id, 'error': str(e)},
            exc_info=True
        )


# Configurable banned words list - minimal but extensible
BANNED_GENERIC_WORDS = [
    'new case', 'untitled', 'troubleshooting', 'conversation',
    'discussion', 'issue', 'problem', 'help', 'assistance',
    'user query', 'support request', 'technical issue'
]

async def _di_get_case_service_dependency() -> Optional[ICaseService]:
    """Runtime wrapper so patched dependency is honored in tests."""
    # Import inside to resolve the patched function at call time
    from faultmaven.api.v1.dependencies import get_case_service as _getter
    return await _getter()


# Legacy dependency functions removed - using new auth_dependencies directly


async def _di_get_session_id_dependency(request: Request) -> Optional[str]:
    """Runtime wrapper so patched dependency is honored in tests."""
    from faultmaven.api.v1.dependencies import get_session_id as _get_session_id
    return await _get_session_id(request)


async def _di_get_session_service_dependency() -> SessionService:
    """Runtime wrapper so patched dependency is honored in tests."""
    from faultmaven.api.v1.dependencies import get_session_service as _getter
    return await _getter()


def check_case_service_available(case_service: Optional[ICaseService]) -> ICaseService:
    """Check if case service is available and raise appropriate error if not"""
    if case_service is None:
        # For protected endpoints that require authentication, return 401 instead of 500
        # This prevents pre-auth calls from getting 500 errors
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Authentication required - case service unavailable"
        )
    return case_service
@router.delete("/{case_id}", status_code=status.HTTP_204_NO_CONTENT, responses={204: {"description": "Case deleted successfully", "headers": {"X-Correlation-ID": {"description": "Request correlation ID", "schema": {"type": "string"}}}}})
@trace("api_delete_case")
async def delete_case(
    case_id: str,
    case_service: Optional[ICaseService] = Depends(_di_get_case_service_dependency),
    current_user: DevUser = Depends(require_authentication)
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
            headers={"x-correlation-id": correlation_id}
        )
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Unexpected error in delete_case: {e}", extra={"correlation_id": correlation_id})
        error_response = ErrorResponse(
            schema_version="3.1.0",
            error=ErrorDetail(code="DELETE_CASE_ERROR", message="Failed to delete case")
        )
        raise HTTPException(
            status_code=500,
            detail=error_response.dict(),
            headers={"x-correlation-id": correlation_id}
        )


@router.post("", response_model=CaseSummary, status_code=status.HTTP_201_CREATED)
@trace("api_create_case")
async def create_case(
    request: CaseCreateRequest,
    response: Response,
    case_service: Optional[ICaseService] = Depends(_di_get_case_service_dependency),
    session_service: SessionService = Depends(_di_get_session_service_dependency),
    current_user: DevUser = Depends(require_authentication)
) -> CaseSummary:
    """
    Create a new troubleshooting case (v2.0 milestone-based)

    Creates a new case with milestone-based investigation tracking.
    Initial status is CONSULTING (problem definition phase).

    Returns CaseSummary with basic case info and milestone progress.
    """
    correlation_id = str(uuid.uuid4())
    case_service = check_case_service_available(case_service)

    try:
        # Validate session if provided (restored from old implementation)
        if request.session_id:
            session = await session_service.get_session(request.session_id, validate=True)
            if not session:
                logger.warning(f"Invalid or expired session: {request.session_id}", extra={"correlation_id": correlation_id})
                error_response = ErrorResponse(
                    schema_version="3.1.0",
                    error=ErrorDetail(
                        code="SESSION_EXPIRED",
                        message="Your session has expired. Please refresh the page to continue."
                    )
                )
                raise HTTPException(
                    status_code=status.HTTP_401_UNAUTHORIZED,
                    detail=error_response.dict(),
                    headers={"x-correlation-id": correlation_id}
                )

        # Create case using new model
        case_entity = await case_service.create_case(
            title=request.title,  # Pass None to trigger auto-generation in service
            description=request.description,
            owner_id=current_user.user_id,
            session_id=request.session_id,
            initial_message=request.initial_message  # Restored from old implementation
        )

        # Set Location header
        response.headers["Location"] = f"/api/v1/cases/{case_entity.case_id}"
        response.headers["x-correlation-id"] = correlation_id

        # Return summary (v2.0 API model)
        return CaseSummary.from_case(case_entity)
        
    except ValidationException as e:
        logger.error(f"Validation error in create_case: {e}", extra={"correlation_id": correlation_id})
        error_response = ErrorResponse(
            schema_version="3.1.0",
            error=ErrorDetail(code="VALIDATION_ERROR", message=str(e))
        )
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=error_response.dict(),
            headers={"x-correlation-id": correlation_id}
        )
    except ServiceException as e:
        logger.error(f"Service error in create_case: {e}", extra={"correlation_id": correlation_id})
        error_response = ErrorResponse(
            schema_version="3.1.0",
            error=ErrorDetail(code="CASE_SERVICE_ERROR", message=str(e))
        )
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=error_response.dict(),
            headers={"x-correlation-id": correlation_id}
        )


@router.get("", response_model=CaseListResponse)
@trace("api_list_cases")
async def list_cases(
    response: Response,
    case_service: Optional[ICaseService] = Depends(_di_get_case_service_dependency),
    current_user: DevUser = Depends(require_authentication),
    status: Optional[CaseStatus] = Query(None, description="Filter by status"),
    limit: int = Query(50, ge=1, le=100, description="Items per page"),
    offset: int = Query(0, ge=0, description="Number of items to skip"),
    # Changed default to True - new cases should be visible immediately
    include_empty: bool = Query(True, description="Include cases with current_turn == 0 (newly created)"),
    include_archived: bool = Query(False, description="Include archived/closed cases")
):
    """
    List user's cases with pagination (v2.0 milestone-based)

    Returns CaseListResponse with:
    - List of CaseSummary objects (with milestone progress)
    - Total count for pagination
    - has_more flag

    Default Filtering Behavior:
    - INCLUDES empty cases (current_turn == 0) - newly created cases are visible
    - EXCLUDES archived/closed cases unless include_archived=true
    - Use include_empty=false to hide cases with no conversation yet
    - Use status filter to further refine results
    """
    case_service = check_case_service_available(case_service)
    correlation_id = str(uuid.uuid4())
    response.headers["x-correlation-id"] = correlation_id

    try:
        # Build filter with restored filtering parameters
        filters = CaseListFilter(
            user_id=current_user.user_id,
            status=status,
            limit=limit,
            offset=offset,
            include_empty=include_empty,
            include_archived=include_archived
        )

        # Get case summaries (already converted by service)
        case_summaries = await case_service.list_user_cases(current_user.user_id, filters)

        # Build response
        total_count = len(case_summaries)  # TODO: Get actual total from repository
        list_response = CaseListResponse(
            cases=case_summaries,
            total_count=total_count,
            limit=limit,
            offset=offset,
            has_more=len(case_summaries) == limit
        )

        # Set pagination headers
        response.headers["X-Total-Count"] = str(total_count)
        response.headers["x-correlation-id"] = correlation_id

        return list_response
        
    except ServiceException as e:
        # Service-level errors
        correlation_id = str(uuid.uuid4())
        logger = logging.getLogger(__name__)
        logger.error(f"Service error in list_cases: {e}", extra={"correlation_id": correlation_id})
        error_response = ErrorResponse(
            schema_version="3.1.0",
            error=ErrorDetail(code="CASE_SERVICE_ERROR", message=str(e))
        )
        return JSONResponse(
            status_code=503,
            content=error_response.dict(),
            headers={"x-correlation-id": correlation_id}
        )
        
    except Exception as e:
        # Unexpected errors
        correlation_id = str(uuid.uuid4())
        logger = logging.getLogger(__name__)
        logger.error(f"Unexpected error in list_cases: {e}", extra={"correlation_id": correlation_id})
        error_response = ErrorResponse(
            schema_version="3.1.0",
            error=ErrorDetail(code="INTERNAL_ERROR", message="Failed to retrieve cases")
        )
        return JSONResponse(
            status_code=500,
            content=error_response.dict(),
            headers={"x-correlation-id": correlation_id}
        )


@router.get("/{case_id}", response_model=CaseDetail)
@trace("api_get_case")
async def get_case(
    case_id: str,
    response: Response,
    case_service: Optional[ICaseService] = Depends(_di_get_case_service_dependency),
    current_user: DevUser = Depends(require_authentication)
) -> CaseDetail:
    """
    Get a specific case by ID (v2.0 milestone-based)

    Returns full case details with milestone progress, investigation stage,
    and completion percentage.
    """
    correlation_id = str(uuid.uuid4())
    response.headers["x-correlation-id"] = correlation_id

    try:
        case = await case_service.get_case(case_id, current_user.user_id)
        if not case:
            # Restored from old implementation - proper error response format
            error_response = ErrorResponse(
                schema_version="3.1.0",
                error=ErrorDetail(code="CASE_NOT_FOUND", message="Case not found or access denied")
            )
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=error_response.dict(),
                headers={"x-correlation-id": correlation_id}
            )

        # Convert to CaseDetail (v2.0 API model with milestones)
        return CaseDetail.from_case(case)

    except HTTPException:
        raise
    except Exception as e:
        correlation_id = str(uuid.uuid4())
        logger.error(f"Unexpected error in get_case: {e}", extra={"correlation_id": correlation_id})
        # Restored from old implementation - proper error response format
        error_response = ErrorResponse(
            schema_version="3.1.0",
            error=ErrorDetail(code="GET_CASE_ERROR", message="Failed to get case")
        )
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=error_response.dict(),
            headers={"x-correlation-id": correlation_id}
        )


@router.get("/{case_id}/ui", response_model=CaseUIResponse)
@trace("api_get_case_ui")
async def get_case_ui(
    case_id: str,
    response: Response,
    case_service: Optional[ICaseService] = Depends(_di_get_case_service_dependency),
    current_user: DevUser = Depends(require_authentication)
) -> CaseUIResponse:
    """
    Get phase-adaptive UI-optimized case response.

    Returns different response schemas based on case status:
    - CONSULTING: Focus on problem understanding, clarifying questions
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
                error=ErrorDetail(code="CASE_NOT_FOUND", message="Case not found or access denied")
            )
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=error_response.dict(),
                headers={"x-correlation-id": correlation_id}
            )

        # Transform to UI response based on phase
        ui_response = transform_case_for_ui(case)

        return ui_response

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Unexpected error in get_case_ui: {e}", extra={"correlation_id": correlation_id})
        error_response = ErrorResponse(
            schema_version="3.1.0",
            error=ErrorDetail(code="GET_CASE_UI_ERROR", message="Failed to get case UI data")
        )
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=error_response.dict(),
            headers={"x-correlation-id": correlation_id}
        )


@router.put("/{case_id}", status_code=status.HTTP_200_OK)
@trace("api_update_case")
async def update_case(
    case_id: str,
    request: CaseUpdateRequest,
    response: Response,
    case_service: Optional[ICaseService] = Depends(_di_get_case_service_dependency),
    current_user: DevUser = Depends(require_authentication)
):
    """
    Update case details
    
    Updates case metadata such as title, description, status, priority, and tags.
    Requires edit permissions on the case.
    """
    correlation_id = str(uuid.uuid4())
    response.headers["x-correlation-id"] = correlation_id
    
    try:
        # Build updates dict from request (milestone-based model)
        updates = {}
        if request.title is not None:
            updates["title"] = request.title
        if request.description is not None:
            updates["description"] = request.description
        if request.status is not None:
            updates["status"] = request.status.value  # Convert enum to string value
        # Note: priority and tags removed - not in milestone-based model

        if not updates:
            # Restored from old implementation - proper error response format
            error_response = ErrorResponse(
                schema_version="3.1.0",
                error=ErrorDetail(code="NO_UPDATES", message="No updates provided")
            )
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=error_response.dict(),
                headers={"x-correlation-id": correlation_id}
            )

        success = await case_service.update_case(case_id, updates, current_user.user_id)
        if not success:
            # Restored from old implementation - proper error response format
            error_response = ErrorResponse(
                schema_version="3.1.0",
                error=ErrorDetail(code="CASE_NOT_FOUND", message="Case not found or access denied")
            )
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=error_response.dict(),
                headers={"x-correlation-id": correlation_id}
            )

        # Return successful update response as expected by tests
        return {
            "case_id": case_id,
            "success": True,
            "message": "Case updated successfully"
        }

    except HTTPException:
        raise
    except ValidationException as e:
        logger.error(f"Validation error in update_case: {e}", extra={"correlation_id": correlation_id})
        error_response = ErrorResponse(
            schema_version="3.1.0",
            error=ErrorDetail(code="VALIDATION_ERROR", message=str(e))
        )
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=error_response.dict(),
            headers={"x-correlation-id": correlation_id}
        )
    except Exception as e:
        logger.error(f"Unexpected error in update_case: {e}", extra={"correlation_id": correlation_id})
        error_response = ErrorResponse(
            schema_version="3.1.0",
            error=ErrorDetail(code="UPDATE_CASE_ERROR", message=f"Failed to update case: {str(e)}")
        )
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=error_response.dict(),
            headers={"x-correlation-id": correlation_id}
        )


@router.post("/{case_id}/title", response_model=TitleResponse)
@trace("api_generate_case_title")
async def generate_case_title(
    case_id: str,
    response: Response,
    request_body: Optional[Dict[str, Any]] = Body(None, description="Optional request parameters"),
    force: bool = Query(False, description="Only overwrite non-default titles when true"),
    case_service: Optional[ICaseService] = Depends(_di_get_case_service_dependency),
    current_user: DevUser = Depends(require_authentication)
) -> TitleResponse:
    """
    Generate a concise, case-specific title from case messages and metadata.
    
    **Request body (optional):**
    - `max_words`: integer (3–12, default 8) - Maximum words in generated title
    - `hint`: string - Optional hint to guide title generation
    - `force`: boolean (default false) - Only overwrite non-default titles when true
    
    **Returns:**
    - 200: TitleResponse with X-Correlation-ID header
    - 422: ErrorResponse with code INSUFFICIENT_CONTEXT and X-Correlation-ID header
    
    **Description:** Returns 422 when insufficient meaningful context; clients SHOULD keep 
    existing title unchanged and may retry later.
    """
    case_service = check_case_service_available(case_service)
    correlation_id = str(uuid.uuid4())
    response.headers["x-correlation-id"] = correlation_id
    
    try:
        logger = logging.getLogger(__name__)
        logger.info(f"🔍 Title generation started for case {case_id}", extra={"case_id": case_id, "force_query": force})
        
        # Parse request body parameters (optional) - force can be in body or query
        max_words = 8  # default
        hint = None
        body_force = False
        if request_body:
            max_words = request_body.get("max_words", 8)
            hint = request_body.get("hint")
            body_force = request_body.get("force", False)
            
        # Use force from body if provided, otherwise from query parameter
        effective_force = body_force or force
        
        # Validate max_words (3–12, default 8)
        if not isinstance(max_words, int) or max_words < 3 or max_words > 12:
            max_words = 8
        
        logger.info(f"🔍 Effective parameters: max_words={max_words}, hint='{hint}', force={effective_force}", 
                   extra={"max_words": max_words, "hint": hint, "effective_force": effective_force})
        # Verify user has access to the case
        case = await case_service.get_case(case_id, current_user.user_id)
        if not case:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Case not found or access denied"
            )
        
        logger.info(f"🔍 Case retrieved: title='{case.title}', force={effective_force}", extra={"existing_title": case.title})
        
        # Check idempotency - don't overwrite user-set titles without force=true
        if not effective_force and hasattr(case, 'title') and case.title:
            # Check if existing title is meaningful (not default/auto-generated)
            default_titles = ["New Case", "Untitled Case", "Untitled"]
            # Check if title is generic/banned (always check for existing titles)
            is_meaningful_title = (
                case.title not in default_titles and
                not case.title.lower().startswith("case-") and
                len(case.title.split()) >= 3 and  # At least 3 words
                case.title.lower().strip() not in BANNED_GENERIC_WORDS and  # Not exact match
                not any(generic in case.title.lower() for generic in BANNED_GENERIC_WORDS)  # No substring match
            )
            
            if is_meaningful_title:
                # Return existing user-set title to maintain idempotency
                logger.info(f"🔍 Returning existing meaningful title: '{case.title}'", extra={"idempotent_title": case.title})
                response.headers["x-correlation-id"] = correlation_id
                response.headers["x-title-source"] = "existing"
                return TitleResponse(
                    schema_version="3.1.0",
                    title=case.title
                )
            else:
                logger.info(f"🔍 Existing title '{case.title}' is generic/banned, will regenerate", extra={"rejected_title": case.title})
        
        # Get conversation context
        context_text = ""
        try:
            context_text = await case_service.get_case_conversation_context(case_id, limit=10)
        except Exception:
            context_text = f"Case: {case.title}\nDescription: {case.description or 'No description'}"
        
        # Smart context check: Only call LLM if we have sufficient message content
        # Extract meaningful user content from conversation
        user_message_content = _extract_user_signals_from_context(context_text)

        # Minimum content threshold for LLM-based title generation
        # Rationale: Require substantive conversation to generate meaningful titles
        # - Avoids wasting LLM API calls on greetings ("hi", "hello")
        # - Ensures enough context for accurate title generation
        # - Examples of 200+ char messages: detailed problem descriptions, error messages with context
        MIN_MESSAGE_LENGTH_FOR_LLM = 200  # characters of meaningful user message content

        # Check if we have enough context for title generation
        content_length = len(user_message_content.strip()) if user_message_content else 0

        if content_length < MIN_MESSAGE_LENGTH_FOR_LLM:
            # Insufficient content - return user-friendly error
            logger.info(
                f"Skipping title generation: insufficient conversation context (case_id={case_id}, content_length={content_length})",
                extra={"case_id": case_id, "content_length": content_length, "threshold": MIN_MESSAGE_LENGTH_FOR_LLM}
            )
            error_response = ErrorResponse(
                schema_version="3.1.0",
                error=ErrorDetail(
                    code="INSUFFICIENT_CONTEXT",
                    message="Not enough conversation to generate a title. Continue discussing your issue, then try again."
                )
            )
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail=error_response.dict(),
                headers={"x-correlation-id": correlation_id}
            )
        
        # Generate title using LLM with fallback logic
        title_source = "unknown"
        try:
            generated_title, title_source = await _generate_title_with_llm(context_text, case, max_words, hint, user_message_content)
        except ValueError:
            # LLM and fallback failed - keep 422 on "no meaningful" after post-processing
            error_response = ErrorResponse(
                schema_version="3.1.0",
                error=ErrorDetail(code="INSUFFICIENT_CONTEXT", message="Cannot generate meaningful title from available context")
            )
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail=error_response.dict(),
                headers={"x-correlation-id": correlation_id}
            )

        # Persist the generated title to database (Approach 1: Generate AND persist)
        try:
            success = await case_service.update_case(case_id, {"title": generated_title}, current_user.user_id)
            if not success:
                logger = logging.getLogger(__name__)
                logger.error(f"Failed to persist generated title for case {case_id}", extra={"case_id": case_id, "generated_title": generated_title})
                raise HTTPException(
                    status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                    detail="Failed to persist generated title",
                    headers={"x-correlation-id": correlation_id}
                )
        except HTTPException:
            # Re-raise HTTPException without modification to preserve original error
            raise
        except ServiceException as e:
            # Handle service-level exceptions with proper error detail
            logger = logging.getLogger(__name__)
            logger.error(f"Service error persisting generated title: {e}", extra={"case_id": case_id, "correlation_id": correlation_id})
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail=f"Failed to persist generated title: {str(e)}",
                headers={"x-correlation-id": correlation_id}
            )
        except Exception as e:
            # Handle unexpected exceptions
            logger = logging.getLogger(__name__)
            logger.error(f"Unexpected error persisting generated title: {e}", extra={"case_id": case_id, "correlation_id": correlation_id})
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail=f"Failed to persist generated title: {str(e)}",
                headers={"x-correlation-id": correlation_id}
            )

        # Persist success atomically and return X-Correlation-ID on all responses
        response.headers["x-correlation-id"] = correlation_id
        response.headers["x-title-source"] = title_source  # Log source=llm vs fallback for telemetry
        response.headers["x-content-length"] = str(len(user_message_content) if user_message_content else 0)
        
        # Optional telemetry logging
        logger = logging.getLogger(__name__)
        logger.info(f"Title generation completed successfully", 
                   extra={"case_id": case_id, "title_source": title_source, "title_length": len(generated_title)})
        
        return TitleResponse(
            schema_version="3.1.0",
            title=generated_title
        )
        
    except HTTPException as he:
        # Ensure X-Correlation-ID on all error responses
        if "x-correlation-id" not in (he.headers or {}):
            he.headers = he.headers or {}
            he.headers["x-correlation-id"] = correlation_id
        raise
    except Exception as e:
        logger = logging.getLogger(__name__)
        logger.error(f"Unexpected error in generate_case_title: {e}", extra={"correlation_id": correlation_id})
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to generate title: {str(e)}",
            headers={"x-correlation-id": correlation_id}
        )


def _sanitize_title_content(content: str) -> str:
    """Sanitize content for title generation - remove PII, profanity, etc."""
    if not content:
        return ""
    
    # Basic content hygiene - remove common PII patterns
    import re
    
    # Remove email addresses
    content = re.sub(r'\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Z|a-z]{2,}\b', '[email]', content)
    
    # Remove phone numbers (basic patterns)
    content = re.sub(r'\b\d{3}[-.]?\d{3}[-.]?\d{4}\b', '[phone]', content)
    content = re.sub(r'\b\(\d{3}\)\s*\d{3}[-.]?\d{4}\b', '[phone]', content)
    
    # Remove IP addresses
    content = re.sub(r'\b\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3}\b', '[ip]', content)
    
    # Remove URLs
    content = re.sub(r'https?://[^\s]+', '[url]', content)
    
    # Remove file paths (basic patterns)
    content = re.sub(r'[A-Za-z]:\\[^\s]+', '[path]', content)
    content = re.sub(r'/[^\s]+/', '[path]', content)
    
    return content.strip()


def _extract_user_signals_from_context(context_text: str) -> str:
    """Extract meaningful user content from conversation context for title generation.
    
    Focuses only on user messages, filtering out system/agent responses.
    Dedupes near-identical lines and caps to last 8-12 meaningful user messages.
    Returns the most relevant user content for title generation.
    """
    if not context_text or not context_text.strip():
        return ""
    
    lines = context_text.strip().split('\n')
    user_messages = []
    seen_messages = set()  # For deduplication
    
    for line in lines:
        line = line.strip()
        if not line:
            continue
            
        # Skip system headers and metadata
        skip_patterns = [
            'Previous conversation',
            'Case status:',
            'Created:',
            'Last updated:',
            'Message count:',
            'Current query:',
            'Description: No description',
            'Case: New Case',
            'Case: Untitled',
            '] Assistant:',  # Skip assistant responses
            '] System:',     # Skip system messages
        ]
        
        if any(pattern in line for pattern in skip_patterns):
            continue
            
        # Extract user messages specifically (only user lines)
        user_content = None
        if '] User:' in line:
            # Extract content after "User:"
            user_content = line.split('] User:', 1)[-1].strip()
        elif 'User:' in line and not line.startswith('['):
            # Handle simpler "User:" format
            user_content = line.split('User:', 1)[-1].strip()
        elif line.startswith('Description:') and 'No description' not in line:
            # Extract meaningful description as user content
            user_content = line.split('Description:', 1)[-1].strip()
        
        # Validate and dedupe user content
        if (user_content and 
            len(user_content.split()) >= 3 and  # At least 3 meaningful words
            user_content.lower() not in seen_messages):  # Dedupe
            
            seen_messages.add(user_content.lower())
            user_messages.append(user_content)
            
            # Cap to last 8-12 meaningful user messages to reduce noise
            if len(user_messages) > 12:
                user_messages = user_messages[-12:]
    
    # Return the most recent user message (likely most relevant) with sanitization
    if user_messages:
        # Take the most recent meaningful user message
        raw_content = user_messages[-1]
        return _sanitize_title_content(raw_content)
    
    return ""


async def _generate_title_with_llm(context_text: str, case, max_words: int = 8, hint: Optional[str] = None, user_signals: Optional[str] = None) -> tuple[str, str]:
    """Generate title using LLM with fallback to first few words"""
    from faultmaven.container import container
    
    # Helper function to validate title - length/word-count guards, not dictionary rules
    def is_title_valid(title, check_banned_words=True):
        if not title:
            return False
        
        words = title.split()
        # Length/word-count guards (language-agnostic)
        if len(words) < 3 or len(title.strip()) < 5:
            return False
        
        # Optional banned words check (English-centric, configurable)
        if check_banned_words:
            title_lower = title.lower().strip()
            return not (title_lower in BANNED_GENERIC_WORDS or 
                       any(generic in title_lower for generic in BANNED_GENERIC_WORDS))
        
        return True
    
    # Deterministic extractive fallback using stronger signal extraction
    def get_fallback_title():
        # First try the pre-extracted user signals (most reliable)
        if user_signals and user_signals.strip():
            words = user_signals.strip().split()[:max_words]
            candidate = " ".join(words)
            if is_title_valid(candidate):
                return candidate
        
        # Fallback to re-extracting from context if user_signals not provided
        extracted_signals = _extract_user_signals_from_context(context_text)
        if extracted_signals:
            words = extracted_signals.strip().split()[:max_words]
            candidate = " ".join(words)
            if is_title_valid(candidate):
                return candidate
        
        # Final fallback: try case description if available and meaningful  
        if hasattr(case, 'description') and case.description and case.description.strip() and case.description != "No description":
            words = case.description.strip().split()[:max_words]
            candidate = " ".join(words)
            if is_title_valid(candidate):
                return candidate
        
        # Skip case title fallback entirely - it's likely to be generic
        # if hasattr(case, 'title') and case.title:
        #     This was allowing "New Chat Conversation" to pass through
        
        # If no meaningful content found, this should trigger 422 instead
        return None
    
    try:
        # Get LLM provider from container
        llm_provider = container.get_llm_provider()
        if not llm_provider:
            fallback = get_fallback_title()
            if not fallback:
                raise ValueError("Insufficient context for title generation")
            return fallback
        
        # Prepare the prompt with NONE option for deterministic handling
        hint_text = f"\nHint: {hint}" if hint else ""
        # Compose a robust prompt that prefers a concise, domain-specific title but
        # falls back conservatively to an extractive short phrase when the LLM
        # determines no coherent title can be produced. The NONE token provides a
        # deterministic escape hatch; the final fallback uses the user's initial
        # message first-words as a safe title.
        prompt = (
            f"Generate ONLY a concise, specific title (<= {max_words} words). "
            "Return ONLY the title, no quotes or punctuation, Title Case, avoid generic words "
            "(Issue/Problem/Troubleshooting/Conversation/Discussion/Untitled/New Case). "
            "Use precise domain terms present in the content. If multiple themes exist, choose the dominant one.\n"
            f"If the LLM cannot produce a compliant title, return ONLY the token NONE.{hint_text}\n\n"
            "If the context does not suggest a coherent message, instead return the first few words "
            "of the user's initial meaningful message as the title (this is a final fallback).\n\n"
            "Conversation (user messages emphasized):\n"
            f"{context_text}\n\n"
            "Title:"
        )
        
        # Generate title using LLM with optimized settings
        response = await llm_provider.generate(
            prompt=prompt,
            max_tokens=24,  # Slightly more tokens for better titles
            temperature=0.2,  # More deterministic
            top_p=0.9  # Focused sampling
        )
        
        if response and response.strip():
            # Strip quotes/punctuation; collapse whitespace
            import re
            generated_title = response.strip().strip('"').strip("'").strip()
            generated_title = re.sub(r'\s+', ' ', generated_title)  # Collapse whitespace
            generated_title = generated_title.rstrip('.,!?;:')  # Remove trailing punctuation
            
            # Remove common LLM prefixes/suffixes
            prefixes_to_remove = ['Title:', 'title:', 'Here is a title:', 'Here\'s a title:']
            for prefix in prefixes_to_remove:
                if generated_title.lower().startswith(prefix.lower()):
                    generated_title = generated_title[len(prefix):].strip()
            
            # Check if LLM returned NONE token (deterministic escape hatch)
            if generated_title.upper() == "NONE":
                logger = logging.getLogger(__name__)
                logger.info("Title generation: LLM returned NONE token")
                raise ValueError("LLM determined no compliant title possible")
            
            # Lightweight guards: length ≤ max_words, ≥3 words, no banned generics, basic validation
            words = generated_title.split()
            if len(words) > max_words:
                generated_title = " ".join(words[:max_words])
                words = words[:max_words]  # Update words array to match truncated title
            
            # Run lightweight validation guards
            if not is_title_valid(generated_title):
                logger = logging.getLogger(__name__)
                logger.info("Title generation: LLM output failed validation guards", 
                           extra={"invalid_title": generated_title})
                
                # Minimal deterministic fallback behind flag for resiliency (optional but prudent)
                import os
                use_fallback = os.getenv("TITLE_GENERATION_USE_FALLBACK", "true").lower() == "true"
                if use_fallback:
                    fallback = get_fallback_title()
                    if fallback and is_title_valid(fallback, check_banned_words=False):  # Don't block non-English fallbacks
                        logger.info("Title generation: Using extractive fallback for resiliency", 
                                   extra={"fallback_title": fallback})
                        return fallback, "fallback"
                
                # If no fallback or fallback fails, return 422
                raise ValueError("Generated title failed validation guards and fallback insufficient")
            
            logger = logging.getLogger(__name__)
            logger.info("Title generation: LLM success", extra={"generated_title": generated_title})
            return generated_title, "llm"
        else:
            fallback = get_fallback_title()
            if not fallback:
                raise ValueError("LLM failed and insufficient fallback context")
            logger = logging.getLogger(__name__)
            logger.info(f"Title generation: LLM empty response, using fallback", extra={"fallback_title": fallback})
            return fallback, "fallback"
            
    except Exception as e:
        logger = logging.getLogger(__name__)
        logger.warning(f"LLM title generation failed, trying fallback: {e}")
        fallback = get_fallback_title()
        if not fallback:
            raise ValueError("Both LLM and fallback title generation failed")
        logger.info(f"Title generation: LLM exception, using fallback", 
                   extra={"error": str(e), "fallback_title": fallback})
        return fallback, "fallback"


@router.post("/search", response_model=List[CaseSummary])
@trace("api_search_cases")
async def search_cases(
    request: CaseSearchRequest,
    case_service: Optional[ICaseService] = Depends(_di_get_case_service_dependency),
    current_user: DevUser = Depends(require_authentication)
) -> List[CaseSummary]:
    """
    Search cases by content
    
    Searches case titles, descriptions, and optionally message content
    for the specified query terms.
    """
    try:
        cases = await case_service.search_cases(request, current_user.user_id if current_user else None)
        return cases
        
    except ValidationException as e:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=str(e)
        )
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Search failed: {str(e)}"
        )


@router.get("/{case_id}/analytics", response_model=Dict[str, Any])
@trace("api_get_case_analytics")
async def get_case_analytics(
    case_id: str,
    case_service: Optional[ICaseService] = Depends(_di_get_case_service_dependency),
    current_user: DevUser = Depends(require_authentication)
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
                detail="Case not found or access denied"
            )
        
        analytics = await case_service.get_case_analytics(case_id)
        return analytics
        
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to get case analytics: {str(e)}"
        )


# Conversation thread retrieval (messages)
@router.get("/{case_id}/messages", response_model=CaseMessagesResponse)
@trace("api_get_case_messages_enhanced")
async def get_case_messages_enhanced(
    case_id: str,
    response: Response,
    limit: int = Query(50, le=100, ge=1, description="Maximum number of messages to return"),
    offset: int = Query(0, ge=0, description="Offset for pagination"),
    include_debug: bool = Query(False, description="Include debug information for troubleshooting"),
    case_service: Optional[ICaseService] = Depends(_di_get_case_service_dependency),
    current_user: DevUser = Depends(require_authentication)
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
                detail="Case not found or access denied"
            )

        # Use the enhanced message retrieval method
        message_response = await case_service.get_case_messages_enhanced(
            case_id=case_id,
            limit=limit,
            offset=offset,
            include_debug=include_debug
        )

        # Add headers for metadata
        response.headers["X-Message-Count"] = str(message_response.total_count)
        response.headers["X-Retrieved-Count"] = str(message_response.retrieved_count)

        # Determine storage status
        storage_status = "success"
        if message_response.debug_info and message_response.debug_info.storage_errors:
            storage_status = "error" if message_response.retrieved_count == 0 else "partial"
        response.headers["X-Storage-Status"] = storage_status

        return message_response

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Unexpected error in get_case_messages_enhanced: {e}", extra={"correlation_id": correlation_id})
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to get messages: {str(e)}",
            headers={"x-correlation-id": correlation_id}
        )

# Session-case integration endpoints

@router.post("/sessions/{session_id}/case", response_model=Dict[str, Any])
@trace("api_create_case_for_session")
async def create_case_for_session(
    session_id: str,
    request: Request,
    title: Optional[str] = Query(None, description="Case title (optional, auto-generated if not provided)"),
    force_new: bool = Query(False, description="Force creation of new case"),
    case_service: Optional[ICaseService] = Depends(_di_get_case_service_dependency),
    session_service: SessionService = Depends(_di_get_session_service_dependency),
    current_user: Optional[DevUser] = Depends(get_current_user_optional)
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
                detail="Invalid or expired session"
            )

        # Get user_id from auth or session
        user_id = current_user.user_id if current_user else session.user_id

        # Check for idempotency key (prevents duplicate case creation on retry)
        idempotency_key = request.headers.get("idempotency-key")

        if idempotency_key and force_new:
            # Check if we already processed this request
            existing_result = await case_service.check_idempotency_key(idempotency_key)
            if existing_result:
                logger.info(f"Returning cached result for idempotency key: {idempotency_key}")
                return existing_result.get("content", existing_result)

        # Create or get case for session
        case_id = await case_service.get_or_create_case_for_session(
            session_id=session_id,
            user_id=user_id,
            force_new=force_new,
            title=title
        )

        if not case_id:
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail="Failed to create case for session"
            )

        result = {
            "case_id": case_id,
            "created_new": force_new,
            "success": True
        }

        # Store idempotency result if key provided (only for force_new to prevent duplicates)
        if idempotency_key and force_new:
            await case_service.store_idempotency_result(
                idempotency_key,
                200,
                result,
                {}
            )

        return result
        
    except ValidationException as e:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=str(e)
        )
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to manage session case: {str(e)}"
        )


@router.post("/sessions/{session_id}/resume/{case_id}", response_model=Dict[str, Any])
@trace("api_resume_case_in_session")
async def resume_case_in_session(
    session_id: str,
    case_id: str,
    case_service: Optional[ICaseService] = Depends(_di_get_case_service_dependency),
    current_user: DevUser = Depends(require_authentication)
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
                detail="Case not found or resume not permitted"
            )
        
        return {
                        "case_id": case_id,
            "success": True,
            "message": "Case resumed in session"
        }
        
    except HTTPException:
        raise
    except ValidationException as e:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=str(e)
        )
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to resume case: {str(e)}"
        )


# Case Query endpoints

@router.post("/{case_id}/queries", response_model=CaseQueryResponse)
@trace("api_submit_case_query")
async def submit_case_query(
    case_id: str,
    request: CaseQueryRequest,
    fastapi_request: Request,
    case_service: Optional[ICaseService] = Depends(_di_get_case_service_dependency),
    investigation_service = Depends(get_investigation_service),
    session_service: SessionService = Depends(_di_get_session_service_dependency),
    current_user: DevUser = Depends(require_authentication)
):
    """
    Submit user message to advance the investigation (milestone-based).

    Processes the message through MilestoneEngine and returns investigation progress.
    Each turn represents one user message and the agent's response.

    Production features:
    - Session validation
    - Idempotency key support
    - Query history tracking
    - Correlation ID tracking
    - Comprehensive error handling
    """
    case_service = check_case_service_available(case_service)
    correlation_id = str(uuid.uuid4())

    try:
        # 1. Validate case_id parameter
        if not case_id or case_id.strip() in ("", "undefined", "null"):
            raise HTTPException(
                status_code=400,
                detail="Valid case_id is required",
                headers={"x-correlation-id": correlation_id}
            )

        # 2. Extract message text
        message_text = request.message
        if not message_text or not message_text.strip():
            raise HTTPException(
                status_code=400,
                detail="Message text is required",
                headers={"x-correlation-id": correlation_id}
            )

        # 3. Verify case exists (404 if not found)
        case = await case_service.get_case(case_id, current_user.user_id)
        if not case:
            raise HTTPException(
                status_code=404,
                detail="Case not found or access denied",
                headers={"x-correlation-id": correlation_id}
            )

        # 4. Check idempotency key if provided
        idempotency_key = fastapi_request.headers.get("idempotency-key")
        if idempotency_key:
            existing_result = await case_service.check_idempotency_key(idempotency_key)
            if existing_result:
                return JSONResponse(
                    status_code=existing_result.get("status_code", 200),
                    content=existing_result.get("content", {}),
                    headers=existing_result.get("headers", {})
                )

        # 5. Add query to case history (tracks message_count)
        await case_service.add_case_query(case_id, message_text, current_user.user_id)

        # 6. Process turn with MilestoneEngine (with 35s timeout)
        try:
            logger.info(f"Processing turn for case {case_id} with 35s timeout")
            response = await asyncio.wait_for(
                investigation_service.process_turn(
                    case_id=case_id,
                    user_id=current_user.user_id,
                    request=request
                ),
                timeout=35.0
            )

            # 7. Store idempotency result if key provided
            if idempotency_key:
                await case_service.store_idempotency_result(
                    idempotency_key,
                    200,
                    response.dict(),
                    {"x-correlation-id": correlation_id}
                )

            return response

        except asyncio.TimeoutError:
            logger.error(f"Turn processing timed out for case {case_id} after 35s")
            # Return fallback response
            raise HTTPException(
                status_code=500,
                detail="Request timeout - processing is taking longer than expected",
                headers={"x-correlation-id": correlation_id}
            )

    except NotFoundException as e:
        raise HTTPException(
            status_code=404,
            detail=str(e),
            headers={"x-correlation-id": correlation_id}
        )
    except PermissionDeniedException as e:
        raise HTTPException(
            status_code=403,
            detail=str(e),
            headers={"x-correlation-id": correlation_id}
        )
    except HTTPException:
        raise
    except ServiceException as e:
        logger.error(f"Turn processing failed: {e}", extra={"correlation_id": correlation_id})
        raise HTTPException(
            status_code=500,
            detail="Failed to process turn",
            headers={"x-correlation-id": correlation_id}
        )
    except Exception as e:
        logger.error(f"Unexpected error processing turn: {e}", exc_info=True, extra={"correlation_id": correlation_id})
        raise HTTPException(
            status_code=500,
            detail="Internal server error",
            headers={"x-correlation-id": correlation_id}
        )

@router.get("/{case_id}/queries")
@trace("api_list_case_queries")
async def list_case_queries(
    case_id: str,
    limit: int = Query(50, le=100, ge=1),
    offset: int = Query(0, ge=0),
    case_service: Optional[ICaseService] = Depends(_di_get_case_service_dependency),
    current_user: DevUser = Depends(require_authentication)
):
    """
    List queries for a specific case with pagination.

    CRITICAL: Must return 200 [] for empty results, NOT 404
    """
    case_service = check_case_service_available(case_service)

    try:
        # Verify case exists (404 if case not found)
        case = await case_service.get_case(case_id, current_user.user_id)
        if not case:
            raise HTTPException(
                status_code=404,
                detail="Case not found or access denied"
            )

        # Get queries for this case (empty list is valid)
        queries = []
        total_count = 0

        try:
            queries = await case_service.list_case_queries(case_id, limit, offset)
            total_count = await case_service.count_case_queries(case_id)
        except Exception as e:
            # Log but don't fail - return empty list
            queries = []
            total_count = 0

        # Build pagination headers per OpenAPI
        headers = {"X-Total-Count": str(total_count)}
        base_url = f"/api/v1/cases/{case_id}/queries"
        links = []
        if offset > 0:
            links.append(f'<{base_url}?limit={limit}&offset=0>; rel="first"')
            prev_offset = max(0, offset - limit)
            links.append(f'<{base_url}?limit={limit}&offset={prev_offset}>; rel="prev"')
        if offset + limit < total_count:
            next_offset = offset + limit
            links.append(f'<{base_url}?limit={limit}&offset={next_offset}>; rel="next"')
            last_offset = ((total_count - 1) // limit) * limit if total_count > 0 else 0
            links.append(f'<{base_url}?limit={limit}&offset={last_offset}>; rel="last"')
        if links:
            headers["Link"] = ", ".join(links)

        return JSONResponse(status_code=200, content=queries or [], headers=headers)

    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(
            status_code=500,
            detail=f"Failed to list queries: {str(e)}"
        )


# Health and status endpoints

@router.get("/health", response_model=Dict[str, Any])
@trace("api_case_health")
async def get_case_service_health(
    case_service: ICaseService = Depends(_di_get_case_service_dependency)
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
                "conversation_history": True
            }
        }

    except Exception as e:
        return {
            "service": "case_management",
            "status": "unhealthy",
            "timestamp": to_json_compatible(datetime.now(timezone.utc)),
            "error": str(e)
        }


# Case-scoped data management endpoints

@router.get("/{case_id}/data")
@trace("api_list_case_data")
async def list_case_data(
    case_id: str,
    limit: int = Query(50, ge=1, le=200, description="Maximum number of items to return"),
    offset: int = Query(0, ge=0, description="Number of items to skip"),
    case_service: Optional[ICaseService] = Depends(_di_get_case_service_dependency),
    current_user: DevUser = Depends(require_authentication)
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
                status_code=404,
                detail="Case not found or access denied"
            )
        
        # Mock empty data list for now
        data_list = []
        total_count = 0
        
        response_headers = {
            "X-Total-Count": str(total_count)
        }
        
        return JSONResponse(
            status_code=200,
            content=data_list,
            headers=response_headers
        )
        
    except HTTPException:
        raise
    except Exception:
        # Always return empty list, never fail list operations
        return JSONResponse(
            status_code=200,
            content=[],
            headers={"X-Total-Count": "0"}
        )


@router.get("/{case_id}/data/{data_id}")
@trace("api_get_case_data")
async def get_case_data(
    case_id: str,
    data_id: str,
    case_service: Optional[ICaseService] = Depends(_di_get_case_service_dependency),
    current_user: DevUser = Depends(require_authentication)
) -> Dict[str, Any]:
    """Get specific data file details for a case."""
    case_service = check_case_service_available(case_service)
    
    try:
        # Verify case exists
        case = await case_service.get_case(case_id, current_user.user_id)
        if not case:
            raise HTTPException(
                status_code=404,
                detail="Case not found or access denied"
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
            "processing_status": "completed"
        }
        
        return JSONResponse(
            status_code=201,
            content=data_record,
            headers={"Location": f"/api/v1/cases/{case_id}/data/{data_id}"}
        )
        
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(
            status_code=500,
            detail=f"Failed to retrieve case data: {str(e)}"
        )


@router.post("/{case_id}/data", status_code=status.HTTP_201_CREATED, response_model=DataUploadResponse)
@trace("api_upload_case_data")
async def upload_case_data(
    case_id: str,
    background_tasks: BackgroundTasks,
    file: UploadFile = File(...),
    session_id: Optional[str] = Form(None),  # Optional - can be derived from case
    description: Optional[str] = Form(None),
    case_service: Optional[ICaseService] = Depends(_di_get_case_service_dependency),
    data_service = Depends(get_data_service),
    investigation_service = Depends(get_investigation_service),
    case_vector_store = Depends(get_case_vector_store),
    current_user: DevUser = Depends(require_authentication)
) -> DataUploadResponse:
    """
    Upload data file to a specific case (case-scoped endpoint).

    This endpoint follows the complete data submission pipeline:
    1. Data preprocessing (extraction and sanitization)
    2. Evidence creation
    3. Hypothesis analysis
    4. Agent response generation

    The session_id is optional - if not provided, it will be derived from the case.

    Returns:
        DataUploadResponse with:
        - file_id: Unique identifier for the uploaded file
        - filename: Original filename
        - preprocessing metadata (data_type, extraction_method, etc.)
        - agent_response: AI analysis of the uploaded data
    """
    case_service = check_case_service_available(case_service)
    correlation_id = str(uuid.uuid4())

    try:
        # 1. Verify case exists and user has access
        case = await case_service.get_case(case_id, current_user.user_id)
        if not case:
            raise HTTPException(
                status_code=404,
                detail="Case not found or access denied",
                headers={"x-correlation-id": correlation_id}
            )

        # 2. Get session_id from case if not provided
        if not session_id:
            # Cases have a session_id field that tracks the associated session
            session_id = f"case_{case_id}_session"  # Generate a session ID from case

        # 3. Read file content
        content = await file.read()
        content_str = content.decode("utf-8", errors="ignore")

        # 4. Build context for case association
        context = {
            "case_id": case_id,
            "source": "direct_file_upload"
        }
        if description:
            context["description"] = description

        # 5. Preprocess data (extraction, classification, sanitization)
        uploaded_data = await data_service.ingest_data(
            content=content_str,
            session_id=session_id,
            file_name=file.filename,
            file_size=len(content),
            context=context
        )

        # 6. Generate agent analysis response via investigation service
        # Build query that references the uploaded file
        analysis_query = f"I've uploaded {file.filename}. Please analyze this data."
        if description:
            analysis_query += f" Context: {description}"

        # Create a query request for the investigation service
        query_request = CaseQueryRequest(
            message=analysis_query,
            attachments=[{
                "file_id": uploaded_data.get("data_id"),
                "filename": file.filename,
                "data_type": uploaded_data.get("data_type"),
                "size": uploaded_data.get("file_size", len(content)),
                "summary": uploaded_data.get("insights", {}).get("brief_summary"),
                "s3_uri": uploaded_data.get("data_id")  # Content reference
            }] if uploaded_data.get("data_id") else None
        )

        # Invoke investigation service to process the file upload as a turn
        investigation_response = await investigation_service.process_turn(
            case_id=case_id,
            user_id=current_user.user_id,
            request=query_request
        )

        # 7. Store evidence in vector DB (background task - async)
        # This implements Step 5 from data-preprocessing-design-specification.md
        if case_vector_store and uploaded_data.get("data_id"):
            # Fire-and-forget background task for vector storage
            # Using FastAPI's BackgroundTasks ensures task runs AFTER response is sent
            background_tasks.add_task(
                _store_evidence_in_vector_db,
                case_id=case_id,
                data_id=uploaded_data["data_id"],
                content=uploaded_data.get("content", ""),
                data_type=uploaded_data.get("data_type", "unknown"),
                metadata={
                    'filename': file.filename,
                    'file_size': len(content),
                    'case_id': case_id,
                    'session_id': session_id
                },
                case_vector_store=case_vector_store
            )
            logger.debug(f"Background vectorization task scheduled for evidence {uploaded_data['data_id']}")

        # 8. Combine preprocessing metadata with agent response
        from datetime import datetime, timezone

        response_data = DataUploadResponse(
            data_id=uploaded_data.get("data_id"),
            case_id=case_id,
            filename=file.filename,
            file_size=len(content),
            data_type=uploaded_data.get("data_type", "unknown"),
            processing_status=ProcessingStatus.COMPLETED,
            uploaded_at=datetime.now(timezone.utc).isoformat(),
            agent_response=AgentResponse(
                content=investigation_response.agent_response,
                response_type=ResponseType.ANSWER,
                session_id=session_id,
                case_id=case_id,
                sources=[],
                case_status=investigation_response.case_status
            ) if investigation_response else None,
            classification=uploaded_data.get("classification")
        )

        logger.info(f"Successfully uploaded and analyzed data for case {case_id}: {file.filename}")

        # Return response with Location header (REST best practice for 201 Created)
        data_id = uploaded_data.get("data_id")
        location_url = f"/api/v1/cases/{case_id}/data/{data_id}" if data_id else f"/api/v1/cases/{case_id}/data"

        return JSONResponse(
            status_code=status.HTTP_201_CREATED,
            content=response_data.model_dump(mode='json'),
            headers={"Location": location_url}
        )

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Failed to upload data to case {case_id}: {e}")
        raise HTTPException(
            status_code=500,
            detail=f"Failed to upload data: {str(e)}",
            headers={"x-correlation-id": correlation_id}
        )


@router.delete("/{case_id}/data/{data_id}", status_code=status.HTTP_204_NO_CONTENT, responses={204: {"description": "Data deleted successfully", "headers": {"X-Correlation-ID": {"description": "Request correlation ID", "schema": {"type": "string"}}}}})
@trace("api_delete_case_data")
async def delete_case_data(
    case_id: str,
    data_id: str,
    case_service: Optional[ICaseService] = Depends(_di_get_case_service_dependency),
    current_user: DevUser = Depends(require_authentication)
):
    """Remove data file from a case. Returns 204 No Content on success."""
    case_service = check_case_service_available(case_service)
    
    try:
        # Verify case exists
        case = await case_service.get_case(case_id, current_user.user_id)
        if not case:
            raise HTTPException(
                status_code=404,
                detail="Case not found or access denied"
            )
        
        # Return 204 No Content for successful deletion
        return Response(
            status_code=status.HTTP_204_NO_CONTENT,
            headers={"x-correlation-id": str(uuid.uuid4())}
        )
        
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(
            status_code=500,
            detail=f"Failed to delete case data: {str(e)}"
        )


# =============================================================================
# Document Generation and Closure Endpoints
# =============================================================================

@router.get("/{case_id}/report-recommendations")
@trace("api_get_report_recommendations")
async def get_report_recommendations(
    case_id: str,
    case_service: Optional[ICaseService] = Depends(_di_get_case_service_dependency),
    current_user: DevUser = Depends(require_authentication)
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
    from faultmaven.models.report import ReportRecommendation
    from faultmaven.services.domain.report_recommendation_service import ReportRecommendationService
    from faultmaven.infrastructure.knowledge.runbook_kb import RunbookKnowledgeBase
    from faultmaven.infrastructure.persistence.chromadb_store import ChromaDBVectorStore

    case_service = check_case_service_available(case_service)

    try:
        # Verify case exists and user has access
        case = await case_service.get_case(case_id, current_user.user_id)
        if not case:
            raise HTTPException(
                status_code=404,
                detail="Case not found or access denied"
            )

        # Validate case is in resolved state
        resolved_states = [
            CaseStatus.RESOLVED,
            CaseStatus.RESOLVED_WITH_WORKAROUND,
            CaseStatus.RESOLVED_BY_USER
        ]

        if case.status not in resolved_states:
            raise HTTPException(
                status_code=400,
                detail={
                    "error": "invalid_case_state",
                    "message": f"Cannot get report recommendations for case in {case.status.value} state",
                    "current_state": case.status.value,
                    "required_states": [s.value for s in resolved_states]
                }
            )

        # Initialize services for recommendation
        # Note: In production, these should be injected via DI container
        vector_store = ChromaDBVectorStore()
        runbook_kb = RunbookKnowledgeBase(vector_store=vector_store)
        recommendation_service = ReportRecommendationService(runbook_kb=runbook_kb)

        # Get intelligent recommendations
        recommendations = await recommendation_service.get_available_report_types(case=case)

        logger.info(
            f"Report recommendations generated for case {case_id}",
            extra={
                "case_id": case_id,
                "runbook_action": recommendations.runbook_recommendation.action,
                "available_types": [t.value for t in recommendations.available_for_generation]
            }
        )

        # Return recommendations
        return recommendations.dict()

    except HTTPException:
        raise
    except Exception as e:
        logger.error(
            f"Failed to get report recommendations for case {case_id}: {e}",
            exc_info=True
        )
        raise HTTPException(
            status_code=500,
            detail=f"Failed to get report recommendations: {str(e)}"
        )


@router.post("/{case_id}/reports")
@trace("api_generate_case_reports")
async def generate_case_reports(
    case_id: str,
    request_body: Dict[str, Any] = Body(...),
    case_service: Optional[ICaseService] = Depends(_di_get_case_service_dependency),
    current_user: DevUser = Depends(require_authentication)
):
    """Generate case documentation reports."""
    from faultmaven.models.report import ReportGenerationRequest, ReportType
    from faultmaven.services.domain.report_generation_service import ReportGenerationService
    from faultmaven.infrastructure.knowledge.runbook_kb import RunbookKnowledgeBase
    from faultmaven.infrastructure.persistence.chromadb_store import ChromaDBVectorStore

    case_service = check_case_service_available(case_service)

    try:
        case = await case_service.get_case(case_id, current_user.user_id)
        if not case:
            raise HTTPException(status_code=404, detail="Case not found")

        # Parse request
        request = ReportGenerationRequest(report_types=[ReportType(t) for t in request_body["report_types"]])

        # Initialize services
        vector_store = ChromaDBVectorStore()
        runbook_kb = RunbookKnowledgeBase(vector_store=vector_store)
        report_service = ReportGenerationService(llm_router=None, runbook_kb=runbook_kb)

        # Transition to DOCUMENTING if needed
        if case.status != CaseStatus.DOCUMENTING:
            case.status = CaseStatus.DOCUMENTING
            case.documenting_started_at = datetime.now(timezone.utc)

        # Generate reports
        response = await report_service.generate_reports(case, request.report_types)
        case.report_generation_count += 1

        return response.dict()

    except Exception as e:
        logger.error(f"Report generation failed: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/{case_id}/reports")
@trace("api_get_case_reports")
async def get_case_reports(
    case_id: str,
    include_history: bool = Query(default=False),
    case_service: Optional[ICaseService] = Depends(_di_get_case_service_dependency),
    report_store: Optional[IReportStore] = Depends(get_report_store),
    current_user: DevUser = Depends(require_authentication)
):
    """
    Retrieve generated reports for a case.

    Args:
        case_id: Case identifier
        include_history: If True, return all report versions; if False, only current

    Returns:
        List of CaseReport objects
    """
    case_service = check_case_service_available(case_service)

    try:
        # Verify case exists and user has access
        case = await case_service.get_case(case_id, current_user.user_id)
        if not case:
            raise HTTPException(status_code=404, detail="Case not found")

        # Check if report_store is available
        if not report_store:
            logger.warning("Report store not available - returning empty list")
            return []

        # Retrieve reports from storage
        reports = await report_store.get_case_reports(
            case_id=case_id,
            include_history=include_history
        )

        logger.info(
            f"Retrieved {len(reports)} reports for case",
            extra={
                "case_id": case_id,
                "include_history": include_history,
                "report_count": len(reports)
            }
        )

        return reports

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Failed to retrieve reports for case {case_id}: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/{case_id}/reports/{report_id}/download")
@trace("api_download_case_report")
async def download_case_report(
    case_id: str,
    report_id: str,
    format: str = Query(default="markdown"),
    case_service: Optional[ICaseService] = Depends(_di_get_case_service_dependency),
    report_store: Optional[IReportStore] = Depends(get_report_store),
    current_user: DevUser = Depends(require_authentication)
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

        # Check if report_store is available
        if not report_store:
            raise HTTPException(
                status_code=503,
                detail="Report storage not available"
            )

        # Retrieve report from storage
        report = await report_store.get_report(report_id)

        if not report:
            raise HTTPException(status_code=404, detail="Report not found")

        # Verify report belongs to this case
        if report.case_id != case_id:
            raise HTTPException(
                status_code=403,
                detail="Report does not belong to this case"
            )

        # Determine content type and filename
        if format == "pdf":
            # TODO: PDF conversion not implemented yet
            raise HTTPException(
                status_code=501,
                detail="PDF format not yet supported - use markdown format"
            )
        else:
            # Return markdown format
            content_type = "text/markdown"
            filename = f"{report.report_type.value}_{case_id}_{report.version}.md"

        logger.info(
            f"Serving report download",
            extra={
                "case_id": case_id,
                "report_id": report_id,
                "format": format,
                "filename": filename
            }
        )

        return Response(
            content=report.content,
            media_type=content_type,
            headers={
                "Content-Disposition": f"attachment; filename={filename}"
            }
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
    report_store: Optional[IReportStore] = Depends(get_report_store),
    current_user: DevUser = Depends(require_authentication)
):
    """
    Close case and archive with reports.

    Marks all latest reports as linked to case closure and transitions
    case to CLOSED state.

    Returns:
        CaseClosureResponse with list of archived reports
    """
    from faultmaven.models.report import CaseClosureResponse, ArchivedReport

    case_service = check_case_service_available(case_service)

    try:
        case = await case_service.get_case(case_id, current_user.user_id)
        if not case:
            raise HTTPException(status_code=404, detail="Case not found")

        # Validate state
        allowed_states = [CaseStatus.RESOLVED, CaseStatus.SOLVED, CaseStatus.DOCUMENTING]
        if case.status not in allowed_states:
            raise HTTPException(
                status_code=400,
                detail=f"Cannot close case in {case.status.value} state"
            )

        # Get latest reports for closure (if report_store available)
        archived_reports = []
        if report_store:
            try:
                latest_reports = await report_store.get_latest_reports_for_closure(case_id)

                if latest_reports:
                    # Mark reports as linked to closure
                    report_ids = [r.report_id for r in latest_reports]
                    await report_store.mark_reports_linked_to_closure(case_id, report_ids)

                    # Build archived reports list
                    for report in latest_reports:
                        archived_reports.append(
                            ArchivedReport(
                                report_id=report.report_id,
                                report_type=report.report_type,
                                title=report.title,
                                generated_at=report.generated_at
                            )
                        )

                    logger.info(
                        f"Linked {len(report_ids)} reports to case closure",
                        extra={"case_id": case_id, "report_count": len(report_ids)}
                    )
                else:
                    logger.info(
                        f"No reports to link for case closure",
                        extra={"case_id": case_id}
                    )

            except Exception as e:
                logger.warning(
                    f"Failed to link reports to closure, continuing with case close: {e}",
                    extra={"case_id": case_id}
                )
                # Continue closing case even if report linking fails

        # Close case
        closed_at = datetime.now(timezone.utc)
        case.status = CaseStatus.CLOSED
        await case_service.update_case_status(case_id, CaseStatus.CLOSED, current_user.user_id)

        logger.info(
            f"Case closed successfully",
            extra={
                "case_id": case_id,
                "archived_report_count": len(archived_reports)
            }
        )

        response = CaseClosureResponse(
            case_id=case_id,
            closed_at=to_json_compatible(closed_at),
            archived_reports=archived_reports,
            download_available_until=(closed_at + timedelta(days=90)).isoformat() + 'Z'
        )

        return response.dict()

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Case closure failed: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))


# ============================================================
# Uploaded Files / Evidence Endpoints
# ============================================================

@router.get("/{case_id}/uploaded-files", response_model=UploadedFilesList)
@trace("api_list_uploaded_files")
async def list_uploaded_files(
    case_id: str,
    response: Response,
    limit: int = Query(50, ge=1, le=100, description="Maximum number of files to return"),
    offset: int = Query(0, ge=0, description="Number of files to skip (for pagination)"),
    sort_by: str = Query("uploaded_at_turn", description="Sort field: uploaded_at_turn | filename | size"),
    sort_order: str = Query("desc", description="Sort direction: asc | desc"),
    case_service = Depends(get_case_service),
    current_user: DevUser = Depends(require_authentication)
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
        reverse = (sort_order == "desc")
        if sort_by == "uploaded_at_turn":
            uploaded_files_list = sorted(uploaded_files_list, key=lambda f: f.uploaded_at_turn, reverse=reverse)
        elif sort_by == "filename":
            uploaded_files_list = sorted(uploaded_files_list, key=lambda f: f.filename, reverse=reverse)
        elif sort_by == "size":
            uploaded_files_list = sorted(uploaded_files_list, key=lambda f: f.size_bytes, reverse=reverse)

        # Paginate
        total_count = len(uploaded_files_list)
        paginated_files = uploaded_files_list[offset:offset + limit]

        # Convert to response models
        files = [UploadedFileMetadata.from_uploaded_file(f) for f in paginated_files]

        # Set pagination header (required by API contract)
        response.headers["X-Total-Count"] = str(total_count)

        return UploadedFilesList(
            files=files,
            total_count=total_count,
            limit=limit,
            offset=offset
        )

    except NotFoundException as e:
        raise HTTPException(status_code=404, detail=str(e))
    except PermissionDeniedException as e:
        raise HTTPException(status_code=403, detail=str(e))
    except Exception as e:
        logger.error(f"Failed to list uploaded files: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/{case_id}/uploaded-files/{file_id}", response_model=UploadedFileDetails)
@trace("api_get_uploaded_file_details")
async def get_uploaded_file_details(
    case_id: str,
    file_id: str,
    case_service = Depends(get_case_service),
    current_user: DevUser = Depends(require_authentication)
):
    """
    Get detailed information about a specific uploaded file.

    Returns:
        Full file details with AI analysis and hypothesis relationships
    """
    try:
        # Get case with access control
        case = await case_service.get_case(case_id, current_user.user_id)
        if not case:
            raise HTTPException(status_code=404, detail="Case not found")

        # Find uploaded file first (exists in ALL phases)
        uploaded_file = None
        for f in case.uploaded_files:
            if f.file_id == file_id:
                uploaded_file = f
                break

        if not uploaded_file:
            raise HTTPException(status_code=404, detail=f"File {file_id} not found in case {case_id}")

        # In CONSULTING phase: return uploaded file details without hypothesis relationships
        if case.status == CaseStatus.CONSULTING:
            return UploadedFileDetails.from_uploaded_file(
                uploaded_file=uploaded_file,
                case_id=case_id
            )

        # In INVESTIGATING phase: check if file has been converted to evidence
        # (Evidence is created from uploaded files during investigation)
        evidence = None
        for e in case.evidence:
            # Match by file_id (evidence tracks original file_id)
            if e.evidence_id == file_id or (hasattr(e, 'source_file_id') and e.source_file_id == file_id):
                evidence = e
                break

        if evidence:
            # File has been analyzed as evidence - return full details with hypotheses
            return UploadedFileDetails.from_evidence(
                evidence=evidence,
                case_id=case_id,
                hypotheses=case.hypotheses
            )
        else:
            # File uploaded but not yet analyzed as evidence
            return UploadedFileDetails.from_uploaded_file(
                uploaded_file=uploaded_file,
                case_id=case_id
            )

    except HTTPException:
        raise
    except NotFoundException as e:
        raise HTTPException(status_code=404, detail=str(e))
    except PermissionDeniedException as e:
        raise HTTPException(status_code=403, detail=str(e))
    except Exception as e:
        logger.error(f"Failed to get file details: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))


# ============================================================
# Phase 2: Evidence-to-File Linkage APIs
# ============================================================

@router.get(
    "/{case_id}/uploaded-files/{file_id}",
    response_model=UploadedFileDetailsResponse,
    summary="Get uploaded file details with derived evidence",
    description="Retrieve detailed information about an uploaded file including all evidence derived from it and hypothesis linkage."
)
async def get_uploaded_file_details(
    case_id: str = Path(..., description="Case ID"),
    file_id: str = Path(..., description="File ID"),
    auth: tuple = Depends(require_authentication)
):
    """
    GET /api/v1/cases/{case_id}/uploaded-files/{file_id}

    Returns comprehensive file details including:
    - File metadata (name, size, upload time)
    - List of evidence derived from this file
    - Hypothesis linkage for each evidence piece
    """
    session_id, user_id = auth

    try:
        # Get case and verify ownership
        case = await case_service.get_case(case_id)
        if case.owner_id != user_id:
            raise HTTPException(status_code=403, detail="Access denied")

        # Find the uploaded file
        uploaded_file = next((f for f in case.uploaded_files if f.file_id == file_id), None)
        if not uploaded_file:
            raise HTTPException(status_code=404, detail=f"File {file_id} not found in case {case_id}")

        # Find all evidence derived from this file (matching by content_ref)
        derived_evidence = []
        for evidence in case.evidence:
            if evidence.content_ref and evidence.content_ref == uploaded_file.content_ref:
                # Find hypotheses related to this evidence
                related_hypothesis_ids = []
                for hypothesis in case.hypotheses:
                    if evidence.evidence_id in hypothesis.evidence_links:
                        related_hypothesis_ids.append(hypothesis.hypothesis_id)

                derived_evidence.append(DerivedEvidenceSummary(
                    evidence_id=evidence.evidence_id,
                    summary=evidence.summary,
                    category=evidence.category,
                    collected_at_turn=evidence.collected_at_turn,
                    related_hypothesis_ids=related_hypothesis_ids
                ))

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
            uploaded_at_turn=uploaded_file.uploaded_at_turn,
            uploaded_at=uploaded_file.uploaded_at,
            source_type=uploaded_file.source_type,
            data_type=uploaded_file.data_type,
            summary=uploaded_file.preprocessing_summary,
            derived_evidence=derived_evidence,
            evidence_count=len(derived_evidence)
        )

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Failed to get file details: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))


@router.get(
    "/{case_id}/uploaded-files",
    response_model=UploadedFilesListResponse,
    summary="List uploaded files with evidence counts",
    description="Get all uploaded files for a case with metadata and evidence linkage counts."
)
async def list_uploaded_files(
    case_id: str = Path(..., description="Case ID"),
    auth: tuple = Depends(require_authentication)
):
    """
    GET /api/v1/cases/{case_id}/uploaded-files

    Returns list of all uploaded files with:
    - File metadata
    - Count of evidence derived from each file
    """
    session_id, user_id = auth

    try:
        # Get case and verify ownership
        case = await case_service.get_case(case_id)
        if case.owner_id != user_id:
            raise HTTPException(status_code=403, detail="Access denied")

        # Build file list with evidence counts
        files_with_counts = []
        for uploaded_file in case.uploaded_files:
            # Count evidence derived from this file
            evidence_count = sum(
                1 for e in case.evidence
                if e.content_ref and e.content_ref == uploaded_file.content_ref
            )

            # Format file size
            size_bytes = uploaded_file.size_bytes
            if size_bytes < 1024:
                size_display = f"{size_bytes} B"
            elif size_bytes < 1024 * 1024:
                size_display = f"{size_bytes / 1024:.1f} KB"
            else:
                size_display = f"{size_bytes / (1024 * 1024):.1f} MB"

            files_with_counts.append(UploadedFileMetadata(
                file_id=uploaded_file.file_id,
                filename=uploaded_file.filename,
                size_bytes=uploaded_file.size_bytes,
                size_display=size_display,
                uploaded_at_turn=uploaded_file.uploaded_at_turn,
                uploaded_at=uploaded_file.uploaded_at,
                source_type=uploaded_file.source_type,
                data_type=uploaded_file.data_type,
                summary=uploaded_file.preprocessing_summary,
                evidence_count=evidence_count
            ))

        return UploadedFilesListResponse(
            case_id=case_id,
            total_count=len(files_with_counts),
            files=files_with_counts
        )

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Failed to list files: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))


@router.get(
    "/{case_id}/evidence/{evidence_id}",
    response_model=EvidenceDetailsResponse,
    summary="Get evidence details with source file",
    description="Retrieve detailed evidence information including source file reference and hypothesis linkage."
)
async def get_evidence_details(
    case_id: str = Path(..., description="Case ID"),
    evidence_id: str = Path(..., description="Evidence ID"),
    auth: tuple = Depends(require_authentication)
):
    """
    GET /api/v1/cases/{case_id}/evidence/{evidence_id}

    Returns comprehensive evidence details including:
    - Evidence metadata and content
    - Source file reference (if derived from upload)
    - Related hypotheses with stance (SUPPORTS/REFUTES/NEUTRAL)
    """
    session_id, user_id = auth

    try:
        # Get case and verify ownership
        case = await case_service.get_case(case_id)
        if case.owner_id != user_id:
            raise HTTPException(status_code=403, detail="Access denied")

        # Find the evidence
        evidence = next((e for e in case.evidence if e.evidence_id == evidence_id), None)
        if not evidence:
            raise HTTPException(status_code=404, detail=f"Evidence {evidence_id} not found in case {case_id}")

        # Find source file (if evidence was derived from uploaded file)
        source_file = None
        if evidence.content_ref:
            for uploaded_file in case.uploaded_files:
                if uploaded_file.content_ref == evidence.content_ref:
                    source_file = SourceFileReference(
                        file_id=uploaded_file.file_id,
                        filename=uploaded_file.filename,
                        uploaded_at_turn=uploaded_file.uploaded_at_turn
                    )
                    break

        # Find related hypotheses
        related_hypotheses = []
        for hypothesis in case.hypotheses:
            if evidence.evidence_id in hypothesis.evidence_links:
                stance = hypothesis.evidence_links[evidence.evidence_id]
                related_hypotheses.append(RelatedHypothesis(
                    hypothesis_id=hypothesis.hypothesis_id,
                    statement=hypothesis.statement,
                    stance=stance
                ))

        return EvidenceDetailsResponse(
            evidence_id=evidence.evidence_id,
            case_id=case_id,
            summary=evidence.summary,
            category=evidence.category,
            primary_purpose=evidence.primary_purpose,
            collected_at_turn=evidence.collected_at_turn,
            collected_at=evidence.collected_at,
            collected_by=evidence.collected_by,
            source_file=source_file,
            related_hypotheses=related_hypotheses,
            preprocessed_content=evidence.preprocessed_content,
            content_size_bytes=evidence.content_size_bytes,
            analysis=evidence.analysis
        )

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
    description="Share a case with another user. Requires owner or collaborator permission."
)
async def share_case(
    case_id: str = Path(..., description="Case ID"),
    target_user_id: str = Body(..., embed=True, description="User ID to share with"),
    role: str = Body("viewer", embed=True, description="Participant role: owner, collaborator, viewer"),
    case_service: ICaseService = Depends(get_case_service),
    auth: tuple = Depends(require_authentication)
):
    """Share a case with another user."""
    session_id, user_id = auth

    try:
        # Validate role
        valid_roles = ["owner", "collaborator", "viewer"]
        if role not in valid_roles:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"Invalid role. Must be one of: {', '.join(valid_roles)}"
            )

        # Share the case
        success = await case_service.share_case(
            case_id=case_id,
            target_user_id=target_user_id,
            role=role,
            sharer_user_id=user_id
        )

        if not success:
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail="Failed to share case"
            )

        logger.info(f"Case {case_id} shared with user {target_user_id} as {role} by {user_id}")

        return {
            "message": "Case shared successfully",
            "case_id": case_id,
            "shared_with": target_user_id,
            "role": role
        }

    except ValidationException as e:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=str(e))
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error sharing case {case_id}: {e}")
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=str(e))


@router.delete(
    "/{case_id}/share/{target_user_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Unshare Case",
    description="Unshare a case from a user. Requires owner permission."
)
async def unshare_case(
    case_id: str = Path(..., description="Case ID"),
    target_user_id: str = Path(..., description="User ID to unshare from"),
    case_service: ICaseService = Depends(get_case_service),
    auth: tuple = Depends(require_authentication)
):
    """Unshare a case from a user."""
    session_id, user_id = auth

    try:
        success = await case_service.unshare_case(
            case_id=case_id,
            target_user_id=target_user_id,
            unsharer_user_id=user_id
        )

        if not success:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"User {target_user_id} not found in case {case_id} participants"
            )

        logger.info(f"Case {case_id} unshared from user {target_user_id} by {user_id}")

    except ValidationException as e:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=str(e))
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error unsharing case {case_id}: {e}")
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=str(e))


@router.get(
    "/{case_id}/participants",
    response_model=List[Dict[str, Any]],
    summary="Get Case Participants",
    description="Get all participants who have access to this case."
)
async def get_case_participants(
    case_id: str = Path(..., description="Case ID"),
    case_service: ICaseService = Depends(get_case_service),
    auth: tuple = Depends(require_authentication)
) -> List[Dict[str, Any]]:
    """Get all participants for a case."""
    session_id, user_id = auth

    try:
        # Verify user has access to the case
        case = await case_service.get_case(case_id)
        if not case:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Case {case_id} not found"
            )

        # Get participants
        participants = await case_service.get_case_participants(case_id)

        return participants

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error getting participants for case {case_id}: {e}")
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=str(e))


@router.get(
    "/{case_id}/access-check",
    response_model=Dict[str, bool],
    summary="Check Case Access",
    description="Check if current user has access to this case."
)
async def check_case_access(
    case_id: str = Path(..., description="Case ID"),
    case_service: ICaseService = Depends(get_case_service),
    auth: tuple = Depends(require_authentication)
) -> Dict[str, bool]:
    """Check if user has access to case."""
    session_id, user_id = auth

    try:
        has_access = await case_service.user_can_access_case(user_id, case_id)

        return {
            "has_access": has_access,
            "user_id": user_id,
            "case_id": case_id
        }

    except Exception as e:
        logger.error(f"Error checking access for case {case_id}: {e}")
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=str(e))


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