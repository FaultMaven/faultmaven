"""Planning Service Implementation

This module provides the Planning Service that implements the IPlanningService
interface for strategic planning operations in the FaultMaven system.

The Planning Service acts as the primary gateway to the planning system,
coordinating with the Planning Engine to provide problem decomposition,
solution strategy development, and risk assessment for intelligent
troubleshooting workflows.
"""

import logging
from typing import Dict, Any, Optional, List

from faultmaven.services.base_service import BaseService
from faultmaven.models.interfaces import (
    IPlanningService, ILLMProvider, IMemoryService, 
    StrategicPlan, ProblemComponents
)
from faultmaven.core.planning.planning_engine import PlanningEngine
from faultmaven.exceptions import PlanningException, ValidationException


class PlanningService(BaseService, IPlanningService):
    """Planning Service implementing strategic planning operations
    
    This service provides the main interface for planning operations in FaultMaven,
    including strategic plan development, problem decomposition, and plan adaptation.
    It delegates core planning operations to the PlanningEngine while providing
    service-level concerns like validation, error handling, logging, and
    performance monitoring.
    
    Key Responsibilities:
    - Validate inputs and handle errors gracefully
    - Coordinate with PlanningEngine for core operations
    - Provide business-level logging and metrics
    - Ensure performance targets are met
    - Handle service lifecycle and health monitoring
    
    Performance Targets:
    - Plan generation: < 200ms
    - Problem decomposition: < 100ms
    - Plan adaptation: < 150ms
    
    Integration Points:
    - Works with AgentService for context-aware planning
    - Integrates with MemoryService for historical patterns
    - Uses LLM providers for advanced analysis and insights
    """
    
    def __init__(
        self,
        llm_provider: ILLMProvider,
        memory_service: Optional[IMemoryService] = None
    ):
        """Initialize Planning Service with interface dependencies
        
        Args:
            llm_provider: LLM interface for advanced planning and analysis
            memory_service: Optional memory service for context and learning
        """
        super().__init__()
        
        self._planning_engine = PlanningEngine(
            llm_provider=llm_provider,
            memory_service=memory_service
        )
        
        self._llm_provider = llm_provider
        self._memory_service = memory_service
        
        self._performance_metrics = {
            "strategies_planned": 0,
            "problems_decomposed": 0,
            "plans_adapted": 0,
            "avg_planning_time": 0.0,
            "planning_failures": 0,
            "cache_hits": 0
        }
    
    async def plan_response_strategy(self, query: str, context: Dict[str, Any]) -> StrategicPlan:
        """Plan strategic response approach for user query
        
        This method provides the primary interface for strategic response planning,
        analyzing the user query and available context to develop a comprehensive
        strategic plan for effective troubleshooting assistance.
        
        Args:
            query: User's troubleshooting query or problem description
            context: Available context including conversation history, user profile,
                    domain context, available tools, urgency, and environment
                   
        Returns:
            StrategicPlan containing problem analysis, solution strategy,
            risk assessment, success criteria, and execution guidance
            
        Raises:
            PlanningException: When strategic planning fails
            ValidationException: When query or context is invalid
        """
        return await self.execute_operation(
            "plan_response_strategy",
            self._execute_strategy_planning,
            query,
            context,
            validate_inputs=self._validate_strategy_planning_inputs
        )
    
    async def _execute_strategy_planning(self, query: str, context: Dict[str, Any]) -> StrategicPlan:
        """Execute the core strategy planning logic"""
        import time
        
        start_time = time.time()
        
        try:
            # Log business event
            self.log_business_event(
                "strategy_planning_started",
                "info",
                {
                    "query_length": len(query),
                    "context_keys": list(context.keys()),
                    "urgency": context.get("urgency", "medium"),
                    "environment": context.get("environment", "unknown")
                }
            )
            
            # Enhance context with additional metadata
            enhanced_context = self._enhance_planning_context(query, context)
            
            # Delegate to PlanningEngine
            strategic_plan = await self._planning_engine.create_troubleshooting_plan(
                query, enhanced_context
            )
            
            # Track performance metrics
            planning_time = (time.time() - start_time) * 1000  # Convert to ms
            self._performance_metrics["strategies_planned"] += 1
            self._update_avg_planning_time(planning_time)
            
            # Log performance metrics
            self.log_metric(
                "strategy_planning_time",
                planning_time,
                "milliseconds",
                {
                    "plan_id": strategic_plan.plan_id,
                    "confidence": strategic_plan.confidence_score,
                    "risk_level": strategic_plan.risk_assessment.get("overall_risk_level", "unknown")
                }
            )
            
            # Log business event
            self.log_business_event(
                "strategy_planning_completed",
                "info",
                {
                    "plan_id": strategic_plan.plan_id,
                    "planning_time_ms": planning_time,
                    "confidence_score": strategic_plan.confidence_score,
                    "approach": strategic_plan.solution_strategy.get("approach", "unknown"),
                    "estimated_effort": strategic_plan.estimated_effort,
                    "risk_level": strategic_plan.risk_assessment.get("overall_risk_level", "unknown"),
                    "success_criteria_count": len(strategic_plan.success_criteria)
                }
            )
            
            # Performance warning if target exceeded
            if planning_time > 200:  # 200ms target
                self.logger.warning(
                    f"Strategy planning exceeded target time: {planning_time:.2f}ms for query: {query[:50]}..."
                )
            
            return strategic_plan
            
        except Exception as e:
            self._performance_metrics["planning_failures"] += 1
            self.logger.error(f"Strategy planning failed for query: {query[:50]}... Error: {e}")
            self.log_business_event(
                "strategy_planning_failed",
                "error",
                {
                    "query": query[:100],
                    "error": str(e),
                    "planning_time_ms": (time.time() - start_time) * 1000
                }
            )
            raise
    
    async def _validate_strategy_planning_inputs(self, query: str, context: Dict[str, Any]) -> None:
        """Validate inputs for strategy planning"""
        if not query or not query.strip():
            raise ValidationException("Query cannot be empty")
        
        if len(query) > 10000:  # Reasonable query length limit
            raise ValidationException("Query too long (max 10000 characters)")
        
        if not isinstance(context, dict):
            raise ValidationException("Context must be a dictionary")
        
        # Validate critical context fields if present
        if "urgency" in context:
            valid_urgencies = {"low", "medium", "high", "critical"}
            if context["urgency"] not in valid_urgencies:
                raise ValidationException(f"Invalid urgency level. Must be one of: {valid_urgencies}")
        
        if "environment" in context:
            valid_environments = {"development", "staging", "production", "test"}
            if context["environment"] not in valid_environments:
                raise ValidationException(f"Invalid environment. Must be one of: {valid_environments}")
    
    def _enhance_planning_context(self, query: str, context: Dict[str, Any]) -> Dict[str, Any]:
        """Enhance planning context with additional metadata and analysis"""
        enhanced_context = context.copy()
        
        # Add query analysis
        enhanced_context["problem_summary"] = query
        enhanced_context["query_analysis"] = {
            "length": len(query),
            "complexity_indicators": self._analyze_query_complexity(query),
            "domain_indicators": self._analyze_query_domain(query)
        }
        
        # Add default values for missing critical fields
        if "urgency" not in enhanced_context:
            enhanced_context["urgency"] = "medium"
        
        if "environment" not in enhanced_context:
            enhanced_context["environment"] = "unknown"
        
        if "available_time" not in enhanced_context:
            enhanced_context["available_time"] = "moderate"
        
        if "team_size" not in enhanced_context:
            enhanced_context["team_size"] = 1
        
        # Add planning metadata
        enhanced_context["planning_metadata"] = {
            "service_version": "1.0.0",
            "planning_timestamp": self._get_current_timestamp(),
            "context_enhancement_applied": True
        }
        
        return enhanced_context
    
    def _analyze_query_complexity(self, query: str) -> List[str]:
        """Analyze query for complexity indicators"""
        complexity_indicators = []
        query_lower = query.lower()
        
        # Length-based complexity
        if len(query) > 500:
            complexity_indicators.append("long_description")
        
        # Technical complexity indicators
        if any(term in query_lower for term in ["multiple", "several", "many", "various"]):
            complexity_indicators.append("multiple_components")
        
        if any(term in query_lower for term in ["distributed", "microservice", "cluster", "federation"]):
            complexity_indicators.append("distributed_system")
        
        if any(term in query_lower for term in ["intermittent", "sporadic", "sometimes", "occasionally"]):
            complexity_indicators.append("intermittent_issue")
        
        if any(term in query_lower for term in ["legacy", "old", "deprecated", "end-of-life"]):
            complexity_indicators.append("legacy_system")
        
        if any(term in query_lower for term in ["compliance", "regulation", "audit", "security"]):
            complexity_indicators.append("compliance_sensitive")
        
        return complexity_indicators
    
    def _analyze_query_domain(self, query: str) -> List[str]:
        """Analyze query for domain indicators"""
        domain_indicators = []
        query_lower = query.lower()
        
        # Domain keyword mapping
        domain_keywords = {
            "database": ["database", "sql", "query", "table", "index", "postgres", "mysql", "mongodb"],
            "network": ["network", "connectivity", "dns", "firewall", "routing", "tcp", "udp", "ssl"],
            "application": ["application", "app", "service", "api", "endpoint", "microservice"],
            "infrastructure": ["server", "hardware", "vm", "container", "kubernetes", "docker"],
            "security": ["security", "auth", "authentication", "authorization", "certificate", "encryption"],
            "performance": ["slow", "performance", "latency", "timeout", "bottleneck", "cpu", "memory"]
        }
        
        for domain, keywords in domain_keywords.items():
            if any(keyword in query_lower for keyword in keywords):
                domain_indicators.append(domain)
        
        return domain_indicators
    
    def _get_current_timestamp(self) -> str:
        """Get current timestamp in ISO format"""
        from datetime import datetime
        return datetime.utcnow().isoformat() + 'Z'
    
    async def decompose_problem(self, problem: str, context: Dict[str, Any]) -> ProblemComponents:
        """Decompose complex problem into manageable components
        
        This method breaks down complex troubleshooting problems into
        smaller, manageable components with clear dependencies and
        priority rankings to enable systematic problem resolution.
        
        Args:
            problem: Complex problem description requiring decomposition
            context: Problem context including system information, error symptoms,
                    environment details, and previous troubleshooting attempts
                   
        Returns:
            ProblemComponents containing primary issue, contributing factors,
            dependencies, complexity assessment, and priority ranking
            
        Raises:
            PlanningException: When problem decomposition fails
            ValidationException: When problem description is insufficient
        """
        return await self.execute_operation(
            "decompose_problem",
            self._execute_problem_decomposition,
            problem,
            context,
            validate_inputs=self._validate_problem_decomposition_inputs
        )
    
    async def _execute_problem_decomposition(self, problem: str, context: Dict[str, Any]) -> ProblemComponents:
        """Execute the core problem decomposition logic"""
        import time
        
        start_time = time.time()
        
        try:
            # Log business event
            self.log_business_event(
                "problem_decomposition_started",
                "info",
                {
                    "problem_length": len(problem),
                    "context_keys": list(context.keys()),
                    "has_system_info": "system_info" in context,
                    "has_error_patterns": "error_patterns" in context
                }
            )
            
            # Use planning engine's problem decomposer
            problem_components = await self._planning_engine._problem_decomposer.decompose(problem, context)
            
            # Track performance metrics
            decomposition_time = (time.time() - start_time) * 1000  # Convert to ms
            self._performance_metrics["problems_decomposed"] += 1
            
            # Log performance metrics
            self.log_metric(
                "problem_decomposition_time",
                decomposition_time,
                "milliseconds",
                {
                    "problem_summary": problem[:50],
                    "components_count": len(problem_components.contributing_factors),
                    "complexity_level": problem_components.complexity_assessment.get("level", "unknown")
                }
            )
            
            # Log business event
            self.log_business_event(
                "problem_decomposition_completed",
                "info",
                {
                    "decomposition_time_ms": decomposition_time,
                    "primary_issue": problem_components.primary_issue,
                    "contributing_factors_count": len(problem_components.contributing_factors),
                    "dependencies_count": len(problem_components.dependencies),
                    "complexity_level": problem_components.complexity_assessment.get("level", "unknown"),
                    "priority_ranking_count": len(problem_components.priority_ranking)
                }
            )
            
            return problem_components
            
        except Exception as e:
            self.logger.error(f"Problem decomposition failed for: {problem[:50]}... Error: {e}")
            self.log_business_event(
                "problem_decomposition_failed",
                "error",
                {
                    "problem": problem[:100],
                    "error": str(e),
                    "decomposition_time_ms": (time.time() - start_time) * 1000
                }
            )
            raise
    
    async def _validate_problem_decomposition_inputs(self, problem: str, context: Dict[str, Any]) -> None:
        """Validate inputs for problem decomposition"""
        if not problem or not problem.strip():
            raise ValidationException("Problem description cannot be empty")
        
        if len(problem) < 10:
            raise ValidationException("Problem description too short (minimum 10 characters)")
        
        if len(problem) > 5000:
            raise ValidationException("Problem description too long (max 5000 characters)")
        
        if not isinstance(context, dict):
            raise ValidationException("Context must be a dictionary")
    
    async def adapt_plan(
        self, 
        plan_id: str, 
        adaptation_context: Dict[str, Any]
    ) -> Optional[StrategicPlan]:
        """Adapt existing strategic plan based on new context or feedback
        
        This method provides plan adaptation capabilities to modify existing
        strategic plans based on new information, changed circumstances, or
        execution feedback to maintain plan relevance and effectiveness.
        
        Args:
            plan_id: ID of the strategic plan to adapt
            adaptation_context: New context or feedback requiring plan adaptation
                               including execution progress, changed requirements,
                               new constraints, or effectiveness feedback
                   
        Returns:
            Adapted StrategicPlan if successful, None if plan not found
            
        Raises:
            PlanningException: When plan adaptation fails
            ValidationException: When plan_id or adaptation_context is invalid
        """
        return await self.execute_operation(
            "adapt_plan",
            self._execute_plan_adaptation,
            plan_id,
            adaptation_context,
            validate_inputs=self._validate_plan_adaptation_inputs
        )
    
    async def _execute_plan_adaptation(
        self, 
        plan_id: str, 
        adaptation_context: Dict[str, Any]
    ) -> Optional[StrategicPlan]:
        """Execute the core plan adaptation logic"""
        import time
        
        start_time = time.time()
        
        try:
            # Log business event
            self.log_business_event(
                "plan_adaptation_started",
                "info",
                {
                    "plan_id": plan_id,
                    "adaptation_context_keys": list(adaptation_context.keys()),
                    "adaptation_triggers": [key for key in adaptation_context.keys() 
                                          if key.endswith("_changed") or key.endswith("_updated")]
                }
            )
            
            # Delegate to PlanningEngine
            adapted_plan = await self._planning_engine.adapt_plan(plan_id, adaptation_context)
            
            # Track performance metrics
            adaptation_time = (time.time() - start_time) * 1000  # Convert to ms
            
            if adapted_plan:
                self._performance_metrics["plans_adapted"] += 1
                
                # Log performance metrics
                self.log_metric(
                    "plan_adaptation_time",
                    adaptation_time,
                    "milliseconds",
                    {
                        "plan_id": plan_id,
                        "adaptation_successful": True,
                        "new_confidence": adapted_plan.confidence_score
                    }
                )
                
                # Log business event
                self.log_business_event(
                    "plan_adaptation_completed",
                    "info",
                    {
                        "plan_id": plan_id,
                        "adaptation_time_ms": adaptation_time,
                        "adaptation_successful": True,
                        "original_confidence": adaptation_context.get("original_confidence"),
                        "new_confidence": adapted_plan.confidence_score,
                        "approach_changed": adaptation_context.get("approach_changed", False)
                    }
                )
            else:
                # Plan not found
                self.log_business_event(
                    "plan_adaptation_failed",
                    "warning",
                    {
                        "plan_id": plan_id,
                        "adaptation_time_ms": adaptation_time,
                        "reason": "plan_not_found"
                    }
                )
            
            return adapted_plan
            
        except Exception as e:
            self.logger.error(f"Plan adaptation failed for plan {plan_id}: {e}")
            self.log_business_event(
                "plan_adaptation_error",
                "error",
                {
                    "plan_id": plan_id,
                    "error": str(e),
                    "adaptation_time_ms": (time.time() - start_time) * 1000
                }
            )
            raise
    
    async def _validate_plan_adaptation_inputs(
        self, 
        plan_id: str, 
        adaptation_context: Dict[str, Any]
    ) -> None:
        """Validate inputs for plan adaptation"""
        if not plan_id or not plan_id.strip():
            raise ValidationException("Plan ID cannot be empty")
        
        if not isinstance(adaptation_context, dict):
            raise ValidationException("Adaptation context must be a dictionary")
        
        if not adaptation_context:
            raise ValidationException("Adaptation context cannot be empty")
    
    def _update_avg_planning_time(self, new_time: float) -> None:
        """Update average planning time metric"""
        current_avg = self._performance_metrics["avg_planning_time"]
        total_plans = self._performance_metrics["strategies_planned"]
        
        if total_plans == 1:
            self._performance_metrics["avg_planning_time"] = new_time
        else:
            # Calculate running average
            self._performance_metrics["avg_planning_time"] = (
                (current_avg * (total_plans - 1) + new_time) / total_plans
            )
    
    async def get_plan_status(self, plan_id: str) -> Optional[Dict[str, Any]]:
        """Get status and metrics for a strategic plan
        
        Args:
            plan_id: ID of the plan to get status for
            
        Returns:
            Dictionary with plan status and metrics, None if not found
        """
        try:
            return await self._planning_engine.get_plan_status(plan_id)
        except Exception as e:
            self.logger.error(f"Failed to get plan status for {plan_id}: {e}")
            return None
    
    async def health_check(self) -> Dict[str, Any]:
        """Check health of planning service and underlying components
        
        Returns:
            Dictionary with health status, component details, and performance metrics
        """
        # Get base health from BaseService
        base_health = await super().health_check()
        
        # Get PlanningEngine health
        planning_health = await self._planning_engine.health_check()
        
        # Check external dependencies
        dependencies = {
            "llm_provider": "unknown",
            "memory_service": "unknown"
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
        
        # Determine overall status
        planning_status = planning_health.get("status", "unknown")
        dependency_issues = [dep for status in dependencies.values() 
                           for dep in [status] if "unhealthy" in str(status)]
        
        if dependency_issues or planning_status == "degraded":
            overall_status = "degraded"
        elif planning_status == "healthy":
            overall_status = "healthy"
        else:
            overall_status = "unknown"
        
        # Combine health information
        health_info = {
            **base_health,
            "service": "planning_service",
            "status": overall_status,
            "planning_engine": planning_health,
            "dependencies": dependencies,
            "performance_metrics": self._performance_metrics.copy(),
            "capabilities": {
                "strategy_planning": True,
                "problem_decomposition": True,
                "plan_adaptation": True,
                "risk_assessment": True,
                "memory_integration": self._memory_service is not None,
                "advanced_analysis": self._llm_provider is not None
            }
        }
        
        return health_info