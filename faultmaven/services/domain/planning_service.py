"""Planning Service - Strategic Planning and Execution Planning

Provides intelligent planning capabilities for the Agentic Framework,
creating execution plans, strategies, and coordinating multi-step workflows.
"""

import logging
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional, Tuple
from dataclasses import dataclass, field
from enum import Enum
import json
import uuid

logger = logging.getLogger(__name__)


class PlanType(Enum):
    """Types of plans that can be created."""
    INVESTIGATION = "investigation"
    TROUBLESHOOTING = "troubleshooting"  
    EXECUTION = "execution"
    WORKFLOW = "workflow"
    CONTINGENCY = "contingency"


class PlanStatus(Enum):
    """Status of a plan."""
    DRAFT = "draft"
    APPROVED = "approved"
    EXECUTING = "executing"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


@dataclass
class PlanStep:
    """A single step in a plan."""
    id: str
    name: str
    description: str
    dependencies: List[str] = field(default_factory=list)
    estimated_duration: Optional[int] = None  # minutes
    status: str = "pending"
    metadata: Dict[str, Any] = field(default_factory=dict)


@dataclass
class ExecutionPlan:
    """A complete execution plan."""
    id: str
    name: str
    description: str
    plan_type: PlanType
    status: PlanStatus
    steps: List[PlanStep]
    created_at: datetime
    updated_at: datetime
    metadata: Dict[str, Any] = field(default_factory=dict)
    estimated_total_duration: Optional[int] = None  # minutes


class PlanningService:
    """Service for creating and managing execution plans."""
    
    def __init__(self):
        """Initialize the planning service."""
        self._plans: Dict[str, ExecutionPlan] = {}
        self._session_plans: Dict[str, List[str]] = {}  # session_id -> plan_ids
        
    async def create_execution_plan(
        self,
        session_id: str,
        query: str,
        context: Optional[Dict[str, Any]] = None
    ) -> str:
        """Create an execution plan for a query.
        
        Args:
            session_id: Session identifier
            query: User query to plan for
            context: Optional context information
            
        Returns:
            Plan ID
        """
        try:
            plan_id = str(uuid.uuid4())
            
            # Analyze query to determine plan type and steps
            plan_type, steps = await self._analyze_query_for_planning(query, context or {})
            
            # Create the execution plan
            plan = ExecutionPlan(
                id=plan_id,
                name=f"Plan for: {query[:50]}...",
                description=f"Execution plan created for query: {query}",
                plan_type=plan_type,
                status=PlanStatus.DRAFT,
                steps=steps,
                created_at=datetime.utcnow(),
                updated_at=datetime.utcnow(),
                metadata={
                    "session_id": session_id,
                    "original_query": query,
                    "context": context or {}
                }
            )
            
            # Calculate total estimated duration
            plan.estimated_total_duration = sum(
                step.estimated_duration or 5 for step in steps
            )
            
            # Store the plan
            self._plans[plan_id] = plan
            
            # Associate with session
            if session_id not in self._session_plans:
                self._session_plans[session_id] = []
            self._session_plans[session_id].append(plan_id)
            
            logger.info(f"Created execution plan {plan_id} for session {session_id}")
            return plan_id
            
        except Exception as e:
            logger.error(f"Failed to create execution plan for session {session_id}: {e}")
            raise
    
    async def get_planning_state(self, session_id: str) -> Dict[str, Any]:
        """Get the current planning state for a session.
        
        Args:
            session_id: Session identifier
            
        Returns:
            Planning state information
        """
        try:
            plan_ids = self._session_plans.get(session_id, [])
            plans = []
            
            for plan_id in plan_ids:
                if plan_id in self._plans:
                    plan = self._plans[plan_id]
                    plans.append({
                        "id": plan.id,
                        "name": plan.name,
                        "type": plan.plan_type.value,
                        "status": plan.status.value,
                        "steps_count": len(plan.steps),
                        "estimated_duration": plan.estimated_total_duration,
                        "created_at": plan.created_at.isoformat() + 'Z',
                        "updated_at": plan.updated_at.isoformat() + 'Z'
                    })
            
            # Find the most recent active plan
            active_plan = None
            if plans:
                active_plans = [p for p in plans if p["status"] in ["draft", "approved", "executing"]]
                if active_plans:
                    active_plan = max(active_plans, key=lambda x: x["created_at"])
            
            planning_state = {
                "session_id": session_id,
                "total_plans": len(plans),
                "active_plan": active_plan,
                "all_plans": plans[-5:],  # Last 5 plans
                "planning_capability": "enabled",
                "last_updated": datetime.utcnow().isoformat() + 'Z'
            }
            
            return planning_state
            
        except Exception as e:
            logger.error(f"Failed to get planning state for session {session_id}: {e}")
            return {
                "session_id": session_id,
                "error": str(e),
                "planning_capability": "error"
            }
    
    async def update_execution_plan(
        self,
        plan_id: str,
        updates: Dict[str, Any]
    ) -> bool:
        """Update an execution plan.
        
        Args:
            plan_id: Plan identifier
            updates: Updates to apply
            
        Returns:
            True if successful, False otherwise
        """
        try:
            if plan_id not in self._plans:
                logger.warning(f"Plan {plan_id} not found for update")
                return False
            
            plan = self._plans[plan_id]
            
            # Update allowed fields
            if "status" in updates:
                try:
                    plan.status = PlanStatus(updates["status"])
                except ValueError:
                    logger.warning(f"Invalid status value: {updates['status']}")
            
            if "metadata" in updates and isinstance(updates["metadata"], dict):
                plan.metadata.update(updates["metadata"])
            
            # Update step statuses if provided
            if "step_updates" in updates:
                step_updates = updates["step_updates"]
                for step in plan.steps:
                    if step.id in step_updates:
                        step.status = step_updates[step.id].get("status", step.status)
                        if "metadata" in step_updates[step.id]:
                            step.metadata.update(step_updates[step.id]["metadata"])
            
            plan.updated_at = datetime.utcnow()
            
            logger.debug(f"Updated execution plan {plan_id}")
            return True
            
        except Exception as e:
            logger.error(f"Failed to update execution plan {plan_id}: {e}")
            return False
    
    async def get_execution_plan(self, plan_id: str) -> Optional[Dict[str, Any]]:
        """Get a specific execution plan.
        
        Args:
            plan_id: Plan identifier
            
        Returns:
            Plan information or None if not found
        """
        try:
            if plan_id not in self._plans:
                return None
            
            plan = self._plans[plan_id]
            
            plan_data = {
                "id": plan.id,
                "name": plan.name,
                "description": plan.description,
                "type": plan.plan_type.value,
                "status": plan.status.value,
                "created_at": plan.created_at.isoformat() + 'Z',
                "updated_at": plan.updated_at.isoformat() + 'Z',
                "estimated_total_duration": plan.estimated_total_duration,
                "metadata": plan.metadata,
                "steps": [
                    {
                        "id": step.id,
                        "name": step.name,
                        "description": step.description,
                        "dependencies": step.dependencies,
                        "estimated_duration": step.estimated_duration,
                        "status": step.status,
                        "metadata": step.metadata
                    }
                    for step in plan.steps
                ]
            }
            
            return plan_data
            
        except Exception as e:
            logger.error(f"Failed to get execution plan {plan_id}: {e}")
            return None
    
    async def _analyze_query_for_planning(
        self,
        query: str,
        context: Dict[str, Any]
    ) -> Tuple[PlanType, List[PlanStep]]:
        """Analyze a query to determine the appropriate plan type and steps.
        
        Args:
            query: User query
            context: Query context
            
        Returns:
            Tuple of (plan_type, steps)
        """
        try:
            query_lower = query.lower()
            
            # Determine plan type based on query content
            if any(keyword in query_lower for keyword in ["troubleshoot", "debug", "fix", "error", "issue"]):
                plan_type = PlanType.TROUBLESHOOTING
                steps = await self._create_troubleshooting_steps(query, context)
            elif any(keyword in query_lower for keyword in ["investigate", "analyze", "understand", "explain"]):
                plan_type = PlanType.INVESTIGATION
                steps = await self._create_investigation_steps(query, context)
            elif any(keyword in query_lower for keyword in ["execute", "run", "implement", "deploy"]):
                plan_type = PlanType.EXECUTION
                steps = await self._create_execution_steps(query, context)
            else:
                plan_type = PlanType.WORKFLOW
                steps = await self._create_general_workflow_steps(query, context)
            
            return plan_type, steps
            
        except Exception as e:
            logger.error(f"Failed to analyze query for planning: {e}")
            # Return basic workflow as fallback
            return PlanType.WORKFLOW, [
                PlanStep(
                    id="basic_analysis",
                    name="Analyze Request",
                    description="Perform basic analysis of the user request",
                    estimated_duration=5
                ),
                PlanStep(
                    id="provide_response",
                    name="Provide Response", 
                    description="Generate and provide response to the user",
                    dependencies=["basic_analysis"],
                    estimated_duration=3
                )
            ]
    
    async def _create_troubleshooting_steps(
        self,
        query: str,
        context: Dict[str, Any]
    ) -> List[PlanStep]:
        """Create troubleshooting plan steps."""
        steps = [
            PlanStep(
                id="define_blast_radius",
                name="Define Blast Radius",
                description="Determine the scope and impact of the issue",
                estimated_duration=5,
                metadata={"phase": "1_blast_radius"}
            ),
            PlanStep(
                id="establish_timeline",
                name="Establish Timeline",
                description="Determine when the issue started and progression",
                estimated_duration=5,
                metadata={"phase": "2_timeline"}
            ),
            PlanStep(
                id="formulate_hypothesis",
                name="Formulate Hypothesis",
                description="Generate potential root causes based on evidence",
                dependencies=["define_blast_radius", "establish_timeline"],
                estimated_duration=8,
                metadata={"phase": "3_hypothesis"}
            ),
            PlanStep(
                id="validate_hypothesis",
                name="Validate Hypothesis",
                description="Test and validate the most likely hypotheses",
                dependencies=["formulate_hypothesis"],
                estimated_duration=10,
                metadata={"phase": "4_validation"}
            ),
            PlanStep(
                id="propose_solution",
                name="Propose Solution",
                description="Recommend solutions based on validated root cause",
                dependencies=["validate_hypothesis"],
                estimated_duration=7,
                metadata={"phase": "5_solution"}
            )
        ]
        return steps
    
    async def _create_investigation_steps(
        self,
        query: str,
        context: Dict[str, Any]
    ) -> List[PlanStep]:
        """Create investigation plan steps."""
        steps = [
            PlanStep(
                id="gather_information",
                name="Gather Information",
                description="Collect relevant data and context",
                estimated_duration=5
            ),
            PlanStep(
                id="analyze_data",
                name="Analyze Data",
                description="Analyze collected information for patterns",
                dependencies=["gather_information"],
                estimated_duration=8
            ),
            PlanStep(
                id="synthesize_findings",
                name="Synthesize Findings",
                description="Compile analysis into actionable insights",
                dependencies=["analyze_data"],
                estimated_duration=5
            )
        ]
        return steps
    
    async def _create_execution_steps(
        self,
        query: str,
        context: Dict[str, Any]
    ) -> List[PlanStep]:
        """Create execution plan steps."""
        steps = [
            PlanStep(
                id="validate_requirements",
                name="Validate Requirements",
                description="Ensure all requirements are met for execution",
                estimated_duration=3
            ),
            PlanStep(
                id="prepare_execution",
                name="Prepare for Execution",
                description="Set up environment and dependencies",
                dependencies=["validate_requirements"],
                estimated_duration=5
            ),
            PlanStep(
                id="execute_task",
                name="Execute Task",
                description="Perform the requested execution",
                dependencies=["prepare_execution"],
                estimated_duration=10
            ),
            PlanStep(
                id="verify_results",
                name="Verify Results", 
                description="Validate execution results and success",
                dependencies=["execute_task"],
                estimated_duration=3
            )
        ]
        return steps
    
    async def _create_general_workflow_steps(
        self,
        query: str,
        context: Dict[str, Any]
    ) -> List[PlanStep]:
        """Create general workflow steps."""
        steps = [
            PlanStep(
                id="analyze_request",
                name="Analyze Request",
                description="Understand the user's request and requirements",
                estimated_duration=3
            ),
            PlanStep(
                id="gather_resources",
                name="Gather Resources",
                description="Collect necessary information and tools",
                dependencies=["analyze_request"],
                estimated_duration=5
            ),
            PlanStep(
                id="process_request",
                name="Process Request",
                description="Execute the main processing logic",
                dependencies=["gather_resources"],
                estimated_duration=7
            ),
            PlanStep(
                id="deliver_response",
                name="Deliver Response",
                description="Format and deliver the final response",
                dependencies=["process_request"],
                estimated_duration=2
            )
        ]
        return steps