"""Agent API Routes

Purpose: Thin API layer for agent operations with pure delegation pattern

This module follows clean API architecture principles by removing
all business logic from the API layer and delegating to the service layer.

Key Features:
- Removed all business logic (session validation, sanitization, processing)
- Pure delegation to AgentService
- Simplified error handling at API boundary
- Proper dependency injection via DI container
- Clean separation of concerns (API vs Business logic)

Architecture Pattern:
API Route (validation + delegation) → Service Layer (business logic) → Core Domain
"""

import logging
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException

from faultmaven.models import QueryRequest, TroubleshootingResponse, AgentResponse, ErrorResponse, TitleGenerateRequest, TitleResponse
from faultmaven.api.v1.dependencies import get_agent_service, get_session_service
from faultmaven.services.agent_service import AgentService
from faultmaven.services.session_service import SessionService
from faultmaven.infrastructure.observability.tracing import trace
from faultmaven.exceptions import ValidationException

router = APIRouter(prefix="/agent", tags=["query_processing"])

logger = logging.getLogger(__name__)


@router.post("/query", response_model=AgentResponse)
@trace("api_query")
async def query(
    request: QueryRequest,
    agent_service: AgentService = Depends(get_agent_service)
) -> AgentResponse:
    """
    Process query using v3.1.0 schema with clean delegation pattern
    
    This endpoint follows the thin controller pattern:
    1. Minimal input validation (handled by Pydantic models)
    2. Pure delegation to service layer
    3. Clean error boundary handling
    
    Args:
        request: QueryRequest with query, session_id, context, priority
        agent_service: Injected AgentService from DI container
        
    Returns:
        AgentResponse with v3.1.0 schema including content, response_type, and view_state
        
    Raises:
        HTTPException: On service layer errors (404, 500, etc.)
    """
    logger.info(f"Received troubleshooting request for session {request.session_id}")
    
    try:
        # Pure delegation - all business logic is in the service layer
        response = await agent_service.process_query(request)
        
        logger.info(f"Successfully processed query for case {response.view_state.case_id}")
        return response
        
    except ValidationException as e:
        # Input validation errors - should return 422 Unprocessable Entity
        logger.warning(f"Query validation failed: {e}")
        raise HTTPException(status_code=422, detail=str(e))
    
    except RuntimeError as e:
        # Check if this is a wrapped validation error from service layer
        if "Validation failed:" in str(e):
            logger.warning(f"Query validation failed (wrapped): {e}")
            raise HTTPException(status_code=422, detail=str(e))
        else:
            # Other runtime errors
            logger.error(f"Query processing runtime error: {e}")
            raise HTTPException(status_code=500, detail="Service error during query processing")
        
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


@router.post("/title", response_model=TitleResponse)
@trace("api_generate_title")
async def generate_title(
    request: TitleGenerateRequest,
    agent_service: AgentService = Depends(get_agent_service)
) -> TitleResponse:
    """
    Generate a concise conversation title (3-8 words) for the current session/context.
    Dedicated endpoint to avoid overloading troubleshooting intent.
    """
    logger.info(f"Received title generation request for session {request.session_id}")
    try:
        result = await agent_service.generate_title(request)
        return result
    except ValidationException as e:
        raise HTTPException(status_code=422, detail=str(e))
    except Exception as e:
        logger.error(f"Title generation failed: {e}")
        raise HTTPException(status_code=500, detail="Service error during title generation")


@router.post("/troubleshoot", response_model=TroubleshootingResponse)  # Compatibility endpoint
@trace("api_troubleshoot")
async def troubleshoot_legacy(
    request: QueryRequest,
    agent_service: AgentService = Depends(get_agent_service)
) -> TroubleshootingResponse:
    """
    Legacy troubleshooting endpoint for backward compatibility
    
    This endpoint maintains compatibility with existing clients by converting
    the new v3.1.0 AgentResponse back to the old TroubleshootingResponse format.
    
    Args:
        request: QueryRequest with query, session_id, context, priority
        agent_service: Injected AgentService from DI container
        
    Returns:
        TroubleshootingResponse in legacy format
        
    Raises:
        HTTPException: On service layer errors (404, 500, etc.)
    """
    logger.info(f"Received legacy troubleshooting request for session {request.session_id}")
    
    try:
        # Get new format response
        agent_response = await agent_service.process_query(request)
        
        # Convert to legacy format
        legacy_response = _convert_to_legacy_response(agent_response, request.session_id)
        
        logger.info(f"Successfully processed legacy query for case {legacy_response.case_id}")
        return legacy_response
        
    except ValidationException as e:
        logger.warning(f"Legacy query validation failed: {e}")
        raise HTTPException(status_code=422, detail=str(e))
    
    except RuntimeError as e:
        if "Validation failed:" in str(e):
            logger.warning(f"Legacy query validation failed (wrapped): {e}")
            raise HTTPException(status_code=422, detail=str(e))
        else:
            logger.error(f"Legacy query processing runtime error: {e}")
            raise HTTPException(status_code=500, detail="Service error during query processing")
        
    except ValueError as e:
        logger.warning(f"Legacy query validation failed: {e}")
        raise HTTPException(status_code=400, detail=str(e))
        
    except PermissionError as e:
        logger.warning(f"Legacy query authorization failed: {e}")
        raise HTTPException(status_code=403, detail="Access denied")
        
    except FileNotFoundError as e:
        logger.warning(f"Resource not found: {e}")
        raise HTTPException(status_code=404, detail="Resource not found")
        
    except Exception as e:
        logger.error(f"Legacy query processing failed unexpectedly: {e}")
        raise HTTPException(
            status_code=500, 
            detail="Internal server error during query processing"
        )


def _convert_to_legacy_response(agent_response: AgentResponse, session_id: str) -> TroubleshootingResponse:
    """Convert new v3.1.0 AgentResponse to legacy TroubleshootingResponse format"""
    from datetime import datetime
    
    # Extract content parts from agent response
    content = agent_response.content
    findings = []
    recommendations = []
    next_steps = []
    
    # Parse content to extract structured data
    content_lines = content.split('\n')
    current_section = None
    
    for line in content_lines:
        line = line.strip()
        if line.startswith('Root Cause:'):
            root_cause = line.replace('Root Cause:', '').strip()
        elif line == 'Key Findings:':
            current_section = 'findings'
        elif line == 'Recommendations:':
            current_section = 'recommendations'
        elif line.startswith('•'):
            item = line[1:].strip()
            if current_section == 'findings':
                findings.append({
                    'type': 'observation',
                    'message': item,
                    'severity': 'medium',
                    'timestamp': datetime.utcnow().isoformat() + 'Z',
                    'source': 'agent_analysis'
                })
            elif current_section == 'recommendations':
                recommendations.append(item)
    
    # Extract next steps from plan if available
    if agent_response.plan:
        next_steps = [step.description for step in agent_response.plan]
    
    # Handle case where no structured content was found
    if not findings and not recommendations:
        findings = [{
            'type': 'general',
            'message': content,
            'severity': 'info',
            'timestamp': datetime.utcnow().isoformat() + 'Z',
            'source': 'agent_response'
        }]
        recommendations = ['Review the analysis above']
    
    root_cause = locals().get('root_cause', 'Analysis completed')
    
    return TroubleshootingResponse(
        case_id=agent_response.view_state.case_id,
        session_id=session_id,
        status="completed",
        findings=findings,
        root_cause=root_cause,
        recommendations=recommendations,
        confidence_score=0.8,  # Default confidence
        estimated_mttr="15 minutes",  # Default MTTR
        next_steps=next_steps,
        created_at=datetime.utcnow(),
        completed_at=datetime.utcnow()
    )


@router.get("/cases/{case_id}", response_model=TroubleshootingResponse)
@trace("api_get_case")
async def get_case(
    case_id: str,
    session_id: str,
    agent_service: AgentService = Depends(get_agent_service)
) -> TroubleshootingResponse:
    """
    Get case results by ID with clean delegation
    
    Args:
        case_id: Case identifier
        session_id: Session identifier for validation
        agent_service: Injected AgentService
        
    Returns:
        TroubleshootingResponse with case results
    """
    logger.info(f"Retrieving case {case_id} for session {session_id}")
    
    try:
        # Prefer legacy-named method if present (tests stub this), else fallback
        if hasattr(agent_service, 'get_investigation_results'):
            raw = await getattr(agent_service, 'get_investigation_results')(case_id, session_id=session_id)
        else:
            raw = await agent_service.get_case_results(case_id=case_id, session_id=session_id)

        # Normalize to TroubleshootingResponse with case_id
        def to_dict(obj):
            try:
                # pydantic v2
                return obj.model_dump()
            except Exception:
                try:
                    # pydantic v1
                    return obj.dict()
                except Exception:
                    return dict(obj) if isinstance(obj, dict) else vars(obj)

        data = to_dict(raw)
        if 'case_id' not in data and 'investigation_id' in data:
            data['case_id'] = data.pop('investigation_id')

        return TroubleshootingResponse(**data)
        
    except ValidationException as e:
        logger.warning(f"Case retrieval validation failed: {e}")
        raise HTTPException(status_code=422, detail=str(e))
        
    except ValueError as e:
        logger.warning(f"Case retrieval validation failed: {e}")
        raise HTTPException(status_code=400, detail=str(e))
        
    except FileNotFoundError as e:
        logger.warning(f"Case not found: {e}")
        raise HTTPException(status_code=404, detail="Case not found")
        
    except Exception as e:
        logger.error(f"Case retrieval failed: {e}")
        raise HTTPException(status_code=500, detail="Failed to retrieve case")


@router.get("/sessions/{session_id}/cases")
@trace("api_list_session_cases")
async def list_session_cases(
    session_id: str,
    limit: Optional[int] = 10,
    offset: Optional[int] = 0,
    agent_service: AgentService = Depends(get_agent_service)
):
    """
    List cases for a session with clean delegation
    
    Args:
        session_id: Session identifier
        limit: Maximum number of results
        offset: Pagination offset
        agent_service: Injected AgentService
        
    Returns:
        List of case summaries
    """
    logger.info(f"Listing cases for session {session_id}")
    
    try:
        # Delegate pagination and business logic to service layer
        if hasattr(agent_service, 'list_session_investigations'):
            items = await getattr(agent_service, 'list_session_investigations')(session_id=session_id, limit=limit or 10, offset=offset or 0)
        else:
            items = await agent_service.list_session_cases(session_id=session_id, limit=limit or 10, offset=offset or 0)

        # Normalize investigation_id -> case_id in listing
        normalized = []
        for item in items or []:
            if isinstance(item, dict):
                item = {**item}
                if 'case_id' not in item and 'investigation_id' in item:
                    item['case_id'] = item.pop('investigation_id')
            normalized.append(item)

        return {
            "session_id": session_id,
            "cases": normalized,
            "limit": limit or 10,
            "offset": offset or 0,
            "total": len(normalized)
        }
        
    except ValidationException as e:
        logger.warning(f"Case listing validation failed: {e}")
        raise HTTPException(status_code=422, detail=str(e))
        
    except ValueError as e:
        logger.warning(f"Case listing validation failed: {e}")
        raise HTTPException(status_code=400, detail=str(e))
        
    except FileNotFoundError as e:
        logger.warning(f"Session not found: {e}")
        raise HTTPException(status_code=404, detail="Session not found")
        
    except Exception as e:
        logger.error(f"Case listing failed: {e}")
        raise HTTPException(status_code=500, detail="Failed to list cases")


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


@router.post("/sessions/{session_id}/new-case")
@trace("api_agent_new_case")
async def start_new_case(
    session_id: str,
    session_service: SessionService = Depends(get_session_service)
):
    """
    Start a new case/conversation thread for a session
    
    This endpoint allows users to start a fresh conversation thread within 
    the same session. The existing case_id will be replaced with a new one,
    effectively starting a new troubleshooting conversation.
    
    Args:
        session_id: Session identifier
        session_service: Injected session service
        
    Returns:
        New case information
        
    Raises:
        HTTPException: If session not found or case creation fails
    """
    try:
        # Validate session exists
        session = await session_service.get_session(session_id, validate=True)
        if not session:
            logger.warning(f"Attempt to start new case for non-existent session: {session_id}")
            raise HTTPException(status_code=404, detail="Session not found or expired")
        
        # Start new case
        new_case_id = await session_service.start_new_case(session_id)
        
        logger.info(f"Started new case {new_case_id} for session {session_id}")
        
        return {
            "session_id": session_id,
            "new_case_id": new_case_id,
            "message": "New conversation thread started successfully",
            "previous_case_id": session.current_case_id if session.current_case_id != new_case_id else None
        }
        
    except ValidationException as e:
        logger.warning(f"Validation error starting new case: {e}")
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        logger.error(f"Failed to start new case for session {session_id}: {e}")
        raise HTTPException(
            status_code=500, 
            detail="Failed to start new conversation thread"
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