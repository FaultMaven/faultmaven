"""Enhanced Agent API Routes - Phase 2

Purpose: Advanced intelligence capabilities for the enhanced agent service

This module provides API endpoints for Phase 2 advanced intelligence features:
- Memory-enhanced query processing
- Strategic planning and orchestration  
- Context-aware troubleshooting
- Multi-step problem resolution

Key Features:
- Enhanced agent service integration with memory and planning
- Strategic response planning with risk assessment
- Context-aware conversation management
- Advanced problem decomposition and orchestration
- Performance monitoring and metrics

Architecture Pattern:
API Route (validation + delegation) → Enhanced Service Layer → Core Intelligence → Infrastructure
"""

import logging
from typing import Optional, Dict, Any

from fastapi import APIRouter, Depends, HTTPException

from faultmaven.models import QueryRequest, AgentResponse, ErrorResponse
from faultmaven.models.interfaces import StrategicPlan, ProblemComponents, ConversationContext
from faultmaven.api.v1.dependencies import get_enhanced_agent_service, get_memory_service, get_planning_service
from faultmaven.services.enhanced_agent_service import EnhancedAgentService
from faultmaven.services.memory_service import MemoryService
from faultmaven.services.planning_service import PlanningService
from faultmaven.infrastructure.observability.tracing import trace
from faultmaven.exceptions import ValidationException, MemoryException, PlanningException

router = APIRouter(prefix="/enhanced-agent", tags=["enhanced_intelligence"])

logger = logging.getLogger(__name__)


@router.post("/query", response_model=AgentResponse)
@trace("enhanced_api_query")
async def enhanced_query(
    request: QueryRequest,
    enhanced_agent_service: EnhancedAgentService = Depends(get_enhanced_agent_service)
) -> AgentResponse:
    """
    Process query using enhanced agent with memory and planning capabilities
    
    This endpoint provides advanced intelligence features including:
    - Memory-enhanced context retrieval
    - Strategic response planning  
    - Enhanced reasoning and orchestration
    - Context-aware troubleshooting
    
    Args:
        request: QueryRequest with query, session_id, context, priority
        enhanced_agent_service: Injected EnhancedAgentService from DI container
        
    Returns:
        AgentResponse with enhanced content, strategic plan, and enriched view state
        
    Raises:
        HTTPException: On service layer errors (404, 500, etc.)
    """
    logger.info(f"Received enhanced query request for session {request.session_id}")
    
    try:
        # Enhanced processing with memory and planning integration
        response = await enhanced_agent_service.process_query(request)
        
        logger.info(
            f"Successfully processed enhanced query for case {response.view_state.case_id} "
            f"with confidence {getattr(response, 'confidence_score', 'unknown')}"
        )
        return response
        
    except ValidationException as e:
        logger.warning(f"Enhanced query validation failed: {e}")
        raise HTTPException(status_code=422, detail=str(e))
    
    except MemoryException as e:
        logger.warning(f"Memory service error during enhanced query: {e}")
        raise HTTPException(status_code=503, detail="Memory service temporarily unavailable")
    
    except PlanningException as e:
        logger.warning(f"Planning service error during enhanced query: {e}")
        raise HTTPException(status_code=503, detail="Planning service temporarily unavailable")
    
    except RuntimeError as e:
        if "Validation failed:" in str(e):
            logger.warning(f"Enhanced query validation failed (wrapped): {e}")
            raise HTTPException(status_code=422, detail=str(e))
        else:
            logger.error(f"Enhanced query processing runtime error: {e}")
            raise HTTPException(status_code=500, detail="Service error during enhanced query processing")
        
    except ValueError as e:
        logger.warning(f"Enhanced query validation failed: {e}")
        raise HTTPException(status_code=400, detail=str(e))
        
    except PermissionError as e:
        logger.warning(f"Enhanced query authorization failed: {e}")
        raise HTTPException(status_code=403, detail="Access denied")
        
    except FileNotFoundError as e:
        logger.warning(f"Resource not found: {e}")
        raise HTTPException(status_code=404, detail="Resource not found")
        
    except Exception as e:
        logger.error(f"Enhanced query processing failed unexpectedly: {e}")
        raise HTTPException(
            status_code=500, 
            detail="Internal server error during enhanced query processing"
        )


@router.post("/plan-strategy", response_model=Dict[str, Any])
@trace("enhanced_api_plan_strategy")
async def plan_strategy(
    query: str,
    context: Dict[str, Any],
    planning_service: PlanningService = Depends(get_planning_service)
) -> Dict[str, Any]:
    """
    Generate strategic response plan for troubleshooting query
    
    This endpoint provides strategic planning capabilities including:
    - Problem analysis and decomposition
    - Solution strategy development  
    - Risk assessment and mitigation
    - Success criteria definition
    
    Args:
        query: Troubleshooting query or problem description
        context: Available context including environment, urgency, resources
        planning_service: Injected PlanningService from DI container
        
    Returns:
        Strategic plan with problem analysis, solution strategy, and execution guidance
        
    Raises:
        HTTPException: On planning service errors
    """
    logger.info(f"Received strategy planning request for query: {query[:50]}...")
    
    try:
        # Validate inputs
        if not query or not query.strip():
            raise HTTPException(status_code=400, detail="Query cannot be empty")
        
        if not isinstance(context, dict):
            raise HTTPException(status_code=400, detail="Context must be a dictionary")
        
        # Generate strategic plan
        strategic_plan = await planning_service.plan_response_strategy(query, context)
        
        # Convert to API response format
        plan_response = {
            "plan_id": strategic_plan.plan_id,
            "problem_analysis": strategic_plan.problem_analysis,
            "solution_strategy": strategic_plan.solution_strategy,
            "risk_assessment": strategic_plan.risk_assessment,
            "success_criteria": strategic_plan.success_criteria,
            "estimated_effort": strategic_plan.estimated_effort,
            "confidence_score": strategic_plan.confidence_score,
            "execution_guidance": getattr(strategic_plan, 'execution_guidance', {}),
            "created_at": getattr(strategic_plan, 'created_at', None)
        }
        
        logger.info(
            f"Successfully generated strategic plan {strategic_plan.plan_id} "
            f"with confidence {strategic_plan.confidence_score}"
        )
        return plan_response
        
    except PlanningException as e:
        logger.warning(f"Strategic planning failed: {e}")
        raise HTTPException(status_code=503, detail="Planning service temporarily unavailable")
    
    except ValidationException as e:
        logger.warning(f"Strategy planning validation failed: {e}")
        raise HTTPException(status_code=422, detail=str(e))
        
    except Exception as e:
        logger.error(f"Strategic planning failed unexpectedly: {e}")
        raise HTTPException(
            status_code=500, 
            detail="Internal server error during strategic planning"
        )


@router.post("/decompose-problem", response_model=Dict[str, Any])
@trace("enhanced_api_decompose_problem")
async def decompose_problem(
    problem: str,
    context: Dict[str, Any],
    planning_service: PlanningService = Depends(get_planning_service)
) -> Dict[str, Any]:
    """
    Decompose complex problem into manageable components
    
    This endpoint provides problem decomposition capabilities including:
    - Primary issue identification
    - Contributing factors analysis
    - Dependency mapping
    - Priority ranking
    
    Args:
        problem: Complex problem description requiring decomposition
        context: Problem context including system information and constraints
        planning_service: Injected PlanningService from DI container
        
    Returns:
        Problem components with primary issue, factors, dependencies, and priorities
        
    Raises:
        HTTPException: On planning service errors
    """
    logger.info(f"Received problem decomposition request for: {problem[:50]}...")
    
    try:
        # Validate inputs
        if not problem or not problem.strip():
            raise HTTPException(status_code=400, detail="Problem description cannot be empty")
        
        if len(problem) < 10:
            raise HTTPException(status_code=400, detail="Problem description too short (minimum 10 characters)")
        
        if not isinstance(context, dict):
            raise HTTPException(status_code=400, detail="Context must be a dictionary")
        
        # Decompose problem
        problem_components = await planning_service.decompose_problem(problem, context)
        
        # Convert to API response format
        components_response = {
            "primary_issue": problem_components.primary_issue,
            "contributing_factors": problem_components.contributing_factors,
            "dependencies": problem_components.dependencies,
            "complexity_assessment": problem_components.complexity_assessment,
            "priority_ranking": problem_components.priority_ranking,
            "decomposition_metadata": getattr(problem_components, 'metadata', {})
        }
        
        logger.info(
            f"Successfully decomposed problem into {len(problem_components.contributing_factors)} components "
            f"with complexity level {problem_components.complexity_assessment.get('level', 'unknown')}"
        )
        return components_response
        
    except PlanningException as e:
        logger.warning(f"Problem decomposition failed: {e}")
        raise HTTPException(status_code=503, detail="Planning service temporarily unavailable")
    
    except ValidationException as e:
        logger.warning(f"Problem decomposition validation failed: {e}")
        raise HTTPException(status_code=422, detail=str(e))
        
    except Exception as e:
        logger.error(f"Problem decomposition failed unexpectedly: {e}")
        raise HTTPException(
            status_code=500, 
            detail="Internal server error during problem decomposition"
        )


@router.get("/memory/context/{session_id}", response_model=Dict[str, Any])
@trace("enhanced_api_memory_context")
async def get_conversation_context(
    session_id: str,
    query: str,
    memory_service: MemoryService = Depends(get_memory_service)
) -> Dict[str, Any]:
    """
    Retrieve conversation context for enhanced responses
    
    This endpoint provides memory-enhanced context including:
    - Conversation history analysis
    - Relevant insights from past interactions
    - User profile and preferences
    - Domain context and patterns
    
    Args:
        session_id: Session identifier for context scope
        query: Current query for context relevance matching
        memory_service: Injected MemoryService from DI container
        
    Returns:
        Conversation context with history, insights, and user profile
        
    Raises:
        HTTPException: On memory service errors
    """
    logger.info(f"Received context retrieval request for session {session_id}")
    
    try:
        # Validate inputs
        if not session_id or not session_id.strip():
            raise HTTPException(status_code=400, detail="Session ID cannot be empty")
        
        if not query or not query.strip():
            raise HTTPException(status_code=400, detail="Query cannot be empty")
        
        # Retrieve conversation context
        context = await memory_service.retrieve_context(session_id, query)
        
        # Convert to API response format
        context_response = {
            "session_id": context.session_id,
            "conversation_history": context.conversation_history,
            "relevant_insights": context.relevant_insights,
            "user_profile": {
                "skill_level": context.user_profile.skill_level if context.user_profile else None,
                "communication_style": context.user_profile.preferred_communication_style if context.user_profile else None,
                "domain_expertise": context.user_profile.domain_expertise if context.user_profile else [],
                "interaction_patterns": context.user_profile.interaction_patterns if context.user_profile else {}
            },
            "domain_context": context.domain_context,
            "context_metadata": {
                "retrieval_timestamp": getattr(context, 'retrieval_timestamp', None),
                "relevance_score": getattr(context, 'relevance_score', None)
            }
        }
        
        logger.info(
            f"Successfully retrieved context for session {session_id} "
            f"with {len(context.conversation_history)} history items and "
            f"{len(context.relevant_insights)} insights"
        )
        return context_response
        
    except MemoryException as e:
        logger.warning(f"Context retrieval failed: {e}")
        raise HTTPException(status_code=503, detail="Memory service temporarily unavailable")
    
    except ValidationException as e:
        logger.warning(f"Context retrieval validation failed: {e}")
        raise HTTPException(status_code=422, detail=str(e))
        
    except Exception as e:
        logger.error(f"Context retrieval failed unexpectedly: {e}")
        raise HTTPException(
            status_code=500, 
            detail="Internal server error during context retrieval"
        )


@router.post("/memory/consolidate/{session_id}")
@trace("enhanced_api_memory_consolidate")
async def consolidate_insights(
    session_id: str,
    result: Dict[str, Any],
    memory_service: MemoryService = Depends(get_memory_service)
) -> Dict[str, Any]:
    """
    Consolidate insights from troubleshooting results into memory
    
    This endpoint processes troubleshooting results to extract patterns,
    insights, and learning for future interactions.
    
    Args:
        session_id: Session identifier for context attribution
        result: Troubleshooting result containing findings, solutions, and outcomes
        memory_service: Injected MemoryService from DI container
        
    Returns:
        Consolidation status and metadata
        
    Raises:
        HTTPException: On memory service errors
    """
    logger.info(f"Received insight consolidation request for session {session_id}")
    
    try:
        # Validate inputs
        if not session_id or not session_id.strip():
            raise HTTPException(status_code=400, detail="Session ID cannot be empty")
        
        if not isinstance(result, dict) or not result:
            raise HTTPException(status_code=400, detail="Result must be a non-empty dictionary")
        
        # Consolidate insights
        success = await memory_service.consolidate_insights(session_id, result)
        
        consolidation_response = {
            "session_id": session_id,
            "consolidation_success": success,
            "result_summary": {
                "findings_count": len(result.get("findings", [])),
                "recommendations_count": len(result.get("recommendations", [])),
                "has_root_cause": "root_cause" in result,
                "effectiveness_score": result.get("effectiveness", 0.0)
            },
            "processing_status": "initiated" if success else "failed"
        }
        
        logger.info(
            f"Successfully initiated insight consolidation for session {session_id} "
            f"with {len(result.get('findings', []))} findings"
        )
        return consolidation_response
        
    except MemoryException as e:
        logger.warning(f"Insight consolidation failed: {e}")
        raise HTTPException(status_code=503, detail="Memory service temporarily unavailable")
    
    except ValidationException as e:
        logger.warning(f"Insight consolidation validation failed: {e}")
        raise HTTPException(status_code=422, detail=str(e))
        
    except Exception as e:
        logger.error(f"Insight consolidation failed unexpectedly: {e}")
        raise HTTPException(
            status_code=500, 
            detail="Internal server error during insight consolidation"
        )


@router.get("/health")
@trace("enhanced_api_health")
async def enhanced_health_check(
    enhanced_agent_service: EnhancedAgentService = Depends(get_enhanced_agent_service),
    memory_service: MemoryService = Depends(get_memory_service),
    planning_service: PlanningService = Depends(get_planning_service)
):
    """
    Comprehensive health check for enhanced agent capabilities
    
    Returns:
        Health status for enhanced agent, memory, and planning services
    """
    try:
        # Get health status from all enhanced services
        enhanced_health = await enhanced_agent_service.health_check()
        memory_health = await memory_service.health_check()
        planning_health = await planning_service.health_check()
        
        # Determine overall status
        all_services = [enhanced_health, memory_health, planning_health]
        overall_status = "healthy"
        
        for service_health in all_services:
            service_status = service_health.get("status", "unknown")
            if service_status == "degraded":
                overall_status = "degraded"
            elif service_status == "unhealthy":
                overall_status = "unhealthy"
                break
        
        return {
            "status": overall_status,
            "service": "enhanced_agent",
            "components": {
                "enhanced_agent_service": enhanced_health,
                "memory_service": memory_health,
                "planning_service": planning_health
            },
            "capabilities": {
                "memory_enhanced_responses": memory_health.get("status") == "healthy",
                "strategic_planning": planning_health.get("status") == "healthy",
                "problem_decomposition": planning_health.get("status") == "healthy",
                "context_retrieval": memory_health.get("status") == "healthy",
                "insight_consolidation": memory_health.get("status") == "healthy"
            }
        }
        
    except Exception as e:
        logger.error(f"Enhanced agent health check failed: {e}")
        raise HTTPException(
            status_code=503, 
            detail="Enhanced agent service unavailable"
        )


@router.get("/metrics")
@trace("enhanced_api_metrics")
async def get_enhanced_metrics(
    enhanced_agent_service: EnhancedAgentService = Depends(get_enhanced_agent_service),
    memory_service: MemoryService = Depends(get_memory_service),
    planning_service: PlanningService = Depends(get_planning_service)
):
    """
    Get performance metrics for enhanced intelligence services
    
    Returns:
        Performance metrics and usage statistics
    """
    try:
        # Get metrics from all enhanced services
        enhanced_health = await enhanced_agent_service.health_check()
        memory_health = await memory_service.health_check()
        planning_health = await planning_service.health_check()
        
        # Extract performance metrics
        metrics = {
            "enhanced_agent": {
                "queries_processed": enhanced_health.get("performance_metrics", {}).get("queries_processed", 0),
                "avg_response_time": enhanced_health.get("performance_metrics", {}).get("avg_response_time", 0.0),
                "success_rate": enhanced_health.get("performance_metrics", {}).get("success_rate", 0.0)
            },
            "memory_service": {
                "context_retrievals": memory_health.get("performance_metrics", {}).get("context_retrievals", 0),
                "consolidations": memory_health.get("performance_metrics", {}).get("consolidations", 0),
                "avg_retrieval_time": memory_health.get("performance_metrics", {}).get("avg_retrieval_time", 0.0),
                "cache_hits": memory_health.get("performance_metrics", {}).get("cache_hits", 0)
            },
            "planning_service": {
                "strategies_planned": planning_health.get("performance_metrics", {}).get("strategies_planned", 0),
                "problems_decomposed": planning_health.get("performance_metrics", {}).get("problems_decomposed", 0),
                "plans_adapted": planning_health.get("performance_metrics", {}).get("plans_adapted", 0),
                "avg_planning_time": planning_health.get("performance_metrics", {}).get("avg_planning_time", 0.0)
            }
        }
        
        return {
            "timestamp": None,  # Add current timestamp
            "service": "enhanced_agent",
            "metrics": metrics,
            "system_status": "operational"
        }
        
    except Exception as e:
        logger.error(f"Enhanced metrics retrieval failed: {e}")
        raise HTTPException(
            status_code=500, 
            detail="Failed to retrieve enhanced metrics"
        )