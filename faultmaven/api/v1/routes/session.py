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
• Cost-Efficiency: Use semantic caching
• Extensibility: Use interfaces for pluggable components
• Observability: Add tracing spans for key operations
"""

from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query

from faultmaven.infrastructure.observability.tracing import trace
from faultmaven.api.v1.dependencies import get_session_service
from faultmaven.services.session_service import SessionService
import logging

router = APIRouter(prefix="/sessions", tags=["session_management"])

# Use standard logger to avoid infrastructure imports
logger = logging.getLogger(__name__)




@router.post("/")
@trace("api_create_session")
async def create_session(
    user_id: Optional[str] = Query(None),
    session_service: SessionService = Depends(get_session_service),
):
    """
    Create a new troubleshooting session.

    Args:
        user_id: Optional user identifier

    Returns:
        Session creation response
    """
    try:
        session = await session_service.create_session(user_id)
        logger.info(f"Session created successfully: {session.session_id}")
        return {
            "session_id": session.session_id,
            "user_id": session.user_id,
            "created_at": session.created_at.isoformat(),
            "message": "Session created successfully",
        }
    except Exception as e:
        logger.error(f"Failed to create session: {e}")
        raise HTTPException(
            status_code=500, detail=f"Failed to create session: {str(e)}"
        )


@router.get("/{session_id}")
async def get_session(
    session_id: str,
    session_service: SessionService = Depends(get_session_service),
):
    """
    Retrieve a specific session by ID.

    Args:
        session_id: Session identifier

    Returns:
        Session details
    """
    try:
        session = await session_service.get_session(session_id)
        if not session:
            raise HTTPException(status_code=404, detail="Session not found")

        return {
            "session_id": session.session_id,
            "user_id": session.user_id,
            "created_at": session.created_at.isoformat(),
            "last_activity": session.last_activity.isoformat(),
            "data_uploads_count": len(session.data_uploads),
            "investigation_history_count": len(session.investigation_history),
        }
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Failed to get session {session_id}: {e}")
        raise HTTPException(
            status_code=500, detail=f"Failed to get session: {str(e)}"
        )


@router.get("/")
async def list_sessions(
    user_id: Optional[str] = Query(None),
    limit: int = Query(50, ge=1, le=100),
    offset: int = Query(0, ge=0),
    session_service: SessionService = Depends(get_session_service),
):
    """
    List all sessions with optional filtering.

    Args:
        user_id: Optional user ID filter
        limit: Maximum number of sessions to return
        offset: Number of sessions to skip

    Returns:
        List of sessions
    """
    try:
        # Get sessions from SessionManager and apply filters/pagination
        all_sessions = await session_service.list_sessions(user_id=user_id)
        
        # Apply pagination
        total = len(all_sessions)
        paginated_sessions = all_sessions[offset:offset + limit]
        
        # Format response
        return {
            "sessions": [
                {
                    "session_id": session.session_id,
                    "user_id": session.user_id,
                    "created_at": session.created_at.isoformat(),
                    "last_activity": session.last_activity.isoformat(),
                    "data_uploads_count": len(session.data_uploads),
                    "investigation_history_count": len(session.investigation_history),
                }
                for session in paginated_sessions
            ],
            "total": total,
            "limit": limit,
            "offset": offset,
        }
    except Exception as e:
        logger.error(f"Failed to list sessions: {e}")
        raise HTTPException(
            status_code=500, detail=f"Failed to list sessions: {str(e)}"
        )


@router.delete("/{session_id}")
async def delete_session(
    session_id: str,
    session_service: SessionService = Depends(get_session_service),
):
    """
    Delete a specific session.

    Args:
        session_id: Session identifier

    Returns:
        Deletion confirmation
    """
    try:
        # Check if session exists first
        session = await session_service.get_session(session_id)
        if not session:
            raise HTTPException(status_code=404, detail="Session not found")
        
        # Delete session
        success = await session_service.delete_session(session_id)
        if not success:
            raise HTTPException(status_code=500, detail="Failed to delete session")

        return {
            "session_id": session_id,
            "message": "Session deleted successfully",
        }
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Failed to delete session {session_id}: {e}")
        raise HTTPException(
            status_code=500, detail=f"Failed to delete session: {str(e)}"
        )


@router.post("/{session_id}/heartbeat")
async def session_heartbeat(
    session_id: str,
    session_service: SessionService = Depends(get_session_service),
):
    """
    Update session activity timestamp (heartbeat).

    Args:
        session_id: Session identifier

    Returns:
        Heartbeat confirmation
    """
    try:
        # Use the actual SessionManager method name
        result = await session_service.update_last_activity(session_id)
        if not result:
            raise HTTPException(status_code=404, detail="Session not found")

        return {
            "session_id": session_id,
            "status": "active",
            "message": "Session heartbeat updated",
        }
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Failed to update heartbeat for session {session_id}: {e}")
        raise HTTPException(
            status_code=500, detail=f"Failed to update heartbeat: {str(e)}"
        )


@router.get("/{session_id}/stats")
async def get_session_stats(
    session_id: str,
    session_service: SessionService = Depends(get_session_service),
):
    """
    Get session statistics and activity summary.

    Args:
        session_id: Session identifier

    Returns:
        Session statistics
    """
    try:
        # First check if session exists
        session = await session_service.get_session(session_id)
        if not session:
            raise HTTPException(status_code=404, detail="Session not found")

        # Calculate statistics for this specific session
        total_investigations = len([
            h for h in session.investigation_history
            if h.get("action") == "query_processed"
        ])

        total_uploads = len(session.data_uploads)

        # Get latest investigation confidence
        latest_confidence = 0.0
        for history in reversed(session.investigation_history):
            if history.get("action") == "query_processed":
                latest_confidence = history.get("confidence_score", 0.0)
                break

        return {
            "session_id": session_id,
            "user_id": session.user_id,
            "created_at": session.created_at.isoformat(),
            "last_activity": session.last_activity.isoformat(),
            "statistics": {
                "total_investigations": total_investigations,
                "total_data_uploads": total_uploads,
                "latest_confidence_score": latest_confidence,
                "session_duration_minutes": int(
                    (session.last_activity - session.created_at).total_seconds() / 60
                ),
            },
        }
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Failed to get session stats for {session_id}: {e}")
        raise HTTPException(
            status_code=500, detail=f"Failed to get session stats: {str(e)}"
        )
