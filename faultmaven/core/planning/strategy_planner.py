"""Strategy Planner Implementation

This module implements the strategic planning functionality for developing
comprehensive troubleshooting strategies based on problem analysis and
available context.

The Strategy Planner creates strategic plans that include:
- Solution approach and methodology
- Resource allocation and timeline
- Risk mitigation strategies
- Success criteria and measurement
- Alternative approaches and fallback plans
"""

import json
import logging
from typing import Dict, List, Any, Optional
from dataclasses import dataclass
from enum import Enum

from faultmaven.models.interfaces import ILLMProvider, IMemoryService
from faultmaven.exceptions import PlanningException


class StrategyApproach(Enum):
    """Strategic approaches for problem resolution"""
    SYSTEMATIC_ANALYSIS = "systematic_analysis"
    RAPID_TROUBLESHOOTING = "rapid_troubleshooting"
    PREVENTIVE_ANALYSIS = "preventive_analysis"
    COLLABORATIVE_RESOLUTION = "collaborative_resolution"
    ESCALATED_SUPPORT = "escalated_support"


@dataclass
class SolutionStrategy:
    """Represents a solution strategy with details and confidence"""
    approach: StrategyApproach
    methodology: List[str]
    estimated_time: str
    confidence: float
    prerequisites: List[str]
    expected_outcome: str


class StrategyPlanner:
    """Strategy Planner for developing troubleshooting strategies
    
    This class provides intelligent strategy planning capabilities to develop
    comprehensive approaches for troubleshooting problems based on problem
    analysis, user context, and historical patterns.
    
    Key Capabilities:
    - Adaptive strategy selection based on problem complexity
    - User skill level and preference integration
    - Resource-aware planning and timeline estimation
    - Risk-aware strategy development
    - Success criteria definition and measurement
    
    Performance Targets:
    - Strategy generation: < 150ms
    - Strategy adaptation: real-time based on user feedback
    - Success prediction: > 80% accuracy for strategy effectiveness
    """
    
    def __init__(
        self, 
        llm_provider: Optional[ILLMProvider] = None,
        memory_service: Optional[IMemoryService] = None
    ):
        """Initialize Strategy Planner
        
        Args:
            llm_provider: Optional LLM interface for advanced strategy analysis
            memory_service: Optional memory service for context and learning
        """
        self._llm = llm_provider
        self._memory = memory_service
        self._logger = logging.getLogger(__name__)
        
        # Strategy templates based on problem characteristics
        self._strategy_templates = {
            StrategyApproach.SYSTEMATIC_ANALYSIS: {
                "methodology": [
                    "Gather comprehensive system information",
                    "Analyze error patterns and symptoms", 
                    "Identify root cause through systematic elimination",
                    "Develop and test solution hypothesis",
                    "Implement fix with monitoring",
                    "Verify resolution and document learnings"
                ],
                "best_for": ["complex", "multi-component", "unclear symptoms"],
                "time_range": (60, 240),  # 1-4 hours
                "skill_requirements": ["intermediate", "advanced"]
            },
            StrategyApproach.RAPID_TROUBLESHOOTING: {
                "methodology": [
                    "Quick symptom assessment",
                    "Apply common fixes for known patterns",
                    "Test immediate resolution",
                    "Escalate if not resolved quickly"
                ],
                "best_for": ["urgent", "known patterns", "simple issues"],
                "time_range": (5, 30),  # 5-30 minutes
                "skill_requirements": ["beginner", "intermediate"]
            },
            StrategyApproach.PREVENTIVE_ANALYSIS: {
                "methodology": [
                    "Analyze potential failure modes",
                    "Implement monitoring and alerting",
                    "Develop preventive measures",
                    "Create runbooks and procedures",
                    "Regular health checks and maintenance"
                ],
                "best_for": ["recurring", "preventable", "infrastructure"],
                "time_range": (120, 480),  # 2-8 hours
                "skill_requirements": ["intermediate", "advanced"]
            },
            StrategyApproach.COLLABORATIVE_RESOLUTION: {
                "methodology": [
                    "Identify required expertise areas",
                    "Coordinate with domain experts",
                    "Parallel investigation tracks",
                    "Consolidated analysis and solution",
                    "Shared documentation and knowledge transfer"
                ],
                "best_for": ["cross-functional", "expertise-heavy", "critical systems"],
                "time_range": (180, 600),  # 3-10 hours
                "skill_requirements": ["advanced", "team coordination"]
            },
            StrategyApproach.ESCALATED_SUPPORT: {
                "methodology": [
                    "Document problem comprehensively",
                    "Gather all relevant information",
                    "Escalate to specialized support",
                    "Monitor resolution progress",
                    "Document resolution for future reference"
                ],
                "best_for": ["vendor-specific", "highly specialized", "critical"],
                "time_range": (240, 1440),  # 4 hours to 1 day
                "skill_requirements": ["coordination", "documentation"]
            }
        }
    
    async def develop_strategy(
        self, 
        problem_analysis: Dict[str, Any], 
        context: Dict[str, Any]
    ) -> Dict[str, Any]:
        """Develop comprehensive solution strategy
        
        Args:
            problem_analysis: Analysis of the problem including components and complexity
            context: Available context including user profile, urgency, resources
            
        Returns:
            Dictionary containing solution strategy with approach, methodology,
            timeline, resources, and success criteria
            
        Raises:
            PlanningException: When strategy development fails
        """
        try:
            self._logger.info("Starting solution strategy development")
            
            # Phase 1: Select optimal strategy approach
            selected_approach = await self._select_strategy_approach(problem_analysis, context)
            
            # Phase 2: Adapt strategy template to specific problem
            adapted_strategy = await self._adapt_strategy_template(
                selected_approach, problem_analysis, context
            )
            
            # Phase 3: Estimate resources and timeline
            resource_estimate = await self._estimate_resources(adapted_strategy, context)
            
            # Phase 4: Define success criteria
            success_criteria = await self._define_success_criteria(problem_analysis, context)
            
            # Phase 5: Develop alternative approaches
            alternatives = await self._develop_alternatives(
                selected_approach, problem_analysis, context
            )
            
            # Phase 6: Create comprehensive strategy
            comprehensive_strategy = {
                "primary_approach": selected_approach.value,
                "methodology": adapted_strategy["methodology"],
                "timeline": resource_estimate["timeline"],
                "resource_requirements": resource_estimate["resources"],
                "success_criteria": success_criteria,
                "risk_factors": adapted_strategy.get("risks", []),
                "confidence": adapted_strategy["confidence"],
                "alternatives": alternatives,
                "adaptation_triggers": self._define_adaptation_triggers(selected_approach),
                "monitoring_points": self._define_monitoring_points(adapted_strategy)
            }
            
            self._logger.info(f"Strategy development completed: {selected_approach.value}")
            return comprehensive_strategy
            
        except Exception as e:
            self._logger.error(f"Strategy development failed: {e}")
            raise PlanningException(f"Failed to develop strategy: {str(e)}")
    
    async def _select_strategy_approach(
        self, 
        problem_analysis: Dict[str, Any], 
        context: Dict[str, Any]
    ) -> StrategyApproach:
        """Select the optimal strategy approach based on problem and context"""
        
        # Extract key factors for strategy selection
        complexity = problem_analysis.get("complexity", {}).get("level", "medium")
        urgency = context.get("urgency", "medium")
        user_skill = context.get("user_profile", {}).get("skill_level", "intermediate")
        available_time = context.get("available_time", "moderate")
        team_size = context.get("team_size", 1)
        
        # Score each approach based on context
        approach_scores = {}
        
        for approach, template in self._strategy_templates.items():
            score = 0.5  # Base score
            
            # Complexity alignment
            if complexity == "high" and approach in [StrategyApproach.SYSTEMATIC_ANALYSIS, 
                                                   StrategyApproach.COLLABORATIVE_RESOLUTION]:
                score += 0.3
            elif complexity == "low" and approach == StrategyApproach.RAPID_TROUBLESHOOTING:
                score += 0.3
            elif complexity == "medium" and approach in [StrategyApproach.SYSTEMATIC_ANALYSIS,
                                                        StrategyApproach.RAPID_TROUBLESHOOTING]:
                score += 0.2
            
            # Urgency alignment
            if urgency == "high" and approach == StrategyApproach.RAPID_TROUBLESHOOTING:
                score += 0.4
            elif urgency == "low" and approach == StrategyApproach.PREVENTIVE_ANALYSIS:
                score += 0.3
            elif urgency == "critical" and approach == StrategyApproach.ESCALATED_SUPPORT:
                score += 0.4
            
            # Skill level alignment
            if user_skill in template["skill_requirements"]:
                score += 0.2
            elif user_skill == "beginner" and approach == StrategyApproach.ESCALATED_SUPPORT:
                score += 0.3
            
            # Team size alignment
            if team_size > 1 and approach == StrategyApproach.COLLABORATIVE_RESOLUTION:
                score += 0.2
            elif team_size == 1 and approach in [StrategyApproach.SYSTEMATIC_ANALYSIS,
                                               StrategyApproach.RAPID_TROUBLESHOOTING]:
                score += 0.1
            
            # Time constraint alignment
            time_range = template["time_range"]
            if available_time == "limited" and time_range[1] <= 60:  # <= 1 hour
                score += 0.3
            elif available_time == "moderate" and time_range[1] <= 240:  # <= 4 hours
                score += 0.2
            elif available_time == "flexible":
                score += 0.1
            
            approach_scores[approach] = min(score, 1.0)  # Cap at 1.0
        
        # Use LLM for enhanced selection if available
        if self._llm:
            try:
                llm_recommendation = await self._llm_recommend_approach(
                    problem_analysis, context, approach_scores
                )
                if llm_recommendation in approach_scores:
                    approach_scores[llm_recommendation] += 0.2
            except Exception as e:
                self._logger.warning(f"LLM approach recommendation failed: {e}")
        
        # Select highest scoring approach
        selected_approach = max(approach_scores, key=approach_scores.get)
        
        self._logger.info(
            f"Selected strategy approach: {selected_approach.value} "
            f"(score: {approach_scores[selected_approach]:.2f})"
        )
        
        return selected_approach
    
    async def _llm_recommend_approach(
        self,
        problem_analysis: Dict[str, Any],
        context: Dict[str, Any],
        current_scores: Dict[StrategyApproach, float]
    ) -> Optional[StrategyApproach]:
        """Use LLM to recommend strategy approach"""
        
        approaches_desc = {
            StrategyApproach.SYSTEMATIC_ANALYSIS: "Thorough, methodical analysis",
            StrategyApproach.RAPID_TROUBLESHOOTING: "Quick, pattern-based resolution",
            StrategyApproach.PREVENTIVE_ANALYSIS: "Prevention-focused approach",
            StrategyApproach.COLLABORATIVE_RESOLUTION: "Team-based resolution",
            StrategyApproach.ESCALATED_SUPPORT: "Expert/vendor escalation"
        }
        
        prompt = f"""
        Recommend the best troubleshooting strategy approach:
        
        Problem Analysis: {json.dumps(problem_analysis, indent=2)}
        Context: {json.dumps(context, indent=2)}
        
        Available Approaches:
        {json.dumps({approach.value: desc for approach, desc in approaches_desc.items()}, indent=2)}
        
        Current Scores: {json.dumps({approach.value: score for approach, score in current_scores.items()}, indent=2)}
        
        Recommend the single best approach based on:
        - Problem complexity and characteristics
        - User skill level and available time
        - Urgency and business impact
        - Available resources and team size
        
        Respond with just the approach name: {list(approaches_desc.keys())}
        """
        
        try:
            response = await self._llm.generate_response(prompt)
            
            # Parse response to extract approach
            for approach in StrategyApproach:
                if approach.value in response.lower():
                    return approach
        except Exception as e:
            self._logger.warning(f"Failed to parse LLM approach recommendation: {e}")
        
        return None
    
    async def _adapt_strategy_template(
        self,
        approach: StrategyApproach,
        problem_analysis: Dict[str, Any],
        context: Dict[str, Any]
    ) -> Dict[str, Any]:
        """Adapt strategy template to specific problem and context"""
        
        template = self._strategy_templates[approach].copy()
        adapted_methodology = template["methodology"].copy()
        
        # Adapt methodology based on problem specifics
        primary_issue = problem_analysis.get("primary_issue", "")
        components = problem_analysis.get("contributing_factors", [])
        
        # Add problem-specific steps
        if "database" in primary_issue.lower():
            if approach == StrategyApproach.SYSTEMATIC_ANALYSIS:
                adapted_methodology.insert(2, "Analyze database performance metrics and query patterns")
                adapted_methodology.insert(3, "Check database connectivity and resource usage")
        
        if "network" in primary_issue.lower():
            if approach in [StrategyApproach.SYSTEMATIC_ANALYSIS, StrategyApproach.RAPID_TROUBLESHOOTING]:
                adapted_methodology.insert(1, "Test network connectivity and latency")
                adapted_methodology.insert(2, "Check firewall and routing configuration")
        
        if "performance" in primary_issue.lower():
            if approach == StrategyApproach.SYSTEMATIC_ANALYSIS:
                adapted_methodology.insert(2, "Collect performance baselines and metrics")
                adapted_methodology.insert(3, "Identify performance bottlenecks and resource constraints")
        
        # Adapt based on user skill level
        user_skill = context.get("user_profile", {}).get("skill_level", "intermediate")
        if user_skill == "beginner":
            # Add more guidance and checks
            adapted_methodology = [
                f"(Guided) {step}" if not step.startswith("(") else step 
                for step in adapted_methodology
            ]
            adapted_methodology.append("Verify each step before proceeding to next")
        elif user_skill == "advanced":
            # Allow more parallelization and flexibility
            adapted_methodology.append("Consider parallel execution of independent steps")
        
        # Calculate confidence based on adaptation quality
        confidence = 0.7  # Base confidence
        
        # Boost confidence for good problem-approach alignment
        if approach == StrategyApproach.RAPID_TROUBLESHOOTING and context.get("urgency") == "high":
            confidence += 0.2
        elif approach == StrategyApproach.SYSTEMATIC_ANALYSIS and problem_analysis.get("complexity", {}).get("level") == "high":
            confidence += 0.2
        
        # Boost confidence for skill alignment
        if user_skill in template["skill_requirements"]:
            confidence += 0.1
        
        # Add risk factors
        risks = []
        if approach == StrategyApproach.RAPID_TROUBLESHOOTING:
            risks.append("May miss underlying root cause")
            risks.append("Solution might be temporary")
        elif approach == StrategyApproach.SYSTEMATIC_ANALYSIS:
            risks.append("May take longer than expected")
            risks.append("Requires sustained focus and attention")
        elif approach == StrategyApproach.COLLABORATIVE_RESOLUTION:
            risks.append("Coordination overhead may slow progress")
            risks.append("Requires availability of multiple experts")
        
        return {
            "methodology": adapted_methodology,
            "confidence": min(confidence, 1.0),
            "risks": risks,
            "original_template": template
        }
    
    async def _estimate_resources(
        self, 
        adapted_strategy: Dict[str, Any], 
        context: Dict[str, Any]
    ) -> Dict[str, Any]:
        """Estimate resources and timeline for strategy execution"""
        
        methodology_steps = len(adapted_strategy["methodology"])
        base_time_range = adapted_strategy["original_template"]["time_range"]
        
        # Adjust time estimate based on factors
        time_multiplier = 1.0
        
        # User skill adjustment
        user_skill = context.get("user_profile", {}).get("skill_level", "intermediate")
        if user_skill == "beginner":
            time_multiplier *= 1.5
        elif user_skill == "advanced":
            time_multiplier *= 0.8
        
        # Problem complexity adjustment
        complexity = context.get("complexity", {}).get("level", "medium")
        if complexity == "high":
            time_multiplier *= 1.4
        elif complexity == "low":
            time_multiplier *= 0.7
        
        # Team size adjustment
        team_size = context.get("team_size", 1)
        if team_size > 1:
            time_multiplier *= 0.8  # Some parallelization benefits
        
        # Calculate adjusted time range
        min_time = int(base_time_range[0] * time_multiplier)
        max_time = int(base_time_range[1] * time_multiplier)
        
        # Format timeline
        def format_time(minutes):
            if minutes < 60:
                return f"{minutes} minutes"
            elif minutes < 1440:
                hours = minutes / 60
                return f"{hours:.1f} hours"
            else:
                days = minutes / 1440
                return f"{days:.1f} days"
        
        timeline = f"{format_time(min_time)} - {format_time(max_time)}"
        
        # Determine required resources
        resources = {
            "time_estimate": timeline,
            "personnel": self._estimate_personnel_needs(adapted_strategy, context),
            "tools": self._estimate_tool_needs(adapted_strategy, context),
            "access_requirements": self._estimate_access_needs(adapted_strategy, context)
        }
        
        return {
            "timeline": timeline,
            "resources": resources,
            "confidence": adapted_strategy["confidence"]
        }
    
    def _estimate_personnel_needs(self, adapted_strategy: Dict[str, Any], context: Dict[str, Any]) -> List[str]:
        """Estimate personnel requirements"""
        personnel = ["Primary troubleshooter"]
        
        methodology = adapted_strategy["methodology"]
        
        # Check for domain expertise needs
        if any("database" in step.lower() for step in methodology):
            personnel.append("Database administrator (if available)")
        
        if any("network" in step.lower() for step in methodology):
            personnel.append("Network specialist (if available)")
        
        if any("security" in step.lower() for step in methodology):
            personnel.append("Security expert (if available)")
        
        # Check for coordination needs
        if "coordinate" in str(methodology).lower():
            personnel.append("Team coordinator")
        
        return personnel
    
    def _estimate_tool_needs(self, adapted_strategy: Dict[str, Any], context: Dict[str, Any]) -> List[str]:
        """Estimate tool requirements"""
        tools = ["Basic troubleshooting tools"]
        
        methodology = adapted_strategy["methodology"]
        
        # Add specific tools based on methodology steps
        if any("performance" in step.lower() for step in methodology):
            tools.extend(["Performance monitoring tools", "System metrics dashboard"])
        
        if any("network" in step.lower() for step in methodology):
            tools.extend(["Network diagnostic tools", "Connectivity testing tools"])
        
        if any("database" in step.lower() for step in methodology):
            tools.extend(["Database monitoring tools", "Query analysis tools"])
        
        if any("log" in step.lower() for step in methodology):
            tools.extend(["Log analysis tools", "Centralized logging system"])
        
        return list(set(tools))  # Remove duplicates
    
    def _estimate_access_needs(self, adapted_strategy: Dict[str, Any], context: Dict[str, Any]) -> List[str]:
        """Estimate access requirements"""
        access_needs = []
        
        methodology = adapted_strategy["methodology"]
        
        # Determine access needs from methodology
        if any("system" in step.lower() for step in methodology):
            access_needs.append("System administrator access")
        
        if any("database" in step.lower() for step in methodology):
            access_needs.append("Database read/write access")
        
        if any("configuration" in step.lower() for step in methodology):
            access_needs.append("Configuration management access")
        
        if any("restart" in step.lower() or "implement" in step.lower() for step in methodology):
            access_needs.append("Service control permissions")
        
        return access_needs
    
    async def _define_success_criteria(
        self, 
        problem_analysis: Dict[str, Any], 
        context: Dict[str, Any]
    ) -> List[str]:
        """Define measurable success criteria"""
        
        criteria = []
        
        # Primary issue resolution
        primary_issue = problem_analysis.get("primary_issue", "")
        if primary_issue:
            criteria.append(f"Primary issue resolved: {primary_issue}")
        
        # Performance criteria
        if "performance" in primary_issue.lower():
            criteria.extend([
                "System performance returns to baseline levels",
                "Response times within acceptable thresholds",
                "Resource utilization normalized"
            ])
        
        # Connectivity criteria
        if "network" in primary_issue.lower() or "connection" in primary_issue.lower():
            criteria.extend([
                "Network connectivity fully restored",
                "All dependent services accessible",
                "Connection timeouts eliminated"
            ])
        
        # Application criteria
        if "application" in primary_issue.lower():
            criteria.extend([
                "Application functions normally",
                "All features accessible to users",
                "Error rates reduced to baseline"
            ])
        
        # General criteria
        criteria.extend([
            "No recurrence of symptoms for monitoring period",
            "User/stakeholder confirmation of resolution",
            "Documentation completed for future reference"
        ])
        
        return criteria[:5]  # Limit to top 5 criteria
    
    async def _develop_alternatives(
        self,
        primary_approach: StrategyApproach,
        problem_analysis: Dict[str, Any],
        context: Dict[str, Any]
    ) -> List[Dict[str, Any]]:
        """Develop alternative strategy approaches"""
        
        alternatives = []
        
        # Get all approaches except the primary one
        alternative_approaches = [approach for approach in StrategyApproach if approach != primary_approach]
        
        # Select top 2 alternatives based on suitability
        approach_suitability = {}
        
        for approach in alternative_approaches:
            suitability = 0.3  # Base suitability
            
            template = self._strategy_templates[approach]
            
            # Check alignment with problem characteristics
            complexity = problem_analysis.get("complexity", {}).get("level", "medium")
            if complexity in template.get("best_for", []):
                suitability += 0.4
            
            # Check skill alignment
            user_skill = context.get("user_profile", {}).get("skill_level", "intermediate")
            if user_skill in template["skill_requirements"]:
                suitability += 0.3
            
            approach_suitability[approach] = suitability
        
        # Select top 2 alternatives
        top_alternatives = sorted(approach_suitability.items(), key=lambda x: x[1], reverse=True)[:2]
        
        for approach, suitability in top_alternatives:
            template = self._strategy_templates[approach]
            alternatives.append({
                "approach": approach.value,
                "description": f"Alternative approach using {approach.value.replace('_', ' ')}",
                "methodology": template["methodology"][:3],  # First 3 steps
                "estimated_time": f"{template['time_range'][0]}-{template['time_range'][1]} minutes",
                "suitability_score": suitability,
                "when_to_use": f"Consider if primary approach {self._get_fallback_conditions(primary_approach)}"
            })
        
        return alternatives
    
    def _get_fallback_conditions(self, primary_approach: StrategyApproach) -> str:
        """Get conditions when to switch from primary approach"""
        conditions = {
            StrategyApproach.SYSTEMATIC_ANALYSIS: "takes too long or lacks clear progress",
            StrategyApproach.RAPID_TROUBLESHOOTING: "doesn't resolve quickly or causes new issues",
            StrategyApproach.PREVENTIVE_ANALYSIS: "immediate resolution is needed",
            StrategyApproach.COLLABORATIVE_RESOLUTION: "team coordination becomes problematic",
            StrategyApproach.ESCALATED_SUPPORT: "vendor response is delayed"
        }
        return conditions.get(primary_approach, "encounters significant obstacles")
    
    def _define_adaptation_triggers(self, approach: StrategyApproach) -> List[str]:
        """Define triggers for strategy adaptation"""
        base_triggers = [
            "No progress after 50% of estimated time",
            "New information significantly changes problem understanding",
            "Resource constraints change during execution"
        ]
        
        approach_specific = {
            StrategyApproach.RAPID_TROUBLESHOOTING: [
                "Initial fixes don't resolve the issue",
                "Problem appears more complex than initially assessed"
            ],
            StrategyApproach.SYSTEMATIC_ANALYSIS: [
                "Time constraints become critical",
                "Clear pattern emerges suggesting faster approach"
            ],
            StrategyApproach.COLLABORATIVE_RESOLUTION: [
                "Key team members become unavailable",
                "Problem scope reduces to single domain"
            ]
        }
        
        specific_triggers = approach_specific.get(approach, [])
        return base_triggers + specific_triggers
    
    def _define_monitoring_points(self, adapted_strategy: Dict[str, Any]) -> List[str]:
        """Define points where progress should be assessed"""
        methodology = adapted_strategy["methodology"]
        total_steps = len(methodology)
        
        # Create monitoring points at key intervals
        monitoring_points = []
        
        if total_steps >= 3:
            monitoring_points.append(f"After step {max(1, total_steps // 4)}: Initial progress assessment")
        
        if total_steps >= 4:
            monitoring_points.append(f"After step {total_steps // 2}: Mid-point progress review")
        
        if total_steps >= 6:
            monitoring_points.append(f"After step {3 * total_steps // 4}: Pre-completion assessment")
        
        monitoring_points.append(f"After final step: Resolution verification and documentation")
        
        return monitoring_points