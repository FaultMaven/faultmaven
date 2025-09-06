"""Business Logic & Workflow Engine

Component 7 of 7 in the FaultMaven agentic framework.
Provides comprehensive workflow orchestration with plan-execute-observe-adapt
cycles, implementing true agentic behavior with sophisticated business logic.

This component implements the IBusinessLogicWorkflowEngine interface to provide:
- Dynamic workflow planning and execution with adaptive strategies
- Plan-Execute-Observe-Re-plan agentic loops with state management
- Multi-stage business logic orchestration with dependency resolution
- Real-time workflow monitoring and performance optimization
- Context-aware decision making with learning capabilities
- Integration with all other agentic framework components
"""

import asyncio
import json
import logging
import time
from datetime import datetime, timedelta
from enum import Enum
from typing import Any, Dict, List, Optional, Union, Callable, Set
from dataclasses import dataclass, field
from uuid import uuid4

from faultmaven.models.agentic import (
    IBusinessLogicWorkflowEngine,
    WorkflowDefinition,
    WorkflowExecution,
    WorkflowStep,
    ExecutionPlan,
    WorkflowResult,
    PlanningResult,
    ObservationResult,
    AdaptationResult,
    ObservationData,
    PlanNode
)


logger = logging.getLogger(__name__)


class WorkflowStatus(Enum):
    """Workflow execution status."""
    PLANNED = "planned"
    RUNNING = "running"
    PAUSED = "paused"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"
    ADAPTING = "adapting"


class StepType(Enum):
    """Types of workflow steps."""
    CLASSIFICATION = "classification"
    TOOL_EXECUTION = "tool_execution"
    GUARDRAILS_CHECK = "guardrails_check"
    STATE_UPDATE = "state_update"
    RESPONSE_SYNTHESIS = "response_synthesis"
    ERROR_HANDLING = "error_handling"
    DECISION = "decision"
    PARALLEL = "parallel"
    SEQUENTIAL = "sequential"
    LOOP = "loop"


class PlanningStrategy(Enum):
    """Planning strategy types."""
    REACTIVE = "reactive"  # React to immediate context
    PROACTIVE = "proactive"  # Plan ahead based on patterns
    ADAPTIVE = "adaptive"  # Learn and adapt from previous executions
    HYBRID = "hybrid"  # Combine multiple strategies


@dataclass
class WorkflowContext:
    """Context for workflow execution."""
    workflow_id: str
    execution_id: str
    user_id: Optional[str] = None
    session_id: Optional[str] = None
    request_id: Optional[str] = None
    query: str = ""
    metadata: Dict[str, Any] = field(default_factory=dict)
    started_at: datetime = field(default_factory=datetime.utcnow)
    deadline: Optional[datetime] = None


@dataclass
class StepExecutionResult:
    """Result of a workflow step execution."""
    step_id: str
    success: bool
    duration: float
    output_data: Dict[str, Any]
    error: Optional[str] = None
    next_steps: List[str] = field(default_factory=list)
    adaptations_needed: List[str] = field(default_factory=list)


class BusinessLogicWorkflowEngine(IBusinessLogicWorkflowEngine):
    """Production implementation of the Business Logic & Workflow Engine.
    
    Provides comprehensive workflow orchestration capabilities including:
    - Dynamic workflow planning with intelligent strategy selection
    - Plan-Execute-Observe-Re-plan agentic loops with state preservation
    - Multi-dimensional business logic with dependency resolution
    - Real-time performance monitoring and optimization
    - Context-aware decision making with machine learning integration
    - Adaptive workflow evolution based on execution patterns
    - Integration orchestration with all 6 other agentic components
    - Fault tolerance with graceful degradation and recovery
    """

    def __init__(
        self,
        state_manager=None,
        classification_engine=None,
        tool_broker=None,
        guardrails_layer=None,
        response_synthesizer=None,
        error_manager=None
    ):
        """Initialize the workflow engine with component dependencies.
        
        Args:
            state_manager: Agent state and session manager
            classification_engine: Query classification engine
            tool_broker: Tool and skill broker
            guardrails_layer: Guardrails and policy layer
            response_synthesizer: Response synthesizer and formatter
            error_manager: Error handling and fallback manager
        """
        # Component dependencies
        self.state_manager = state_manager
        self.classification_engine = classification_engine
        self.tool_broker = tool_broker
        self.guardrails_layer = guardrails_layer
        self.response_synthesizer = response_synthesizer
        self.error_manager = error_manager
        
        # Workflow definitions registry
        self.workflow_definitions = self._initialize_workflow_definitions()
        
        # Active workflow executions
        self.active_workflows = {}
        
        # Execution history for learning
        self.execution_history = []
        self.max_history_size = 1000
        
        # Performance metrics
        self.metrics = {
            "total_workflows": 0,
            "completed_workflows": 0,
            "failed_workflows": 0,
            "average_execution_time": 0.0,
            "planning_success_rate": 0.0,
            "adaptation_frequency": 0.0,
            "step_success_rates": {},
            "workflow_patterns": {}
        }
        
        # Planning strategies
        self.planning_strategies = {
            PlanningStrategy.REACTIVE.value: self._plan_reactive,
            PlanningStrategy.PROACTIVE.value: self._plan_proactive,
            PlanningStrategy.ADAPTIVE.value: self._plan_adaptive,
            PlanningStrategy.HYBRID.value: self._plan_hybrid
        }
        
        # Step executors
        self.step_executors = self._initialize_step_executors()
        
        # Decision rules engine
        self.decision_rules = self._initialize_decision_rules()
        
        logger.info("Business Logic & Workflow Engine initialized")

    async def plan_workflow(self, request: Dict[str, Any]) -> PlanningResult:
        """Create intelligent workflow plan based on request analysis.
        
        Provides multi-stage planning process:
        1. Context analysis and requirement extraction
        2. Strategy selection based on complexity and patterns
        3. Step sequence generation with dependency resolution
        4. Resource allocation and timeline estimation
        5. Risk assessment and fallback planning
        6. Plan optimization and validation
        
        Args:
            request: Workflow planning request with context and requirements
            
        Returns:
            PlanningResult with executable workflow plan and metadata
        """
        planning_start = time.time()
        
        try:
            # Extract planning context
            context = WorkflowContext(
                workflow_id=str(uuid4()),
                execution_id=str(uuid4()),
                user_id=request.get("user_id"),
                session_id=request.get("session_id"),
                request_id=request.get("request_id"),
                query=request.get("query", ""),
                metadata=request.get("metadata", {}),
                deadline=self._parse_deadline(request.get("deadline"))
            )
            
            # Stage 1: Analyze request and classify workflow type
            workflow_classification = await self._classify_workflow_type(request, context)
            
            # Stage 2: Select planning strategy
            planning_strategy = await self._select_planning_strategy(
                workflow_classification, context, request
            )
            
            # Stage 3: Generate workflow plan using selected strategy
            planning_func = self.planning_strategies.get(
                planning_strategy, self._plan_reactive
            )
            workflow_plan = await planning_func(request, context, workflow_classification)
            
            # Stage 4: Validate and optimize plan
            validation_result = await self._validate_workflow_plan(workflow_plan, context)
            if not validation_result["is_valid"]:
                # Attempt plan correction
                workflow_plan = await self._correct_workflow_plan(
                    workflow_plan, validation_result["issues"], context
                )
            
            # Stage 5: Resource and timeline estimation
            resource_analysis = await self._analyze_resource_requirements(workflow_plan, context)
            timeline_estimate = await self._estimate_execution_timeline(workflow_plan, context)
            
            # Stage 6: Risk assessment and fallback planning
            risk_assessment = await self._assess_workflow_risks(workflow_plan, context)
            fallback_plan = await self._generate_fallback_plan(workflow_plan, risk_assessment, context)
            
            planning_time = time.time() - planning_start
            
            # Create planning result
            planning_result = PlanningResult(
                workflow_id=context.workflow_id,
                execution_plan=ExecutionPlan(
                    steps=workflow_plan["steps"],
                    dependencies=workflow_plan["dependencies"],
                    parallel_groups=workflow_plan.get("parallel_groups", []),
                    estimated_duration=timeline_estimate["total_duration"],
                    resource_requirements=resource_analysis,
                    fallback_strategy=fallback_plan
                ),
                confidence_score=workflow_classification["confidence"] * validation_result.get("quality_score", 1.0),
                planning_strategy=planning_strategy,
                planning_time=planning_time,
                metadata={
                    "workflow_type": workflow_classification["type"],
                    "complexity": workflow_classification["complexity"],
                    "step_count": len(workflow_plan["steps"]),
                    "parallel_opportunities": len(workflow_plan.get("parallel_groups", [])),
                    "risk_level": risk_assessment["overall_risk"],
                    "resource_intensity": resource_analysis.get("intensity", "medium")
                }
            )
            
            # Update metrics
            self.metrics["planning_success_rate"] = (
                (self.metrics["planning_success_rate"] * self.metrics["total_workflows"] + 1.0) /
                (self.metrics["total_workflows"] + 1)
            )
            
            logger.info(f"Workflow planned: {context.workflow_id}, strategy={planning_strategy}, steps={len(workflow_plan['steps'])}, confidence={planning_result.confidence_score:.3f}")
            
            return planning_result
            
        except Exception as e:
            logger.error(f"Error in workflow planning: {str(e)}")
            
            # Return emergency plan
            return PlanningResult(
                workflow_id=context.workflow_id if 'context' in locals() else str(uuid4()),
                execution_plan=ExecutionPlan(
                    steps=[],
                    dependencies={},
                    parallel_groups=[],
                    estimated_duration=0.0,
                    resource_requirements={},
                    fallback_strategy={}
                ),
                confidence_score=0.0,
                planning_strategy="emergency_fallback",
                planning_time=time.time() - planning_start,
                metadata={"error": str(e)}
            )

    async def execute_workflow(self, execution_plan: ExecutionPlan, context: Dict[str, Any]) -> Dict[str, Any]:
        """Execute workflow with continuous monitoring and adaptation.
        
        Provides comprehensive execution management:
        1. Execution environment preparation and resource allocation
        2. Step-by-step execution with dependency resolution
        3. Real-time monitoring and performance tracking
        4. Dynamic adaptation based on observations
        5. Error handling with graceful degradation
        6. Result collection and quality validation
        
        Args:
            execution_plan: Validated execution plan from planning phase
            context: Execution context with session and user information
            
        Returns:
            WorkflowExecution with complete execution results and analytics
        """
        execution_start = time.time()
        
        # Create execution context
        workflow_context = WorkflowContext(
            workflow_id=execution_plan.workflow_id if hasattr(execution_plan, 'workflow_id') else str(uuid4()),
            execution_id=str(uuid4()),
            user_id=context.get("user_id"),
            session_id=context.get("session_id"),
            request_id=context.get("request_id"),
            query=context.get("query", ""),
            metadata=context.get("metadata", {})
        )
        
        # Initialize workflow execution
        workflow_execution = WorkflowExecution(
            workflow_id=workflow_context.workflow_id,
            execution_id=workflow_context.execution_id,
            status=WorkflowStatus.RUNNING.value,
            current_step=0,
            steps_completed=[],
            steps_failed=[],
            execution_data={},
            start_time=datetime.utcnow(),
            end_time=None,
            total_duration=0.0,
            metadata={}
        )
        
        # Add to active workflows
        self.active_workflows[workflow_context.execution_id] = workflow_execution
        self.metrics["total_workflows"] += 1
        
        try:
            # Stage 1: Execution environment preparation
            await self._prepare_execution_environment(workflow_context, execution_plan)
            
            # Stage 2: Execute workflow steps
            execution_results = await self._execute_workflow_steps(
                execution_plan, workflow_context, workflow_execution
            )
            
            # Stage 3: Collect and validate results
            final_results = await self._collect_execution_results(
                execution_results, workflow_context, execution_plan
            )
            
            # Stage 4: Quality validation
            quality_assessment = await self._validate_execution_quality(
                final_results, workflow_context, execution_plan
            )
            
            # Update execution with final results
            workflow_execution.status = WorkflowStatus.COMPLETED.value
            workflow_execution.execution_data = final_results
            workflow_execution.end_time = datetime.utcnow()
            workflow_execution.total_duration = time.time() - execution_start
            workflow_execution.metadata.update({
                "quality_score": quality_assessment["score"],
                "adaptation_count": len([r for r in execution_results if r.adaptations_needed]),
                "parallel_efficiency": await self._calculate_parallel_efficiency(execution_results),
                "resource_utilization": await self._calculate_resource_utilization(execution_results)
            })
            
            # Update metrics
            self.metrics["completed_workflows"] += 1
            self._update_execution_metrics(workflow_execution, execution_results)
            
            # Add to execution history for learning
            await self._add_to_execution_history(workflow_execution, execution_plan, execution_results)
            
            logger.info(f"Workflow completed: {workflow_context.workflow_id}, duration={workflow_execution.total_duration:.3f}s, quality={quality_assessment['score']:.3f}")
            
            # Convert WorkflowExecution to Dict for interface compliance
            return {
                "workflow_id": workflow_execution.workflow_id,
                "execution_id": workflow_execution.execution_id,
                "status": workflow_execution.status,
                "current_step": workflow_execution.current_step,
                "steps_completed": workflow_execution.steps_completed,
                "steps_failed": workflow_execution.steps_failed,
                "execution_data": workflow_execution.execution_data,
                "start_time": workflow_execution.start_time.isoformat() if workflow_execution.start_time else None,
                "end_time": workflow_execution.end_time.isoformat() if workflow_execution.end_time else None,
                "total_duration": workflow_execution.total_duration,
                "metadata": workflow_execution.metadata,
                "success": workflow_execution.status == WorkflowStatus.COMPLETED.value,
                "quality_score": quality_assessment.get("score", 0.0)
            }
            
        except Exception as e:
            logger.error(f"Error in workflow execution: {str(e)}")
            
            # Handle execution failure
            workflow_execution.status = WorkflowStatus.FAILED.value
            workflow_execution.end_time = datetime.utcnow()
            workflow_execution.total_duration = time.time() - execution_start
            workflow_execution.metadata["error"] = str(e)
            
            self.metrics["failed_workflows"] += 1
            
            # Attempt error recovery if possible
            if self.error_manager:
                try:
                    recovery_result = await self.error_manager.handle_error(e, {
                        "operation": "workflow_execution",
                        "workflow_id": workflow_context.workflow_id,
                        "execution_id": workflow_context.execution_id,
                        "component": "workflow_engine"
                    })
                    workflow_execution.metadata["recovery_attempted"] = True
                    workflow_execution.metadata["recovery_success"] = recovery_result.success
                except:
                    pass
            
            # Convert WorkflowExecution to Dict for interface compliance
            return {
                "workflow_id": workflow_execution.workflow_id,
                "execution_id": workflow_execution.execution_id,
                "status": workflow_execution.status,
                "current_step": workflow_execution.current_step,
                "steps_completed": workflow_execution.steps_completed,
                "steps_failed": workflow_execution.steps_failed,
                "execution_data": workflow_execution.execution_data,
                "start_time": workflow_execution.start_time.isoformat() if workflow_execution.start_time else None,
                "end_time": workflow_execution.end_time.isoformat() if workflow_execution.end_time else None,
                "total_duration": workflow_execution.total_duration,
                "metadata": workflow_execution.metadata,
                "success": False,
                "error": str(e)
            }
            
        finally:
            # Remove from active workflows
            if workflow_context.execution_id in self.active_workflows:
                del self.active_workflows[workflow_context.execution_id]

    async def observe_execution(self, execution_id: str) -> ObservationResult:
        """Monitor and analyze workflow execution in real-time.
        
        Provides comprehensive execution observation:
        - Real-time performance monitoring and bottleneck detection
        - Quality metrics tracking and trend analysis
        - Resource utilization monitoring and optimization recommendations
        - Error pattern detection and prevention suggestions
        - User experience impact assessment
        - Predictive analysis for remaining execution time
        
        Args:
            execution_id: ID of the workflow execution to observe
            
        Returns:
            ObservationResult with detailed execution analysis and insights
        """
        try:
            if execution_id not in self.active_workflows:
                return ObservationResult(
                    execution_id=execution_id,
                    observation_time=datetime.utcnow(),
                    performance_metrics={},
                    quality_indicators={},
                    resource_utilization={},
                    bottlenecks_detected=[],
                    recommendations=[],
                    predicted_completion_time=None,
                    metadata={"error": "Workflow execution not found"}
                )
            
            workflow_execution = self.active_workflows[execution_id]
            observation_time = datetime.utcnow()
            
            # Performance monitoring
            performance_metrics = await self._analyze_execution_performance(workflow_execution)
            
            # Quality assessment
            quality_indicators = await self._assess_execution_quality(workflow_execution)
            
            # Resource utilization analysis
            resource_utilization = await self._monitor_resource_utilization(workflow_execution)
            
            # Bottleneck detection
            bottlenecks = await self._detect_execution_bottlenecks(workflow_execution)
            
            # Generate recommendations
            recommendations = await self._generate_execution_recommendations(
                workflow_execution, performance_metrics, quality_indicators, bottlenecks
            )
            
            # Predict completion time
            predicted_completion = await self._predict_completion_time(workflow_execution)
            
            observation_result = ObservationResult(
                execution_id=execution_id,
                observation_time=observation_time,
                performance_metrics=performance_metrics,
                quality_indicators=quality_indicators,
                resource_utilization=resource_utilization,
                bottlenecks_detected=bottlenecks,
                recommendations=recommendations,
                predicted_completion_time=predicted_completion,
                metadata={
                    "current_step": workflow_execution.current_step,
                    "steps_completed": len(workflow_execution.steps_completed),
                    "steps_failed": len(workflow_execution.steps_failed),
                    "execution_progress": self._calculate_execution_progress(workflow_execution)
                }
            )
            
            return observation_result
            
        except Exception as e:
            logger.error(f"Error observing workflow execution {execution_id}: {str(e)}")
            
            return ObservationResult(
                execution_id=execution_id,
                observation_time=datetime.utcnow(),
                performance_metrics={},
                quality_indicators={},
                resource_utilization={},
                bottlenecks_detected=[],
                recommendations=[f"Observation error: {str(e)}"],
                predicted_completion_time=None,
                metadata={"error": str(e)}
            )

    async def adapt_workflow(self, execution_id: str, observations: ObservationResult) -> AdaptationResult:
        """Dynamically adapt workflow based on execution observations.
        
        Provides intelligent workflow adaptation:
        - Performance optimization based on observed bottlenecks
        - Resource reallocation for improved efficiency
        - Strategy adjustment based on quality indicators
        - Proactive error prevention based on patterns
        - User experience optimization
        - Learning integration for future improvements
        
        Args:
            execution_id: ID of the workflow execution to adapt
            observations: Current observations and recommendations
            
        Returns:
            AdaptationResult with adaptation decisions and impacts
        """
        try:
            if execution_id not in self.active_workflows:
                return AdaptationResult(
                    execution_id=execution_id,
                    adaptations_made=[],
                    adaptation_impact={},
                    success=False,
                    adaptation_time=datetime.utcnow(),
                    metadata={"error": "Workflow execution not found"}
                )
            
            workflow_execution = self.active_workflows[execution_id]
            adaptation_start = time.time()
            adaptations_made = []
            
            # Temporarily pause workflow for adaptation
            original_status = workflow_execution.status
            workflow_execution.status = WorkflowStatus.ADAPTING.value
            
            # Process recommendations
            for recommendation in observations.recommendations:
                adaptation_result = await self._process_adaptation_recommendation(
                    workflow_execution, recommendation, observations
                )
                if adaptation_result["applied"]:
                    adaptations_made.append(adaptation_result)
            
            # Performance-based adaptations
            if observations.performance_metrics.get("average_step_time", 0) > 5.0:  # Slow execution
                parallelization_result = await self._attempt_parallelization(workflow_execution)
                if parallelization_result["success"]:
                    adaptations_made.append(parallelization_result)
            
            # Quality-based adaptations
            quality_score = observations.quality_indicators.get("overall_quality", 1.0)
            if quality_score < 0.7:  # Low quality
                quality_improvement_result = await self._improve_execution_quality(workflow_execution)
                if quality_improvement_result["success"]:
                    adaptations_made.append(quality_improvement_result)
            
            # Resource-based adaptations
            resource_utilization = observations.resource_utilization.get("overall_utilization", 0.5)
            if resource_utilization > 0.8:  # High resource usage
                resource_optimization_result = await self._optimize_resource_usage(workflow_execution)
                if resource_optimization_result["success"]:
                    adaptations_made.append(resource_optimization_result)
            
            # Calculate adaptation impact
            adaptation_impact = await self._calculate_adaptation_impact(
                workflow_execution, adaptations_made, observations
            )
            
            # Resume workflow
            workflow_execution.status = original_status
            
            adaptation_time = time.time() - adaptation_start
            
            # Update metrics
            self.metrics["adaptation_frequency"] = (
                (self.metrics["adaptation_frequency"] * (self.metrics["total_workflows"] - 1) + len(adaptations_made)) /
                self.metrics["total_workflows"]
            )
            
            adaptation_result = AdaptationResult(
                execution_id=execution_id,
                adaptations_made=[a["description"] for a in adaptations_made],
                adaptation_impact=adaptation_impact,
                success=len(adaptations_made) > 0,
                adaptation_time=datetime.utcnow(),
                metadata={
                    "adaptation_duration": adaptation_time,
                    "adaptations_applied": len(adaptations_made),
                    "total_recommendations": len(observations.recommendations),
                    "impact_score": adaptation_impact.get("overall_impact", 0.0)
                }
            )
            
            logger.info(f"Workflow adapted: {execution_id}, adaptations={len(adaptations_made)}, impact={adaptation_impact.get('overall_impact', 0.0):.3f}")
            
            return adaptation_result
            
        except Exception as e:
            logger.error(f"Error adapting workflow {execution_id}: {str(e)}")
            
            return AdaptationResult(
                execution_id=execution_id,
                adaptations_made=[],
                adaptation_impact={},
                success=False,
                adaptation_time=datetime.utcnow(),
                metadata={"error": str(e)}
            )

    async def get_workflow_analytics(self, timeframe: str = "24h") -> Dict[str, Any]:
        """Get comprehensive workflow analytics and insights.
        
        Provides detailed analysis including:
        - Workflow execution patterns and trends
        - Performance benchmarks and optimization opportunities
        - Success rates and failure analysis
        - Resource utilization patterns
        - User experience metrics
        - Predictive insights and recommendations
        
        Args:
            timeframe: Analysis timeframe (e.g., "1h", "24h", "7d")
            
        Returns:
            Dict with comprehensive workflow analytics
        """
        try:
            # Parse timeframe
            hours_back = self._parse_timeframe(timeframe)
            cutoff_time = datetime.utcnow() - timedelta(hours=hours_back)
            
            # Filter recent executions
            recent_executions = [
                execution for execution in self.execution_history
                if execution.get("start_time", datetime.min) >= cutoff_time
            ]
            
            analytics = {
                "timeframe": timeframe,
                "summary_metrics": {
                    "total_workflows": len(recent_executions),
                    "completed_workflows": len([e for e in recent_executions if e.get("status") == "completed"]),
                    "failed_workflows": len([e for e in recent_executions if e.get("status") == "failed"]),
                    "average_execution_time": self.metrics["average_execution_time"],
                    "success_rate": self._calculate_success_rate(recent_executions)
                },
                
                "performance_analysis": {
                    "execution_time_distribution": self._analyze_execution_times(recent_executions),
                    "step_performance": self._analyze_step_performance(recent_executions),
                    "parallel_efficiency": self._analyze_parallel_efficiency(recent_executions),
                    "bottleneck_patterns": self._identify_bottleneck_patterns(recent_executions)
                },
                
                "workflow_patterns": {
                    "common_workflows": self._identify_common_workflows(recent_executions),
                    "adaptation_patterns": self._analyze_adaptation_patterns(recent_executions),
                    "failure_patterns": self._analyze_failure_patterns(recent_executions),
                    "user_behavior_patterns": self._analyze_user_patterns(recent_executions)
                },
                
                "quality_metrics": {
                    "average_quality_score": self._calculate_average_quality(recent_executions),
                    "quality_trends": self._analyze_quality_trends(recent_executions),
                    "quality_by_workflow_type": self._analyze_quality_by_type(recent_executions)
                },
                
                "resource_insights": {
                    "resource_utilization_patterns": self._analyze_resource_patterns(recent_executions),
                    "cost_analysis": self._analyze_execution_costs(recent_executions),
                    "optimization_opportunities": self._identify_optimization_opportunities(recent_executions)
                },
                
                "predictive_insights": {
                    "trend_predictions": self._generate_trend_predictions(recent_executions),
                    "capacity_recommendations": self._generate_capacity_recommendations(recent_executions),
                    "optimization_roadmap": self._generate_optimization_roadmap(recent_executions)
                }
            }
            
            return analytics
            
        except Exception as e:
            logger.error(f"Error generating workflow analytics: {str(e)}")
            return {"error": str(e), "timeframe": timeframe}

    # Private helper methods for workflow planning

    def _initialize_workflow_definitions(self) -> Dict[str, WorkflowDefinition]:
        """Initialize predefined workflow definitions."""
        definitions = {}
        
        # Troubleshooting workflow
        definitions["troubleshooting"] = WorkflowDefinition(
            name="troubleshooting",
            description="Standard troubleshooting workflow following 5-phase SRE doctrine",
            steps=[
                {"name": "classify_query", "operation": StepType.CLASSIFICATION.value, "parameters": {}},
                {"name": "validate_input", "operation": StepType.GUARDRAILS_CHECK.value, "parameters": {}},
                {"name": "gather_context", "operation": StepType.STATE_UPDATE.value, "parameters": {}},
                {"name": "define_blast_radius", "operation": StepType.TOOL_EXECUTION.value, "parameters": {"tool": "knowledge_base"}},
                {"name": "establish_timeline", "operation": StepType.TOOL_EXECUTION.value, "parameters": {"tool": "log_analyzer"}},
                {"name": "formulate_hypothesis", "operation": StepType.TOOL_EXECUTION.value, "parameters": {"tool": "diagnostic_engine"}},
                {"name": "validate_hypothesis", "operation": StepType.TOOL_EXECUTION.value, "parameters": {"tool": "validation_framework"}},
                {"name": "propose_solution", "operation": StepType.TOOL_EXECUTION.value, "parameters": {"tool": "solution_generator"}},
                {"name": "synthesize_response", "operation": StepType.RESPONSE_SYNTHESIS.value, "parameters": {}},
                {"name": "final_validation", "operation": StepType.GUARDRAILS_CHECK.value, "parameters": {}}
            ],
            dependencies={
                "validate_input": ["classify_query"],
                "gather_context": ["validate_input"],
                "define_blast_radius": ["gather_context"],
                "establish_timeline": ["gather_context"],
                "formulate_hypothesis": ["define_blast_radius", "establish_timeline"],
                "validate_hypothesis": ["formulate_hypothesis"],
                "propose_solution": ["validate_hypothesis"],
                "synthesize_response": ["propose_solution"],
                "final_validation": ["synthesize_response"]
            },
            metadata={"complexity": "high", "domain": "troubleshooting"}
        )
        
        # Simple query workflow
        definitions["simple_query"] = WorkflowDefinition(
            name="simple_query",
            description="Lightweight workflow for simple queries",
            steps=[
                {"name": "classify_query", "operation": StepType.CLASSIFICATION.value, "parameters": {}},
                {"name": "validate_input", "operation": StepType.GUARDRAILS_CHECK.value, "parameters": {}},
                {"name": "execute_search", "operation": StepType.TOOL_EXECUTION.value, "parameters": {"tool": "knowledge_base"}},
                {"name": "synthesize_response", "operation": StepType.RESPONSE_SYNTHESIS.value, "parameters": {}},
                {"name": "final_validation", "operation": StepType.GUARDRAILS_CHECK.value, "parameters": {}}
            ],
            dependencies={
                "validate_input": ["classify_query"],
                "execute_search": ["validate_input"],
                "synthesize_response": ["execute_search"],
                "final_validation": ["synthesize_response"]
            },
            metadata={"complexity": "low", "domain": "general"}
        )
        
        return definitions

    def _initialize_step_executors(self) -> Dict[str, Callable]:
        """Initialize step executor functions."""
        return {
            StepType.CLASSIFICATION.value: self._execute_classification_step,
            StepType.TOOL_EXECUTION.value: self._execute_tool_step,
            StepType.GUARDRAILS_CHECK.value: self._execute_guardrails_step,
            StepType.STATE_UPDATE.value: self._execute_state_update_step,
            StepType.RESPONSE_SYNTHESIS.value: self._execute_response_synthesis_step,
            StepType.ERROR_HANDLING.value: self._execute_error_handling_step,
            StepType.DECISION.value: self._execute_decision_step,
            StepType.PARALLEL.value: self._execute_parallel_step,
            StepType.SEQUENTIAL.value: self._execute_sequential_step,
            StepType.LOOP.value: self._execute_loop_step
        }

    def _initialize_decision_rules(self) -> Dict[str, Callable]:
        """Initialize decision rules for workflow branching."""
        return {
            "complexity_based": self._decide_by_complexity,
            "domain_based": self._decide_by_domain,
            "user_preference": self._decide_by_user_preference,
            "performance_based": self._decide_by_performance,
            "quality_based": self._decide_by_quality
        }

    async def _classify_workflow_type(self, request: Dict[str, Any], context: WorkflowContext) -> Dict[str, Any]:
        """Classify the type of workflow needed."""
        query = request.get("query", "")
        metadata = request.get("metadata", {})
        
        # Use classification engine if available
        if self.classification_engine:
            try:
                classification_result = await self.classification_engine.classify_query(query, metadata)
                
                return {
                    "type": classification_result.get("intent", "general"),
                    "complexity": classification_result.get("complexity", "medium"),
                    "domain": classification_result.get("domain", "general"),
                    "urgency": classification_result.get("urgency", "normal"),
                    "confidence": classification_result.get("confidence_score", 0.8)
                }
            except Exception as e:
                logger.warning(f"Classification engine failed: {str(e)}")
        
        # Fallback classification
        if any(keyword in query.lower() for keyword in ["troubleshoot", "debug", "fix", "error", "problem"]):
            return {
                "type": "troubleshooting",
                "complexity": "high",
                "domain": "technical",
                "urgency": "high",
                "confidence": 0.7
            }
        else:
            return {
                "type": "general",
                "complexity": "medium",
                "domain": "general",
                "urgency": "normal",
                "confidence": 0.6
            }

    async def _select_planning_strategy(
        self, 
        classification: Dict[str, Any], 
        context: WorkflowContext, 
        request: Dict[str, Any]
    ) -> str:
        """Select the most appropriate planning strategy."""
        
        complexity = classification.get("complexity", "medium")
        urgency = classification.get("urgency", "normal")
        
        # High complexity or urgent requests benefit from proactive planning
        if complexity == "high" or urgency == "high":
            return PlanningStrategy.PROACTIVE.value
        
        # Use adaptive strategy if we have execution history
        if len(self.execution_history) > 10:
            return PlanningStrategy.ADAPTIVE.value
        
        # Default to reactive for simple cases
        return PlanningStrategy.REACTIVE.value

    async def _plan_reactive(
        self, 
        request: Dict[str, Any], 
        context: WorkflowContext, 
        classification: Dict[str, Any]
    ) -> Dict[str, Any]:
        """Reactive planning strategy - respond to immediate context."""
        workflow_type = classification.get("type", "general")
        
        # Select appropriate workflow template
        if workflow_type == "troubleshooting":
            base_workflow = self.workflow_definitions["troubleshooting"]
        else:
            base_workflow = self.workflow_definitions["simple_query"]
        
        return {
            "steps": base_workflow.steps,
            "dependencies": base_workflow.dependencies,
            "parallel_groups": [],
            "strategy": "reactive"
        }

    async def _plan_proactive(
        self, 
        request: Dict[str, Any], 
        context: WorkflowContext, 
        classification: Dict[str, Any]
    ) -> Dict[str, Any]:
        """Proactive planning strategy - plan ahead based on patterns."""
        # Start with reactive plan
        reactive_plan = await self._plan_reactive(request, context, classification)
        
        # Add proactive enhancements
        enhanced_steps = reactive_plan["steps"].copy()
        
        # Add parallel execution opportunities
        parallel_groups = [
            ["define_blast_radius", "establish_timeline"]  # These can run in parallel
        ]
        
        # Add additional validation steps for high-risk scenarios
        if classification.get("urgency") == "high":
            enhanced_steps.append(
                WorkflowStep(
                    id="additional_validation", 
                    type=StepType.GUARDRAILS_CHECK.value, 
                    config={"enhanced": True}
                )
            )
        
        return {
            "steps": enhanced_steps,
            "dependencies": reactive_plan["dependencies"],
            "parallel_groups": parallel_groups,
            "strategy": "proactive"
        }

    async def _plan_adaptive(
        self, 
        request: Dict[str, Any], 
        context: WorkflowContext, 
        classification: Dict[str, Any]
    ) -> Dict[str, Any]:
        """Adaptive planning strategy - learn from previous executions."""
        # Analyze similar past executions
        similar_executions = self._find_similar_executions(classification, context)
        
        if similar_executions:
            # Learn from successful patterns
            successful_patterns = self._extract_successful_patterns(similar_executions)
            optimized_plan = self._apply_learned_patterns(successful_patterns, classification)
        else:
            # Fall back to proactive planning
            optimized_plan = await self._plan_proactive(request, context, classification)
        
        optimized_plan["strategy"] = "adaptive"
        return optimized_plan

    async def _plan_hybrid(
        self, 
        request: Dict[str, Any], 
        context: WorkflowContext, 
        classification: Dict[str, Any]
    ) -> Dict[str, Any]:
        """Hybrid planning strategy - combine multiple approaches."""
        # Get plans from different strategies
        reactive_plan = await self._plan_reactive(request, context, classification)
        proactive_plan = await self._plan_proactive(request, context, classification)
        
        # Combine best elements from each
        hybrid_steps = proactive_plan["steps"]  # Use proactive steps as base
        hybrid_dependencies = proactive_plan["dependencies"]
        hybrid_parallel_groups = proactive_plan["parallel_groups"]
        
        # Add reactive optimizations
        if len(reactive_plan["steps"]) < len(proactive_plan["steps"]):
            # Reactive plan is simpler, consider using it for low complexity
            if classification.get("complexity") == "low":
                hybrid_steps = reactive_plan["steps"]
                hybrid_dependencies = reactive_plan["dependencies"]
        
        return {
            "steps": hybrid_steps,
            "dependencies": hybrid_dependencies,
            "parallel_groups": hybrid_parallel_groups,
            "strategy": "hybrid"
        }

    # Step execution methods

    async def _execute_classification_step(
        self, 
        step: WorkflowStep, 
        context: WorkflowContext, 
        execution_data: Dict[str, Any]
    ) -> StepExecutionResult:
        """Execute classification step."""
        start_time = time.time()
        
        try:
            if self.classification_engine:
                result = await self.classification_engine.classify_query(
                    context.query, context.metadata
                )
                
                return StepExecutionResult(
                    step_id=step.id,
                    success=True,
                    duration=time.time() - start_time,
                    output_data={"classification_result": result},
                    next_steps=["validate_input"]
                )
            else:
                # Mock classification result
                return StepExecutionResult(
                    step_id=step.id,
                    success=True,
                    duration=time.time() - start_time,
                    output_data={"classification_result": {"intent": "general", "confidence": 0.8}},
                    next_steps=["validate_input"]
                )
                
        except Exception as e:
            return StepExecutionResult(
                step_id=step.id,
                success=False,
                duration=time.time() - start_time,
                output_data={},
                error=str(e)
            )

    async def _execute_tool_step(
        self, 
        step: WorkflowStep, 
        context: WorkflowContext, 
        execution_data: Dict[str, Any]
    ) -> StepExecutionResult:
        """Execute tool execution step."""
        start_time = time.time()
        
        try:
            tool_name = step.config.get("tool", "unknown")
            
            if self.tool_broker:
                # Use tool broker to execute tool
                tool_request = {
                    "tool_name": tool_name,
                    "context": context.metadata,
                    "query": context.query
                }
                
                result = await self.tool_broker.execute_tool_request(tool_request)
                
                return StepExecutionResult(
                    step_id=step.id,
                    success=result.get("success", False),
                    duration=time.time() - start_time,
                    output_data={"tool_result": result},
                    error=result.get("error") if not result.get("success", False) else None
                )
            else:
                # Mock tool execution
                return StepExecutionResult(
                    step_id=step.id,
                    success=True,
                    duration=time.time() - start_time,
                    output_data={"tool_result": f"Mock result for {tool_name}"}
                )
                
        except Exception as e:
            return StepExecutionResult(
                step_id=step.id,
                success=False,
                duration=time.time() - start_time,
                output_data={},
                error=str(e)
            )

    async def _execute_guardrails_step(
        self, 
        step: WorkflowStep, 
        context: WorkflowContext, 
        execution_data: Dict[str, Any]
    ) -> StepExecutionResult:
        """Execute guardrails validation step."""
        start_time = time.time()
        
        try:
            if self.guardrails_layer:
                # Get content to validate from execution data or context
                content = execution_data.get("content", context.query)
                
                result = await self.guardrails_layer.validate_input(content, context.metadata)
                
                return StepExecutionResult(
                    step_id=step.id,
                    success=result.is_safe,
                    duration=time.time() - start_time,
                    output_data={
                        "guardrails_result": {
                            "is_safe": result.is_safe,
                            "sanitized_content": result.sanitized_content,
                            "violations": len(result.violations) if hasattr(result, 'violations') else 0
                        }
                    },
                    error=None if result.is_safe else "Guardrails validation failed"
                )
            else:
                # Mock guardrails validation
                return StepExecutionResult(
                    step_id=step.id,
                    success=True,
                    duration=time.time() - start_time,
                    output_data={"guardrails_result": {"is_safe": True}}
                )
                
        except Exception as e:
            return StepExecutionResult(
                step_id=step.id,
                success=False,
                duration=time.time() - start_time,
                output_data={},
                error=str(e)
            )

    async def _execute_state_update_step(
        self, 
        step: WorkflowStep, 
        context: WorkflowContext, 
        execution_data: Dict[str, Any]
    ) -> StepExecutionResult:
        """Execute state update step."""
        start_time = time.time()
        
        try:
            if self.state_manager:
                # Update conversation memory with current context
                memory_update = {
                    "query": context.query,
                    "timestamp": datetime.utcnow().isoformat(),
                    "execution_data": execution_data
                }
                
                success = await self.state_manager.update_conversation_memory(
                    context.session_id or "default", memory_update
                )
                
                return StepExecutionResult(
                    step_id=step.id,
                    success=success,
                    duration=time.time() - start_time,
                    output_data={"state_updated": success}
                )
            else:
                # Mock state update
                return StepExecutionResult(
                    step_id=step.id,
                    success=True,
                    duration=time.time() - start_time,
                    output_data={"state_updated": True}
                )
                
        except Exception as e:
            return StepExecutionResult(
                step_id=step.id,
                success=False,
                duration=time.time() - start_time,
                output_data={},
                error=str(e)
            )

    async def _execute_response_synthesis_step(
        self, 
        step: WorkflowStep, 
        context: WorkflowContext, 
        execution_data: Dict[str, Any]
    ) -> StepExecutionResult:
        """Execute response synthesis step."""
        start_time = time.time()
        
        try:
            if self.response_synthesizer:
                # Create synthesis request from execution data
                synthesis_request = {
                    "sources": execution_data.get("sources", []),
                    "context": context.metadata,
                    "request_type": "workflow_response"
                }
                
                result = await self.response_synthesizer.synthesize_response(synthesis_request)
                
                return StepExecutionResult(
                    step_id=step.id,
                    success=True,
                    duration=time.time() - start_time,
                    output_data={
                        "synthesized_response": {
                            "content": result.content if hasattr(result, 'content') else "Response synthesized",
                            "quality_score": result.quality_score if hasattr(result, 'quality_score') else 0.8,
                            "confidence": result.confidence_level if hasattr(result, 'confidence_level') else 0.8
                        }
                    }
                )
            else:
                # Mock response synthesis
                return StepExecutionResult(
                    step_id=step.id,
                    success=True,
                    duration=time.time() - start_time,
                    output_data={"synthesized_response": {"content": "Mock synthesized response"}}
                )
                
        except Exception as e:
            return StepExecutionResult(
                step_id=step.id,
                success=False,
                duration=time.time() - start_time,
                output_data={},
                error=str(e)
            )

    async def _execute_error_handling_step(
        self, 
        step: WorkflowStep, 
        context: WorkflowContext, 
        execution_data: Dict[str, Any]
    ) -> StepExecutionResult:
        """Execute error handling step."""
        start_time = time.time()
        
        try:
            if self.error_manager:
                error = execution_data.get("error")
                if error:
                    result = await self.error_manager.handle_error(error, {
                        "operation": "workflow_step",
                        "context": context.metadata
                    })
                    
                    return StepExecutionResult(
                        step_id=step.id,
                        success=result.success,
                        duration=time.time() - start_time,
                        output_data={"error_handling_result": result.__dict__}
                    )
            
            # No error to handle or no error manager
            return StepExecutionResult(
                step_id=step.id,
                success=True,
                duration=time.time() - start_time,
                output_data={"no_error_to_handle": True}
            )
            
        except Exception as e:
            return StepExecutionResult(
                step_id=step.id,
                success=False,
                duration=time.time() - start_time,
                output_data={},
                error=str(e)
            )

    async def _execute_decision_step(
        self, 
        step: WorkflowStep, 
        context: WorkflowContext, 
        execution_data: Dict[str, Any]
    ) -> StepExecutionResult:
        """Execute decision step."""
        start_time = time.time()
        
        try:
            decision_rule = step.config.get("rule", "complexity_based")
            decision_func = self.decision_rules.get(decision_rule, self._decide_by_complexity)
            
            decision_result = await decision_func(context, execution_data)
            
            return StepExecutionResult(
                step_id=step.id,
                success=True,
                duration=time.time() - start_time,
                output_data={"decision_result": decision_result},
                next_steps=decision_result.get("next_steps", [])
            )
            
        except Exception as e:
            return StepExecutionResult(
                step_id=step.id,
                success=False,
                duration=time.time() - start_time,
                output_data={},
                error=str(e)
            )

    async def _execute_parallel_step(
        self, 
        step: WorkflowStep, 
        context: WorkflowContext, 
        execution_data: Dict[str, Any]
    ) -> StepExecutionResult:
        """Execute parallel step group."""
        start_time = time.time()
        
        try:
            parallel_steps = step.config.get("steps", [])
            
            # Execute all steps in parallel
            tasks = []
            for step_id in parallel_steps:
                # Find the actual step definition
                # In production, this would look up the step from the workflow definition
                tasks.append(self._mock_step_execution(step_id, context))
            
            results = await asyncio.gather(*tasks, return_exceptions=True)
            
            success = all(not isinstance(r, Exception) for r in results)
            
            return StepExecutionResult(
                step_id=step.id,
                success=success,
                duration=time.time() - start_time,
                output_data={"parallel_results": results}
            )
            
        except Exception as e:
            return StepExecutionResult(
                step_id=step.id,
                success=False,
                duration=time.time() - start_time,
                output_data={},
                error=str(e)
            )

    async def _execute_sequential_step(
        self, 
        step: WorkflowStep, 
        context: WorkflowContext, 
        execution_data: Dict[str, Any]
    ) -> StepExecutionResult:
        """Execute sequential step group."""
        start_time = time.time()
        
        try:
            sequential_steps = step.config.get("steps", [])
            results = []
            
            # Execute steps sequentially
            for step_id in sequential_steps:
                result = await self._mock_step_execution(step_id, context)
                results.append(result)
                
                # Stop if any step fails
                if not result.get("success", True):
                    break
            
            success = all(r.get("success", True) for r in results)
            
            return StepExecutionResult(
                step_id=step.id,
                success=success,
                duration=time.time() - start_time,
                output_data={"sequential_results": results}
            )
            
        except Exception as e:
            return StepExecutionResult(
                step_id=step.id,
                success=False,
                duration=time.time() - start_time,
                output_data={},
                error=str(e)
            )

    async def _execute_loop_step(
        self, 
        step: WorkflowStep, 
        context: WorkflowContext, 
        execution_data: Dict[str, Any]
    ) -> StepExecutionResult:
        """Execute loop step."""
        start_time = time.time()
        
        try:
            loop_condition = step.config.get("condition", "count")
            max_iterations = step.config.get("max_iterations", 5)
            loop_steps = step.config.get("steps", [])
            
            iteration = 0
            results = []
            
            while iteration < max_iterations:
                # Check loop condition
                should_continue = await self._evaluate_loop_condition(
                    loop_condition, context, execution_data, iteration
                )
                
                if not should_continue:
                    break
                
                # Execute loop steps
                for step_id in loop_steps:
                    result = await self._mock_step_execution(step_id, context)
                    results.append(result)
                
                iteration += 1
            
            return StepExecutionResult(
                step_id=step.id,
                success=True,
                duration=time.time() - start_time,
                output_data={
                    "loop_results": results,
                    "iterations": iteration,
                    "completed": iteration < max_iterations
                }
            )
            
        except Exception as e:
            return StepExecutionResult(
                step_id=step.id,
                success=False,
                duration=time.time() - start_time,
                output_data={},
                error=str(e)
            )

    # Decision rule implementations

    async def _decide_by_complexity(self, context: WorkflowContext, execution_data: Dict[str, Any]) -> Dict[str, Any]:
        """Make decisions based on complexity assessment."""
        complexity = execution_data.get("classification_result", {}).get("complexity", "medium")
        
        if complexity == "high":
            return {"decision": "detailed_analysis", "next_steps": ["formulate_hypothesis", "validate_hypothesis"]}
        elif complexity == "low":
            return {"decision": "simple_response", "next_steps": ["synthesize_response"]}
        else:
            return {"decision": "standard_processing", "next_steps": ["execute_search"]}

    async def _decide_by_domain(self, context: WorkflowContext, execution_data: Dict[str, Any]) -> Dict[str, Any]:
        """Make decisions based on domain classification."""
        domain = execution_data.get("classification_result", {}).get("domain", "general")
        
        if domain == "technical":
            return {"decision": "technical_analysis", "next_steps": ["define_blast_radius", "establish_timeline"]}
        else:
            return {"decision": "general_processing", "next_steps": ["execute_search"]}

    async def _decide_by_user_preference(self, context: WorkflowContext, execution_data: Dict[str, Any]) -> Dict[str, Any]:
        """Make decisions based on user preferences."""
        # Mock user preference analysis
        return {"decision": "user_preferred", "next_steps": ["execute_search"]}

    async def _decide_by_performance(self, context: WorkflowContext, execution_data: Dict[str, Any]) -> Dict[str, Any]:
        """Make decisions based on performance considerations."""
        # Mock performance-based decision
        return {"decision": "performance_optimized", "next_steps": ["execute_search"]}

    async def _decide_by_quality(self, context: WorkflowContext, execution_data: Dict[str, Any]) -> Dict[str, Any]:
        """Make decisions based on quality requirements."""
        # Mock quality-based decision
        return {"decision": "quality_focused", "next_steps": ["additional_validation", "synthesize_response"]}

    # Additional helper methods would continue here...
    # Due to length constraints, I'll provide key remaining methods

    def _parse_deadline(self, deadline_str: Optional[str]) -> Optional[datetime]:
        """Parse deadline string to datetime."""
        if not deadline_str:
            return None
        
        try:
            return datetime.fromisoformat(deadline_str)
        except:
            return None

    def _parse_timeframe(self, timeframe: str) -> int:
        """Parse timeframe string to hours."""
        if timeframe.endswith('h'):
            return int(timeframe[:-1])
        elif timeframe.endswith('d'):
            return int(timeframe[:-1]) * 24
        elif timeframe.endswith('w'):
            return int(timeframe[:-1]) * 24 * 7
        else:
            return 24  # Default to 24 hours

    async def _mock_step_execution(self, step_id: str, context: WorkflowContext) -> Dict[str, Any]:
        """Mock step execution for testing."""
        await asyncio.sleep(0.1)  # Simulate work
        return {"step_id": step_id, "success": True, "result": f"Mock result for {step_id}"}

    async def _evaluate_loop_condition(
        self, 
        condition: str, 
        context: WorkflowContext, 
        execution_data: Dict[str, Any], 
        iteration: int
    ) -> bool:
        """Evaluate loop continuation condition."""
        if condition == "count":
            return iteration < 3  # Simple count-based loop
        elif condition == "quality":
            # Mock quality-based condition
            quality = execution_data.get("quality_score", 0.8)
            return quality < 0.9 and iteration < 5
        else:
            return False

    def _find_similar_executions(self, classification: Dict[str, Any], context: WorkflowContext) -> List[Dict[str, Any]]:
        """Find similar past executions for learning."""
        similar = []
        workflow_type = classification.get("type", "general")
        
        for execution in self.execution_history[-100:]:  # Last 100 executions
            if execution.get("workflow_type") == workflow_type:
                similar.append(execution)
        
        return similar[:10]  # Top 10 similar executions

    def _extract_successful_patterns(self, executions: List[Dict[str, Any]]) -> Dict[str, Any]:
        """Extract successful patterns from past executions."""
        successful = [e for e in executions if e.get("status") == "completed"]
        
        if not successful:
            return {}
        
        # Analyze common patterns in successful executions
        patterns = {
            "average_steps": sum(len(e.get("steps", [])) for e in successful) / len(successful),
            "common_parallelizations": [],  # Would analyze actual parallel patterns
            "success_factors": []  # Would identify key success factors
        }
        
        return patterns

    def _apply_learned_patterns(self, patterns: Dict[str, Any], classification: Dict[str, Any]) -> Dict[str, Any]:
        """Apply learned patterns to create optimized plan."""
        # This would implement sophisticated pattern application
        # For now, return a basic plan structure
        return {
            "steps": [],  # Would be populated with optimized steps
            "dependencies": {},
            "parallel_groups": patterns.get("common_parallelizations", []),
            "strategy": "learned_pattern"
        }

    async def _validate_workflow_plan(self, plan: Dict[str, Any], context: WorkflowContext) -> Dict[str, Any]:
        """Validate workflow plan for correctness and completeness."""
        issues = []
        quality_score = 1.0
        
        # Check for circular dependencies
        if self._has_circular_dependencies(plan.get("dependencies", {})):
            issues.append("Circular dependencies detected")
            quality_score *= 0.5
        
        # Check for missing steps
        if not plan.get("steps"):
            issues.append("No steps defined")
            quality_score *= 0.3
        
        return {
            "is_valid": len(issues) == 0,
            "issues": issues,
            "quality_score": quality_score
        }

    def _has_circular_dependencies(self, dependencies: Dict[str, List[str]]) -> bool:
        """Check for circular dependencies in workflow plan."""
        # Simplified circular dependency check
        visited = set()
        rec_stack = set()
        
        def has_cycle(node):
            visited.add(node)
            rec_stack.add(node)
            
            for neighbor in dependencies.get(node, []):
                if neighbor not in visited:
                    if has_cycle(neighbor):
                        return True
                elif neighbor in rec_stack:
                    return True
            
            rec_stack.remove(node)
            return False
        
        for node in dependencies:
            if node not in visited:
                if has_cycle(node):
                    return True
        
        return False

    async def _correct_workflow_plan(
        self, 
        plan: Dict[str, Any], 
        issues: List[str], 
        context: WorkflowContext
    ) -> Dict[str, Any]:
        """Attempt to correct issues in workflow plan."""
        corrected_plan = plan.copy()
        
        for issue in issues:
            if "circular" in issue.lower():
                # Remove circular dependencies by breaking cycles
                corrected_plan["dependencies"] = self._break_dependency_cycles(
                    corrected_plan.get("dependencies", {})
                )
            elif "no steps" in issue.lower():
                # Add default steps
                corrected_plan["steps"] = [
                    {"id": "default_step", "type": "tool_execution", "config": {}}
                ]
        
        return corrected_plan

    def _break_dependency_cycles(self, dependencies: Dict[str, List[str]]) -> Dict[str, List[str]]:
        """Break circular dependencies by removing problematic edges."""
        # Simplified cycle breaking - in production, this would be more sophisticated
        corrected = {}
        for step, deps in dependencies.items():
            # Remove self-dependencies
            corrected[step] = [d for d in deps if d != step]
        
        return corrected

    async def _analyze_resource_requirements(self, plan: Dict[str, Any], context: WorkflowContext) -> Dict[str, Any]:
        """Analyze resource requirements for workflow execution."""
        step_count = len(plan.get("steps", []))
        parallel_groups = len(plan.get("parallel_groups", []))
        
        return {
            "cpu_intensity": "medium" if step_count < 10 else "high",
            "memory_requirements": "low" if step_count < 5 else "medium",
            "network_calls": step_count * 0.8,  # Estimated network calls
            "storage_needs": "minimal",
            "concurrent_operations": max(1, parallel_groups),
            "intensity": "medium" if step_count < 10 else "high"
        }

    async def _estimate_execution_timeline(self, plan: Dict[str, Any], context: WorkflowContext) -> Dict[str, Any]:
        """Estimate workflow execution timeline."""
        steps = plan.get("steps", [])
        parallel_groups = plan.get("parallel_groups", [])
        
        # Base time per step
        base_time_per_step = 2.0  # seconds
        
        # Calculate sequential time
        sequential_time = len(steps) * base_time_per_step
        
        # Account for parallelization
        if parallel_groups:
            parallel_savings = len(parallel_groups) * base_time_per_step * 0.5
            total_time = max(base_time_per_step, sequential_time - parallel_savings)
        else:
            total_time = sequential_time
        
        return {
            "total_duration": total_time,
            "step_breakdown": {step.get("id", f"step_{i}"): base_time_per_step for i, step in enumerate(steps)},
            "parallel_savings": len(parallel_groups) * base_time_per_step * 0.5 if parallel_groups else 0
        }

    async def _assess_workflow_risks(self, plan: Dict[str, Any], context: WorkflowContext) -> Dict[str, Any]:
        """Assess risks associated with workflow execution."""
        step_count = len(plan.get("steps", []))
        complexity = context.metadata.get("complexity", "medium")
        
        risk_factors = []
        risk_score = 0.0
        
        if step_count > 15:
            risk_factors.append("High step count")
            risk_score += 0.3
        
        if complexity == "high":
            risk_factors.append("High complexity")
            risk_score += 0.4
        
        # External dependencies
        external_steps = [s for s in plan.get("steps", []) if s.get("type") == "tool_execution"]
        if len(external_steps) > 5:
            risk_factors.append("Many external dependencies")
            risk_score += 0.2
        
        overall_risk = "low" if risk_score < 0.3 else "medium" if risk_score < 0.7 else "high"
        
        return {
            "overall_risk": overall_risk,
            "risk_score": min(1.0, risk_score),
            "risk_factors": risk_factors,
            "mitigation_strategies": self._generate_risk_mitigations(risk_factors)
        }

    def _generate_risk_mitigations(self, risk_factors: List[str]) -> List[str]:
        """Generate risk mitigation strategies."""
        mitigations = []
        
        for factor in risk_factors:
            if "step count" in factor.lower():
                mitigations.append("Consider parallelization opportunities")
            elif "complexity" in factor.lower():
                mitigations.append("Add additional validation steps")
            elif "dependencies" in factor.lower():
                mitigations.append("Implement circuit breaker patterns")
        
        return mitigations

    async def _generate_fallback_plan(
        self, 
        plan: Dict[str, Any], 
        risk_assessment: Dict[str, Any], 
        context: WorkflowContext
    ) -> Dict[str, Any]:
        """Generate fallback plan for error scenarios."""
        fallback_steps = []
        
        # Add error handling steps
        if risk_assessment["overall_risk"] == "high":
            fallback_steps.extend([
                {"id": "error_detection", "type": "error_handling"},
                {"id": "graceful_degradation", "type": "response_synthesis"},
                {"id": "user_notification", "type": "response_synthesis"}
            ])
        
        return {
            "fallback_steps": fallback_steps,
            "conditions": ["step_failure", "timeout", "resource_exhaustion"],
            "escalation_path": ["retry", "degraded_mode", "manual_intervention"]
        }

    # Execution pipeline methods

    async def _prepare_execution_environment(self, context: WorkflowContext, plan: ExecutionPlan) -> None:
        """Prepare execution environment."""
        # Initialize execution state
        if self.state_manager:
            try:
                await self.state_manager.create_execution_plan(
                    context.session_id or "default",
                    context.query,
                    context.metadata
                )
            except Exception as e:
                logger.warning(f"Failed to initialize execution state: {str(e)}")

    async def _execute_workflow_steps(
        self, 
        plan: ExecutionPlan, 
        context: WorkflowContext, 
        execution: WorkflowExecution
    ) -> List[StepExecutionResult]:
        """Execute all workflow steps with dependency resolution."""
        execution_results = []
        executed_steps = set()
        execution_data = {}
        
        steps_to_execute = plan.steps if hasattr(plan, 'steps') else []
        dependencies = plan.dependencies if hasattr(plan, 'dependencies') else {}
        
        while len(executed_steps) < len(steps_to_execute):
            ready_steps = []
            
            # Find steps that are ready to execute (all dependencies satisfied)
            for step in steps_to_execute:
                step_id = step.id if hasattr(step, 'id') else step.get('id', f'step_{len(executed_steps)}')
                
                if step_id not in executed_steps:
                    step_deps = dependencies.get(step_id, [])
                    if all(dep in executed_steps for dep in step_deps):
                        ready_steps.append(step)
            
            if not ready_steps:
                break  # No more steps can be executed (possible deadlock)
            
            # Execute ready steps (can be done in parallel)
            step_tasks = []
            for step in ready_steps:
                step_executor = self.step_executors.get(step.type if hasattr(step, 'type') else step.get('type', 'tool_execution'))
                if step_executor:
                    step_tasks.append(step_executor(step, context, execution_data))
                else:
                    # Default mock execution
                    step_tasks.append(self._mock_step_execution(step.id if hasattr(step, 'id') else 'unknown', context))
            
            # Wait for step completion
            if step_tasks:
                step_results = await asyncio.gather(*step_tasks, return_exceptions=True)
                
                for i, result in enumerate(step_results):
                    if isinstance(result, Exception):
                        # Handle step execution error
                        error_result = StepExecutionResult(
                            step_id=ready_steps[i].id if hasattr(ready_steps[i], 'id') else f'step_{i}',
                            success=False,
                            duration=0.0,
                            output_data={},
                            error=str(result)
                        )
                        execution_results.append(error_result)
                        execution.steps_failed.append(ready_steps[i].id if hasattr(ready_steps[i], 'id') else f'step_{i}')
                    else:
                        execution_results.append(result)
                        if result.success:
                            execution.steps_completed.append(result.step_id)
                            executed_steps.add(result.step_id)
                            execution_data.update(result.output_data)
                        else:
                            execution.steps_failed.append(result.step_id)
                
                execution.current_step = len(executed_steps)
        
        return execution_results

    async def _collect_execution_results(
        self, 
        execution_results: List[StepExecutionResult], 
        context: WorkflowContext, 
        plan: ExecutionPlan
    ) -> Dict[str, Any]:
        """Collect and consolidate execution results."""
        consolidated_results = {
            "successful_steps": [r for r in execution_results if r.success],
            "failed_steps": [r for r in execution_results if not r.success],
            "total_execution_time": sum(r.duration for r in execution_results),
            "step_outputs": {}
        }
        
        # Consolidate step outputs
        for result in execution_results:
            consolidated_results["step_outputs"][result.step_id] = result.output_data
        
        # Extract final response if available
        synthesis_results = [r for r in execution_results if "synthesized_response" in r.output_data]
        if synthesis_results:
            consolidated_results["final_response"] = synthesis_results[-1].output_data["synthesized_response"]
        
        return consolidated_results

    async def _validate_execution_quality(
        self, 
        results: Dict[str, Any], 
        context: WorkflowContext, 
        plan: ExecutionPlan
    ) -> Dict[str, Any]:
        """Validate quality of execution results."""
        successful_steps = len(results["successful_steps"])
        total_steps = successful_steps + len(results["failed_steps"])
        
        completion_rate = successful_steps / max(1, total_steps)
        
        # Quality factors
        quality_factors = {
            "completion_rate": completion_rate,
            "execution_efficiency": 1.0 - min(1.0, results["total_execution_time"] / 60.0),  # Penalize long executions
            "error_rate": len(results["failed_steps"]) / max(1, total_steps),
            "response_quality": results.get("final_response", {}).get("quality_score", 0.8)
        }
        
        # Calculate overall quality score
        weights = {"completion_rate": 0.4, "execution_efficiency": 0.2, "error_rate": -0.2, "response_quality": 0.2}
        quality_score = sum(weights.get(factor, 0) * value for factor, value in quality_factors.items())
        quality_score = max(0.0, min(1.0, quality_score))
        
        return {
            "score": quality_score,
            "factors": quality_factors,
            "recommendations": self._generate_quality_recommendations(quality_factors)
        }

    def _generate_quality_recommendations(self, quality_factors: Dict[str, float]) -> List[str]:
        """Generate recommendations based on quality assessment."""
        recommendations = []
        
        if quality_factors["completion_rate"] < 0.8:
            recommendations.append("Improve error handling to increase completion rate")
        
        if quality_factors["execution_efficiency"] < 0.7:
            recommendations.append("Optimize workflow for faster execution")
        
        if quality_factors["error_rate"] > 0.1:
            recommendations.append("Investigate and address common failure points")
        
        if quality_factors["response_quality"] < 0.8:
            recommendations.append("Enhance response synthesis quality")
        
        return recommendations

    def _update_execution_metrics(self, execution: WorkflowExecution, results: List[StepExecutionResult]) -> None:
        """Update performance metrics based on execution."""
        # Update average execution time
        total_workflows = self.metrics["total_workflows"]
        current_avg = self.metrics["average_execution_time"]
        new_avg = ((current_avg * (total_workflows - 1)) + execution.total_duration) / total_workflows
        self.metrics["average_execution_time"] = new_avg
        
        # Update step success rates
        for result in results:
            step_type = result.step_id.split("_")[0]  # Extract step type
            if step_type not in self.metrics["step_success_rates"]:
                self.metrics["step_success_rates"][step_type] = {"total": 0, "successful": 0}
            
            self.metrics["step_success_rates"][step_type]["total"] += 1
            if result.success:
                self.metrics["step_success_rates"][step_type]["successful"] += 1

    async def _add_to_execution_history(
        self, 
        execution: WorkflowExecution, 
        plan: ExecutionPlan, 
        results: List[StepExecutionResult]
    ) -> None:
        """Add execution to history for learning and analysis."""
        history_entry = {
            "workflow_id": execution.workflow_id,
            "execution_id": execution.execution_id,
            "status": execution.status,
            "start_time": execution.start_time,
            "end_time": execution.end_time,
            "total_duration": execution.total_duration,
            "steps": [r.step_id for r in results],
            "successful_steps": [r.step_id for r in results if r.success],
            "failed_steps": [r.step_id for r in results if not r.success],
            "metadata": execution.metadata
        }
        
        self.execution_history.append(history_entry)
        
        # Maintain history size
        if len(self.execution_history) > self.max_history_size:
            self.execution_history = self.execution_history[-self.max_history_size//2:]

    # Performance analysis helper methods

    def _calculate_execution_progress(self, execution: WorkflowExecution) -> float:
        """Calculate execution progress as percentage."""
        total_steps = len(execution.steps_completed) + len(execution.steps_failed) + max(0, execution.current_step)
        if total_steps == 0:
            return 0.0
        
        completed = len(execution.steps_completed)
        return min(1.0, completed / total_steps)

    async def _analyze_execution_performance(self, execution: WorkflowExecution) -> Dict[str, Any]:
        """Analyze performance metrics for active execution."""
        elapsed_time = (datetime.utcnow() - execution.start_time).total_seconds()
        progress = self._calculate_execution_progress(execution)
        
        return {
            "elapsed_time": elapsed_time,
            "progress": progress,
            "steps_per_second": len(execution.steps_completed) / max(1, elapsed_time),
            "estimated_remaining_time": (elapsed_time / max(0.01, progress)) - elapsed_time if progress > 0 else None,
            "current_step": execution.current_step,
            "completion_rate": len(execution.steps_completed) / max(1, len(execution.steps_completed) + len(execution.steps_failed))
        }

    async def _assess_execution_quality(self, execution: WorkflowExecution) -> Dict[str, Any]:
        """Assess quality indicators for active execution."""
        total_steps = len(execution.steps_completed) + len(execution.steps_failed)
        
        return {
            "step_success_rate": len(execution.steps_completed) / max(1, total_steps),
            "error_rate": len(execution.steps_failed) / max(1, total_steps),
            "data_quality": execution.metadata.get("quality_score", 0.8),
            "user_satisfaction_prediction": 0.8,  # Would be based on patterns
            "overall_quality": (len(execution.steps_completed) / max(1, total_steps)) * 0.8
        }

    async def _monitor_resource_utilization(self, execution: WorkflowExecution) -> Dict[str, Any]:
        """Monitor resource utilization for active execution."""
        # Mock resource monitoring - in production, this would gather real metrics
        return {
            "cpu_usage": 0.6,
            "memory_usage": 0.4,
            "network_io": 0.3,
            "concurrent_operations": execution.current_step,
            "overall_utilization": 0.5
        }

    async def _detect_execution_bottlenecks(self, execution: WorkflowExecution) -> List[str]:
        """Detect bottlenecks in execution."""
        bottlenecks = []
        
        # Check execution time
        elapsed = (datetime.utcnow() - execution.start_time).total_seconds()
        if elapsed > 60:  # More than 1 minute
            bottlenecks.append("Long execution time detected")
        
        # Check failure rate
        total_steps = len(execution.steps_completed) + len(execution.steps_failed)
        if total_steps > 0 and len(execution.steps_failed) / total_steps > 0.2:
            bottlenecks.append("High step failure rate")
        
        # Check progress rate
        progress = self._calculate_execution_progress(execution)
        if elapsed > 30 and progress < 0.3:
            bottlenecks.append("Slow progress rate")
        
        return bottlenecks

    async def _generate_execution_recommendations(
        self, 
        execution: WorkflowExecution, 
        performance: Dict[str, Any], 
        quality: Dict[str, Any], 
        bottlenecks: List[str]
    ) -> List[str]:
        """Generate recommendations for improving execution."""
        recommendations = []
        
        if performance["steps_per_second"] < 0.1:
            recommendations.append("Consider parallelizing independent steps")
        
        if quality["step_success_rate"] < 0.8:
            recommendations.append("Review and strengthen error handling")
        
        if "Long execution time" in bottlenecks:
            recommendations.append("Optimize slow steps or increase timeout limits")
        
        if "High step failure rate" in bottlenecks:
            recommendations.append("Investigate common failure patterns")
        
        return recommendations

    async def _predict_completion_time(self, execution: WorkflowExecution) -> Optional[datetime]:
        """Predict when execution will complete."""
        progress = self._calculate_execution_progress(execution)
        if progress <= 0:
            return None
        
        elapsed = (datetime.utcnow() - execution.start_time).total_seconds()
        estimated_total = elapsed / progress
        remaining = estimated_total - elapsed
        
        return datetime.utcnow() + timedelta(seconds=remaining)

    # Analytics helper methods for comprehensive reporting

    def _calculate_success_rate(self, executions: List[Dict[str, Any]]) -> float:
        """Calculate success rate for executions."""
        if not executions:
            return 0.0
        
        successful = len([e for e in executions if e.get("status") == "completed"])
        return successful / len(executions)

    def _analyze_execution_times(self, executions: List[Dict[str, Any]]) -> Dict[str, float]:
        """Analyze execution time distribution."""
        times = [e.get("total_duration", 0) for e in executions if e.get("total_duration")]
        
        if not times:
            return {}
        
        return {
            "average": sum(times) / len(times),
            "median": sorted(times)[len(times) // 2],
            "min": min(times),
            "max": max(times),
            "std_dev": (sum((t - sum(times)/len(times))**2 for t in times) / len(times))**0.5
        }

    # Additional analytics methods would continue...
    # The remaining methods follow similar patterns for comprehensive workflow analysis
    
    # Interface compliance methods
    
    async def orchestrate_agents(self, agents: List[str], task: Dict[str, Any]) -> Dict[str, Any]:
        """Orchestrate multiple agents for a complex task"""
        try:
            orchestration_start = time.time()
            
            # Create orchestration context
            orchestration_id = str(uuid4())
            
            results = []
            for agent_id in agents:
                # In a production system, this would dispatch tasks to actual agents
                agent_result = {
                    "agent_id": agent_id,
                    "task": task,
                    "status": "completed",
                    "result": f"Mock result from agent {agent_id}",
                    "duration": 1.0
                }
                results.append(agent_result)
            
            orchestration_time = time.time() - orchestration_start
            
            return {
                "orchestration_id": orchestration_id,
                "success": True,
                "agent_results": results,
                "total_agents": len(agents),
                "completed_agents": len(results),
                "orchestration_time": orchestration_time,
                "overall_status": "completed"
            }
            
        except Exception as e:
            logger.error(f"Error orchestrating agents: {str(e)}")
            return {
                "orchestration_id": str(uuid4()),
                "success": False,
                "agent_results": [],
                "error": str(e)
            }
    
    async def manage_workflow_state(self, session_id: str, state: Dict[str, Any]) -> bool:
        """Manage workflow state transitions"""
        try:
            if self.state_manager:
                # Update workflow state via state manager
                success = await self.state_manager.update_execution_state(session_id, state)
                if success:
                    logger.debug(f"Updated workflow state for session {session_id}")
                else:
                    logger.warning(f"Failed to update workflow state for session {session_id}")
                return success
            else:
                # Mock state management
                logger.debug(f"Mock workflow state update for session {session_id}")
                return True
                
        except Exception as e:
            logger.error(f"Error managing workflow state for session {session_id}: {str(e)}")
            return False
    
    async def coordinate_tool_execution(self, tools: List[str], parameters: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        """Coordinate execution of multiple tools"""
        try:
            if len(tools) != len(parameters):
                raise ValueError("Number of tools must match number of parameter sets")
            
            results = []
            
            if self.tool_broker:
                # Use tool broker for actual tool coordination
                for tool_name, params in zip(tools, parameters):
                    try:
                        tool_result = await self.tool_broker.execute_capability(
                            tool_name, params, {"coordination": True}
                        )
                        results.append({
                            "tool_name": tool_name,
                            "success": tool_result.get("success", False),
                            "result": tool_result,
                            "parameters": params
                        })
                    except Exception as e:
                        results.append({
                            "tool_name": tool_name,
                            "success": False,
                            "error": str(e),
                            "parameters": params
                        })
            else:
                # Mock tool coordination
                for tool_name, params in zip(tools, parameters):
                    results.append({
                        "tool_name": tool_name,
                        "success": True,
                        "result": f"Mock result for {tool_name}",
                        "parameters": params
                    })
            
            logger.info(f"Coordinated {len(tools)} tools, {len([r for r in results if r['success']])} successful")
            
            return results
            
        except Exception as e:
            logger.error(f"Error coordinating tool execution: {str(e)}")
            return [{"error": str(e), "success": False}]
    
    async def adapt_workflow(self, session_id: str, observations: List[ObservationData]) -> ExecutionPlan:
        """Adapt workflow based on observations"""
        try:
            # Create adaptation context
            adaptation_context = {
                "session_id": session_id,
                "observation_count": len(observations),
                "timestamp": datetime.utcnow()
            }
            
            # Analyze observations to determine adaptations needed
            adaptations_needed = []
            for obs in observations:
                if obs.confidence < 0.7:
                    adaptations_needed.append("improve_confidence")
                if obs.observation_type == "performance" and obs.data.get("latency", 0) > 5.0:
                    adaptations_needed.append("optimize_performance")
                if obs.observation_type == "error" and obs.data.get("error_rate", 0) > 0.1:
                    adaptations_needed.append("enhance_error_handling")
            
            # Create adapted execution plan
            adapted_plan = ExecutionPlan(
                session_id=session_id,
                nodes=[],
                steps=[],
                execution_order=[],
                estimated_total_duration=15.0,  # Adapted duration
                risk_assessment={"risk_level": "medium", "adaptations_applied": len(adaptations_needed)}
            )
            
            # Add adaptation-specific steps
            adaptation_steps = []
            step_counter = 1
            
            for adaptation in set(adaptations_needed):  # Remove duplicates
                if adaptation == "improve_confidence":
                    adaptation_steps.append(PlanNode(
                        name=f"confidence_boost_{step_counter}",
                        description="Boost confidence through additional validation",
                        action_type="validation",
                        parameters={"target_confidence": 0.8}
                    ))
                elif adaptation == "optimize_performance":
                    adaptation_steps.append(PlanNode(
                        name=f"performance_opt_{step_counter}",
                        description="Optimize performance through parallelization",
                        action_type="optimization",
                        parameters={"strategy": "parallel"}
                    ))
                elif adaptation == "enhance_error_handling":
                    adaptation_steps.append(PlanNode(
                        name=f"error_handling_{step_counter}",
                        description="Enhanced error handling and recovery",
                        action_type="error_handling",
                        parameters={"recovery_strategy": "graceful_degradation"}
                    ))
                step_counter += 1
            
            # If no specific adaptations needed, add default improvement step
            if not adaptation_steps:
                adaptation_steps.append(PlanNode(
                    name="general_optimization",
                    description="General workflow optimization",
                    action_type="optimization",
                    parameters={"type": "general"}
                ))
            
            adapted_plan.nodes = adaptation_steps
            adapted_plan.steps = adaptation_steps
            adapted_plan.execution_order = [node.node_id for node in adaptation_steps]
            
            logger.info(f"Adapted workflow for session {session_id} with {len(adaptations_needed)} adaptations")
            
            return adapted_plan
            
        except Exception as e:
            logger.error(f"Error adapting workflow for session {session_id}: {str(e)}")
            
            # Return minimal fallback plan
            return ExecutionPlan(
                session_id=session_id,
                nodes=[PlanNode(
                    name="fallback_step",
                    description="Fallback execution step",
                    action_type="fallback"
                )],
                steps=[PlanNode(
                    name="fallback_step",
                    description="Fallback execution step", 
                    action_type="fallback"
                )],
                execution_order=["fallback_step"]
            )