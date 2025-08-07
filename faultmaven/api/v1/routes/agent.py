"""Refactored Agent Routes - Phase 6.1

Purpose: Thin API layer for agent operations with pure delegation pattern

This refactored module follows clean API architecture principles by removing
all business logic from the API layer and delegating to the service layer.

Key Changes from Original:
- Removed all business logic (session validation, sanitization, processing)
- Pure delegation to AgentServiceRefactored
- Simplified error handling at API boundary
- Proper dependency injection via DI container
- Clean separation of concerns (API vs Business logic)

Architecture Pattern:
API Route (validation + delegation) → Service Layer (business logic) → Core Domain
"""

import logging
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException

from faultmaven.models import QueryRequest, TroubleshootingResponse
from faultmaven.api.v1.dependencies import get_agent_service
from faultmaven.services.agent_service import AgentService
from faultmaven.infrastructure.observability.tracing import trace

router = APIRouter(prefix="/query", tags=["query_processing"])

logger = logging.getLogger(__name__)


@router.post("/troubleshoot", response_model=TroubleshootingResponse)
@router.post("/", response_model=TroubleshootingResponse)  # Compatibility endpoint for tests
@trace("api_troubleshoot")
async def troubleshoot(
    request: QueryRequest,
    agent_service: AgentService = Depends(get_agent_service)
) -> TroubleshootingResponse:
    """
    Process troubleshooting query with clean delegation pattern
    
    This endpoint follows the thin controller pattern:
    1. Minimal input validation (handled by Pydantic models)
    2. Pure delegation to service layer
    3. Clean error boundary handling
    
    Args:
        request: QueryRequest with query, session_id, context, priority
        agent_service: Injected AgentServiceRefactored from DI container
        
    Returns:
        TroubleshootingResponse with findings and recommendations
        
    Raises:
        HTTPException: On service layer errors (404, 500, etc.)
    """
    logger.info(f"Received troubleshooting request for session {request.session_id}")
    
    try:
        # Pure delegation - all business logic is in the service layer
        response = await agent_service.process_query(request)
        
        logger.info(f"Successfully processed query {response.investigation_id}")
        return response
        
    except ValueError as e:
        # Business logic validation errors (session not found, invalid input)
        logger.warning(f"Query validation failed: {e}")
        raise HTTPException(status_code=400, detail=str(e))
        
    except PermissionError as e:
        # Authorization/access errors
        logger.warning(f"Query authorization failed: {e}")
        raise HTTPException(status_code=403, detail="Access denied")
        
    except FileNotFoundError as e:
        # Resource not found errors (session, data, etc.)
        logger.warning(f"Resource not found: {e}")
        raise HTTPException(status_code=404, detail="Resource not found")
        
    except Exception as e:
        # Unexpected service layer errors
        logger.error(f"Query processing failed unexpectedly: {e}")
        raise HTTPException(
            status_code=500, 
            detail="Internal server error during query processing"
        )


@router.get("/investigations/{investigation_id}", response_model=TroubleshootingResponse)
@trace("api_get_investigation")
async def get_investigation(
    investigation_id: str,
    session_id: str,
    agent_service: AgentService = Depends(get_agent_service)
) -> TroubleshootingResponse:
    """
    Get investigation results by ID with clean delegation
    
    Args:
        investigation_id: Investigation identifier
        session_id: Session identifier for validation
        agent_service: Injected AgentServiceRefactored
        
    Returns:
        TroubleshootingResponse with investigation results
    """
    logger.info(f"Retrieving investigation {investigation_id} for session {session_id}")
    
    try:
        # Delegate to service layer for all logic
        response = await agent_service.get_investigation_results(
            investigation_id=investigation_id,
            session_id=session_id
        )
        
        return response
        
    except ValueError as e:
        logger.warning(f"Investigation retrieval validation failed: {e}")
        raise HTTPException(status_code=400, detail=str(e))
        
    except FileNotFoundError as e:
        logger.warning(f"Investigation not found: {e}")
        raise HTTPException(status_code=404, detail="Investigation not found")
        
    except Exception as e:
        logger.error(f"Investigation retrieval failed: {e}")
        raise HTTPException(status_code=500, detail="Failed to retrieve investigation")


@router.get("/sessions/{session_id}/investigations")
@trace("api_list_session_investigations")
async def list_session_investigations(
    session_id: str,
    limit: Optional[int] = 10,
    offset: Optional[int] = 0,
    agent_service: AgentService = Depends(get_agent_service)
):
    """
    List investigations for a session with clean delegation
    
    Args:
        session_id: Session identifier
        limit: Maximum number of results
        offset: Pagination offset
        agent_service: Injected AgentServiceRefactored
        
    Returns:
        List of investigation summaries
    """
    logger.info(f"Listing investigations for session {session_id}")
    
    try:
        # Delegate pagination and business logic to service layer
        investigations = await agent_service.list_session_investigations(
            session_id=session_id,
            limit=limit,
            offset=offset
        )
        
        return {
            "session_id": session_id,
            "investigations": investigations,
            "limit": limit,
            "offset": offset,
            "total": len(investigations)  # Service layer provides this
        }
        
    except ValueError as e:
        logger.warning(f"Investigation listing validation failed: {e}")
        raise HTTPException(status_code=400, detail=str(e))
        
    except FileNotFoundError as e:
        logger.warning(f"Session not found: {e}")
        raise HTTPException(status_code=404, detail="Session not found")
        
    except Exception as e:
        logger.error(f"Investigation listing failed: {e}")
        raise HTTPException(status_code=500, detail="Failed to list investigations")


@router.get("/health")
@trace("api_agent_health")
async def health_check(
    agent_service: AgentService = Depends(get_agent_service)
):
    """
    Health check endpoint with service delegation
    
    Returns:
        Service health status
    """
    try:
        # Delegate health check logic to service layer
        health_status = await agent_service.health_check()
        
        return {
            "status": "healthy",
            "service": "agent",
            "details": health_status
        }
        
    except Exception as e:
        logger.error(f"Agent health check failed: {e}")
        raise HTTPException(
            status_code=503, 
            detail="Agent service unavailable"
        )


# Compatibility functions for legacy tests
# These are stubs to support existing test infrastructure
def get_session_manager():
    """Compatibility function for legacy tests"""
    from faultmaven.container import container
    return container.session_service

def get_core_agent():
    """Compatibility function for legacy tests"""
    # Return None - tests can override this
    return None

def get_data_sanitizer():
    """Compatibility function for legacy tests"""
    from faultmaven.container import container  
    return container.data_sanitizer