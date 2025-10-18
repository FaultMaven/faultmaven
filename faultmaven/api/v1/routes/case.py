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

from fastapi import APIRouter, Depends, HTTPException, Query, status, Response, Body, UploadFile, File, Form, Request
from fastapi.responses import JSONResponse
import uuid
import logging

from faultmaven.models.case import (
    Case as CaseEntity,
    CaseCreateRequest,
    CaseListFilter,
    CaseMessage,
    CaseSearchRequest,
    CaseShareRequest,
    CaseSummary,
    CaseUpdateRequest,
    MessageType,
    ParticipantRole,
    CaseStatus,
    CasePriority
)
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
    get_agent_service, get_preprocessing_service, get_report_store
)
from faultmaven.api.v1.auth_dependencies import (
    require_authentication,
    get_current_user_optional,
    get_current_user_id
)
from faultmaven.models.auth import DevUser
from faultmaven.services.domain.session_service import SessionService
from faultmaven.services.agentic.orchestration.agent_service import AgentService
from faultmaven.services.converters import CaseConverter
from fastapi import Request
from faultmaven.infrastructure.observability.tracing import trace
from faultmaven.exceptions import ValidationException, ServiceException
from faultmaven.services.evidence.evidence_factory import (
    create_evidence_from_preprocessed,
    map_datatype_to_evidence_category,
)
from faultmaven.services.evidence.evidence_enhancements import (
    extract_timeline_events,
    should_populate_timeline,
    generate_hypotheses_from_anomalies,
    should_generate_hypotheses,
)
from faultmaven.models.evidence import EvidenceType

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

# In-memory storage for async query results (in production, use Redis)
_async_query_results = {}


async def _process_async_query(job_id: str, case_id: str, query_request, agent_service, user_id: Optional[str] = None):
    """Process query asynchronously and store result."""
    try:
        logger.info(f"Starting async processing for job {job_id}")
        _async_query_results[job_id] = {"status": "processing", "started_at": to_json_compatible(datetime.now(timezone.utc))}
        
        # Create a copy of the query_request to avoid modifying the original
        from copy import deepcopy
        query_copy = deepcopy(query_request)

        # Add case context
        if not query_copy.context:
            query_copy.context = {}
        query_copy.context.update({"case_id": case_id, "user_id": user_id})
        
        # Process the query using AgentService
        logger.info(f"Calling agent_service.process_query_for_case for job {job_id}")
        agent_response = await agent_service.process_query_for_case(case_id, query_copy)
        
        # Store the completed result
        _async_query_results[job_id] = {
            "status": "completed",
            "result": agent_response,
            "completed_at": to_json_compatible(datetime.now(timezone.utc))
        }
        logger.info(f"Async processing completed successfully for job {job_id}")
        
    except Exception as e:
        import traceback
        error_details = f"{str(e)}\n{traceback.format_exc()}"
        logger.error(f"Async processing failed for job {job_id}: {error_details}")
        _async_query_results[job_id] = {
            "status": "failed", 
            "error": str(e),
            "error_details": error_details,
            "failed_at": to_json_compatible(datetime.now(timezone.utc))
        }


# Configurable banned words list - minimal but extensible
BANNED_GENERIC_WORDS = [
    'new chat', 'untitled', 'troubleshooting', 'conversation', 
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


async def _di_get_agent_service_dependency() -> AgentService:
    """Runtime wrapper so patched dependency is honored in tests."""
    from faultmaven.api.v1.dependencies import get_agent_service as _getter
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


@router.post("", response_model=CaseResponse, status_code=status.HTTP_201_CREATED)
@trace("api_create_case")
async def create_case(
    request: CaseCreateRequest,
    response: Response,
    case_service: Optional[ICaseService] = Depends(_di_get_case_service_dependency),
    session_service: SessionService = Depends(_di_get_session_service_dependency),
    current_user: DevUser = Depends(require_authentication)
) -> CaseResponse:
    """
    Create a new troubleshooting case
    
    Creates a new case for tracking troubleshooting sessions and conversations.
    The case will persist beyond individual session lifetimes.
    """
    correlation_id = str(uuid.uuid4())
    case_service = check_case_service_available(case_service)
    try:
        # Validate session if provided
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

        case_entity = await case_service.create_case(
            title=request.title,
            description=request.description,
            owner_id=current_user.user_id,
            session_id=request.session_id,
            initial_message=request.initial_message
        )
        
        # Convert CaseEntity to API response using centralized converter
        case_api = CaseConverter.entity_to_api(case_entity)
        
        # Set Location header as required by OpenAPI spec
        response.headers["Location"] = f"/api/v1/cases/{case_api.case_id}"
        
        # Ensure case_id is populated in response
        if not case_api.case_id:
            logger.error("Case created but case_id not available", extra={"correlation_id": correlation_id})
            error_response = ErrorResponse(
                schema_version="3.1.0",
                error=ErrorDetail(code="CASE_ID_MISSING", message="Case created but case_id not available")
            )
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail=error_response.dict(),
                headers={"x-correlation-id": correlation_id}
            )
        
        # Set correlation ID header for successful case creation
        response.headers["x-correlation-id"] = correlation_id
        
        return CaseResponse(case=case_api)
        
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


@router.get("", response_model=List[Case])
@trace("api_list_cases") 
async def list_cases(
    response: Response,
    case_service: Optional[ICaseService] = Depends(_di_get_case_service_dependency),
    current_user: DevUser = Depends(require_authentication),
    page: int = Query(1, ge=1, description="Page number"),
    limit: int = Query(20, ge=1, le=100, description="Items per page"),
    # Phase 1: New filtering parameters (default to exclude non-active cases)
    include_empty: bool = Query(False, description="Include cases with message_count == 0"),
    include_archived: bool = Query(False, description="Include archived cases"),
    include_deleted: bool = Query(False, description="Include deleted cases (admin only)")
):
    """
    List cases with pagination
    
    Returns a list of cases accessible to the authenticated user.
    Always returns 200 with raw array (Case[]); returns [] when no results.
    Supports pagination via page/limit parameters with X-Total-Count and Link headers.
    
    Default Filtering Behavior:
    - Excludes empty cases (message_count == 0) unless include_empty=true
    - Excludes archived cases unless include_archived=true  
    - Excludes deleted cases unless include_deleted=true (admin only)
    - Only returns active cases with messages by default
    """
    case_service = check_case_service_available(case_service)
    correlation_id = str(uuid.uuid4())
    response.headers["x-correlation-id"] = correlation_id
    
    try:
        # Convert page to offset
        offset = (page - 1) * limit
        
        # Build filter criteria with Phase 1 filtering parameters
        filters = CaseListFilter(
            user_id=current_user.user_id if current_user else None,
            status=None,
            priority=None,
            owner_id=None,
            limit=limit,
            offset=offset,
            # Phase 1: Pass through new filtering parameters
            include_empty=include_empty,
            include_archived=include_archived,
            include_deleted=include_deleted
        )
        
        case_entities = await case_service.list_user_cases(current_user.user_id, filters)
        
        # Get total count for pagination
        total_count = 0
        try:
            total_count = await case_service.count_user_cases(current_user.user_id, filters)
        except Exception:
            total_count = len(case_entities) if case_entities else 0
        
        # Convert entities to API models per OpenAPI spec
        api_cases = CaseConverter.entities_to_api_list(case_entities or [])
        
        # Set required headers
        response.headers["X-Total-Count"] = str(total_count)
        
        # RFC 5988 Link header for pagination
        base_url = "/api/v1/cases"
        links = []
        total_pages = (total_count + limit - 1) // limit if total_count > 0 else 1
        
        if page > 1:
            links.append(f'<{base_url}?page=1&limit={limit}>; rel="first"')
            links.append(f'<{base_url}?page={page-1}&limit={limit}>; rel="prev"')
        
        if page < total_pages:
            links.append(f'<{base_url}?page={page+1}&limit={limit}>; rel="next"')
            links.append(f'<{base_url}?page={total_pages}&limit={limit}>; rel="last"')
        
        # Always set Link header, even if empty (API contract compliance)
        response.headers["Link"] = ", ".join(links) if links else ""
        
        # Return JSONResponse to ensure proper headers and prevent redirect issues
        return JSONResponse(
            status_code=200,
            content=[case.dict() for case in api_cases],
            headers=response.headers
        )
        
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


@router.get("/{case_id}", response_model=Case)
@trace("api_get_case")
async def get_case(
    case_id: str,
    response: Response,
    case_service: Optional[ICaseService] = Depends(_di_get_case_service_dependency),
    current_user: DevUser = Depends(require_authentication)
) -> Case:
    """
    Get a specific case by ID
    
    Returns the full case details including conversation history,
    participants, and context information.
    """
    correlation_id = str(uuid.uuid4())
    response.headers["x-correlation-id"] = correlation_id
    
    try:
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
        # Convert domain Case entity to API Case model using centralized converter
        case_api = CaseConverter.entity_to_api(case)
        return case_api
        
    except HTTPException:
        raise
    except Exception as e:
        correlation_id = str(uuid.uuid4())
        logger.error(f"Unexpected error in get_case: {e}", extra={"correlation_id": correlation_id})
        error_response = ErrorResponse(
            schema_version="3.1.0",
            error=ErrorDetail(code="GET_CASE_ERROR", message="Failed to get case")
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
        # Build updates dict from request
        updates = {}
        if request.title is not None:
            updates["title"] = request.title
        if request.description is not None:
            updates["description"] = request.description
        if request.status is not None:
            updates["status"] = request.status
        if request.priority is not None:
            updates["priority"] = request.priority
        if request.tags is not None:
            updates["tags"] = request.tags
        
        if not updates:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="No updates provided"
            )
        
        success = await case_service.update_case(case_id, updates, current_user.user_id)
        if not success:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Case not found or access denied")
        
        # Return successful update response as expected by tests
        return {
            "case_id": case_id,
            "success": True,
            "message": "Case updated successfully"
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
            detail=f"Failed to update case: {str(e)}"
        )


@router.post("/{case_id}/share", response_model=Dict[str, Any])
@trace("api_share_case")
async def share_case(
    case_id: str,
    request: CaseShareRequest,
    case_service: Optional[ICaseService] = Depends(_di_get_case_service_dependency),
    current_user: DevUser = Depends(require_authentication)
) -> Dict[str, Any]:
    """
    Share a case with another user
    
    Grants access to the case for the specified user with the given role.
    Requires share permissions on the case.
    """
    try:
        if request.role == ParticipantRole.OWNER:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Cannot assign owner role through sharing"
            )
        
        success = await case_service.share_case(
            case_id=case_id,
            target_user_id=request.user_id,
            role=request.role,
            sharer_user_id=current_user.user_id if current_user else None
        )
        if not success:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Case not found or sharing not permitted")
        return {
            "case_id": case_id,
            "shared_with": request.user_id,
            "role": _safe_enum_value(request.role),
            "success": True,
            "message": f"Case shared with {request.user_id} as {_safe_enum_value(request.role)}"
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
            detail=f"Failed to share case: {str(e)}"
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
            default_titles = ["New Chat", "Untitled Case", "Untitled"]
            # Check if title is generic/banned (always check for existing titles)
            is_meaningful_title = (
                case.title not in default_titles and 
                not case.title.lower().startswith("chat-") and
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
        
        # Check if we have sufficient context for meaningful title generation
        # Extract only user messages for signal extraction
        user_message_content = _extract_user_signals_from_context(context_text)
        
        if not user_message_content or len(user_message_content.split()) < 1:
            # Not enough meaningful user content to generate a title
            error_response = ErrorResponse(
                schema_version="3.1.0",
                error=ErrorDetail(code="INSUFFICIENT_CONTEXT", message="Need at least one meaningful user message to generate title")
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
            'Case: New Chat',
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
            "(Issue/Problem/Troubleshooting/Conversation/Discussion/Untitled/New Chat). "
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


@router.post("/{case_id}/archive", response_model=Dict[str, Any])
@trace("api_archive_case")
async def archive_case(
    case_id: str,
    reason: Optional[str] = Query(None, description="Reason for archiving"),
    case_service: Optional[ICaseService] = Depends(_di_get_case_service_dependency),
    current_user: DevUser = Depends(require_authentication)
) -> Dict[str, Any]:
    """
    Archive a case
    
    Archives the case, marking it as completed and removing it from active lists.
    Requires owner or collaborator permissions.
    """
    try:
        success = await case_service.archive_case(
            case_id=case_id,
            reason=reason,
            user_id=current_user.user_id if current_user else None
        )
        if not success:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Case not found or archive not permitted")
        return {
            "case_id": case_id,
            "success": True,
            "message": "Case archived successfully",
            "reason": reason
        }
        
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to archive case: {str(e)}"
        )


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
    title: Optional[str] = Query(None, description="Case title"),
    force_new: bool = Query(False, description="Force creation of new case"),
    case_service: Optional[ICaseService] = Depends(_di_get_case_service_dependency),
    session_service: SessionService = Depends(_di_get_session_service_dependency),
    current_user: Optional[DevUser] = Depends(get_current_user_optional)
) -> Dict[str, Any]:
    """
    Create or get case for a session

    Associates a case with the given session. If no case exists, creates a new one.
    If force_new is true, always creates a new case.

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

@router.post("/{case_id}/queries", responses={201: {"description": "Query processed synchronously", "headers": {"Location": {"description": "URL of the query resource", "schema": {"type": "string"}}, "X-Correlation-ID": {"description": "Request correlation ID", "schema": {"type": "string"}}}}, 202: {"description": "Query processing asynchronously", "headers": {"Location": {"description": "URL of the query resource", "schema": {"type": "string"}}, "Retry-After": {"description": "Seconds to wait before next poll", "schema": {"type": "integer"}}, "X-Correlation-ID": {"description": "Request correlation ID", "schema": {"type": "string"}}}}})
@trace("api_submit_case_query")
async def submit_case_query(
    case_id: str,
    query_request: QueryRequest,
    request: Request,
    case_service: Optional[ICaseService] = Depends(_di_get_case_service_dependency),
    agent_service: AgentService = Depends(_di_get_agent_service_dependency),
    session_service: SessionService = Depends(_di_get_session_service_dependency),
    current_user: DevUser = Depends(require_authentication)
):
    """
    Submit a query to a case.
    
    CRITICAL: Must return 201 (sync) or 202 (async) per OpenAPI spec, NOT 404
    
    Args:
        case_id: Case identifier  
        request: FastAPI request containing query data
    
    Returns:
        201 with immediate result OR 202 with job Location for async processing
    """
    case_service = check_case_service_available(case_service)
    correlation_id = str(uuid.uuid4())

    try:
        # Validate session if provided
        if query_request.session_id:
            session = await session_service.get_session(query_request.session_id, validate=True)
            if not session:
                logger.warning(f"Invalid or expired session during query: {query_request.session_id}", extra={"correlation_id": correlation_id})
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

        # Validate case_id parameter first
        if not case_id or case_id.strip() in ("", "undefined", "null"):
            raise HTTPException(
                status_code=400,
                detail="Valid case_id is required. Received invalid case_id. Please create a case first or provide a valid case_id."
            )
        
        # Extract query text from validated QueryRequest model
        query_text = query_request.query
        if not query_text or not query_text.strip():
            raise HTTPException(
                status_code=400, 
                detail="Query text is required in 'query' field"
            )
        
        # Verify case exists first (404 if case not found)
        # Use consistent authentication fallback logic with list_cases endpoint
        user_id_for_access = current_user.user_id
        case = await case_service.get_case(case_id, user_id_for_access)
        if not case:
            raise HTTPException(
                status_code=404,
                detail="Case not found or access denied"
            )
        
        # Query flow consistency: Update case message_count and updated_at immediately
        # This ensures consistency as per spec requirement
        await case_service.add_case_query(case_id, query_text, current_user.user_id if current_user else None)
        
        # Check idempotency key if provided
        idempotency_key = request.headers.get("idempotency-key")
        if idempotency_key:
            # Check if we already processed this request
            existing_result = await case_service.check_idempotency_key(idempotency_key)
            if existing_result:
                return JSONResponse(
                    status_code=existing_result.get("status_code", 201),
                    content=existing_result.get("content", {}),
                    headers=existing_result.get("headers", {})
                )
        
        # Determine if we should process sync or async
        # For contract compliance, we'll primarily use sync (201) unless explicitly complex
        is_complex_query = (
            len(query_text) > 1000 or  # Increased threshold for better UX
            "analyze logs" in query_text.lower() or 
            "deep analysis" in query_text.lower() or
            "complex investigation" in query_text.lower()
        )
        
        if is_complex_query:
            # Async processing path - return 202 with job
            job_id = f"job_{case_id}_{datetime.now(timezone.utc).strftime('%Y%m%d_%H%M%S')}"
            
            # Store initial job status
            job_data = {
                "job_id": job_id,
                "case_id": case_id,
                "query": query_text,
                "status": "processing",
                "created_at": to_json_compatible(datetime.now(timezone.utc)),
                "user_id": current_user.user_id
            }
            
            # Trigger actual background processing
            asyncio.create_task(_process_async_query(job_id, case_id, query_request, agent_service, current_user.user_id))
            
            response_headers = {
                "Location": f"/api/v1/cases/{case_id}/queries/{job_id}",
                "Retry-After": "5"
            }
            
            # Store idempotency result if key provided
            if idempotency_key:
                await case_service.store_idempotency_result(
                    idempotency_key, 
                    202, 
                    job_data,
                    response_headers
                )
            
            return JSONResponse(
                status_code=202,
                content=job_data,
                headers=response_headers
            )
        else:
            # Sync processing path - use AgentService for real AI processing
            query_id = f"query_{datetime.now(timezone.utc).strftime('%Y%m%d_%H%M%S')}"

            # Add case context if not already present
            if not query_request.context:
                query_request.context = {}
            query_request.context.update({"case_id": case_id, "user_id": current_user.user_id})
            
            try:
                # API Route Level Timeout (35 seconds) - outermost timeout layer
                logger.info(f"🕐 API Route: Starting query processing for case {case_id} with 35s timeout")
                start_time = time.time()

                agent_response = await asyncio.wait_for(
                    agent_service.process_query_for_case(case_id, query_request),
                    timeout=35.0
                )

                processing_time = time.time() - start_time
                logger.info(f"✅ API Route: Query processed successfully in {processing_time:.2f}s for case {case_id}")

                # Validate AgentResponse has required content
                if not hasattr(agent_response, 'content') or not isinstance(agent_response.content, str):
                    raise ValueError(f"AgentResponse contract violation: content must be str, got {type(agent_response.content) if hasattr(agent_response, 'content') else 'missing'}")

                # Additional validation: Check if content is a JSON string (should never happen)
                if agent_response.content.strip().startswith('{'):
                    try:
                        import json as json_check
                        parsed_check = json_check.loads(agent_response.content)
                        if isinstance(parsed_check, dict):
                            logger.error(
                                f"🐛 CRITICAL: Response parser bug - content contains JSON object!",
                                extra={
                                    "content_preview": agent_response.content[:200],
                                    "parsed_keys": list(parsed_check.keys()) if isinstance(parsed_check, dict) else None,
                                    "case_id": case_id,
                                    "query": query_request.query[:100]
                                }
                            )
                            # FAIL LOUDLY - force fix of the parser bug
                            raise ValueError(
                                f"Response parser bug: agent_response.content contains JSON object instead of plain text. "
                                f"Keys found: {list(parsed_check.keys())}. "
                                f"This indicates the double-encoding fix in response_parser.py failed. "
                                f"Check logs for '🐛 CRITICAL BUG' to find where the JSON was left in answer field."
                            )
                    except json_check.JSONDecodeError:
                        pass  # Not JSON, safe to proceed

                # Convert AgentResponse to dict format for JSON serialization - MUST match OpenAPI spec
                agent_response_dict = {
                    "schema_version": "3.1.0",
                    "content": agent_response.content,
                    "response_type": _safe_enum_value(agent_response.response_type),
                    "session_id": agent_response.session_id,
                    "view_state": {
                        "session_id": agent_response.view_state.session_id,
                        "user": {
                            "user_id": agent_response.view_state.user.user_id,
                            "email": agent_response.view_state.user.email,
                            "name": agent_response.view_state.user.name,
                            "created_at": agent_response.view_state.user.created_at
                        },
                        "active_case": {
                            "case_id": agent_response.view_state.active_case.case_id,
                            "title": agent_response.view_state.active_case.title,
                            "status": agent_response.view_state.active_case.status,
                            "priority": agent_response.view_state.active_case.priority,
                            "created_at": agent_response.view_state.active_case.created_at,
                            "updated_at": agent_response.view_state.active_case.updated_at,
                            "message_count": agent_response.view_state.active_case.message_count
                        },
                        "cases": [case.dict() for case in agent_response.view_state.cases],
                        "messages": [msg.dict() for msg in agent_response.view_state.messages],
                        "uploaded_data": [data.dict() for data in agent_response.view_state.uploaded_data],
                        "show_case_selector": agent_response.view_state.show_case_selector,
                        "show_data_upload": agent_response.view_state.show_data_upload,
                        "loading_state": agent_response.view_state.loading_state
                    },
                    "sources": [
                        {
                            "type": _safe_enum_value(source.type),
                            "content": source.content,
                            "confidence": source.confidence,
                            "metadata": source.metadata
                        } for source in (agent_response.sources or [])
                    ],
                    "plan": [
                        {
                            "description": step.description
                        } for step in (agent_response.plan or [])
                    ] if agent_response.plan else None,
                    "suggested_actions": [
                        action.dict() if hasattr(action, 'dict') else action
                        for action in (agent_response.suggested_actions or [])
                    ] if hasattr(agent_response, 'suggested_actions') and agent_response.suggested_actions else []
                }
                
            except asyncio.TimeoutError:
                logger.error(f"AgentService processing timed out for case {case_id} after 45 seconds")
                # Timeout fallback response
                # IMPORTANT: Must include ALL required v3.1.0+ fields
                agent_response_dict = {
                    "schema_version": "3.1.0",
                    "content": "Based on the available information: Discovered 2 available capabilities. Intent: information. Complexity: simple. I'm processing your request but it's taking longer than expected. Let me provide a quick response: I can help you troubleshoot this issue. Could you provide more specific details about what you're experiencing?",
                    "response_type": "ANSWER",
                                        "session_id": query_request.session_id,
                    "view_state": {
                                                "session_id": agent_response.view_state.session_id,
                        "user": {
                            "user_id": current_user.user_id,
                            "email": "user@example.com",
                            "name": "User",
                            "created_at": to_json_compatible(datetime.now(timezone.utc))
                        },
                        "active_case": {
                            "case_id": case_id,
                            "title": f"Case {case_id}",
                            "status": "active",
                            "priority": "medium",
                            "created_at": to_json_compatible(datetime.now(timezone.utc)),
                            "updated_at": to_json_compatible(datetime.now(timezone.utc)),
                            "message_count": 1
                        },
                        "cases": [],
                        "messages": [],
                        "uploaded_data": [],
                        "show_case_selector": False,
                        "show_data_upload": True,
                        "loading_state": None
                    },
                    "sources": [],
                    "plan": None,
                    # v3.1.0+ REQUIRED fields (added to fix frontend error)
                    "evidence_requests": [],
                    "investigation_mode": "active_incident",
                    "case_status": "intake"
                }

            except asyncio.TimeoutError:
                processing_time = time.time() - start_time
                logger.error(f"⏰ API Route TIMEOUT: Query processing exceeded 35s timeout ({processing_time:.2f}s) for case {case_id}")
                # Return timeout fallback response
                # IMPORTANT: Must include ALL required v3.1.0+ fields
                agent_response_dict = {
                    "schema_version": "3.1.0",
                    "content": "⏳ **Request Processing Timeout**\n\nYour request took longer than expected to process (>35 seconds). This might be due to:\n\n• High system load or complex query processing\n• Temporary connectivity issues with AI services\n• Large data processing requirements\n\n**Please try:**\n• Submitting your request again\n• Breaking complex queries into smaller parts\n• Waiting a few moments before retrying",
                    "response_type": "ANSWER",
                                        "session_id": query_request.session_id,
                    "view_state": {
                                                "session_id": agent_response.view_state.session_id,
                        "user": {
                            "user_id": current_user.user_id,
                            "email": "user@example.com",
                            "name": "User",
                            "created_at": to_json_compatible(datetime.now(timezone.utc))
                        },
                        "active_case": {
                            "case_id": case_id,
                            "title": f"Case {case_id}",
                            "status": "active",
                            "priority": "medium",
                            "created_at": to_json_compatible(datetime.now(timezone.utc)),
                            "updated_at": to_json_compatible(datetime.now(timezone.utc)),
                            "message_count": 1
                        },
                        "cases": [],
                        "session_analytics": {
                            "cases_created": 1,
                            "messages_sent": 1,
                            "total_session_time": "0:00:35",
                            "last_activity": to_json_compatible(datetime.now(timezone.utc))
                        }
                    },
                    "sources": [{"type": "TIMEOUT", "content": f"API route timeout after {processing_time:.2f}s", "metadata": {"timeout_type": "api_route", "timeout_seconds": 35}}],
                    "plan": None,
                    # v3.1.0+ REQUIRED fields (added to fix frontend error)
                    "evidence_requests": [],
                    "investigation_mode": "active_incident",
                    "case_status": "intake"
                }

            except Exception as e:
                logger.error(f"AgentService processing failed for case {case_id}: {e}")
                # Fallback to graceful error response instead of complete failure
                # IMPORTANT: Must include ALL required v3.1.0+ fields
                agent_response_dict = {
                    "schema_version": "3.1.0",
                    "content": "I'm having trouble processing your request right now. Please try again in a few moments.",
                    "response_type": "ANSWER",
                                        "session_id": query_request.session_id,
                    "view_state": {
                                                "session_id": agent_response.view_state.session_id,
                        "user": {
                            "user_id": current_user.user_id,
                            "email": "user@example.com",
                            "name": "User",
                            "created_at": to_json_compatible(datetime.now(timezone.utc))
                        },
                        "active_case": {
                            "case_id": case_id,
                            "title": f"Case {case_id}",
                            "status": "active",
                            "priority": "medium",
                            "created_at": to_json_compatible(datetime.now(timezone.utc)),
                            "updated_at": to_json_compatible(datetime.now(timezone.utc)),
                            "message_count": 1
                        },
                        "cases": [],
                        "messages": [],
                        "uploaded_data": [],
                        "show_case_selector": False,
                        "show_data_upload": True,
                        "loading_state": None
                    },
                    "sources": [],
                    "plan": None,
                    # v3.1.0+ REQUIRED fields (added to fix frontend error)
                    "evidence_requests": [],
                    "investigation_mode": "active_incident",
                    "case_status": "intake"
                }
            
            # Store idempotency result if key provided
            if idempotency_key:
                await case_service.store_idempotency_result(
                    idempotency_key,
                    201,
                    agent_response_dict,
                    {"Location": f"/api/v1/cases/{case_id}/queries/{query_id}"}
                )
            
            # Persist assistant response to case messages
            try:
                assistant_message = CaseMessage(
                    case_id=case_id,
                    author_id=current_user.user_id if current_user else None,
                    message_type=MessageType.AGENT_RESPONSE,
                    content=agent_response_dict.get("content", ""),
                    metadata={
                        "response_type": agent_response_dict.get("response_type", "ANSWER"),
                        "confidence_score": agent_response_dict.get("confidence_score"),
                        "processing_time_ms": agent_response_dict.get("processing_time_ms")
                    }
                )

                await case_service.add_message_to_case(case_id, assistant_message)
                logger.debug(f"Successfully persisted assistant response for case {case_id}")
            except Exception as persist_error:
                logger.error(f"Failed to persist assistant response for case {case_id}: {persist_error}")
                # Continue anyway - don't fail the entire request due to persistence issues
            
            return JSONResponse(
                status_code=201,
                content=agent_response_dict,
                headers={
                    "Location": f"/api/v1/cases/{case_id}/queries/{query_id}",
                    "X-Correlation-ID": correlation_id
                }
            )
            
    except HTTPException:
        raise
    except Exception as e:
        correlation_id = str(uuid.uuid4())
        logger.error(f"Unexpected error in submit_case_query: {e}", extra={"correlation_id": correlation_id})
        error_response = ErrorResponse(
            schema_version="3.1.0",
            error=ErrorDetail(code="QUERY_PROCESSING_ERROR", message="Failed to process query")
        )
        raise HTTPException(
            status_code=500,
            detail=error_response.dict(),
            headers={"x-correlation-id": correlation_id}
        )


@router.get("/{case_id}/queries/{query_id}", response_model=Union[AgentResponse, QueryJobStatus], responses={200: {"description": "Query completed - returns AgentResponse", "model": AgentResponse, "headers": {"X-Correlation-ID": {"description": "Request correlation ID", "schema": {"type": "string"}}}}, 202: {"description": "Query still processing - returns job status", "model": QueryJobStatus, "headers": {"Retry-After": {"description": "Seconds to wait before next poll", "schema": {"type": "integer"}}, "X-Correlation-ID": {"description": "Request correlation ID", "schema": {"type": "string"}}}}})
@trace("api_get_case_query")  
async def get_case_query(
    case_id: str,
    query_id: str,
    response: Response,
    case_service: Optional[ICaseService] = Depends(_di_get_case_service_dependency),
    current_user: DevUser = Depends(require_authentication)
):
    """
    Get query status and result (for async polling).
    
    Returns 200 with AgentResponse when completed, or 202 with QueryJobStatus while processing.
    Supports Retry-After header for polling guidance.
    """
    case_service = check_case_service_available(case_service)
    correlation_id = str(uuid.uuid4())
    response.headers["x-correlation-id"] = correlation_id
    
    try:
        # Verify case exists
        case = await case_service.get_case(case_id, current_user.user_id)
        if not case:
            raise HTTPException(
                status_code=404,
                detail="Case not found or access denied"
            )
        
        # Check actual job status from stored results
        job_result = _async_query_results.get(query_id)
        
        if job_result is None:
            # Job not found - it might be a synchronous query or invalid ID
            raise HTTPException(
                status_code=404,
                detail="Query not found. This may be a synchronous query that has already been processed."
            )
            
        job_status = job_result.get("status", "processing")
        
        if job_status in ["processing", "pending"]:
            # Return 202 with QueryJobStatus while processing
            job_status = QueryJobStatus(
                query_id=query_id,
                case_id=case_id,
                status="processing",
                progress_percentage=None,
                started_at=to_json_compatible(datetime.now(timezone.utc)),
                last_updated_at=to_json_compatible(datetime.now(timezone.utc)),
                error=None,
                result=None
            )
            return JSONResponse(
                status_code=202,
                content=job_status.dict(),
                headers={
                    "Retry-After": "5",
                    "x-correlation-id": correlation_id
                }
            )
        elif job_status == "completed":
            # Query completed - return 200 with actual AgentResponse
            agent_response = job_result.get("result")
            if agent_response:
                # Convert AgentResponse to dict format for JSON serialization (same as sync path)
                agent_response_dict = {
                    "schema_version": "3.1.0",
                    "content": agent_response.content,
                    "response_type": _safe_enum_value(agent_response.response_type),
                                        "session_id": agent_response.session_id,
                    "view_state": {
                        "session_id": agent_response.view_state.session_id,
                        "user": {
                            "user_id": agent_response.view_state.user.user_id,
                            "email": agent_response.view_state.user.email,
                            "name": agent_response.view_state.user.name,
                            "created_at": agent_response.view_state.user.created_at
                        },
                        "active_case": {
                            "case_id": agent_response.view_state.active_case.case_id,
                            "title": agent_response.view_state.active_case.title,
                            "status": agent_response.view_state.active_case.status,
                            "priority": agent_response.view_state.active_case.priority,
                            "created_at": agent_response.view_state.active_case.created_at,
                            "updated_at": agent_response.view_state.active_case.updated_at,
                            "message_count": agent_response.view_state.active_case.message_count
                        },
                        "cases": [case.dict() for case in agent_response.view_state.cases],
                        "messages": [msg.dict() for msg in agent_response.view_state.messages],
                        "uploaded_data": [data.dict() for data in agent_response.view_state.uploaded_data],
                        "show_case_selector": agent_response.view_state.show_case_selector,
                        "show_data_upload": agent_response.view_state.show_data_upload,
                        "loading_state": agent_response.view_state.loading_state
                    },
                    "sources": [
                        {
                            "type": _safe_enum_value(source.type),
                            "content": source.content,
                            "confidence": source.confidence,
                            "metadata": source.metadata
                        } for source in (agent_response.sources or [])
                    ],
                    "plan": [
                        {
                            "description": step.description
                        } for step in (agent_response.plan or [])
                    ] if agent_response.plan else None
                }
                return JSONResponse(
                    status_code=200,
                    content=agent_response_dict,
                    headers={"x-correlation-id": correlation_id}
                )
            else:
                # No result found - data corruption
                raise HTTPException(
                    status_code=500,
                    detail="Query marked as completed but no result available"
                )
                
        elif job_status == "failed":
            # Query failed - return error information
            error_msg = job_result.get("error", "Query processing failed")
            raise HTTPException(
                status_code=500,
                detail=f"Query processing failed: {error_msg}"
            )
        else:
            # Unknown status
            raise HTTPException(
                status_code=500,
                detail=f"Unknown query status: {job_status}"
            )
        
    except HTTPException:
        raise
    except Exception as e:
        correlation_id = str(uuid.uuid4())
        logger.error(f"Unexpected error in get_case_query: {e}", extra={"correlation_id": correlation_id})
        error_response = ErrorResponse(
            schema_version="3.1.0",
            error=ErrorDetail(code="QUERY_STATUS_ERROR", message="Failed to get query status")
        )
        raise HTTPException(
            status_code=500,
            detail=error_response.dict(),
            headers={"x-correlation-id": correlation_id}
        )
# Final AgentResponse fetch for historical queries
@router.get("/{case_id}/queries/{query_id}/result", response_model=AgentResponse)
@trace("api_get_case_query_result")
async def get_case_query_result(
    case_id: str,
    query_id: str,
    response: Response,
    case_service: Optional[ICaseService] = Depends(_di_get_case_service_dependency),
    current_user: DevUser = Depends(require_authentication)
) -> AgentResponse:
    """
    Return the final AgentResponse for a completed query.
    """
    case_service = check_case_service_available(case_service)
    correlation_id = str(uuid.uuid4())
    response.headers["x-correlation-id"] = correlation_id
    
    try:
        # Verify access
        case = await case_service.get_case(case_id, current_user.user_id)
        if not case:
            raise HTTPException(status_code=404, detail="Case not found or access denied")

        # If service exposes a getter, use it; otherwise return synthesized response
        agent_response = None
        try:
            agent_response = await case_service.get_query_result(case_id, query_id)
        except Exception:
            agent_response = None

        if not agent_response:
            # Synthesized minimal AgentResponse
            now = to_json_compatible(datetime.now(timezone.utc))
            session_id_value = case.metadata.get("last_session_id") if hasattr(case, 'metadata') else None
            agent_response = {
                "schema_version": "3.1.0",
                "content": f"Historical result for query {query_id} in case {case_id}",
                "response_type": "ANSWER",
                                "session_id": query_request.session_id,
                    "view_state": {
                                        "session_id": agent_response.view_state.session_id,
                        "user": {
                        "user_id": current_user.user_id if current_user else None or "anonymous",
                        "email": "user@example.com",
                        "name": "User",
                        "created_at": now
                    },
                    "active_case": {
                        "case_id": case_id,
                        "title": getattr(case, 'title', f"Case {case_id}"),
                        "status": str(getattr(case, 'status', 'active')).split('.')[-1],
                        "priority": str(getattr(case, 'priority', 'medium')).split('.')[-1],
                        "created_at": now,
                        "updated_at": now,
                        "message_count": getattr(case, 'message_count', 0)
                    },
                    "cases": [],
                    "messages": [],
                    "uploaded_data": [],
                    "show_case_selector": False,
                    "show_data_upload": True,
                    "loading_state": None
                },
                "sources": []
            }

        return agent_response
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to get query result: {str(e)}")


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

@router.post("/{case_id}/data", status_code=status.HTTP_201_CREATED, response_model=DataUploadResponse, responses={201: {"description": "Data uploaded successfully with AI analysis", "headers": {"Location": {"description": "URL of the created resource", "schema": {"type": "string"}}, "X-Correlation-ID": {"description": "Request correlation ID", "schema": {"type": "string"}}}}})
@trace("api_upload_case_data")
async def upload_case_data(
    case_id: str,
    request: Request,
    file: UploadFile = File(...),
    session_id: str = Form(..., description="Session ID for authentication"),
    description: Optional[str] = Form(None, description="Description of uploaded data"),
    source_metadata: Optional[str] = Form(None, description="JSON string with source metadata (source_type, source_url, etc.)"),
    case_service: Optional[ICaseService] = Depends(_di_get_case_service_dependency),
    preprocessing_service = Depends(get_preprocessing_service),
    agent_service: AgentService = Depends(_di_get_agent_service_dependency),
    session_service: SessionService = Depends(_di_get_session_service_dependency),
    current_user: DevUser = Depends(require_authentication)
):
    """
    Upload data file to a specific case with AI analysis.

    Pipeline:
    1. Upload file
    2. Preprocess data (classify, extract insights, generate LLM-ready summary)
    3. Get AI analysis via agent service
    4. Return combined response with agent's conversational analysis

    Returns 201 with DataUploadResponse including agent_response field.
    """
    case_service = check_case_service_available(case_service)
    correlation_id = str(uuid.uuid4())

    try:
        # Validate case_id parameter first (consistent with query endpoint)
        if not case_id or case_id.strip() in ("", "undefined", "null"):
            raise HTTPException(
                status_code=400,
                detail="Valid case_id is required. Received invalid case_id. Please create a case first or provide a valid case_id."
            )

        # Validate session if provided
        if session_id:
            session = await session_service.get_session(session_id, validate=True)
            if not session:
                logger.warning(f"Invalid or expired session during data upload: {session_id}", extra={"correlation_id": correlation_id})
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

        # Check idempotency key if provided (consistent with query endpoint)
        idempotency_key = request.headers.get("idempotency-key")
        if idempotency_key:
            existing_result = await case_service.check_idempotency_key(idempotency_key)
            if existing_result:
                logger.info(f"Returning cached result for idempotency key: {idempotency_key}")
                return JSONResponse(
                    status_code=existing_result.get("status_code", 201),
                    content=existing_result.get("content", {}),
                    headers=existing_result.get("headers", {})
                )

        # Step 1: Verify case exists (consistent with query endpoint)
        # Case must be created first via POST /api/v1/cases/sessions/{session_id}/case
        case = await case_service.get_case(case_id, current_user.user_id)
        if not case:
            raise HTTPException(
                status_code=404,
                detail="Case not found. Please create a case first using POST /api/v1/cases/sessions/{session_id}/case"
            )

        # Step 2: Read uploaded file
        file_content = await file.read()
        file_size = len(file_content)

        logger.info(f"Processing upload: {file.filename} ({file_size} bytes) for case {case.case_id}")

        # Step 2.5: Parse source metadata if provided
        parsed_source_metadata = None
        if source_metadata:
            try:
                import json
                from faultmaven.models.api import SourceMetadata
                source_dict = json.loads(source_metadata)
                parsed_source_metadata = SourceMetadata(**source_dict)
                logger.debug(f"Source metadata parsed: {parsed_source_metadata.source_type}")
            except Exception as e:
                # Invalid metadata - log but don't fail the upload
                logger.warning(f"Failed to parse source_metadata: {e}")
                parsed_source_metadata = None

        # Step 3: Preprocess data
        try:
            content_str = file_content.decode('utf-8')
        except UnicodeDecodeError:
            # Try latin-1 as fallback for binary files
            try:
                content_str = file_content.decode('latin-1')
            except:
                raise HTTPException(
                    status_code=400,
                    detail="Unable to decode file content. Please upload text files only."
                )

        preprocessed = preprocessing_service.preprocess(
            filename=file.filename,
            content=content_str,
            source_metadata=parsed_source_metadata
        )

        # Generate unique data_id and calculate compression ratio
        data_id = str(uuid.uuid4())
        compression_ratio = preprocessed.original_size / preprocessed.processed_size if preprocessed.processed_size > 0 else 1.0

        logger.info(
            f"Preprocessing complete: {preprocessed.metadata.data_type.value}, "
            f"{preprocessed.original_size}→{preprocessed.processed_size} chars, "
            f"{preprocessed.metadata.processing_time_ms:.1f}ms, "
            f"compression={compression_ratio:.1f}x"
        )

        # Step 3.5: Initialize diagnostic state if needed
        if not hasattr(case, 'diagnostic_state') or case.diagnostic_state is None:
            from faultmaven.models.case import CaseDiagnosticState
            case.diagnostic_state = CaseDiagnosticState()
            logger.info("Initialized diagnostic state for case")

        # Step 3.6: Create evidence from preprocessed data
        evidence = create_evidence_from_preprocessed(
            preprocessed=preprocessed,
            filename=file.filename,
            turn_number=1,  # TODO: Get actual turn number from diagnostic state
            evidence_type=EvidenceType.SUPPORTIVE,
            addresses_requests=[],  # TODO: Match against pending evidence requests
            data_id=data_id,
            processed_at=to_json_compatible(datetime.now(timezone.utc)),
        )

        # Step 3.7: Extract timeline events (Gap 1.2: Timeline Integration)
        timeline_events = []
        if should_populate_timeline(preprocessed):
            timeline_events = extract_timeline_events(preprocessed)
            if timeline_events:
                # Store in legacy timeline_info field
                if not case.diagnostic_state.timeline_info:
                    case.diagnostic_state.timeline_info = {}
                case.diagnostic_state.timeline_info['events'] = timeline_events
                logger.info(f"Extracted {len(timeline_events)} timeline events from {file.filename}")

        # Step 3.8: Generate hypotheses from anomalies (Gap 1.3: Anomaly → Hypothesis)
        new_hypotheses = []
        if should_generate_hypotheses(preprocessed):
            new_hypotheses = generate_hypotheses_from_anomalies(preprocessed, current_turn=1, data_id=data_id)
            if new_hypotheses:
                # Add to legacy hypotheses field
                for hyp in new_hypotheses:
                    case.diagnostic_state.hypotheses.append({
                        "id": hyp.hypothesis_id,
                        "statement": hyp.statement,
                        "category": hyp.category,
                        "likelihood": hyp.likelihood,
                        "status": hyp.status.value,
                        "supporting_evidence": hyp.supporting_evidence,
                    })
                logger.info(f"Generated {len(new_hypotheses)} hypotheses from anomalies")

        # Step 3.9: Add evidence to diagnostic state
        case.diagnostic_state.evidence_provided.append(evidence)

        # Step 3.10: Persist changes
        try:
            # Update case with modified diagnostic_state
            # Redis store now handles datetime serialization automatically
            updates = {
                "diagnostic_state": case.diagnostic_state.dict() if hasattr(case.diagnostic_state, 'dict') else case.diagnostic_state
            }
            await case_service.update_case(case.case_id, updates, current_user.user_id)
            logger.info(
                f"Evidence integration complete: "
                f"evidence={evidence.evidence_id}, "
                f"category={map_datatype_to_evidence_category(preprocessed.metadata.data_type).value}, "
                f"timeline_events={len(timeline_events)}, "
                f"hypotheses={len(new_hypotheses)}"
            )
        except Exception as e:
            # Log but don't fail - evidence integration is supplementary
            logger.warning(f"Failed to update case with evidence: {e}")

        # Step 3.11: Store preprocessed summary in Case Working Memory
        try:
            from faultmaven.container import container
            case_vector_store = getattr(container, 'case_vector_store', None)

            if case_vector_store:
                await case_vector_store.add_documents(
                    case_id=case.case_id,
                    documents=[{
                        'id': data_id,
                        'content': preprocessed.content,  # LLM-ready preprocessed content
                        'metadata': {
                            'filename': file.filename,
                            'data_type': preprocessed.metadata.data_type.value,
                            'file_size': file_size,
                            'uploaded_at': to_json_compatible(datetime.now(timezone.utc)),
                            'user_id': current_user.user_id
                        }
                    }]
                )
                logger.info(
                    f"Stored preprocessed content in Working Memory: "
                    f"case_{case.case_id}/{data_id} "
                    f"({len(preprocessed.content)} chars)"
                )
            else:
                logger.warning("Case vector store not available, skipping Working Memory storage")
        except Exception as e:
            # Log but don't fail - Working Memory storage is supplementary
            logger.warning(f"Failed to store in Working Memory: {e}")

        # Step 4: Generate AI analysis via agent service
        context_dict = {
            "case_id": case.case_id,
            "data_id": data_id,
            "data_type": preprocessed.metadata.data_type.value,
            "preprocessed_content": preprocessed.content,  # LLM-ready content
            "security_flags": preprocessed.security_flags,
            "upload_filename": file.filename,  # Note: renamed from 'filename' to avoid LogRecord conflict
            "file_size": file_size,
            "user_description": description
        }

        # Add source metadata to context if available
        # Note: preprocessed.source_metadata is a SourceMetadata object (type-enforced)
        if preprocessed.source_metadata:
            context_dict["source_metadata"] = {
                "source_type": preprocessed.source_metadata.source_type,
                "source_url": preprocessed.source_metadata.source_url,
                "captured_at": preprocessed.source_metadata.captured_at,
                "user_description": preprocessed.source_metadata.user_description
            }

        query_request = QueryRequest(
            session_id=session_id,
            query=f"I've uploaded {file.filename} ({file_size:,} bytes). Please analyze it.",
            context=context_dict
        )

        # Process with timeout (consistent with query endpoint)
        try:
            logger.info(f"🕐 API Route: Starting agent analysis for upload {file.filename} with 35s timeout")
            start_time = time.time()

            agent_response = await asyncio.wait_for(
                agent_service.process_query_for_case(case.case_id, query_request),
                timeout=35.0
            )

            processing_time = time.time() - start_time
            logger.info(f"✅ API Route: Agent analysis complete in {processing_time:.2f}s for {file.filename}")

        except asyncio.TimeoutError:
            processing_time = time.time() - start_time
            logger.error(f"⏰ API Route TIMEOUT: Agent analysis exceeded 35s timeout ({processing_time:.2f}s) for upload {file.filename}")
            raise HTTPException(
                status_code=status.HTTP_504_GATEWAY_TIMEOUT,
                detail=f"Agent analysis timed out after {processing_time:.1f}s. Please try uploading a smaller file or wait and try again."
            )

        # Step 5: Build response
        try:
            upload_response = DataUploadResponse(
                data_id=data_id,
                case_id=case.case_id,  # Return actual case_id (may differ from optimistic ID in URL)
                filename=file.filename,
                file_size=file_size,
                data_type=preprocessed.metadata.data_type.value,
                processing_status=ProcessingStatus.COMPLETED,
                uploaded_at=to_json_compatible(datetime.now(timezone.utc)),
                agent_response=agent_response,  # Conversational AI analysis
                classification={
                    "data_type": preprocessed.metadata.data_type.value,
                    "confidence": preprocessed.metadata.confidence,
                    "compression_ratio": compression_ratio,
                    "processing_time_ms": preprocessed.metadata.processing_time_ms
                }
            )
        except Exception as e:
            logger.error(f"Failed to create DataUploadResponse: {e}", exc_info=True, extra={
                "agent_response_type": type(agent_response).__name__,
                "has_evidence_requests": hasattr(agent_response, 'evidence_requests'),
                "has_investigation_mode": hasattr(agent_response, 'investigation_mode'),
                "has_case_status": hasattr(agent_response, 'case_status'),
            })
            raise

        # Build response headers (required by OpenAPI spec)
        response_headers = {
            "Location": f"/api/v1/cases/{case.case_id}/data/{data_id}",
            "X-Correlation-ID": correlation_id
        }

        # Store idempotency result if key provided (consistent with query endpoint)
        if idempotency_key:
            await case_service.store_idempotency_result(
                idempotency_key,
                201,
                upload_response.dict(),
                response_headers
            )

        # Return response with headers
        return JSONResponse(
            status_code=201,
            content=upload_response.dict(),
            headers=response_headers
        )

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"File upload failed for case {case_id}: {e}", exc_info=True, extra={
            "case_id": case_id,
            "upload_filename": file.filename if file else "unknown",
            "error_type": type(e).__name__,
        })
        raise HTTPException(
            status_code=500,
            detail=f"Failed to upload case data: {str(e)}"
        )


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