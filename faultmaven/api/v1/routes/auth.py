# File: faultmaven/api/v1/routes/auth.py

"""
Authentication API endpoints for FaultMaven.

Provides developer login mock implementation for browser extension authentication.
"""

from fastapi import APIRouter, HTTPException, Depends
from typing import Dict, Any
import uuid
from datetime import datetime, timedelta

from ....models.api import DevLoginRequest, AuthResponse, ViewState, User, Case, ErrorResponse, ErrorDetail
from ....services.domain.session_service import SessionService
from ....api.v1.dependencies import get_session_service

router = APIRouter(prefix="/auth", tags=["authentication"])


@router.post("/dev-login", response_model=AuthResponse)
async def dev_login(
    request: DevLoginRequest,
    session_service: SessionService = Depends(get_session_service)
) -> AuthResponse:
    """
    Developer login mock endpoint.
    
    Creates or authenticates a user with minimal validation (username/email only).
    Returns complete ViewState for immediate UI rendering.
    
    Args:
        request: DevLoginRequest with username (email)
        session_service: Injected session service
        
    Returns:
        AuthResponse with ViewState containing user context and initial data
    """
    try:
        # Validate input - use 422 for validation errors per OpenAPI spec
        if not request.username or not request.username.strip():
            raise HTTPException(
                status_code=422,
                detail="Username is required"
            )
        
        # Clean username (use as email)
        email = request.username.strip().lower()
        
        # Generate or retrieve user
        user = User(
            user_id=f"user_{uuid.uuid4().hex[:8]}",
            email=email,
            name=email.split('@')[0].title() if '@' in email else email.title(),
            last_login=datetime.utcnow().isoformat() + 'Z'
        )
        
        # Create new session (service will generate session_id)
        session_result = await session_service.create_session(
            user_id=user.user_id,
            metadata={
                "user_email": email,
                "login_method": "dev_mock",
                "created_at": datetime.utcnow().isoformat() + 'Z'
            }
        )

        # Handle both SessionContext and (SessionContext, bool) return types
        if isinstance(session_result, tuple):
            session_context, was_resumed = session_result
        else:
            session_context = session_result
            was_resumed = False

        # Use the session_id from the created session
        session_id = session_context.session_id
        
        # Initialize user's cases (empty for new session)
        cases: list[Case] = []
        
        # Create complete ViewState for UI
        view_state = ViewState(
            session_id=session_id,
            user=user,
            active_case=None,  # No active case initially
            cases=cases,  # Empty cases list for new user
            messages=[],  # No messages initially
            uploaded_data=[],  # No uploaded data initially
            show_case_selector=True,  # Show case creation UI
            show_data_upload=True,  # Show data upload UI
            loading_state=None
        )
        
        return AuthResponse(
            success=True,
            view_state=view_state
        )
        
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(
            status_code=500,
            detail=f"AUTH_ROUTE_v2.0: Authentication failed: {str(e)}"
        )


@router.post("/logout")
async def logout(
    session_service: SessionService = Depends(get_session_service)
) -> Dict[str, Any]:
    """
    Logout endpoint to clean up session.
    
    Returns:
        Success confirmation
    """
    # Note: In a real implementation, we would get the session_id from
    # Authorization header and clean up the specific session.
    # For dev mock, we just return success.
    
    return {
        "success": True,
        "message": "Logged out successfully"
    }


@router.get("/session/{session_id}", response_model=AuthResponse)
async def verify_session(
    session_id: str,
    session_service: SessionService = Depends(get_session_service)
) -> AuthResponse:
    """
    Verify existing session and return current ViewState.
    
    Used by frontend to restore session on app startup.
    
    Args:
        session_id: Session ID to verify
        session_service: Injected session service
        
    Returns:
        AuthResponse with current ViewState if session is valid
    """
    try:
        # Get session from session service
        session = await session_service.get_session(session_id, validate=True)
        
        if not session:
            raise HTTPException(
                status_code=401,
                detail="Session not found or expired"
            )
        
        # Reconstruct user from session metadata (handle missing metadata attribute)
        metadata = getattr(session, 'metadata', {}) or {}
        user = User(
            user_id=session.user_id or f"user_{session.session_id[:8]}",
            email=metadata.get("user_email", "unknown@dev.local"),
            name=metadata.get("user_email", "unknown").split('@')[0].title(),
            last_login=metadata.get("created_at")
        )
        
        # Get user's cases (for now, return empty - will be populated by case service)
        cases: list[Case] = []
        
        # Build ViewState
        view_state = ViewState(
            session_id=session_id,
            user=user,
            active_case=None,
            cases=cases,
            messages=[],
            uploaded_data=[],
            show_case_selector=True,
            show_data_upload=True,
            loading_state=None
        )
        
        return AuthResponse(
            success=True,
            view_state=view_state
        )
        
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(
            status_code=500,
            detail=f"Session verification failed: {str(e)}"
        )