"""Reasoning Service Implementation

This module provides the Reasoning Service that implements intelligent reasoning
workflows for advanced troubleshooting and problem-solving in the FaultMaven system.

The Reasoning Service acts as the orchestration layer for enhanced reasoning
capabilities, coordinating with memory and planning services to provide
sophisticated multi-step reasoning workflows.
"""

import logging
from typing import Dict, Any, Optional, List
from datetime import datetime

from faultmaven.services.base_service import BaseService
from faultmaven.models.interfaces import (
    ILLMProvider, IMemoryService, IPlanningService,
    StrategicPlan, ConversationContext
)
from faultmaven.core.reasoning.enhanced_workflows import (
    EnhancedReasoningEngine, ReasoningWorkflow, ReasoningStep
)
from faultmaven.exceptions import ReasoningException, ValidationException


class ReasoningService(BaseService):
    """Reasoning Service implementing enhanced reasoning workflows
    
    This service provides the main interface for reasoning operations in FaultMaven,
    including multi-step reasoning workflows, problem analysis, and intelligent
    solution generation. It coordinates with memory and planning services to
    provide context-aware reasoning capabilities.
    
    Key Responsibilities:
    - Execute sophisticated reasoning workflows
    - Integrate memory insights into reasoning processes
    - Apply strategic planning to problem-solving approaches
    - Provide adaptive confidence scoring and validation
    - Track reasoning performance and effectiveness
    - Support multiple reasoning paradigms (diagnostic, analytical, strategic, creative)
    
    Performance Targets:
    - Reasoning workflow execution: < 5 seconds
    - Memory integration: < 500ms
    - Strategic planning integration: < 1 second
    
    Integration Points:
    - Works with MemoryService for context-aware reasoning
    - Integrates with PlanningService for strategic approaches
    - Uses LLM providers for advanced analysis and synthesis
    """
    
    def __init__(
        self,
        llm_provider: ILLMProvider,
        memory_service: Optional[IMemoryService] = None,
        planning_service: Optional[IPlanningService] = None
    ):
        """Initialize Reasoning Service with interface dependencies
        
        Args:
            llm_provider: LLM interface for reasoning and analysis
            memory_service: Optional memory service for context enhancement
            planning_service: Optional planning service for strategic reasoning
        """
        super().__init__()
        
        self._reasoning_engine = EnhancedReasoningEngine(
            llm_provider=llm_provider,
            memory_service=memory_service,
            planning_service=planning_service
        )
        
        self._llm_provider = llm_provider
        self._memory_service = memory_service
        self._planning_service = planning_service
        
        self._performance_metrics = {
            "reasoning_workflows_executed": 0,
            "diagnostic_workflows": 0,
            "analytical_workflows": 0,
            "strategic_workflows": 0,
            "creative_workflows": 0,
            "avg_workflow_duration": 0.0,
            "avg_confidence_score": 0.0,
            "memory_integrations": 0,
            "planning_integrations": 0,
            "reasoning_failures": 0
        }
    
    async def execute_diagnostic_reasoning(
        self,
        problem_statement: str,
        session_id: str,
        context: Dict[str, Any]
    ) -> ReasoningWorkflow:
        """Execute diagnostic reasoning workflow for troubleshooting
        
        This method provides structured diagnostic reasoning that breaks down
        problems, matches patterns, generates hypotheses, validates evidence,
        and recommends solutions with risk assessment.
        
        Args:
            problem_statement: Problem description requiring diagnostic reasoning
            session_id: Session identifier for memory context
            context: Additional context including user profile, data, constraints
                   
        Returns:
            ReasoningWorkflow containing diagnostic analysis and recommendations
            
        Raises:
            ReasoningException: When diagnostic reasoning fails
            ValidationException: When inputs are invalid
        """
        return await self.execute_operation(
            "execute_diagnostic_reasoning",
            self._execute_diagnostic_workflow,
            problem_statement,
            session_id,
            context,
            validate_inputs=self._validate_reasoning_inputs
        )
    
    async def execute_analytical_reasoning(
        self,
        problem_statement: str,
        session_id: str,
        context: Dict[str, Any]
    ) -> ReasoningWorkflow:
        """Execute analytical reasoning workflow for complex analysis
        
        This method provides sophisticated analytical reasoning that examines
        problems from multiple dimensions, traces causal relationships,
        assesses impact, and synthesizes strategic recommendations.
        
        Args:
            problem_statement: Problem requiring analytical reasoning
            session_id: Session identifier for memory context
            context: Additional context including business factors, constraints
                   
        Returns:
            ReasoningWorkflow containing analytical findings and insights
            
        Raises:
            ReasoningException: When analytical reasoning fails
            ValidationException: When inputs are invalid
        """
        return await self.execute_operation(
            "execute_analytical_reasoning",
            self._execute_analytical_workflow,
            problem_statement,
            session_id,
            context,
            validate_inputs=self._validate_reasoning_inputs
        )
    
    async def execute_strategic_reasoning(
        self,
        problem_statement: str,
        session_id: str,
        context: Dict[str, Any]
    ) -> ReasoningWorkflow:
        """Execute strategic reasoning workflow for high-level planning
        
        This method provides strategic reasoning that analyzes organizational
        context, generates alternative approaches, and recommends optimal
        strategic directions with implementation guidance.
        
        Args:
            problem_statement: Strategic challenge requiring reasoning
            session_id: Session identifier for memory context
            context: Context including organizational factors, stakeholders
                   
        Returns:
            ReasoningWorkflow containing strategic analysis and recommendations
            
        Raises:
            ReasoningException: When strategic reasoning fails
            ValidationException: When inputs are invalid
        """
        return await self.execute_operation(
            "execute_strategic_reasoning",
            self._execute_strategic_workflow,
            problem_statement,
            session_id,
            context,
            validate_inputs=self._validate_reasoning_inputs
        )
    
    async def execute_creative_reasoning(
        self,
        problem_statement: str,
        session_id: str,
        context: Dict[str, Any]
    ) -> ReasoningWorkflow:
        """Execute creative reasoning workflow for innovative solutions
        
        This method provides creative reasoning that reframes problems,
        generates innovative solutions using creative techniques, and
        evaluates feasibility of novel approaches.
        
        Args:
            problem_statement: Problem requiring creative solutions
            session_id: Session identifier for memory context
            context: Context including innovation constraints, risk tolerance
                   
        Returns:
            ReasoningWorkflow containing creative solutions and evaluations
            
        Raises:
            ReasoningException: When creative reasoning fails
            ValidationException: When inputs are invalid
        """
        return await self.execute_operation(
            "execute_creative_reasoning",
            self._execute_creative_workflow,
            problem_statement,
            session_id,
            context,
            validate_inputs=self._validate_reasoning_inputs
        )
    
    async def analyze_reasoning_patterns(
        self,
        session_id: str,
        time_window_hours: int = 24
    ) -> Dict[str, Any]:
        """Analyze reasoning patterns and effectiveness over time
        
        This method provides insights into reasoning effectiveness, pattern
        recognition, and areas for improvement based on historical data.
        
        Args:
            session_id: Session identifier for pattern analysis
            time_window_hours: Time window for pattern analysis
            
        Returns:
            Dictionary with pattern analysis and insights
            
        Raises:
            ReasoningException: When pattern analysis fails
        """
        return await self.execute_operation(
            "analyze_reasoning_patterns",
            self._analyze_patterns,
            session_id,
            time_window_hours,
            validate_inputs=self._validate_pattern_analysis_inputs
        )
    
    async def optimize_reasoning_approach(
        self,
        problem_type: str,
        historical_context: Dict[str, Any],
        user_profile: Optional[Dict[str, Any]] = None
    ) -> Dict[str, Any]:
        """Optimize reasoning approach based on problem type and context
        
        This method provides recommendations for optimal reasoning approaches
        based on problem characteristics, user capabilities, and historical
        effectiveness data.
        
        Args:
            problem_type: Type of problem (technical, operational, strategic)
            historical_context: Historical reasoning effectiveness data
            user_profile: Optional user profile for personalization
            
        Returns:
            Dictionary with optimized reasoning approach recommendations
            
        Raises:
            ReasoningException: When optimization fails
        """
        return await self.execute_operation(
            "optimize_reasoning_approach",
            self._optimize_approach,
            problem_type,
            historical_context,
            user_profile,
            validate_inputs=self._validate_optimization_inputs
        )
    
    # Implementation methods
    async def _execute_diagnostic_workflow(
        self,
        problem_statement: str,
        session_id: str,
        context: Dict[str, Any]
    ) -> ReasoningWorkflow:
        """Execute the diagnostic reasoning workflow"""
        import time
        
        start_time = time.time()
        
        try:
            # Log business event
            self.log_business_event(
                "diagnostic_reasoning_started",
                "info",
                {
                    "session_id": session_id,
                    "problem_length": len(problem_statement),
                    "context_keys": list(context.keys()),
                    "has_memory_service": self._memory_service is not None,
                    "has_planning_service": self._planning_service is not None
                }
            )
            
            # Execute diagnostic reasoning workflow
            workflow = await self._reasoning_engine.execute_enhanced_reasoning(
                problem_statement=problem_statement,
                session_id=session_id,
                context=context,
                workflow_type="diagnostic"
            )
            
            # Track performance metrics
            execution_time = (time.time() - start_time) * 1000  # Convert to ms
            self._performance_metrics["reasoning_workflows_executed"] += 1
            self._performance_metrics["diagnostic_workflows"] += 1
            self._update_avg_duration(execution_time)
            self._update_avg_confidence(workflow.overall_confidence)
            
            if workflow.memory_insights_used:
                self._performance_metrics["memory_integrations"] += 1
            
            if workflow.strategic_plan_applied:
                self._performance_metrics["planning_integrations"] += 1
            
            # Log performance metrics
            self.log_metric(
                "diagnostic_reasoning_duration",
                execution_time,
                "milliseconds",
                {
                    "session_id": session_id,
                    "workflow_id": workflow.workflow_id,
                    "confidence_score": workflow.overall_confidence,
                    "steps_executed": len(workflow.steps),
                    "memory_insights_count": len(workflow.memory_insights_used)
                }
            )
            
            # Log business event
            self.log_business_event(
                "diagnostic_reasoning_completed",
                "info",
                {
                    "session_id": session_id,
                    "workflow_id": workflow.workflow_id,
                    "execution_time_ms": execution_time,
                    "confidence_score": workflow.overall_confidence,
                    "steps_executed": len(workflow.steps),
                    "successful_steps": len([s for s in workflow.steps if s.outputs.get("success", True)]),
                    "memory_insights_used": len(workflow.memory_insights_used),
                    "strategic_plan_applied": workflow.strategic_plan_applied is not None,
                    "learning_outcomes": len(workflow.learning_outcomes)
                }
            )
            
            # Performance warning if target exceeded
            if execution_time > 5000:  # 5 second target
                self.logger.warning(
                    f"Diagnostic reasoning exceeded target time: {execution_time:.2f}ms for session: {session_id}"
                )
            
            return workflow
            
        except Exception as e:
            self._performance_metrics["reasoning_failures"] += 1
            self.logger.error(f"Diagnostic reasoning failed for session {session_id}: {e}")
            self.log_business_event(
                "diagnostic_reasoning_failed",
                "error",
                {
                    "session_id": session_id,
                    "error": str(e),
                    "execution_time_ms": (time.time() - start_time) * 1000
                }
            )
            raise
    
    async def _execute_analytical_workflow(
        self,
        problem_statement: str,
        session_id: str,
        context: Dict[str, Any]
    ) -> ReasoningWorkflow:
        """Execute the analytical reasoning workflow"""
        import time
        
        start_time = time.time()
        
        try:
            # Log business event
            self.log_business_event(
                "analytical_reasoning_started",
                "info",
                {
                    "session_id": session_id,
                    "problem_complexity": self._assess_problem_complexity(problem_statement),
                    "context_dimensions": len(context.keys())
                }
            )
            
            # Execute analytical reasoning workflow
            workflow = await self._reasoning_engine.execute_enhanced_reasoning(
                problem_statement=problem_statement,
                session_id=session_id,
                context=context,
                workflow_type="analytical"
            )
            
            # Track performance metrics
            execution_time = (time.time() - start_time) * 1000
            self._performance_metrics["analytical_workflows"] += 1
            self._update_performance_metrics(workflow, execution_time)
            
            # Log completion
            self.log_business_event(
                "analytical_reasoning_completed",
                "info",
                {
                    "session_id": session_id,
                    "workflow_id": workflow.workflow_id,
                    "execution_time_ms": execution_time,
                    "confidence_score": workflow.overall_confidence
                }
            )
            
            return workflow
            
        except Exception as e:
            self._performance_metrics["reasoning_failures"] += 1
            self.logger.error(f"Analytical reasoning failed for session {session_id}: {e}")
            raise
    
    async def _execute_strategic_workflow(
        self,
        problem_statement: str,
        session_id: str,
        context: Dict[str, Any]
    ) -> ReasoningWorkflow:
        """Execute the strategic reasoning workflow"""
        import time
        
        start_time = time.time()
        
        try:
            # Verify planning service availability for strategic reasoning
            if not self._planning_service:
                self.logger.warning(f"Strategic reasoning requested but planning service unavailable for session {session_id}")
                # Could fall back to analytical reasoning or raise exception
                workflow = await self._reasoning_engine.execute_enhanced_reasoning(
                    problem_statement=problem_statement,
                    session_id=session_id,
                    context=context,
                    workflow_type="analytical"  # Fallback to analytical
                )
            else:
                workflow = await self._reasoning_engine.execute_enhanced_reasoning(
                    problem_statement=problem_statement,
                    session_id=session_id,
                    context=context,
                    workflow_type="strategic"
                )
            
            # Track performance metrics
            execution_time = (time.time() - start_time) * 1000
            self._performance_metrics["strategic_workflows"] += 1
            self._update_performance_metrics(workflow, execution_time)
            
            return workflow
            
        except Exception as e:
            self._performance_metrics["reasoning_failures"] += 1
            self.logger.error(f"Strategic reasoning failed for session {session_id}: {e}")
            raise
    
    async def _execute_creative_workflow(
        self,
        problem_statement: str,
        session_id: str,
        context: Dict[str, Any]
    ) -> ReasoningWorkflow:
        """Execute the creative reasoning workflow"""
        import time
        
        start_time = time.time()
        
        try:
            # Execute creative reasoning workflow
            workflow = await self._reasoning_engine.execute_enhanced_reasoning(
                problem_statement=problem_statement,
                session_id=session_id,
                context=context,
                workflow_type="creative"
            )
            
            # Track performance metrics
            execution_time = (time.time() - start_time) * 1000
            self._performance_metrics["creative_workflows"] += 1
            self._update_performance_metrics(workflow, execution_time)
            
            return workflow
            
        except Exception as e:
            self._performance_metrics["reasoning_failures"] += 1
            self.logger.error(f"Creative reasoning failed for session {session_id}: {e}")
            raise
    
    async def _analyze_patterns(
        self,
        session_id: str,
        time_window_hours: int
    ) -> Dict[str, Any]:
        """Analyze reasoning patterns and effectiveness"""
        try:
            # This would typically analyze historical reasoning data
            # For now, return basic pattern analysis
            
            analysis = {
                "session_id": session_id,
                "time_window_hours": time_window_hours,
                "analysis_timestamp": datetime.utcnow().isoformat(),
                "pattern_summary": {
                    "most_effective_workflow": "diagnostic",
                    "avg_confidence_score": self._performance_metrics.get("avg_confidence_score", 0.7),
                    "workflow_preferences": {
                        "diagnostic": self._performance_metrics.get("diagnostic_workflows", 0),
                        "analytical": self._performance_metrics.get("analytical_workflows", 0),
                        "strategic": self._performance_metrics.get("strategic_workflows", 0),
                        "creative": self._performance_metrics.get("creative_workflows", 0)
                    }
                },
                "recommendations": [
                    "Continue using diagnostic workflows for troubleshooting",
                    "Consider strategic reasoning for complex organizational issues",
                    "Use creative workflows for innovative solution development"
                ]
            }
            
            return analysis
            
        except Exception as e:
            self.logger.error(f"Pattern analysis failed for session {session_id}: {e}")
            raise ReasoningException(f"Pattern analysis failed: {str(e)}")
    
    async def _optimize_approach(
        self,
        problem_type: str,
        historical_context: Dict[str, Any],
        user_profile: Optional[Dict[str, Any]]
    ) -> Dict[str, Any]:
        """Optimize reasoning approach based on context"""
        try:
            # Determine optimal workflow type based on problem type
            workflow_recommendations = {
                "technical": "diagnostic",
                "operational": "analytical", 
                "strategic": "strategic",
                "innovation": "creative"
            }
            
            recommended_workflow = workflow_recommendations.get(problem_type, "diagnostic")
            
            # Adjust based on user profile
            confidence_adjustment = 1.0
            if user_profile:
                skill_level = user_profile.get("skill_level", "intermediate")
                if skill_level == "advanced":
                    confidence_adjustment = 1.1
                elif skill_level == "beginner":
                    confidence_adjustment = 0.9
            
            optimization = {
                "problem_type": problem_type,
                "recommended_workflow": recommended_workflow,
                "confidence_adjustment": confidence_adjustment,
                "reasoning_parameters": {
                    "depth_level": "high" if problem_type in ["strategic", "innovation"] else "medium",
                    "memory_integration": True,
                    "planning_integration": problem_type in ["strategic", "operational"],
                    "creative_techniques": problem_type == "innovation"
                },
                "expected_benefits": [
                    f"Optimized for {problem_type} problem solving",
                    "Enhanced context awareness through memory integration",
                    "Improved solution quality through strategic planning"
                ]
            }
            
            return optimization
            
        except Exception as e:
            self.logger.error(f"Approach optimization failed for problem type {problem_type}: {e}")
            raise ReasoningException(f"Approach optimization failed: {str(e)}")
    
    # Validation methods
    async def _validate_reasoning_inputs(
        self,
        problem_statement: str,
        session_id: str,
        context: Dict[str, Any]
    ) -> None:
        """Validate inputs for reasoning workflows"""
        if not problem_statement or not problem_statement.strip():
            raise ValidationException("Problem statement cannot be empty")
        
        if len(problem_statement) > 5000:
            raise ValidationException("Problem statement too long (max 5000 characters)")
        
        if not session_id or not session_id.strip():
            raise ValidationException("Session ID cannot be empty")
        
        if not isinstance(context, dict):
            raise ValidationException("Context must be a dictionary")
    
    async def _validate_pattern_analysis_inputs(
        self,
        session_id: str,
        time_window_hours: int
    ) -> None:
        """Validate inputs for pattern analysis"""
        if not session_id or not session_id.strip():
            raise ValidationException("Session ID cannot be empty")
        
        if not isinstance(time_window_hours, int) or time_window_hours <= 0:
            raise ValidationException("Time window must be a positive integer")
        
        if time_window_hours > 720:  # 30 days max
            raise ValidationException("Time window too large (max 720 hours)")
    
    async def _validate_optimization_inputs(
        self,
        problem_type: str,
        historical_context: Dict[str, Any],
        user_profile: Optional[Dict[str, Any]]
    ) -> None:
        """Validate inputs for approach optimization"""
        valid_problem_types = {"technical", "operational", "strategic", "innovation"}
        if problem_type not in valid_problem_types:
            raise ValidationException(f"Invalid problem type. Must be one of: {valid_problem_types}")
        
        if not isinstance(historical_context, dict):
            raise ValidationException("Historical context must be a dictionary")
        
        if user_profile is not None and not isinstance(user_profile, dict):
            raise ValidationException("User profile must be a dictionary or None")
    
    # Helper methods
    def _update_performance_metrics(self, workflow: ReasoningWorkflow, execution_time: float) -> None:
        """Update performance metrics with workflow results"""
        self._performance_metrics["reasoning_workflows_executed"] += 1
        self._update_avg_duration(execution_time)
        self._update_avg_confidence(workflow.overall_confidence)
        
        if workflow.memory_insights_used:
            self._performance_metrics["memory_integrations"] += 1
        
        if workflow.strategic_plan_applied:
            self._performance_metrics["planning_integrations"] += 1
    
    def _update_avg_duration(self, new_duration: float) -> None:
        """Update average workflow duration"""
        current_avg = self._performance_metrics["avg_workflow_duration"]
        total_workflows = self._performance_metrics["reasoning_workflows_executed"]
        
        if total_workflows == 1:
            self._performance_metrics["avg_workflow_duration"] = new_duration
        else:
            self._performance_metrics["avg_workflow_duration"] = (
                (current_avg * (total_workflows - 1) + new_duration) / total_workflows
            )
    
    def _update_avg_confidence(self, new_confidence: float) -> None:
        """Update average confidence score"""
        current_avg = self._performance_metrics["avg_confidence_score"]
        total_workflows = self._performance_metrics["reasoning_workflows_executed"]
        
        if total_workflows == 1:
            self._performance_metrics["avg_confidence_score"] = new_confidence
        else:
            self._performance_metrics["avg_confidence_score"] = (
                (current_avg * (total_workflows - 1) + new_confidence) / total_workflows
            )
    
    def _assess_problem_complexity(self, problem_statement: str) -> str:
        """Assess problem complexity for metrics"""
        word_count = len(problem_statement.split())
        
        if word_count > 50:
            return "high"
        elif word_count > 20:
            return "medium"
        else:
            return "low"
    
    async def health_check(self) -> Dict[str, Any]:
        """Check health of reasoning service and components"""
        # Get base health from BaseService
        base_health = await super().health_check()
        
        # Get reasoning engine health
        engine_health = await self._reasoning_engine.health_check()
        
        # Check external dependencies
        dependencies = {
            "llm_provider": "unknown",
            "memory_service": "unknown",
            "planning_service": "unknown"
        }
        
        # Check LLM provider
        try:
            if self._llm_provider and hasattr(self._llm_provider, 'generate_response'):
                dependencies["llm_provider"] = "healthy"
            else:
                dependencies["llm_provider"] = "unavailable"
        except Exception:
            dependencies["llm_provider"] = "unhealthy"
        
        # Check memory service
        if self._memory_service:
            try:
                if hasattr(self._memory_service, 'retrieve_context'):
                    dependencies["memory_service"] = "healthy"
                else:
                    dependencies["memory_service"] = "unavailable"
            except Exception:
                dependencies["memory_service"] = "unhealthy"
        else:
            dependencies["memory_service"] = "unavailable"
        
        # Check planning service
        if self._planning_service:
            try:
                if hasattr(self._planning_service, 'plan_response_strategy'):
                    dependencies["planning_service"] = "healthy"
                else:
                    dependencies["planning_service"] = "unavailable"
            except Exception:
                dependencies["planning_service"] = "unhealthy"
        else:
            dependencies["planning_service"] = "unavailable"
        
        # Determine overall status
        critical_deps = ["llm_provider"]
        critical_issues = [dep for dep in critical_deps if "unhealthy" in dependencies.get(dep, "")]
        
        if critical_issues:
            overall_status = "unhealthy"
        elif any("unavailable" in status for status in dependencies.values()):
            overall_status = "degraded"
        else:
            overall_status = "healthy"
        
        # Combine health information
        health_info = {
            **base_health,
            "service": "reasoning_service",
            "status": overall_status,
            "reasoning_engine": engine_health,
            "dependencies": dependencies,
            "performance_metrics": self._performance_metrics.copy(),
            "capabilities": {
                "diagnostic_reasoning": True,
                "analytical_reasoning": True,
                "strategic_reasoning": self._planning_service is not None,
                "creative_reasoning": True,
                "memory_integration": self._memory_service is not None,
                "planning_integration": self._planning_service is not None,
                "pattern_analysis": True,
                "approach_optimization": True
            }
        }
        
        return health_info