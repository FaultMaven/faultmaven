"""API Dependencies Module

Purpose: FastAPI dependency injection functions

This module provides dependency injection functions for FastAPI endpoints,
integrating with the main DI container.

Key Features:
- Request-scoped dependencies
- Service access helpers
- Authentication/authorization dependencies
- Request validation dependencies
"""

from typing import Optional

from fastapi import Depends, HTTPException, Request

from ...container import container
from ...models import SessionContext
from ...services import AgentService, DataService, KnowledgeService, SessionService


# Service Dependencies

async def get_session_service() -> SessionService:
    """Get SessionService instance from container"""
    return container.get_session_service()


async def get_agent_service() -> AgentService:
    """Get AgentService instance from container"""
    return container.get_agent_service()


async def get_enhanced_agent_service():
    """Get EnhancedAgentService instance from container"""
    return container.get_enhanced_agent_service()


async def get_memory_service():
    """Get MemoryService instance from container"""
    return container.get_memory_service()


async def get_planning_service():
    """Get PlanningService instance from container"""
    return container.get_planning_service()


async def get_orchestration_service():
    """Get OrchestrationService instance from container"""
    return container.get_orchestration_service()


async def get_data_service() -> DataService:
    """Get DataService instance from container"""
    return container.get_data_service()


async def get_knowledge_service() -> KnowledgeService:
    """Get KnowledgeService instance from container"""
    return container.get_knowledge_service()


async def get_tracer():
    """Get tracer instance from container"""
    return container.get_tracer()



# Session Dependencies

async def get_current_session(
    session_id: str,
    session_service: SessionService = Depends(get_session_service),
) -> SessionContext:
    """
    Get and validate current session
    
    Args:
        session_id: Session ID from request
        session_service: Injected session service
        
    Returns:
        Valid SessionContext
        
    Raises:
        HTTPException: If session not found or invalid
    """
    session = await session_service.get_session(session_id, validate=True)
    if not session:
        raise HTTPException(status_code=404, detail="Session not found or expired")
    return session


async def get_optional_session(
    session_id: Optional[str] = None,
    session_service: SessionService = Depends(get_session_service),
) -> Optional[SessionContext]:
    """
    Get optional session if ID provided
    
    Args:
        session_id: Optional session ID
        session_service: Injected session service
        
    Returns:
        SessionContext or None
    """
    if not session_id:
        return None
        
    return await session_service.get_session(session_id, validate=True)


# Request Context Dependencies

async def get_request_metadata(request: Request) -> dict:
    """
    Extract metadata from request
    
    Args:
        request: FastAPI request object
        
    Returns:
        Dictionary of request metadata
    """
    return {
        "client_host": request.client.host if request.client else None,
        "user_agent": request.headers.get("user-agent"),
        "request_id": request.headers.get("x-request-id"),
        "content_type": request.headers.get("content-type"),
    }


# Authentication Dependencies (placeholder for future implementation)

async def get_current_user(request: Request) -> Optional[dict]:
    """
    Get current authenticated user
    
    Args:
        request: FastAPI request object
        
    Returns:
        User info dict or None
    """
    # Placeholder for authentication logic
    # In production, this would validate JWT tokens, API keys, etc.
    auth_header = request.headers.get("authorization")
    if auth_header and auth_header.startswith("Bearer "):
        # Validate token and return user info
        return {"user_id": "anonymous", "roles": ["user"]}
    return None


async def require_authenticated_user(
    user: Optional[dict] = Depends(get_current_user),
) -> dict:
    """
    Require authenticated user
    
    Args:
        user: User from get_current_user
        
    Returns:
        Authenticated user dict
        
    Raises:
        HTTPException: If not authenticated
    """
    if not user:
        raise HTTPException(status_code=401, detail="Authentication required")
    return user


# Rate Limiting Dependencies (placeholder)

async def check_rate_limit(
    request: Request,
    user: Optional[dict] = Depends(get_current_user),
) -> bool:
    """
    Check rate limits
    
    Args:
        request: FastAPI request
        user: Optional authenticated user
        
    Returns:
        True if within limits
        
    Raises:
        HTTPException: If rate limit exceeded
    """
    # Placeholder for rate limiting logic
    # In production, this would check Redis or similar
    return True


# Validation Dependencies

async def validate_content_size(
    content: str,
    max_size_mb: int = 10,
) -> str:
    """
    Validate content size
    
    Args:
        content: Content to validate
        max_size_mb: Maximum size in MB
        
    Returns:
        Validated content
        
    Raises:
        HTTPException: If content too large
    """
    size_bytes = len(content.encode("utf-8"))
    size_mb = size_bytes / (1024 * 1024)
    
    if size_mb > max_size_mb:
        raise HTTPException(
            status_code=413,
            detail=f"Content too large: {size_mb:.2f}MB (max: {max_size_mb}MB)",
        )
    
    return content


# Composite Dependencies

class TroubleshootingContext:
    """Context for troubleshooting operations"""
    
    def __init__(
        self,
        session: SessionContext,
        agent_service: AgentService,
        data_service: DataService,
        knowledge_service: KnowledgeService,
        user: Optional[dict] = None,
        metadata: Optional[dict] = None,
    ):
        self.session = session
        self.agent_service = agent_service
        self.data_service = data_service
        self.knowledge_service = knowledge_service
        self.user = user
        self.metadata = metadata or {}


async def get_troubleshooting_context(
    session_id: str,
    request: Request,
    session: SessionContext = Depends(get_current_session),
    agent_service: AgentService = Depends(get_agent_service),
    data_service: DataService = Depends(get_data_service),
    knowledge_service: KnowledgeService = Depends(get_knowledge_service),
    user: Optional[dict] = Depends(get_current_user),
    metadata: dict = Depends(get_request_metadata),
) -> TroubleshootingContext:
    """
    Get complete troubleshooting context
    
    Returns:
        TroubleshootingContext with all services and context
    """
    return TroubleshootingContext(
        session=session,
        agent_service=agent_service,
        data_service=data_service,
        knowledge_service=knowledge_service,
        user=user,
        metadata=metadata,
    )


# Health Check Dependencies

async def check_service_health() -> dict:
    """
    Check health of all services
    
    Returns:
        Dictionary of service health status
    """
    health_status = {
        "session_manager": "unknown",
        "agent": "unknown",
        "data_processor": "unknown",
        "knowledge_base": "unknown",
    }
    
    try:
        # Check session service
        session_service = container.get_session_service()
        if session_service:
            health_status["session_manager"] = "healthy"
        else:
            health_status["session_manager"] = "unhealthy"
    except:
        health_status["session_manager"] = "unhealthy"
        
    # Add more health checks as needed
    
    return health_status