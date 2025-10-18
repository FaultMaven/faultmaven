"""Multi-Step Troubleshooting Orchestration System

This module implements a comprehensive troubleshooting orchestration system that
coordinates memory management, strategic planning, reasoning workflows, and
knowledge base search to provide intelligent, multi-step troubleshooting guidance.

Key Features:
- Orchestrated multi-step troubleshooting workflows
- Integration with enhanced agent service, memory, planning, and knowledge systems
- Dynamic workflow adaptation based on findings and context
- Cross-step knowledge sharing and context propagation
- Intelligent step prioritization and sequencing
- Performance tracking and optimization
"""

import logging
import time
from typing import Dict, List, Any, Optional, Tuple
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum

from faultmaven.models.interfaces import (
    IMemoryService, IPlanningService, ILLMProvider, ITracer, IVectorStore
)
from faultmaven.services.domain.knowledge_service import KnowledgeService
# ReasoningService removed during agentic framework migration
from faultmaven.exceptions import ServiceException


class TroubleshootingPhase(Enum):
    """Troubleshooting phases following SRE doctrine"""
    DEFINE_BLAST_RADIUS = "define_blast_radius"
    ESTABLISH_TIMELINE = "establish_timeline"  
    FORMULATE_HYPOTHESIS = "formulate_hypothesis"
    VALIDATE_HYPOTHESIS = "validate_hypothesis"
    PROPOSE_SOLUTION = "propose_solution"
    IMPLEMENTATION_PLANNING = "implementation_planning"
    VERIFICATION = "verification"


class WorkflowStatus(Enum):
    """Workflow execution status"""
    INITIALIZED = "initialized"
    IN_PROGRESS = "in_progress"
    STEP_COMPLETED = "step_completed"
    WAITING_INPUT = "waiting_input"
    COMPLETED = "completed"
    FAILED = "failed"
    SUSPENDED = "suspended"


@dataclass
class TroubleshootingStep:
    """Individual troubleshooting step"""
    step_id: str
    phase: TroubleshootingPhase
    title: str
    description: str
    reasoning_type: str
    knowledge_focus: Optional[str] = None
    required_inputs: List[str] = None
    dependencies: List[str] = None
    estimated_duration: int = 300  # seconds
    priority: int = 1  # 1=highest, 5=lowest
    
    def __post_init__(self):
        if self.required_inputs is None:
            self.required_inputs = []
        if self.dependencies is None:
            self.dependencies = []


@dataclass
class StepResult:
    """Result from executing a troubleshooting step"""
    step_id: str
    status: str
    findings: List[Dict[str, Any]]
    knowledge_gathered: List[Dict[str, Any]]
    insights: List[str]
    next_steps: List[str]
    confidence_score: float
    execution_time: float
    knowledge_gaps: List[str]
    recommendations: List[str]


@dataclass  
class WorkflowContext:
    """Context for troubleshooting workflow"""
    session_id: str
    case_id: str
    user_id: str
    problem_description: str
    initial_context: Dict[str, Any]
    priority_level: str = "medium"
    domain_expertise: str = "general"
    time_constraints: Optional[int] = None
    available_tools: List[str] = None
    
    def __post_init__(self):
        if self.available_tools is None:
            self.available_tools = []


class TroubleshootingOrchestrator:
    """Multi-Step Troubleshooting Orchestration System
    
    This orchestrator coordinates all enhanced intelligence systems to provide
    comprehensive, multi-step troubleshooting workflows that adapt based on
    findings and context, ensuring systematic problem resolution.
    
    Key Capabilities:
    - Dynamic workflow generation based on problem context
    - Intelligent step sequencing and prioritization
    - Cross-step knowledge sharing and context propagation
    - Adaptive reasoning strategy selection
    - Performance tracking and workflow optimization
    - Integration with memory, planning, reasoning, and knowledge systems
    
    Performance Targets:
    - Workflow initialization: < 200ms
    - Step execution: < 2000ms
    - Context propagation: < 50ms
    - Knowledge integration: < 500ms
    """
    
    def __init__(
        self,
        memory_service: Optional[IMemoryService] = None,
        planning_service: Optional[IPlanningService] = None,
        reasoning_service: Optional[Any] = None,  # Legacy parameter - service removed
        enhanced_knowledge_service: Optional[KnowledgeService] = None,
        llm_provider: Optional[ILLMProvider] = None,
        tracer: Optional[ITracer] = None
    ):
        """Initialize Troubleshooting Orchestrator
        
        Args:
            memory_service: Enhanced memory service for context management
            planning_service: Strategic planning service for workflow planning
            reasoning_service: Enhanced reasoning service for step execution
            enhanced_knowledge_service: Enhanced knowledge service for information retrieval
            llm_provider: LLM provider for intelligent analysis
            tracer: Tracing service for observability
        """
        self._memory = memory_service
        self._planning = planning_service
        self._reasoning = reasoning_service
        self._knowledge = enhanced_knowledge_service
        self._llm = llm_provider
        self._tracer = tracer
        self._logger = logging.getLogger(__name__)
        
        # Workflow state management
        self._active_workflows: Dict[str, Dict[str, Any]] = {}
        self._step_templates: Dict[TroubleshootingPhase, List[TroubleshootingStep]] = {}
        self._workflow_patterns: Dict[str, List[TroubleshootingPhase]] = {}
        
        # Performance metrics
        self._metrics = {
            "workflows_initiated": 0,
            "steps_executed": 0,
            "successful_resolutions": 0,
            "avg_workflow_duration": 0.0,
            "avg_step_execution_time": 0.0,
            "knowledge_integrations": 0,
            "adaptive_adjustments": 0
        }
        
        # Initialize step templates and workflow patterns
        self._initialize_step_templates()
        self._initialize_workflow_patterns()
    
    async def initiate_troubleshooting_workflow(
        self,
        context: WorkflowContext
    ) -> Dict[str, Any]:
        """Initiate a comprehensive troubleshooting workflow
        
        Args:
            context: Workflow context with problem details and constraints
            
        Returns:
            Dictionary with workflow details and first steps
            
        Raises:
            ServiceException: When workflow initiation fails
        """
        try:
            workflow_start = time.time()
            
            self._logger.info(f"Initiating troubleshooting workflow for case: {context.case_id}")
            
            # Generate unique workflow ID
            workflow_id = f"workflow_{context.case_id}_{int(time.time())}"
            
            # Retrieve memory context for personalization
            memory_context = {}
            if self._memory:
                try:
                    conversation_context = await self._memory.retrieve_context(
                        context.session_id, context.problem_description
                    )
                    memory_context = {
                        "insights": conversation_context.relevant_insights,
                        "domain": conversation_context.domain_context,
                        "patterns": conversation_context.interaction_patterns
                    }
                except Exception as e:
                    self._logger.warning(f"Memory context retrieval failed: {e}")
            
            # Generate strategic plan using planning service
            strategic_plan = None
            if self._planning:
                try:
                    strategic_plan = await self._planning.plan_response_strategy(
                        context.problem_description,
                        {
                            "urgency": context.priority_level,
                            "domain": context.domain_expertise,
                            "constraints": context.time_constraints,
                            "memory_context": memory_context
                        }
                    )
                except Exception as e:
                    self._logger.warning(f"Strategic planning failed: {e}")
            
            # Determine optimal workflow pattern
            workflow_pattern = self._determine_workflow_pattern(context, memory_context, strategic_plan)
            
            # Generate customized step sequence
            workflow_steps = await self._generate_workflow_steps(
                workflow_pattern, context, memory_context, strategic_plan
            )
            
            # Initialize workflow state
            workflow_state = {
                "workflow_id": workflow_id,
                "context": context,
                "memory_context": memory_context,
                "strategic_plan": strategic_plan,
                "pattern": workflow_pattern,
                "steps": workflow_steps,
                "current_step_index": 0,
                "status": WorkflowStatus.INITIALIZED,
                "findings": [],
                "knowledge_base": [],
                "execution_log": [],
                "created_at": datetime.now(timezone.utc),
                "estimated_completion": None,
                "performance_metrics": {
                    "initialization_time": (time.time() - workflow_start) * 1000,
                    "steps_completed": 0,
                    "total_knowledge_retrieved": 0,
                    "adaptive_changes": 0
                }
            }
            
            # Calculate estimated completion time
            total_duration = sum(step.estimated_duration for step in workflow_steps)
            workflow_state["estimated_completion"] = datetime.now(timezone.utc).timestamp() + total_duration
            
            # Store workflow state
            self._active_workflows[workflow_id] = workflow_state
            
            # Update metrics
            self._metrics["workflows_initiated"] += 1
            
            self._logger.info(
                f"Workflow {workflow_id} initiated with {len(workflow_steps)} steps "
                f"(pattern: {workflow_pattern}, estimated duration: {total_duration}s)"
            )
            
            return {
                "workflow_id": workflow_id,
                "pattern": workflow_pattern,
                "total_steps": len(workflow_steps),
                "estimated_duration": total_duration,
                "current_step": workflow_steps[0] if workflow_steps else None,
                "strategic_insights": strategic_plan.insights if strategic_plan else [],
                "memory_enhancements": len(memory_context.get("insights", [])),
                "initialization_time": workflow_state["performance_metrics"]["initialization_time"]
            }
            
        except Exception as e:
            self._logger.error(f"Workflow initiation failed: {e}")
            raise ServiceException(f"Failed to initiate troubleshooting workflow: {str(e)}")
    
    async def execute_workflow_step(
        self,
        workflow_id: str,
        step_inputs: Optional[Dict[str, Any]] = None
    ) -> Dict[str, Any]:
        """Execute the current step in a troubleshooting workflow
        
        Args:
            workflow_id: Unique workflow identifier
            step_inputs: Optional inputs for step execution
            
        Returns:
            Dictionary with step results and next step information
            
        Raises:
            ServiceException: When step execution fails
        """
        try:
            step_start = time.time()
            
            if workflow_id not in self._active_workflows:
                raise ServiceException(f"Workflow {workflow_id} not found")
            
            workflow_state = self._active_workflows[workflow_id]
            
            if workflow_state["status"] == WorkflowStatus.COMPLETED:
                return {"status": "completed", "message": "Workflow already completed"}
            
            if workflow_state["status"] == WorkflowStatus.FAILED:
                return {"status": "failed", "message": "Workflow has failed"}
            
            current_step_index = workflow_state["current_step_index"]
            workflow_steps = workflow_state["steps"]
            
            if current_step_index >= len(workflow_steps):
                # Workflow completed
                workflow_state["status"] = WorkflowStatus.COMPLETED
                self._metrics["successful_resolutions"] += 1
                return {"status": "completed", "message": "All steps completed"}
            
            current_step = workflow_steps[current_step_index]
            context = workflow_state["context"]
            memory_context = workflow_state["memory_context"]
            
            self._logger.info(f"Executing step {current_step_index + 1}/{len(workflow_steps)}: {current_step.title}")
            
            # Update workflow status
            workflow_state["status"] = WorkflowStatus.IN_PROGRESS
            
            # Execute reasoning for current step
            reasoning_result = None
            if self._reasoning:
                try:
                    reasoning_context = {
                        "step_context": {
                            "phase": current_step.phase.value,
                            "step_title": current_step.title,
                            "step_description": current_step.description,
                            "step_inputs": step_inputs or {}
                        },
                        "workflow_context": {
                            "problem_description": context.problem_description,
                            "priority_level": context.priority_level,
                            "domain_expertise": context.domain_expertise
                        },
                        "accumulated_findings": workflow_state["findings"],
                        "memory_context": memory_context,
                        "previous_steps": workflow_state["execution_log"]
                    }
                    
                    reasoning_result = await self._reasoning.execute_reasoning_workflow(
                        current_step.reasoning_type,
                        context.session_id,
                        reasoning_context
                    )
                except Exception as e:
                    self._logger.warning(f"Reasoning execution failed: {e}")
            
            # Retrieve relevant knowledge for step
            knowledge_results = []
            if self._knowledge:
                try:
                    knowledge_query = self._build_knowledge_query(current_step, context, workflow_state)
                    
                    knowledge_search = await self._knowledge.search_with_reasoning_context(
                        query=knowledge_query,
                        session_id=context.session_id,
                        reasoning_type=current_step.reasoning_type,
                        context={
                            "phase": current_step.phase.value,
                            "urgency_level": context.priority_level,
                            "technical_constraints": context.initial_context.get("technical_constraints", []),
                            "domain_expertise": context.domain_expertise
                        },
                        limit=8
                    )
                    
                    knowledge_results = knowledge_search.get("results", [])
                    self._metrics["knowledge_integrations"] += 1
                    
                except Exception as e:
                    self._logger.warning(f"Knowledge retrieval failed: {e}")
            
            # Synthesize step results
            step_result = await self._synthesize_step_results(
                current_step, reasoning_result, knowledge_results, workflow_state, step_inputs
            )
            
            # Update workflow state
            workflow_state["findings"].extend(step_result.findings)
            workflow_state["knowledge_base"].extend(step_result.knowledge_gathered)
            workflow_state["execution_log"].append({
                "step_index": current_step_index,
                "step_id": current_step.step_id,
                "result": step_result,
                "timestamp": datetime.now(timezone.utc).isoformat()
            })
            
            # Update performance metrics
            execution_time = (time.time() - step_start) * 1000
            workflow_state["performance_metrics"]["steps_completed"] += 1
            workflow_state["performance_metrics"]["total_knowledge_retrieved"] += len(knowledge_results)
            
            # Determine next step
            next_step_info = await self._determine_next_step(workflow_state, step_result)
            
            if next_step_info.get("adaptive_change"):
                workflow_state["performance_metrics"]["adaptive_changes"] += 1
                self._metrics["adaptive_adjustments"] += 1
            
            # Advance to next step or complete workflow
            if next_step_info.get("skip_to_step") is not None:
                workflow_state["current_step_index"] = next_step_info["skip_to_step"]
            elif next_step_info.get("complete_workflow"):
                workflow_state["status"] = WorkflowStatus.COMPLETED
                workflow_state["current_step_index"] = len(workflow_steps)
            else:
                workflow_state["current_step_index"] += 1
            
            # Update metrics
            self._update_step_metrics(execution_time)
            
            self._logger.info(
                f"Step {current_step_index + 1} completed in {execution_time:.2f}ms "
                f"with confidence {step_result.confidence_score:.3f}"
            )
            
            return {
                "step_result": {
                    "step_id": step_result.step_id,
                    "status": step_result.status,
                    "findings": step_result.findings,
                    "insights": step_result.insights,
                    "recommendations": step_result.recommendations,
                    "confidence_score": step_result.confidence_score,
                    "knowledge_gaps": step_result.knowledge_gaps
                },
                "workflow_status": {
                    "current_step": workflow_state["current_step_index"] + 1,
                    "total_steps": len(workflow_steps),
                    "status": workflow_state["status"].value,
                    "progress_percentage": (workflow_state["current_step_index"] / len(workflow_steps)) * 100
                },
                "next_step": workflow_steps[workflow_state["current_step_index"]] if workflow_state["current_step_index"] < len(workflow_steps) else None,
                "adaptive_changes": next_step_info.get("adaptive_changes", []),
                "execution_time": execution_time
            }
            
        except Exception as e:
            # Mark workflow as failed
            if workflow_id in self._active_workflows:
                self._active_workflows[workflow_id]["status"] = WorkflowStatus.FAILED
            
            self._logger.error(f"Step execution failed: {e}")
            raise ServiceException(f"Failed to execute workflow step: {str(e)}")
    
    async def get_workflow_status(self, workflow_id: str) -> Dict[str, Any]:
        """Get current status and progress of a troubleshooting workflow
        
        Args:
            workflow_id: Unique workflow identifier
            
        Returns:
            Dictionary with workflow status and progress information
        """
        if workflow_id not in self._active_workflows:
            return {"error": "Workflow not found"}
        
        workflow_state = self._active_workflows[workflow_id]
        
        return {
            "workflow_id": workflow_id,
            "status": workflow_state["status"].value,
            "progress": {
                "current_step": workflow_state["current_step_index"] + 1,
                "total_steps": len(workflow_state["steps"]),
                "progress_percentage": (workflow_state["current_step_index"] / len(workflow_state["steps"])) * 100,
                "steps_completed": workflow_state["performance_metrics"]["steps_completed"]
            },
            "findings_summary": {
                "total_findings": len(workflow_state["findings"]),
                "knowledge_items": len(workflow_state["knowledge_base"]),
                "execution_log_entries": len(workflow_state["execution_log"])
            },
            "performance": workflow_state["performance_metrics"],
            "estimated_completion": workflow_state.get("estimated_completion"),
            "created_at": workflow_state["created_at"].isoformat()
        }
    
    async def pause_workflow(self, workflow_id: str) -> Dict[str, Any]:
        """Pause a troubleshooting workflow for later resumption
        
        Args:
            workflow_id: Unique workflow identifier
            
        Returns:
            Dictionary with pause confirmation and resumption information
        """
        if workflow_id not in self._active_workflows:
            return {"error": "Workflow not found"}
        
        workflow_state = self._active_workflows[workflow_id]
        
        if workflow_state["status"] in [WorkflowStatus.COMPLETED, WorkflowStatus.FAILED]:
            return {"error": "Cannot pause completed or failed workflow"}
        
        workflow_state["status"] = WorkflowStatus.SUSPENDED
        workflow_state["suspended_at"] = datetime.now(timezone.utc)
        
        self._logger.info(f"Workflow {workflow_id} paused")
        
        return {
            "workflow_id": workflow_id,
            "status": "suspended",
            "suspended_at": workflow_state["suspended_at"].isoformat(),
            "resume_instructions": "Call resume_workflow() to continue execution"
        }
    
    async def resume_workflow(self, workflow_id: str) -> Dict[str, Any]:
        """Resume a paused troubleshooting workflow
        
        Args:
            workflow_id: Unique workflow identifier
            
        Returns:
            Dictionary with resumption confirmation and next step information
        """
        if workflow_id not in self._active_workflows:
            return {"error": "Workflow not found"}
        
        workflow_state = self._active_workflows[workflow_id]
        
        if workflow_state["status"] != WorkflowStatus.SUSPENDED:
            return {"error": "Workflow is not suspended"}
        
        workflow_state["status"] = WorkflowStatus.IN_PROGRESS
        workflow_state["resumed_at"] = datetime.now(timezone.utc)
        
        self._logger.info(f"Workflow {workflow_id} resumed")
        
        current_step_index = workflow_state["current_step_index"]
        next_step = workflow_state["steps"][current_step_index] if current_step_index < len(workflow_state["steps"]) else None
        
        return {
            "workflow_id": workflow_id,
            "status": "resumed",
            "resumed_at": workflow_state["resumed_at"].isoformat(),
            "next_step": next_step,
            "progress": {
                "current_step": current_step_index + 1,
                "total_steps": len(workflow_state["steps"])
            }
        }
    
    def _determine_workflow_pattern(
        self,
        context: WorkflowContext,
        memory_context: Dict[str, Any],
        strategic_plan: Optional[Any]
    ) -> str:
        """Determine optimal workflow pattern based on context"""
        
        # Analyze problem characteristics
        problem_lower = context.problem_description.lower()
        
        # Check for specific problem types
        if any(keyword in problem_lower for keyword in ["performance", "slow", "latency", "timeout"]):
            return "performance_investigation"
        elif any(keyword in problem_lower for keyword in ["error", "exception", "crash", "failure"]):
            return "error_diagnosis"
        elif any(keyword in problem_lower for keyword in ["security", "unauthorized", "breach", "attack"]):
            return "security_incident"
        elif any(keyword in problem_lower for keyword in ["deploy", "rollout", "release", "update"]):
            return "deployment_issue"
        elif any(keyword in problem_lower for keyword in ["scale", "capacity", "load", "resource"]):
            return "capacity_planning"
        
        # Consider memory patterns
        if memory_context.get("patterns"):
            for pattern in memory_context["patterns"]:
                if pattern.get("type") == "recurring_issue":
                    return "recurring_problem"
        
        # Consider strategic plan recommendations
        if strategic_plan and hasattr(strategic_plan, 'recommended_approach'):
            if strategic_plan.recommended_approach == "systematic_analysis":
                return "comprehensive_analysis"
            elif strategic_plan.recommended_approach == "rapid_resolution":
                return "quick_diagnosis"
        
        # Consider priority and constraints
        if context.priority_level == "critical":
            return "rapid_response"
        elif context.time_constraints and context.time_constraints < 1800:  # 30 minutes
            return "time_constrained"
        
        # Default comprehensive pattern
        return "standard_troubleshooting"
    
    async def _generate_workflow_steps(
        self,
        pattern: str,
        context: WorkflowContext,
        memory_context: Dict[str, Any],
        strategic_plan: Optional[Any]
    ) -> List[TroubleshootingStep]:
        """Generate customized workflow steps based on pattern and context"""
        
        # Get base steps for pattern
        phase_sequence = self._workflow_patterns.get(pattern, [
            TroubleshootingPhase.DEFINE_BLAST_RADIUS,
            TroubleshootingPhase.ESTABLISH_TIMELINE,
            TroubleshootingPhase.FORMULATE_HYPOTHESIS,
            TroubleshootingPhase.VALIDATE_HYPOTHESIS,
            TroubleshootingPhase.PROPOSE_SOLUTION
        ])
        
        steps = []
        step_counter = 1
        
        for phase in phase_sequence:
            phase_steps = self._step_templates.get(phase, [])
            
            for template_step in phase_steps:
                # Customize step based on context
                customized_step = TroubleshootingStep(
                    step_id=f"step_{step_counter:02d}_{template_step.step_id}",
                    phase=template_step.phase,
                    title=self._customize_step_title(template_step.title, context),
                    description=self._customize_step_description(template_step.description, context),
                    reasoning_type=template_step.reasoning_type,
                    knowledge_focus=self._determine_knowledge_focus(template_step, context),
                    required_inputs=template_step.required_inputs.copy(),
                    dependencies=template_step.dependencies.copy(),
                    estimated_duration=self._estimate_step_duration(template_step, context),
                    priority=self._calculate_step_priority(template_step, context, memory_context)
                )
                
                steps.append(customized_step)
                step_counter += 1
        
        # Sort by priority and dependencies
        steps = self._optimize_step_sequence(steps)
        
        return steps
    
    def _customize_step_title(self, template_title: str, context: WorkflowContext) -> str:
        """Customize step title based on context"""
        
        replacements = {
            "{service}": context.initial_context.get("service_name", "system"),
            "{environment}": context.initial_context.get("environment", "environment"),
            "{component}": context.initial_context.get("component", "component"),
            "{priority}": context.priority_level
        }
        
        customized_title = template_title
        for placeholder, value in replacements.items():
            customized_title = customized_title.replace(placeholder, value)
        
        return customized_title
    
    def _customize_step_description(self, template_description: str, context: WorkflowContext) -> str:
        """Customize step description based on context"""
        
        replacements = {
            "{problem}": context.problem_description,
            "{domain}": context.domain_expertise,
            "{constraints}": str(context.time_constraints) if context.time_constraints else "standard timeframe"
        }
        
        customized_description = template_description
        for placeholder, value in replacements.items():
            customized_description = customized_description.replace(placeholder, value)
        
        return customized_description
    
    def _determine_knowledge_focus(self, step: TroubleshootingStep, context: WorkflowContext) -> str:
        """Determine knowledge focus for step based on context"""
        
        if step.phase == TroubleshootingPhase.DEFINE_BLAST_RADIUS:
            return f"impact assessment {context.initial_context.get('service_name', '')}"
        elif step.phase == TroubleshootingPhase.ESTABLISH_TIMELINE:
            return f"timeline analysis {context.initial_context.get('environment', '')}"
        elif step.phase == TroubleshootingPhase.FORMULATE_HYPOTHESIS:
            return f"root cause {context.problem_description[:50]}"
        elif step.phase == TroubleshootingPhase.VALIDATE_HYPOTHESIS:
            return f"validation testing {context.domain_expertise}"
        elif step.phase == TroubleshootingPhase.PROPOSE_SOLUTION:
            return f"solution implementation {context.initial_context.get('technology', '')}"
        
        return "general troubleshooting"
    
    def _estimate_step_duration(self, step: TroubleshootingStep, context: WorkflowContext) -> int:
        """Estimate step duration based on context and complexity"""
        
        base_duration = step.estimated_duration
        
        # Adjust based on priority
        if context.priority_level == "critical":
            base_duration = int(base_duration * 0.7)  # Faster execution for critical issues
        elif context.priority_level == "low":
            base_duration = int(base_duration * 1.3)  # More thorough for low priority
        
        # Adjust based on domain expertise
        if context.domain_expertise == "expert":
            base_duration = int(base_duration * 0.8)  # Experts work faster
        elif context.domain_expertise == "novice":
            base_duration = int(base_duration * 1.5)  # Novices need more time
        
        # Adjust based on time constraints
        if context.time_constraints:
            max_allowed = context.time_constraints / 5  # Assume 5 steps average
            base_duration = min(base_duration, int(max_allowed))
        
        return max(base_duration, 30)  # Minimum 30 seconds per step
    
    def _calculate_step_priority(
        self,
        step: TroubleshootingStep,
        context: WorkflowContext,
        memory_context: Dict[str, Any]
    ) -> int:
        """Calculate step priority based on context and memory insights"""
        
        base_priority = step.priority
        
        # Adjust based on urgency
        if context.priority_level == "critical":
            base_priority = max(1, base_priority - 1)
        elif context.priority_level == "low":
            base_priority = min(5, base_priority + 1)
        
        # Adjust based on memory insights
        if memory_context.get("insights"):
            for insight in memory_context["insights"]:
                if insight.get("type") == "recurring_pattern" and step.phase.value in insight.get("relevant_phases", []):
                    base_priority = max(1, base_priority - 1)
        
        return base_priority
    
    def _optimize_step_sequence(self, steps: List[TroubleshootingStep]) -> List[TroubleshootingStep]:
        """Optimize step sequence based on dependencies and priorities"""
        
        # Simple priority-based sorting for now
        # In future, could implement more sophisticated dependency resolution
        return sorted(steps, key=lambda x: (x.priority, x.phase.value))
    
    def _build_knowledge_query(
        self,
        step: TroubleshootingStep,
        context: WorkflowContext,
        workflow_state: Dict[str, Any]
    ) -> str:
        """Build knowledge search query for current step"""
        
        query_parts = []
        
        # Add step-specific focus
        if step.knowledge_focus:
            query_parts.append(step.knowledge_focus)
        
        # Add problem description keywords
        query_parts.append(context.problem_description)
        
        # Add phase-specific keywords
        phase_keywords = {
            TroubleshootingPhase.DEFINE_BLAST_RADIUS: "impact scope assessment",
            TroubleshootingPhase.ESTABLISH_TIMELINE: "timeline events changes",
            TroubleshootingPhase.FORMULATE_HYPOTHESIS: "root cause analysis",
            TroubleshootingPhase.VALIDATE_HYPOTHESIS: "testing validation",
            TroubleshootingPhase.PROPOSE_SOLUTION: "solution fix remediation"
        }
        
        if step.phase in phase_keywords:
            query_parts.append(phase_keywords[step.phase])
        
        # Add context from previous findings
        recent_findings = workflow_state["findings"][-3:]  # Last 3 findings
        for finding in recent_findings:
            if finding.get("keywords"):
                query_parts.extend(finding["keywords"][:2])  # Top 2 keywords
        
        return " ".join(query_parts)
    
    async def _synthesize_step_results(
        self,
        step: TroubleshootingStep,
        reasoning_result: Optional[Dict[str, Any]],
        knowledge_results: List[Dict[str, Any]],
        workflow_state: Dict[str, Any],
        step_inputs: Optional[Dict[str, Any]]
    ) -> StepResult:
        """Synthesize results from reasoning and knowledge retrieval"""
        
        findings = []
        knowledge_gathered = []
        insights = []
        recommendations = []
        knowledge_gaps = []
        
        # Process reasoning results
        if reasoning_result:
            findings.extend(reasoning_result.get("findings", []))
            insights.extend(reasoning_result.get("insights", []))
            recommendations.extend(reasoning_result.get("recommendations", []))
            knowledge_gaps.extend(reasoning_result.get("knowledge_gaps", []))
        
        # Process knowledge results
        for knowledge_item in knowledge_results:
            knowledge_gathered.append({
                "content": knowledge_item.get("content", ""),
                "source": knowledge_item.get("metadata", {}).get("source", "unknown"),
                "relevance": knowledge_item.get("relevance_score", 0.0),
                "step_id": step.step_id
            })
        
        # Generate step-specific insights
        step_insights = await self._generate_step_insights(step, workflow_state, knowledge_results)
        insights.extend(step_insights)
        
        # Calculate confidence score
        confidence_score = self._calculate_step_confidence(reasoning_result, knowledge_results, step)
        
        # Determine next steps
        next_steps = await self._suggest_next_steps(step, findings, workflow_state)
        
        return StepResult(
            step_id=step.step_id,
            status="completed",
            findings=findings,
            knowledge_gathered=knowledge_gathered,
            insights=insights,
            next_steps=next_steps,
            confidence_score=confidence_score,
            execution_time=0.0,  # Will be set by caller
            knowledge_gaps=knowledge_gaps,
            recommendations=recommendations
        )
    
    async def _generate_step_insights(
        self,
        step: TroubleshootingStep,
        workflow_state: Dict[str, Any],
        knowledge_results: List[Dict[str, Any]]
    ) -> List[str]:
        """Generate insights specific to the current step"""
        
        insights = []
        
        # Analyze knowledge relevance
        if knowledge_results:
            avg_relevance = sum(item.get("relevance_score", 0.0) for item in knowledge_results) / len(knowledge_results)
            if avg_relevance > 0.8:
                insights.append(f"High-quality knowledge retrieved for {step.phase.value} (relevance: {avg_relevance:.2f})")
            elif avg_relevance < 0.5:
                insights.append(f"Limited relevant knowledge available for {step.phase.value}")
        
        # Analyze step progress
        total_steps = len(workflow_state["steps"])
        current_index = workflow_state["current_step_index"]
        progress = (current_index + 1) / total_steps
        
        if progress > 0.5:
            insights.append(f"Workflow is {progress*100:.0f}% complete, focusing on resolution")
        
        # Analyze findings accumulation
        total_findings = len(workflow_state["findings"])
        if total_findings > 10:
            insights.append("Substantial evidence gathered, consider consolidating findings")
        
        return insights
    
    def _calculate_step_confidence(
        self,
        reasoning_result: Optional[Dict[str, Any]],
        knowledge_results: List[Dict[str, Any]],
        step: TroubleshootingStep
    ) -> float:
        """Calculate confidence score for step execution"""
        
        confidence_factors = []
        
        # Reasoning confidence
        if reasoning_result and reasoning_result.get("confidence_score"):
            confidence_factors.append(reasoning_result["confidence_score"])
        
        # Knowledge relevance confidence
        if knowledge_results:
            avg_relevance = sum(item.get("relevance_score", 0.0) for item in knowledge_results) / len(knowledge_results)
            confidence_factors.append(avg_relevance)
        
        # Step completeness confidence
        if reasoning_result and knowledge_results:
            confidence_factors.append(0.8)  # High confidence when both systems provide input
        elif reasoning_result or knowledge_results:
            confidence_factors.append(0.6)  # Medium confidence with partial input
        else:
            confidence_factors.append(0.3)  # Low confidence with no intelligent input
        
        return sum(confidence_factors) / len(confidence_factors) if confidence_factors else 0.5
    
    async def _suggest_next_steps(
        self,
        current_step: TroubleshootingStep,
        findings: List[Dict[str, Any]],
        workflow_state: Dict[str, Any]
    ) -> List[str]:
        """Suggest next steps based on current step results"""
        
        next_steps = []
        
        # Analyze findings for next step suggestions
        if findings:
            for finding in findings:
                if finding.get("type") == "critical_issue":
                    next_steps.append("Immediate escalation to critical issue resolution")
                elif finding.get("type") == "root_cause_identified":
                    next_steps.append("Proceed to solution validation and implementation")
                elif finding.get("type") == "insufficient_data":
                    next_steps.append("Gather additional diagnostic information")
        
        # Phase-specific next step suggestions
        if current_step.phase == TroubleshootingPhase.DEFINE_BLAST_RADIUS:
            next_steps.append("Establish timeline of issue occurrence")
        elif current_step.phase == TroubleshootingPhase.ESTABLISH_TIMELINE:
            next_steps.append("Formulate hypotheses based on timeline analysis")
        elif current_step.phase == TroubleshootingPhase.FORMULATE_HYPOTHESIS:
            next_steps.append("Design validation tests for hypotheses")
        elif current_step.phase == TroubleshootingPhase.VALIDATE_HYPOTHESIS:
            next_steps.append("Develop solution based on validated hypothesis")
        
        return next_steps
    
    async def _determine_next_step(
        self,
        workflow_state: Dict[str, Any],
        step_result: StepResult
    ) -> Dict[str, Any]:
        """Determine next step or workflow adjustments based on results"""
        
        next_step_info = {
            "adaptive_change": False,
            "adaptive_changes": [],
            "skip_to_step": None,
            "complete_workflow": False
        }
        
        # Check for critical findings that require workflow adaptation
        for finding in step_result.findings:
            if finding.get("type") == "critical_issue" and finding.get("confidence", 0) > 0.8:
                # Skip to solution phase for critical issues
                steps = workflow_state["steps"]
                solution_step_index = None
                
                for i, step in enumerate(steps):
                    if step.phase == TroubleshootingPhase.PROPOSE_SOLUTION:
                        solution_step_index = i
                        break
                
                if solution_step_index and solution_step_index > workflow_state["current_step_index"]:
                    next_step_info["skip_to_step"] = solution_step_index
                    next_step_info["adaptive_change"] = True
                    next_step_info["adaptive_changes"].append("Skipping to solution phase due to critical issue identification")
            
            elif finding.get("type") == "root_cause_confirmed" and finding.get("confidence", 0) > 0.9:
                # Can complete workflow early if root cause is confirmed with high confidence
                next_step_info["complete_workflow"] = True
                next_step_info["adaptive_change"] = True
                next_step_info["adaptive_changes"].append("Completing workflow early due to confirmed root cause")
        
        # Check for insufficient confidence requiring additional steps
        if step_result.confidence_score < 0.4:
            next_step_info["adaptive_changes"].append("Low confidence detected, may require additional investigation")
        
        return next_step_info
    
    def _update_step_metrics(self, execution_time: float) -> None:
        """Update step execution metrics"""
        
        self._metrics["steps_executed"] += 1
        
        # Update average step execution time
        current_avg = self._metrics["avg_step_execution_time"]
        total_steps = self._metrics["steps_executed"]
        
        if total_steps == 1:
            self._metrics["avg_step_execution_time"] = execution_time
        else:
            self._metrics["avg_step_execution_time"] = (
                (current_avg * (total_steps - 1) + execution_time) / total_steps
            )
    
    def _initialize_step_templates(self) -> None:
        """Initialize troubleshooting step templates"""
        
        self._step_templates = {
            TroubleshootingPhase.DEFINE_BLAST_RADIUS: [
                TroubleshootingStep(
                    step_id="blast_radius_assessment",
                    phase=TroubleshootingPhase.DEFINE_BLAST_RADIUS,
                    title="Assess Impact Scope and Affected Systems",
                    description="Determine which systems, users, and services are affected by {problem}",
                    reasoning_type="analytical",
                    estimated_duration=300,
                    priority=1
                ),
                TroubleshootingStep(
                    step_id="severity_classification",
                    phase=TroubleshootingPhase.DEFINE_BLAST_RADIUS,
                    title="Classify Issue Severity and Priority",
                    description="Evaluate the severity level and business impact of the current issue",
                    reasoning_type="strategic",
                    estimated_duration=180,
                    priority=2
                )
            ],
            TroubleshootingPhase.ESTABLISH_TIMELINE: [
                TroubleshootingStep(
                    step_id="timeline_analysis",
                    phase=TroubleshootingPhase.ESTABLISH_TIMELINE,
                    title="Construct Event Timeline",
                    description="Establish when the issue started and identify relevant recent changes",
                    reasoning_type="analytical",
                    estimated_duration=400,
                    priority=1
                ),
                TroubleshootingStep(
                    step_id="change_correlation",
                    phase=TroubleshootingPhase.ESTABLISH_TIMELINE,
                    title="Correlate with Recent Changes",
                    description="Identify deployments, configurations, or other changes that correlate with issue onset",
                    reasoning_type="diagnostic",
                    estimated_duration=300,
                    priority=2
                )
            ],
            TroubleshootingPhase.FORMULATE_HYPOTHESIS: [
                TroubleshootingStep(
                    step_id="hypothesis_generation",
                    phase=TroubleshootingPhase.FORMULATE_HYPOTHESIS,
                    title="Generate Root Cause Hypotheses",
                    description="Develop potential root cause theories based on gathered evidence",
                    reasoning_type="creative",
                    estimated_duration=450,
                    priority=1
                ),
                TroubleshootingStep(
                    step_id="hypothesis_prioritization",
                    phase=TroubleshootingPhase.FORMULATE_HYPOTHESIS,
                    title="Prioritize Hypotheses by Likelihood",
                    description="Rank hypotheses based on evidence strength and probability",
                    reasoning_type="analytical",
                    estimated_duration=200,
                    priority=2
                )
            ],
            TroubleshootingPhase.VALIDATE_HYPOTHESIS: [
                TroubleshootingStep(
                    step_id="hypothesis_testing",
                    phase=TroubleshootingPhase.VALIDATE_HYPOTHESIS,
                    title="Design and Execute Validation Tests",
                    description="Create tests to validate or refute the most likely hypotheses",
                    reasoning_type="diagnostic",
                    estimated_duration=600,
                    priority=1
                ),
                TroubleshootingStep(
                    step_id="evidence_evaluation",
                    phase=TroubleshootingPhase.VALIDATE_HYPOTHESIS,
                    title="Evaluate Test Results and Evidence",
                    description="Analyze test results to confirm or refute hypotheses",
                    reasoning_type="analytical",
                    estimated_duration=300,
                    priority=2
                )
            ],
            TroubleshootingPhase.PROPOSE_SOLUTION: [
                TroubleshootingStep(
                    step_id="solution_development",
                    phase=TroubleshootingPhase.PROPOSE_SOLUTION,
                    title="Develop Resolution Strategy",
                    description="Create comprehensive solution based on validated root cause",
                    reasoning_type="strategic",
                    estimated_duration=500,
                    priority=1
                ),
                TroubleshootingStep(
                    step_id="implementation_planning",
                    phase=TroubleshootingPhase.PROPOSE_SOLUTION,
                    title="Plan Implementation and Rollback",
                    description="Design implementation steps with rollback procedures",
                    reasoning_type="strategic",
                    estimated_duration=400,
                    priority=2
                )
            ]
        }
    
    def _initialize_workflow_patterns(self) -> None:
        """Initialize workflow patterns for different problem types"""
        
        self._workflow_patterns = {
            "standard_troubleshooting": [
                TroubleshootingPhase.DEFINE_BLAST_RADIUS,
                TroubleshootingPhase.ESTABLISH_TIMELINE,
                TroubleshootingPhase.FORMULATE_HYPOTHESIS,
                TroubleshootingPhase.VALIDATE_HYPOTHESIS,
                TroubleshootingPhase.PROPOSE_SOLUTION
            ],
            "rapid_response": [
                TroubleshootingPhase.DEFINE_BLAST_RADIUS,
                TroubleshootingPhase.FORMULATE_HYPOTHESIS,
                TroubleshootingPhase.PROPOSE_SOLUTION
            ],
            "performance_investigation": [
                TroubleshootingPhase.DEFINE_BLAST_RADIUS,
                TroubleshootingPhase.ESTABLISH_TIMELINE,
                TroubleshootingPhase.FORMULATE_HYPOTHESIS,
                TroubleshootingPhase.VALIDATE_HYPOTHESIS,
                TroubleshootingPhase.PROPOSE_SOLUTION,
                TroubleshootingPhase.VERIFICATION
            ],
            "error_diagnosis": [
                TroubleshootingPhase.DEFINE_BLAST_RADIUS,
                TroubleshootingPhase.ESTABLISH_TIMELINE,
                TroubleshootingPhase.FORMULATE_HYPOTHESIS,
                TroubleshootingPhase.VALIDATE_HYPOTHESIS,
                TroubleshootingPhase.PROPOSE_SOLUTION
            ],
            "security_incident": [
                TroubleshootingPhase.DEFINE_BLAST_RADIUS,
                TroubleshootingPhase.ESTABLISH_TIMELINE,
                TroubleshootingPhase.FORMULATE_HYPOTHESIS,
                TroubleshootingPhase.VALIDATE_HYPOTHESIS,
                TroubleshootingPhase.PROPOSE_SOLUTION,
                TroubleshootingPhase.VERIFICATION
            ],
            "deployment_issue": [
                TroubleshootingPhase.ESTABLISH_TIMELINE,
                TroubleshootingPhase.FORMULATE_HYPOTHESIS,
                TroubleshootingPhase.VALIDATE_HYPOTHESIS,
                TroubleshootingPhase.PROPOSE_SOLUTION
            ],
            "capacity_planning": [
                TroubleshootingPhase.DEFINE_BLAST_RADIUS,
                TroubleshootingPhase.FORMULATE_HYPOTHESIS,
                TroubleshootingPhase.PROPOSE_SOLUTION,
                TroubleshootingPhase.IMPLEMENTATION_PLANNING
            ],
            "recurring_problem": [
                TroubleshootingPhase.ESTABLISH_TIMELINE,
                TroubleshootingPhase.FORMULATE_HYPOTHESIS,
                TroubleshootingPhase.VALIDATE_HYPOTHESIS,
                TroubleshootingPhase.PROPOSE_SOLUTION,
                TroubleshootingPhase.VERIFICATION
            ],
            "time_constrained": [
                TroubleshootingPhase.FORMULATE_HYPOTHESIS,
                TroubleshootingPhase.PROPOSE_SOLUTION
            ],
            "comprehensive_analysis": [
                TroubleshootingPhase.DEFINE_BLAST_RADIUS,
                TroubleshootingPhase.ESTABLISH_TIMELINE,
                TroubleshootingPhase.FORMULATE_HYPOTHESIS,
                TroubleshootingPhase.VALIDATE_HYPOTHESIS,
                TroubleshootingPhase.PROPOSE_SOLUTION,
                TroubleshootingPhase.IMPLEMENTATION_PLANNING,
                TroubleshootingPhase.VERIFICATION
            ],
            "quick_diagnosis": [
                TroubleshootingPhase.FORMULATE_HYPOTHESIS,
                TroubleshootingPhase.VALIDATE_HYPOTHESIS,
                TroubleshootingPhase.PROPOSE_SOLUTION
            ]
        }
    
    async def health_check(self) -> Dict[str, Any]:
        """Check health of troubleshooting orchestrator"""
        
        health_info = {
            "service": "troubleshooting_orchestrator",
            "status": "healthy",
            "dependencies": {
                "memory_service": "unknown",
                "planning_service": "unknown", 
                "reasoning_service": "unknown",
                "enhanced_knowledge_service": "unknown",
                "llm_provider": "unknown",
                "tracer": "unknown"
            },
            "active_workflows": len(self._active_workflows),
            "performance_metrics": self._metrics.copy(),
            "capabilities": {
                "workflow_orchestration": True,
                "adaptive_step_sequencing": True,
                "multi_system_integration": True,
                "context_propagation": True,
                "performance_tracking": True,
                "memory_integration": self._memory is not None,
                "planning_integration": self._planning is not None,
                "reasoning_integration": self._reasoning is not None,
                "knowledge_integration": self._knowledge is not None
            },
            "workflow_patterns": list(self._workflow_patterns.keys()),
            "supported_phases": [phase.value for phase in TroubleshootingPhase]
        }
        
        # Check each dependency
        dependencies = [
            ("memory_service", self._memory),
            ("planning_service", self._planning),
            ("reasoning_service", self._reasoning), 
            ("enhanced_knowledge_service", self._knowledge),
            ("llm_provider", self._llm),
            ("tracer", self._tracer)
        ]
        
        for dep_name, dep_service in dependencies:
            if dep_service:
                try:
                    if hasattr(dep_service, 'health_check'):
                        await dep_service.health_check()
                    health_info["dependencies"][dep_name] = "healthy"
                except Exception:
                    health_info["dependencies"][dep_name] = "unhealthy"
                    health_info["status"] = "degraded"
            else:
                health_info["dependencies"][dep_name] = "unavailable"
        
        return health_info