"""query_processing.py

Purpose: /query endpoint implementation

Requirements:
--------------------------------------------------------------------------------
• Handle insight requests
• Route to core agent
• Return TroubleshootingResponse

Key Components:
--------------------------------------------------------------------------------
  router = APIRouter()
  @router.post('/query')

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
import uuid
from datetime import datetime
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException

from faultmaven.core.agent.agent import FaultMavenAgent
from faultmaven.tools.knowledge_base import KnowledgeBaseTool
from faultmaven.tools.web_search import WebSearchTool
from faultmaven.core.knowledge.ingestion import KnowledgeIngester
from faultmaven.infrastructure.llm.router import LLMRouter
from faultmaven.models import QueryRequest, TroubleshootingResponse
from faultmaven.infrastructure.observability.tracing import trace
from faultmaven.infrastructure.security.redaction import DataSanitizer
from faultmaven.session_management import SessionManager

router = APIRouter(prefix="/query", tags=["query_processing"])

# Global instances (in production, these would be dependency injected)

# Initialize session manager with K8s Redis support
redis_host = os.getenv("REDIS_HOST")
redis_port = int(os.getenv("REDIS_PORT", "30379")) if os.getenv("REDIS_PORT") else None
redis_password = os.getenv("REDIS_PASSWORD")
redis_url = os.getenv("REDIS_URL")

session_manager = SessionManager(
    redis_url=redis_url,
    redis_host=redis_host,
    redis_port=redis_port,
    redis_password=redis_password
)

# Initialize agent dependencies

# Create agent dependencies with proper error handling
try:
    llm_router = LLMRouter()
    print("✅ LLMRouter initialized")

    # Create knowledge ingester for knowledge base tool
    knowledge_ingester = KnowledgeIngester()
    print("✅ KnowledgeIngester initialized")

    knowledge_base_tool = KnowledgeBaseTool(knowledge_ingester=knowledge_ingester)
    print("✅ KnowledgeBaseTool initialized")

    web_search_tool = WebSearchTool()
    print("✅ WebSearchTool initialized")

    # Initialize the core agent
    core_agent = FaultMavenAgent(
        llm_router=llm_router,
        knowledge_base_tool=knowledge_base_tool,
        web_search_tool=web_search_tool,
    )
    print("✅ FaultMavenAgent initialized successfully!")

except Exception as e:
    print(f"❌ Agent initialization failed: {e}")
    import traceback

    traceback.print_exc()
    core_agent = None
data_sanitizer = DataSanitizer()


def get_session_manager():
    return session_manager


def get_core_agent():
    return core_agent


def get_data_sanitizer():
    return data_sanitizer


@router.post("/")
@trace("api_process_query")
async def process_query(
    request: QueryRequest,
    session_manager: SessionManager = Depends(get_session_manager),
    core_agent: Optional[FaultMavenAgent] = Depends(get_core_agent),
    data_sanitizer: DataSanitizer = Depends(get_data_sanitizer),
) -> TroubleshootingResponse:
    """
    Process a troubleshooting query using the core agent

    Args:
        request: QueryRequest containing the query and context

    Returns:
        TroubleshootingResponse with analysis and recommendations
    """
    logger = logging.getLogger(__name__)
    logger.info(f"Processing query for session {request.session_id}")

    try:
        # Validate session
        session = await session_manager.get_session(request.session_id)
        if not session:
            raise HTTPException(status_code=404, detail="Session not found")

        # Sanitize the query for security
        sanitized_query = data_sanitizer.sanitize(request.query)

        # Generate investigation ID
        investigation_id = str(uuid.uuid4())

        # Process query with core agent
        logger.info(f"Routing query to core agent: {investigation_id}")

        # Temporary placeholder since core agent initialization requires dependencies
        if core_agent is None:
            logger.warning("Core agent not initialized, returning placeholder response")

            # Create explicit dictionary structure for findings
            placeholder_finding = {
                "type": "status",
                "message": "Agent placeholder response",
                "severity": "info",
                "timestamp": datetime.utcnow().isoformat(),
                "source": "placeholder",
            }

            agent_response = {
                "findings": [placeholder_finding],
                "root_cause": "Investigation pending",
                "recommendations": ["Please wait for agent initialization"],
                "confidence_score": 0.1,
                "estimated_mttr": "Unknown",
                "next_steps": ["Complete agent setup"],
            }
        else:
            agent_response = await core_agent.process_query(
                query=sanitized_query,
                session_id=request.session_id,
                context=request.context or {},
                priority=request.priority,
            )

        # Create troubleshooting response
        response = TroubleshootingResponse(
            session_id=request.session_id,
            investigation_id=investigation_id,
            status="completed",
            findings=agent_response.get("findings", []),
            root_cause=agent_response.get("root_cause"),
            recommendations=agent_response.get("recommendations", []),
            confidence_score=agent_response.get("confidence_score", 0.5),
            estimated_mttr=agent_response.get("estimated_mttr"),
            next_steps=agent_response.get("next_steps", []),
            created_at=datetime.utcnow(),
            completed_at=datetime.utcnow(),
        )

        # Update session with investigation history
        await session_manager.add_investigation_history(
            request.session_id,
            {
                "action": "query_processed",
                "investigation_id": investigation_id,
                "query": sanitized_query,
                "priority": request.priority,
                "findings_count": len(response.findings),
                "recommendations_count": len(response.recommendations),
                "confidence_score": response.confidence_score,
            },
        )

        logger.info(f"Successfully processed query {investigation_id}")
        return response

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Query processing failed: {e}")
        raise HTTPException(
            status_code=500, detail=f"Query processing failed: {str(e)}"
        )


@router.get("/{investigation_id}")
async def get_investigation_status(
    investigation_id: str,
    session_id: str,
    session_manager: SessionManager = Depends(get_session_manager),
) -> TroubleshootingResponse:
    """
    Get the status and results of a specific investigation

    Args:
        investigation_id: Investigation identifier
        session_id: Session identifier

    Returns:
        TroubleshootingResponse with investigation results
    """
    logger = logging.getLogger(__name__)

    try:
        # Validate session
        session = await session_manager.get_session(session_id)
        if not session:
            raise HTTPException(status_code=404, detail="Session not found")

        # In a real implementation, you would retrieve the investigation from storage
        # For now, return a placeholder response
        return TroubleshootingResponse(
            session_id=session_id,
            investigation_id=investigation_id,
            status="completed",
            findings=[],
            recommendations=[],
            confidence_score=0.8,
            created_at=datetime.utcnow(),
            completed_at=datetime.utcnow(),
        )

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Failed to retrieve investigation {investigation_id}: {e}")
        raise HTTPException(
            status_code=500, detail=f"Failed to retrieve investigation: {str(e)}"
        )


@router.get("/session/{session_id}/investigations")
async def list_session_investigations(
    session_id: str, session_manager: SessionManager = Depends(get_session_manager)
):
    """
    List all investigations for a session

    Args:
        session_id: Session identifier

    Returns:
        List of investigation summaries
    """
    logger = logging.getLogger(__name__)

    try:
        # Validate session
        session = await session_manager.get_session(session_id)
        if not session:
            raise HTTPException(status_code=404, detail="Session not found")

        # Extract investigation history from session
        investigations = []
        for history_item in session.investigation_history:
            if history_item.get("action") == "query_processed":
                investigations.append(
                    {
                        "investigation_id": history_item.get("investigation_id"),
                        "query": history_item.get("query"),
                        "priority": history_item.get("priority"),
                        "findings_count": history_item.get("findings_count", 0),
                        "recommendations_count": history_item.get(
                            "recommendations_count", 0
                        ),
                        "confidence_score": history_item.get("confidence_score", 0.0),
                        "timestamp": history_item.get("timestamp"),
                    }
                )

        return {
            "session_id": session_id,
            "investigations": investigations,
            "total": len(investigations),
        }

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Failed to list investigations for session {session_id}: {e}")
        raise HTTPException(
            status_code=500, detail=f"Failed to list investigations: {str(e)}"
        )
