"""Planning Engine Implementation

This module implements the core Planning Engine that orchestrates the strategic
planning system for intelligent troubleshooting workflows in FaultMaven.

The Planning Engine coordinates between:
- Problem Decomposer (breaking down complex problems)
- Strategy Planner (developing solution approaches)
- Risk Assessor (evaluating risks and mitigation strategies)

It provides comprehensive planning capabilities that enhance troubleshooting
effectiveness through intelligent problem analysis and strategic planning.
"""

import uuid
import time
import logging
from typing import Dict, List, Any, Optional
from datetime import datetime

from faultmaven.models.interfaces import (
    ILLMProvider, IMemoryService, StrategicPlan, ProblemComponents
)
from faultmaven.core.planning.problem_decomposer import ProblemDecomposer
from faultmaven.core.planning.strategy_planner import StrategyPlanner
from faultmaven.core.planning.risk_assessor import RiskAssessor
from faultmaven.exceptions import PlanningException


class PlanningEngine:
    """Core Planning Engine for intelligent troubleshooting strategy development
    
    This class orchestrates the strategic planning system to provide comprehensive
    problem analysis, solution strategy development, and risk assessment for
    troubleshooting scenarios. It integrates multiple planning components to
    deliver intelligent, context-aware planning capabilities.
    
    Key Responsibilities:
    - Coordinate problem decomposition and analysis
    - Develop comprehensive solution strategies
    - Assess risks and generate mitigation plans
    - Integrate memory and learning for plan optimization
    - Provide plan adaptation and real-time adjustments
    
    Performance Targets:
    - Complete planning cycle: < 200ms
    - Problem decomposition: < 100ms
    - Strategy development: < 150ms
    - Risk assessment: < 100ms
    
    Integration Points:
    - Memory service for context and historical patterns
    - LLM provider for advanced analysis and insights
    - Agent service for plan execution coordination
    """
    
    def __init__(
        self,
        llm_provider: ILLMProvider,
        memory_service: Optional[IMemoryService] = None
    ):
        """Initialize Planning Engine with interface dependencies
        
        Args:
            llm_provider: LLM interface for advanced analysis and planning
            memory_service: Optional memory service for context and learning
        """
        self._llm = llm_provider
        self._memory = memory_service
        self._logger = logging.getLogger(__name__)
        
        # Initialize planning components
        self._problem_decomposer = ProblemDecomposer(llm_provider)
        self._strategy_planner = StrategyPlanner(llm_provider, memory_service)
        self._risk_assessor = RiskAssessor(llm_provider, memory_service)
        
        # Planning metrics and cache
        self._planning_metrics = {
            "plans_generated": 0,
            "avg_planning_time": 0.0,
            "successful_plans": 0,
            "plan_adaptations": 0
        }
        self._plan_cache: Dict[str, Dict[str, Any]] = {}
    
    async def create_troubleshooting_plan(
        self, 
        problem: str, 
        context: Dict[str, Any]
    ) -> StrategicPlan:
        """Create comprehensive troubleshooting plan
        
        This method provides the main interface for strategic plan creation,
        coordinating problem analysis, strategy development, and risk assessment
        to produce a comprehensive troubleshooting plan.
        
        Args:
            problem: Problem description requiring strategic planning
            context: Planning context including user profile, environment,
                    urgency, available resources, and domain information
                   
        Returns:
            StrategicPlan containing problem analysis, solution strategy,
            risk assessment, success criteria, and execution guidance
            
        Raises:
            PlanningException: When strategic planning fails
        """
        try:
            planning_start = time.time()
            plan_id = str(uuid.uuid4())
            
            self._logger.info(f"Starting strategic planning for problem: {problem[:100]}...")
            
            # Phase 1: Enhanced context retrieval with memory
            enhanced_context = await self._enhance_context_with_memory(context)
            
            # Phase 2: Problem decomposition and analysis
            problem_components = await self._problem_decomposer.decompose(problem, enhanced_context)
            
            # Phase 3: Solution strategy development
            solution_strategy = await self._strategy_planner.develop_strategy(
                self._components_to_dict(problem_components), enhanced_context
            )
            
            # Phase 4: Risk assessment and mitigation planning
            risk_assessment = await self._risk_assessor.assess_risks(solution_strategy, enhanced_context)
            
            # Phase 5: Plan integration and optimization
            strategic_plan = await self._integrate_plan_components(
                plan_id, problem, problem_components, solution_strategy, 
                risk_assessment, enhanced_context
            )
            
            # Phase 6: Plan validation and confidence scoring
            validated_plan = await self._validate_and_score_plan(strategic_plan, enhanced_context)
            
            # Phase 7: Cache plan for future reference and adaptation
            await self._cache_plan(plan_id, validated_plan, enhanced_context)
            
            # Update metrics
            planning_time = (time.time() - planning_start) * 1000  # Convert to ms
            self._update_planning_metrics(planning_time)
            
            self._logger.info(
                f"Strategic planning completed in {planning_time:.2f}ms for plan {plan_id[:8]}"
            )
            
            return validated_plan
            
        except Exception as e:
            self._logger.error(f"Strategic planning failed: {e}")
            raise PlanningException(f"Failed to create troubleshooting plan: {str(e)}")
    
    async def _enhance_context_with_memory(self, context: Dict[str, Any]) -> Dict[str, Any]:
        """Enhance planning context with memory and historical patterns"""
        enhanced_context = context.copy()
        
        if self._memory:
            try:
                # Get session ID for memory context
                session_id = context.get("session_id", "default_session")
                
                # Retrieve conversation context
                memory_context = await self._memory.retrieve_context(
                    session_id, 
                    context.get("problem_summary", "")
                )
                
                # Add memory insights to context
                enhanced_context["memory_insights"] = memory_context.relevant_insights
                enhanced_context["user_profile"] = memory_context.user_profile
                enhanced_context["conversation_history"] = memory_context.conversation_history
                enhanced_context["domain_context"] = memory_context.domain_context
                
                self._logger.info(f"Enhanced context with {len(memory_context.relevant_insights)} memory insights")
                
            except Exception as e:
                self._logger.warning(f"Failed to enhance context with memory: {e}")
        
        return enhanced_context
    
    def _components_to_dict(self, components: ProblemComponents) -> Dict[str, Any]:
        """Convert ProblemComponents to dictionary for strategy planning"""
        return {
            "primary_issue": components.primary_issue,
            "contributing_factors": components.contributing_factors,
            "dependencies": components.dependencies,
            "complexity": components.complexity_assessment,
            "priority_ranking": components.priority_ranking
        }
    
    async def _integrate_plan_components(
        self,
        plan_id: str,
        problem: str,
        problem_components: ProblemComponents,
        solution_strategy: Dict[str, Any],
        risk_assessment: Dict[str, Any],
        context: Dict[str, Any]
    ) -> StrategicPlan:
        """Integrate all planning components into comprehensive strategic plan"""
        
        # Extract success criteria from strategy and problem analysis
        success_criteria = solution_strategy.get("success_criteria", [])
        
        # Add problem-specific success criteria
        success_criteria.extend([
            f"Primary issue resolved: {problem_components.primary_issue}",
            "All contributing factors addressed or mitigated"
        ])
        
        # Estimate effort based on complexity and strategy
        complexity_level = problem_components.complexity_assessment.get("level", "medium")
        strategy_timeline = solution_strategy.get("timeline", "1-2 hours")
        
        if complexity_level == "high":
            estimated_effort = "4-8 hours"
        elif complexity_level == "low":
            estimated_effort = "30 minutes - 1 hour"
        else:
            estimated_effort = strategy_timeline
        
        # Calculate overall confidence
        strategy_confidence = solution_strategy.get("confidence", 0.7)
        risk_impact = 1.0 - (risk_assessment.get("overall_risk_level", "medium") == "high") * 0.2
        confidence_score = min(strategy_confidence * risk_impact, 1.0)
        
        # Create comprehensive strategic plan
        strategic_plan = StrategicPlan(
            plan_id=plan_id,
            problem_analysis={
                "original_problem": problem,
                "primary_issue": problem_components.primary_issue,
                "contributing_factors": problem_components.contributing_factors,
                "dependencies": problem_components.dependencies,
                "complexity_assessment": problem_components.complexity_assessment,
                "priority_ranking": problem_components.priority_ranking
            },
            solution_strategy={
                "approach": solution_strategy.get("primary_approach", "systematic_analysis"),
                "methodology": solution_strategy.get("methodology", []),
                "timeline": solution_strategy.get("timeline", estimated_effort),
                "resource_requirements": solution_strategy.get("resource_requirements", {}),
                "alternatives": solution_strategy.get("alternatives", []),
                "adaptation_triggers": solution_strategy.get("adaptation_triggers", []),
                "monitoring_points": solution_strategy.get("monitoring_points", [])
            },
            risk_assessment={
                "overall_risk_level": risk_assessment.get("overall_risk_level", "medium"),
                "identified_risks": risk_assessment.get("identified_risks", []),
                "high_priority_risks": risk_assessment.get("high_priority_risks", []),
                "mitigation_strategies": risk_assessment.get("mitigation_strategies", {}),
                "monitoring_plan": risk_assessment.get("monitoring_plan", {}),
                "contingency_triggers": risk_assessment.get("contingency_triggers", []),
                "approval_recommendations": risk_assessment.get("approval_recommendations", [])
            },
            success_criteria=success_criteria[:5],  # Limit to top 5 criteria
            estimated_effort=estimated_effort,
            confidence_score=confidence_score
        )
        
        return strategic_plan
    
    async def _validate_and_score_plan(
        self, 
        plan: StrategicPlan, 
        context: Dict[str, Any]
    ) -> StrategicPlan:
        """Validate strategic plan and adjust confidence scoring"""
        
        validation_adjustments = 0.0
        
        # Validate problem-solution alignment
        problem_complexity = plan.problem_analysis.get("complexity_assessment", {}).get("level", "medium")
        solution_approach = plan.solution_strategy.get("approach", "")
        
        # Check for good alignment between complexity and approach
        if problem_complexity == "high" and solution_approach in ["systematic_analysis", "collaborative_resolution"]:
            validation_adjustments += 0.1
        elif problem_complexity == "low" and solution_approach == "rapid_troubleshooting":
            validation_adjustments += 0.1
        elif problem_complexity == "medium" and solution_approach in ["systematic_analysis", "rapid_troubleshooting"]:
            validation_adjustments += 0.05
        
        # Validate resource-timeline alignment
        estimated_effort = plan.estimated_effort
        available_time = context.get("available_time", "moderate")
        
        if available_time == "limited" and "30 minutes" in estimated_effort:
            validation_adjustments += 0.05
        elif available_time == "flexible" and "hours" in estimated_effort:
            validation_adjustments += 0.05
        
        # Validate risk-mitigation alignment
        high_risks = len(plan.risk_assessment.get("high_priority_risks", []))
        mitigation_strategies = plan.risk_assessment.get("mitigation_strategies", {})
        
        if high_risks > 0 and mitigation_strategies.get("immediate_actions", []):
            validation_adjustments += 0.05
        
        # Validate user skill-approach alignment
        user_skill = context.get("user_profile", {}).get("skill_level", "intermediate")
        methodology_complexity = len(plan.solution_strategy.get("methodology", []))
        
        if user_skill == "advanced" and methodology_complexity >= 6:
            validation_adjustments += 0.05
        elif user_skill == "beginner" and methodology_complexity <= 4:
            validation_adjustments += 0.05
        
        # Apply validation adjustments
        adjusted_confidence = min(plan.confidence_score + validation_adjustments, 1.0)
        
        # Create validated plan with adjusted confidence
        validated_plan = StrategicPlan(
            plan_id=plan.plan_id,
            problem_analysis=plan.problem_analysis,
            solution_strategy=plan.solution_strategy,
            risk_assessment=plan.risk_assessment,
            success_criteria=plan.success_criteria,
            estimated_effort=plan.estimated_effort,
            confidence_score=adjusted_confidence
        )
        
        return validated_plan
    
    async def _cache_plan(
        self, 
        plan_id: str, 
        plan: StrategicPlan, 
        context: Dict[str, Any]
    ) -> None:
        """Cache strategic plan for future reference and adaptation"""
        
        cache_entry = {
            "plan": plan,
            "context": context,
            "created_at": datetime.utcnow().isoformat() + 'Z',
            "access_count": 0,
            "adaptation_count": 0
        }
        
        self._plan_cache[plan_id] = cache_entry
        
        # Limit cache size (keep most recent 100 plans)
        if len(self._plan_cache) > 100:
            # Remove oldest entries
            oldest_entries = sorted(
                self._plan_cache.items(),
                key=lambda x: x[1]["created_at"]
            )[:len(self._plan_cache) - 100]
            
            for old_plan_id, _ in oldest_entries:
                del self._plan_cache[old_plan_id]
    
    def _update_planning_metrics(self, planning_time: float) -> None:
        """Update planning performance metrics"""
        self._planning_metrics["plans_generated"] += 1
        
        # Update average planning time
        current_avg = self._planning_metrics["avg_planning_time"]
        total_plans = self._planning_metrics["plans_generated"]
        
        if total_plans == 1:
            self._planning_metrics["avg_planning_time"] = planning_time
        else:
            self._planning_metrics["avg_planning_time"] = (
                (current_avg * (total_plans - 1) + planning_time) / total_plans
            )
    
    async def adapt_plan(
        self, 
        plan_id: str, 
        adaptation_context: Dict[str, Any]
    ) -> Optional[StrategicPlan]:
        """Adapt existing plan based on new context or feedback
        
        Args:
            plan_id: ID of the plan to adapt
            adaptation_context: New context or feedback requiring plan adaptation
            
        Returns:
            Adapted StrategicPlan if successful, None if plan not found
            
        Raises:
            PlanningException: When plan adaptation fails
        """
        try:
            if plan_id not in self._plan_cache:
                self._logger.warning(f"Plan {plan_id} not found in cache for adaptation")
                return None
            
            cached_entry = self._plan_cache[plan_id]
            original_plan = cached_entry["plan"]
            original_context = cached_entry["context"]
            
            self._logger.info(f"Adapting plan {plan_id[:8]} based on new context")
            
            # Merge original context with adaptation context
            merged_context = {**original_context, **adaptation_context}
            
            # Check if significant adaptation is needed
            adaptation_needed = self._assess_adaptation_need(
                original_plan, adaptation_context
            )
            
            if not adaptation_needed:
                self._logger.info(f"No significant adaptation needed for plan {plan_id[:8]}")
                cached_entry["access_count"] += 1
                return original_plan
            
            # Perform plan adaptation
            adapted_plan = await self._perform_plan_adaptation(
                original_plan, merged_context, adaptation_context
            )
            
            # Update cache with adapted plan
            cached_entry["plan"] = adapted_plan
            cached_entry["adaptation_count"] += 1
            cached_entry["access_count"] += 1
            
            self._planning_metrics["plan_adaptations"] += 1
            
            self._logger.info(f"Plan {plan_id[:8]} successfully adapted")
            return adapted_plan
            
        except Exception as e:
            self._logger.error(f"Plan adaptation failed for {plan_id}: {e}")
            raise PlanningException(f"Failed to adapt plan: {str(e)}")
    
    def _assess_adaptation_need(
        self, 
        original_plan: StrategicPlan, 
        adaptation_context: Dict[str, Any]
    ) -> bool:
        """Assess whether significant plan adaptation is needed"""
        
        # Check for significant context changes
        significant_changes = [
            "urgency_increased",
            "resources_changed", 
            "new_information",
            "strategy_ineffective",
            "risk_materialized",
            "timeline_pressure"
        ]
        
        for change in significant_changes:
            if adaptation_context.get(change, False):
                return True
        
        # Check for risk level changes
        if adaptation_context.get("new_risk_level") != original_plan.risk_assessment.get("overall_risk_level"):
            return True
        
        # Check for approach effectiveness feedback
        if adaptation_context.get("approach_effectiveness", 1.0) < 0.5:
            return True
        
        return False
    
    async def _perform_plan_adaptation(
        self,
        original_plan: StrategicPlan,
        merged_context: Dict[str, Any],
        adaptation_context: Dict[str, Any]
    ) -> StrategicPlan:
        """Perform actual plan adaptation based on context changes"""
        
        # Start with original plan structure
        adapted_analysis = original_plan.problem_analysis.copy()
        adapted_strategy = original_plan.solution_strategy.copy()
        adapted_risk_assessment = original_plan.risk_assessment.copy()
        
        # Adapt based on specific changes
        if adaptation_context.get("urgency_increased"):
            # Switch to more urgent approach if needed
            current_approach = adapted_strategy.get("approach", "")
            if current_approach != "rapid_troubleshooting":
                adapted_strategy["approach"] = "rapid_troubleshooting"
                adapted_strategy["methodology"] = [
                    "Quick symptom assessment",
                    "Apply common fixes for known patterns", 
                    "Test immediate resolution",
                    "Escalate if not resolved quickly"
                ]
                adapted_strategy["timeline"] = "15-30 minutes"
        
        if adaptation_context.get("strategy_ineffective"):
            # Switch to alternative approach
            alternatives = adapted_strategy.get("alternatives", [])
            if alternatives:
                best_alternative = alternatives[0]  # Take first alternative
                adapted_strategy["approach"] = best_alternative.get("approach", "systematic_analysis")
                adapted_strategy["methodology"] = best_alternative.get("methodology", [])
        
        if adaptation_context.get("new_risk_level"):
            # Re-assess risks with new information
            new_risk_level = adaptation_context["new_risk_level"]
            adapted_risk_assessment["overall_risk_level"] = new_risk_level
            
            if new_risk_level in ["high", "critical"]:
                # Add more conservative mitigation strategies
                immediate_actions = adapted_risk_assessment.get("mitigation_strategies", {}).get("immediate_actions", [])
                immediate_actions.extend([
                    "Implement additional monitoring and alerting",
                    "Prepare immediate rollback procedures",
                    "Notify additional stakeholders of risk elevation"
                ])
                adapted_risk_assessment["mitigation_strategies"]["immediate_actions"] = immediate_actions
        
        # Recalculate confidence based on adaptations
        adaptation_confidence_penalty = 0.1  # Small penalty for needing adaptation
        new_confidence = max(original_plan.confidence_score - adaptation_confidence_penalty, 0.3)
        
        # Create adapted plan
        adapted_plan = StrategicPlan(
            plan_id=original_plan.plan_id,
            problem_analysis=adapted_analysis,
            solution_strategy=adapted_strategy,
            risk_assessment=adapted_risk_assessment,
            success_criteria=original_plan.success_criteria,
            estimated_effort=adapted_strategy.get("timeline", original_plan.estimated_effort),
            confidence_score=new_confidence
        )
        
        return adapted_plan
    
    async def get_plan_status(self, plan_id: str) -> Optional[Dict[str, Any]]:
        """Get status and metrics for a cached plan
        
        Args:
            plan_id: ID of the plan to get status for
            
        Returns:
            Dictionary with plan status and metrics, None if not found
        """
        if plan_id not in self._plan_cache:
            return None
        
        cached_entry = self._plan_cache[plan_id]
        plan = cached_entry["plan"]
        
        return {
            "plan_id": plan_id,
            "confidence_score": plan.confidence_score,
            "overall_risk_level": plan.risk_assessment.get("overall_risk_level", "unknown"),
            "estimated_effort": plan.estimated_effort,
            "created_at": cached_entry["created_at"],
            "access_count": cached_entry["access_count"],
            "adaptation_count": cached_entry["adaptation_count"],
            "success_criteria_count": len(plan.success_criteria),
            "methodology_steps": len(plan.solution_strategy.get("methodology", []))
        }
    
    async def health_check(self) -> Dict[str, Any]:
        """Check health of planning engine and components"""
        
        health = {
            "status": "healthy",
            "components": {
                "problem_decomposer": "healthy",
                "strategy_planner": "healthy",
                "risk_assessor": "healthy"
            },
            "performance_metrics": self._planning_metrics.copy(),
            "cache_metrics": {
                "cached_plans": len(self._plan_cache),
                "total_adaptations": sum(entry["adaptation_count"] for entry in self._plan_cache.values()),
                "total_accesses": sum(entry["access_count"] for entry in self._plan_cache.values())
            },
            "dependencies": {
                "llm_provider": "unknown",
                "memory_service": "unknown"
            }
        }
        
        # Check LLM provider
        try:
            if self._llm and hasattr(self._llm, 'generate_response'):
                health["dependencies"]["llm_provider"] = "healthy"
            else:
                health["dependencies"]["llm_provider"] = "unavailable"
                health["status"] = "degraded"
        except Exception:
            health["dependencies"]["llm_provider"] = "unhealthy"
            health["status"] = "degraded"
        
        # Check memory service
        if self._memory:
            try:
                if hasattr(self._memory, 'retrieve_context'):
                    health["dependencies"]["memory_service"] = "healthy"
                else:
                    health["dependencies"]["memory_service"] = "unavailable"
            except Exception:
                health["dependencies"]["memory_service"] = "unhealthy"
        else:
            health["dependencies"]["memory_service"] = "unavailable"
        
        return health