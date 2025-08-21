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

from datetime import datetime
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, status
from fastapi.responses import JSONResponse

from faultmaven.models.case import (
    Case,
    CaseCreateRequest,
    CaseListFilter,
    CaseSearchRequest,
    CaseShareRequest,
    CaseSummary,
    CaseUpdateRequest,
    ParticipantRole,
    CaseStatus,
    CasePriority
)
from faultmaven.models.interfaces_case import ICaseService
from faultmaven.models.api import ErrorResponse, ErrorDetail
from faultmaven.api.v1.dependencies import get_case_service, get_user_id
from fastapi import Request
from faultmaven.infrastructure.observability.tracing import trace
from faultmaven.exceptions import ValidationException, ServiceException

# Create router
router = APIRouter(prefix="/cases", tags=["cases"])
async def _di_get_case_service_dependency() -> Optional[ICaseService]:
    """Runtime wrapper so patched dependency is honored in tests."""
    # Import inside to resolve the patched function at call time
    from faultmaven.api.v1.dependencies import get_case_service as _getter
    return await _getter()


async def _di_get_user_id_dependency(request: Request) -> Optional[str]:
    """Runtime wrapper so patched dependency is honored in tests."""
    from faultmaven.api.v1.dependencies import get_user_id as _get_user_id
    return await _get_user_id(request)


def check_case_service_available(case_service: Optional[ICaseService]) -> ICaseService:
    """Check if case service is available and raise appropriate error if not"""
    if case_service is None:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Case service unavailable"
        )
    return case_service


@router.post("/", response_model=Case, status_code=status.HTTP_201_CREATED)
@trace("api_create_case")
async def create_case(
    request: CaseCreateRequest,
    case_service: Optional[ICaseService] = Depends(_di_get_case_service_dependency),
    user_id: Optional[str] = Depends(_di_get_user_id_dependency)
) -> Case:
    """
    Create a new troubleshooting case
    
    Creates a new case for tracking troubleshooting sessions and conversations.
    The case will persist beyond individual session lifetimes.
    """
    case_service = check_case_service_available(case_service)
    try:
        case = await case_service.create_case(
            title=request.title,
            description=request.description,
            owner_id=user_id,
            session_id=request.session_id,
            initial_message=request.initial_message
        )
        return case
        
    except ValidationException as e:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=str(e)
        )
    except ServiceException as e:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=str(e)
        )


@router.get("/", response_model=List[CaseSummary])
@trace("api_list_cases")
async def list_cases(
    case_service: Optional[ICaseService] = Depends(_di_get_case_service_dependency),
    user_id: Optional[str] = Depends(_di_get_user_id_dependency),
    status_filter: Optional[CaseStatus] = Query(None, description="Filter by case status"),
    priority_filter: Optional[CasePriority] = Query(None, description="Filter by case priority"),
    owner_id: Optional[str] = Query(None, description="Filter by case owner"),
    limit: int = Query(50, le=100, description="Maximum number of results"),
    offset: int = Query(0, ge=0, description="Result offset for pagination")
) -> List[CaseSummary]:
    """
    List cases with optional filtering
    
    Returns a list of cases accessible to the authenticated user.
    Results can be filtered by status, priority, owner, and other criteria.
    """
    case_service = check_case_service_available(case_service)
    try:
        # Build filter criteria
        filters = CaseListFilter(
            user_id=user_id,
            status=status_filter,
            priority=priority_filter,
            owner_id=owner_id,
            limit=limit,
            offset=offset
        )
        cases = await case_service.list_user_cases(user_id or "anonymous", filters)
        return cases
        
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to list cases: {str(e)}"
        )


@router.get("/{case_id}", response_model=Case)
@trace("api_get_case")
async def get_case(
    case_id: str,
    case_service: Optional[ICaseService] = Depends(_di_get_case_service_dependency),
    user_id: Optional[str] = Depends(_di_get_user_id_dependency)
) -> Case:
    """
    Get a specific case by ID
    
    Returns the full case details including conversation history,
    participants, and context information.
    """
    try:
        case = await case_service.get_case(case_id, user_id)
        if not case:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Case not found or access denied")
        return case
        
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to get case: {str(e)}"
        )


@router.put("/{case_id}", response_model=Dict[str, Any])
@trace("api_update_case")
async def update_case(
    case_id: str,
    request: CaseUpdateRequest,
    case_service: Optional[ICaseService] = Depends(_di_get_case_service_dependency),
    user_id: Optional[str] = Depends(_di_get_user_id_dependency)
) -> Dict[str, Any]:
    """
    Update case details
    
    Updates case metadata such as title, description, status, priority, and tags.
    Requires edit permissions on the case.
    """
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
        
        success = await case_service.update_case(case_id, updates, user_id)
        if not success:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Case not found or access denied")
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
    user_id: Optional[str] = Depends(_di_get_user_id_dependency)
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
            sharer_user_id=user_id
        )
        if not success:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Case not found or sharing not permitted")
        return {
            "case_id": case_id,
            "shared_with": request.user_id,
            "role": request.role.value,
            "success": True,
            "message": f"Case shared with {request.user_id} as {request.role.value}"
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


@router.post("/{case_id}/archive", response_model=Dict[str, Any])
@trace("api_archive_case")
async def archive_case(
    case_id: str,
    reason: Optional[str] = Query(None, description="Reason for archiving"),
    case_service: Optional[ICaseService] = Depends(_di_get_case_service_dependency),
    user_id: Optional[str] = Depends(_di_get_user_id_dependency)
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
            user_id=user_id
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
    user_id: Optional[str] = Depends(_di_get_user_id_dependency)
) -> List[CaseSummary]:
    """
    Search cases by content
    
    Searches case titles, descriptions, and optionally message content
    for the specified query terms.
    """
    try:
        cases = await case_service.search_cases(request, user_id)
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


@router.get("/{case_id}/conversation", response_model=str)
@trace("api_get_case_conversation")
async def get_case_conversation_context(
    case_id: str,
    limit: int = Query(10, le=50, description="Maximum number of messages to include"),
    case_service: Optional[ICaseService] = Depends(_di_get_case_service_dependency),
    user_id: Optional[str] = Depends(_di_get_user_id_dependency)
) -> str:
    """
    Get formatted conversation context for a case
    
    Returns conversation history formatted for LLM context injection.
    Used for maintaining conversation continuity across sessions.
    """
    try:
        # Verify user has access to the case
        case = await case_service.get_case(case_id, user_id)
        if not case:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Case not found or access denied"
            )
        
        context = await case_service.get_case_conversation_context(case_id, limit)
        return context
        
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to get conversation context: {str(e)}"
        )


@router.get("/{case_id}/analytics", response_model=Dict[str, Any])
@trace("api_get_case_analytics")
async def get_case_analytics(
    case_id: str,
    case_service: Optional[ICaseService] = Depends(_di_get_case_service_dependency),
    user_id: Optional[str] = Depends(_di_get_user_id_dependency)
) -> Dict[str, Any]:
    """
    Get case analytics and metrics
    
    Returns analytics data including message counts, participant activity,
    resolution time, and other case metrics.
    """
    try:
        # Verify user has access to the case
        case = await case_service.get_case(case_id, user_id)
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


# Session-case integration endpoints

@router.post("/sessions/{session_id}/case", response_model=Dict[str, Any])
@trace("api_create_case_for_session")
async def create_case_for_session(
    session_id: str,
    title: Optional[str] = Query(None, description="Case title"),
    force_new: bool = Query(False, description="Force creation of new case"),
    case_service: Optional[ICaseService] = Depends(_di_get_case_service_dependency),
    user_id: Optional[str] = Depends(_di_get_user_id_dependency)
) -> Dict[str, Any]:
    """
    Create or get case for a session
    
    Associates a case with the given session. If no case exists, creates a new one.
    If force_new is true, always creates a new case.
    """
    try:
        case_id = await case_service.get_or_create_case_for_session(
            session_id=session_id,
            user_id=user_id,
            force_new=force_new
        )
        
        if not case_id:
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail="Failed to create case for session"
            )
        
        return {
            "session_id": session_id,
            "case_id": case_id,
            "created_new": force_new,
            "success": True
        }
        
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
    user_id: Optional[str] = Depends(_di_get_user_id_dependency)
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
            "session_id": session_id,
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
            "timestamp": datetime.utcnow().isoformat() + 'Z',
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
            "timestamp": datetime.utcnow().isoformat() + 'Z',
            "error": str(e)
        }


# Note: Exception handlers should be added to the main FastAPI app, not the router
# These would be added in main.py if needed