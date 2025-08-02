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

import logging
import os
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query

from faultmaven.infrastructure.observability.tracing import trace
from faultmaven.session_management import SessionManager

router = APIRouter(prefix="/sessions", tags=["session_management"])

# Global session manager instance
redis_url = os.getenv("REDIS_URL", "redis://localhost:6379")
session_manager = SessionManager(redis_url=redis_url)


def get_session_manager():
    return session_manager


@router.post("/")
@trace("api_create_session")
async def create_session(
    user_id: Optional[str] = Query(None),
    session_manager: SessionManager = Depends(get_session_manager),
):
    """
    Create a new troubleshooting session.

    Args:
        user_id: Optional user identifier

    Returns:
        Session creation response
    """
    logger = logging.getLogger(__name__)
    logger.info(f"Creating new session for user: {user_id}")

    try:
        session = await session_manager.create_session(user_id)
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
    session_manager: SessionManager = Depends(get_session_manager),
):
    """
    Retrieve a specific session by ID.

    Args:
        session_id: Session identifier

    Returns:
        Session details
    """
    logger = logging.getLogger(__name__)

    try:
        session = await session_manager.get_session(session_id)
        if not session:
            raise HTTPException(status_code=404, detail="Session not found")

        return {
            "session_id": session.session_id,
            "user_id": session.user_id,
            "created_at": session.created_at.isoformat(),
            "last_activity": session.last_activity.isoformat(),
            "data_uploads": session.data_uploads,
            "data_uploads_count": len(session.data_uploads),
            "investigation_history": session.investigation_history,
            "agent_state": session.agent_state,
        }
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Failed to retrieve session {session_id}: {e}")
        raise HTTPException(
            status_code=500, detail=f"Failed to retrieve session: {str(e)}"
        )


@router.get("/")
async def list_sessions(
    user_id: Optional[str] = Query(None),
    limit: int = Query(50, ge=1, le=100),
    offset: int = Query(0, ge=0),
    session_manager: SessionManager = Depends(get_session_manager),
):
    """
    List all active sessions with optional filtering.

    Args:
        user_id: Optional user ID filter
        limit: Maximum number of sessions to return
        offset: Number of sessions to skip

    Returns:
        List of sessions
    """
    logger = logging.getLogger(__name__)

    try:
        sessions = await session_manager.list_sessions()

        # Filter by user_id if provided
        if user_id:
            sessions = [s for s in sessions if s.user_id == user_id]

        # Apply pagination
        total_sessions = len(sessions)
        paginated_sessions = sessions[offset : offset + limit]

        return {
            "sessions": [
                {
                    "session_id": session.session_id,
                    "user_id": session.user_id,
                    "created_at": session.created_at.isoformat(),
                    "last_activity": session.last_activity.isoformat(),
                    "data_uploads_count": len(session.data_uploads),
                    "investigation_count": len(
                        [
                            h
                            for h in session.investigation_history
                            if h.get("action") == "query_processed"
                        ]
                    ),
                }
                for session in paginated_sessions
            ],
            "total": total_sessions,
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
    session_manager: SessionManager = Depends(get_session_manager),
):
    """
    Delete a session and all associated data.

    Args:
        session_id: Session identifier

    Returns:
        Deletion confirmation
    """
    logger = logging.getLogger(__name__)

    try:
        # Check if session exists
        session = await session_manager.get_session(session_id)
        if not session:
            raise HTTPException(status_code=404, detail="Session not found")

        # Delete the session
        await session_manager.delete_session(session_id)

        logger.info(f"Successfully deleted session {session_id}")

        return {
            "session_id": session_id,
            "status": "deleted",
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
    session_manager: SessionManager = Depends(get_session_manager),
):
    """
    Update session activity timestamp (heartbeat).

    Args:
        session_id: Session identifier

    Returns:
        Heartbeat confirmation
    """
    logger = logging.getLogger(__name__)

    try:
        # Check if session exists and update activity
        session = await session_manager.get_session(session_id)
        if not session:
            raise HTTPException(status_code=404, detail="Session not found")

        # Update last activity (this would be implemented in session_manager)
        await session_manager.update_last_activity(session_id)

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
    session_manager: SessionManager = Depends(get_session_manager),
):
    """
    Get session statistics and activity summary.

    Args:
        session_id: Session identifier

    Returns:
        Session statistics
    """
    logger = logging.getLogger(__name__)

    try:
        session = await session_manager.get_session(session_id)
        if not session:
            raise HTTPException(status_code=404, detail="Session not found")

        # Calculate statistics
        total_investigations = len(
            [
                h
                for h in session.investigation_history
                if h.get("action") == "query_processed"
            ]
        )

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
