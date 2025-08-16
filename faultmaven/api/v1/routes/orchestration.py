"""Orchestration API Routes

This module provides REST API endpoints for multi-step troubleshooting 
workflow orchestration, enabling comprehensive troubleshooting workflows
that coordinate memory, planning, reasoning, and knowledge systems.

Key Endpoints:
- POST /workflows - Create new troubleshooting workflow
- POST /workflows/{workflow_id}/steps - Execute workflow step
- GET /workflows/{workflow_id}/status - Get workflow status
- POST /workflows/{workflow_id}/pause - Pause workflow
- POST /workflows/{workflow_id}/resume - Resume workflow
- GET /workflows/{workflow_id}/recommendations - Get workflow recommendations
- GET /workflows - List active workflows
"""

import logging
from typing import Dict, List, Any, Optional

from fastapi import APIRouter, HTTPException, Depends, status, BackgroundTasks
from pydantic import BaseModel, Field, validator

from faultmaven.api.v1.dependencies import get_orchestration_service
from faultmaven.services.orchestration_service import OrchestrationService
from faultmaven.exceptions import ValidationException, ServiceException


# Request/Response Models

class WorkflowCreateRequest(BaseModel):
    """Request model for creating a troubleshooting workflow"""
    session_id: str = Field(..., description="Session identifier", min_length=1)
    case_id: str = Field(..., description="Case identifier for this troubleshooting workflow", min_length=1)
    user_id: str = Field(..., description="User identifier", min_length=1)
    problem_description: str = Field(
        ..., 
        description="Description of the problem to be solved",
        min_length=10,
        max_length=2000
    )
    context: Optional[Dict[str, Any]] = Field(
        None, 
        description="Additional context information (service names, environment, etc.)"
    )
    priority_level: str = Field(
        "medium", 
        description="Priority level for the troubleshooting workflow",
        pattern="^(low|medium|high|critical)$"
    )
    domain_expertise: str = Field(
        "general",
        description="User's domain expertise level",
        pattern="^(novice|intermediate|expert|general)$"
    )
    time_constraints: Optional[int] = Field(
        None,
        description="Time constraints in seconds (optional)",
        ge=60,
        le=86400  # Max 24 hours
    )

    @validator('context')
    def validate_context(cls, v):
        if v is not None and not isinstance(v, dict):
            raise ValueError('Context must be a dictionary')
        return v


class StepExecuteRequest(BaseModel):
    """Request model for executing a workflow step"""
    step_inputs: Optional[Dict[str, Any]] = Field(
        None,
        description="Optional inputs for step execution"
    )
    user_feedback: Optional[Dict[str, Any]] = Field(
        None,
        description="Optional user feedback from previous step"
    )


class WorkflowPauseRequest(BaseModel):
    """Request model for pausing a workflow"""
    reason: Optional[str] = Field(
        None,
        description="Optional reason for pausing the workflow",
        max_length=500
    )


class WorkflowCreateResponse(BaseModel):
    """Response model for workflow creation"""
    success: bool
    workflow_id: str
    workflow_details: Dict[str, Any]
    current_step: Optional[Dict[str, Any]]
    strategic_insights: List[str]
    memory_enhancements: int
    next_action: str


class StepExecuteResponse(BaseModel):
    """Response model for step execution"""
    success: bool
    workflow_id: str
    step_execution: Dict[str, Any]
    workflow_progress: Dict[str, Any]
    next_step: Optional[Dict[str, Any]]
    adaptive_changes: List[str]
    execution_time: float
    recommendations: Dict[str, Any]
    workflow_complete: Optional[bool] = None
    completion_summary: Optional[Dict[str, Any]] = None


class WorkflowStatusResponse(BaseModel):
    """Response model for workflow status"""
    success: bool
    workflow_id: str
    status: str
    progress: Dict[str, Any]
    findings_summary: Dict[str, Any]
    performance: Dict[str, Any]
    timeline: Dict[str, Any]
    service_metadata: Dict[str, Any]


class WorkflowRecommendationsResponse(BaseModel):
    """Response model for workflow recommendations"""
    success: bool
    workflow_id: str
    recommendations: Dict[str, List[str]]
    optimization_score: float
    next_optimizations: List[str]


# Initialize router
router = APIRouter(prefix="/orchestration", tags=["Multi-Step Troubleshooting"])
logger = logging.getLogger(__name__)


@router.post(
    "/workflows",
    response_model=WorkflowCreateResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Create Troubleshooting Workflow",
    description="""
    Create a new multi-step troubleshooting workflow that coordinates memory, 
    planning, reasoning, and knowledge systems to provide systematic problem resolution.
    
    The workflow will be customized based on:
    - Problem description and context
    - User's domain expertise level  
    - Priority level and time constraints
    - Available memory insights and patterns
    - Strategic planning recommendations
    
    Returns the workflow ID and first step information for execution.
    """
)
async def create_workflow(
    request: WorkflowCreateRequest,
    orchestration_service: OrchestrationService = Depends(get_orchestration_service)
):
    """Create a new troubleshooting workflow"""
    try:
        logger.info(f"Creating workflow for case: {request.case_id}")
        
        result = await orchestration_service.create_troubleshooting_workflow(
            session_id=request.session_id,
            case_id=request.case_id,
            user_id=request.user_id,
            problem_description=request.problem_description,
            context=request.context,
            priority_level=request.priority_level,
            domain_expertise=request.domain_expertise,
            time_constraints=request.time_constraints
        )
        
        return WorkflowCreateResponse(**result)
        
    except ValidationException as e:
        logger.warning(f"Workflow creation validation failed: {e}")
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Validation error: {str(e)}"
        )
    except ServiceException as e:
        logger.error(f"Workflow creation service error: {e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Service error: {str(e)}"
        )
    except Exception as e:
        logger.error(f"Unexpected error in workflow creation: {e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="An unexpected error occurred while creating the workflow"
        )


@router.post(
    "/workflows/{workflow_id}/steps",
    response_model=StepExecuteResponse,
    summary="Execute Workflow Step",
    description="""
    Execute the next step in a troubleshooting workflow.
    
    Each step execution involves:
    - Retrieving and applying memory context
    - Executing reasoning workflows specific to the step type
    - Gathering relevant knowledge from the knowledge base
    - Synthesizing results and determining next steps
    - Adaptive workflow adjustment based on findings
    
    Returns step results, progress information, and next step details.
    """
)
async def execute_step(
    workflow_id: str,
    request: StepExecuteRequest,
    orchestration_service: OrchestrationService = Depends(get_orchestration_service)
):
    """Execute the next step in a troubleshooting workflow"""
    try:
        logger.info(f"Executing step for workflow: {workflow_id}")
        
        result = await orchestration_service.execute_workflow_step(
            workflow_id=workflow_id,
            step_inputs=request.step_inputs,
            user_feedback=request.user_feedback
        )
        
        return StepExecuteResponse(**result)
        
    except ValidationException as e:
        logger.warning(f"Step execution validation failed: {e}")
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Validation error: {str(e)}"
        )
    except ServiceException as e:
        logger.error(f"Step execution service error: {e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Service error: {str(e)}"
        )
    except Exception as e:
        logger.error(f"Unexpected error in step execution: {e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="An unexpected error occurred while executing the workflow step"
        )


@router.get(
    "/workflows/{workflow_id}/status",
    response_model=WorkflowStatusResponse,
    summary="Get Workflow Status",
    description="""
    Get comprehensive status and progress information for a troubleshooting workflow.
    
    Returns:
    - Current workflow status and progress percentage
    - Summary of findings and knowledge gathered
    - Performance metrics and execution timeline
    - Service metadata and capabilities
    
    Use this endpoint to monitor workflow progress and performance.
    """
)
async def get_status(
    workflow_id: str,
    orchestration_service: OrchestrationService = Depends(get_orchestration_service)
):
    """Get workflow status and progress"""
    try:
        logger.info(f"Getting status for workflow: {workflow_id}")
        
        result = await orchestration_service.get_workflow_status(workflow_id)
        
        return WorkflowStatusResponse(**result)
        
    except ValidationException as e:
        logger.warning(f"Status retrieval validation failed: {e}")
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Validation error: {str(e)}"
        )
    except ServiceException as e:
        if "not found" in str(e).lower():
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Workflow not found: {workflow_id}"
            )
        logger.error(f"Status retrieval service error: {e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Service error: {str(e)}"
        )
    except Exception as e:
        logger.error(f"Unexpected error in status retrieval: {e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="An unexpected error occurred while retrieving workflow status"
        )


@router.post(
    "/workflows/{workflow_id}/pause",
    summary="Pause Workflow",
    description="""
    Pause a troubleshooting workflow for later resumption.
    
    Paused workflows maintain their state and can be resumed at any time.
    This is useful for:
    - Taking breaks during long troubleshooting sessions
    - Waiting for additional information or resources
    - Coordinating with team members
    - Handling interruptions or priority changes
    
    Returns pause confirmation and resumption instructions.
    """
)
async def pause_workflow(
    workflow_id: str,
    request: WorkflowPauseRequest,
    orchestration_service: OrchestrationService = Depends(get_orchestration_service)
):
    """Pause a troubleshooting workflow"""
    try:
        logger.info(f"Pausing workflow: {workflow_id}")
        
        result = await orchestration_service.pause_workflow(
            workflow_id=workflow_id,
            reason=request.reason
        )
        
        return result
        
    except ValidationException as e:
        logger.warning(f"Workflow pause validation failed: {e}")
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Validation error: {str(e)}"
        )
    except ServiceException as e:
        if "not found" in str(e).lower():
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Workflow not found: {workflow_id}"
            )
        elif "cannot pause" in str(e).lower():
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=str(e)
            )
        logger.error(f"Workflow pause service error: {e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Service error: {str(e)}"
        )
    except Exception as e:
        logger.error(f"Unexpected error in workflow pause: {e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="An unexpected error occurred while pausing the workflow"
        )


@router.post(
    "/workflows/{workflow_id}/resume",
    summary="Resume Workflow",
    description="""
    Resume a paused troubleshooting workflow.
    
    The workflow will continue from where it was paused, maintaining all
    previous context, findings, and progress. This includes:
    - All accumulated findings and knowledge
    - Memory context and patterns
    - Performance metrics and timeline
    - Current step position and next actions
    
    Returns resumption confirmation and next step information.
    """
)
async def resume_workflow(
    workflow_id: str,
    orchestration_service: OrchestrationService = Depends(get_orchestration_service)
):
    """Resume a paused troubleshooting workflow"""
    try:
        logger.info(f"Resuming workflow: {workflow_id}")
        
        result = await orchestration_service.resume_workflow(workflow_id)
        
        return result
        
    except ValidationException as e:
        logger.warning(f"Workflow resume validation failed: {e}")
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Validation error: {str(e)}"
        )
    except ServiceException as e:
        if "not found" in str(e).lower():
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Workflow not found: {workflow_id}"
            )
        elif "not suspended" in str(e).lower():
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=str(e)
            )
        logger.error(f"Workflow resume service error: {e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Service error: {str(e)}"
        )
    except Exception as e:
        logger.error(f"Unexpected error in workflow resume: {e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="An unexpected error occurred while resuming the workflow"
        )


@router.get(
    "/workflows/{workflow_id}/recommendations",
    response_model=WorkflowRecommendationsResponse,
    summary="Get Workflow Recommendations",
    description="""
    Get intelligent recommendations for workflow optimization and improvement.
    
    Analyzes current workflow performance and provides recommendations for:
    - Performance optimization (reducing execution time)
    - Methodology improvements (better step sequencing)
    - Efficiency enhancements (more effective information gathering)
    - Quality improvements (better findings and insights)
    
    Returns an optimization score and actionable recommendations.
    """
)
async def get_recommendations(
    workflow_id: str,
    orchestration_service: OrchestrationService = Depends(get_orchestration_service)
):
    """Get workflow optimization recommendations"""
    try:
        logger.info(f"Getting recommendations for workflow: {workflow_id}")
        
        result = await orchestration_service.get_workflow_recommendations(workflow_id)
        
        return WorkflowRecommendationsResponse(**result)
        
    except ValidationException as e:
        logger.warning(f"Recommendations validation failed: {e}")
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Validation error: {str(e)}"
        )
    except ServiceException as e:
        if "not found" in str(e).lower():
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Workflow not found: {workflow_id}"
            )
        logger.error(f"Recommendations service error: {e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Service error: {str(e)}"
        )
    except Exception as e:
        logger.error(f"Unexpected error in recommendations: {e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="An unexpected error occurred while retrieving recommendations"
        )


@router.get(
    "/workflows",
    summary="List Active Workflows",
    description="""
    List active troubleshooting workflows with optional filtering.
    
    Returns:
    - List of active workflows with basic information
    - Summary statistics (total workflows, status distribution)
    - Service performance metrics
    
    Use optional user_id parameter to filter workflows for a specific user.
    """
)
async def list_workflows(
    user_id: Optional[str] = None,
    orchestration_service: OrchestrationService = Depends(get_orchestration_service)
):
    """List active troubleshooting workflows"""
    try:
        logger.info(f"Listing workflows (user_id: {user_id})")
        
        result = await orchestration_service.list_active_workflows(user_id)
        
        return result
        
    except ServiceException as e:
        logger.error(f"Workflow listing service error: {e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Service error: {str(e)}"
        )
    except Exception as e:
        logger.error(f"Unexpected error in workflow listing: {e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="An unexpected error occurred while listing workflows"
        )


@router.get(
    "/health",
    summary="Orchestration Service Health Check",
    description="""
    Check the health and status of the orchestration service and all its dependencies.
    
    Returns comprehensive health information including:
    - Service status and capabilities
    - Dependency health (memory, planning, reasoning, knowledge services)
    - Performance metrics and uptime
    - System capabilities and feature availability
    
    Use this endpoint for monitoring and service discovery.
    """
)
async def health_check(
    orchestration_service: OrchestrationService = Depends(get_orchestration_service)
):
    """Check orchestration service health"""
    try:
        health_info = await orchestration_service.health_check()
        
        # Return appropriate HTTP status based on health
        if health_info.get("status") == "healthy":
            return health_info
        elif health_info.get("status") == "degraded":
            # Service is degraded but functional
            return health_info
        else:
            # Service is unhealthy
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail=health_info
            )
            
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Health check failed: {e}")
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={"status": "unhealthy", "error": str(e)}
        )