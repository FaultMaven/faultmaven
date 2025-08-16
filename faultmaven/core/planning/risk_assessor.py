"""Risk Assessor Implementation

This module implements the risk assessment functionality for evaluating
potential risks and developing mitigation strategies for troubleshooting
approaches and solutions.

The Risk Assessor identifies and evaluates:
- Technical risks and potential failure modes
- Business impact and operational risks
- Resource and timeline risks
- Mitigation strategies and contingency plans
- Risk monitoring and early warning indicators
"""

import logging
from typing import Dict, List, Any, Optional, Tuple
from dataclasses import dataclass
from enum import Enum

from faultmaven.models.interfaces import ILLMProvider, IMemoryService
from faultmaven.exceptions import PlanningException


class RiskLevel(Enum):
    """Risk severity levels"""
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    CRITICAL = "critical"


class RiskCategory(Enum):
    """Categories of risks"""
    TECHNICAL = "technical"
    OPERATIONAL = "operational"
    BUSINESS = "business"
    SECURITY = "security"
    COMPLIANCE = "compliance"
    RESOURCE = "resource"


@dataclass
class Risk:
    """Represents an identified risk with assessment and mitigation"""
    id: str
    category: RiskCategory
    level: RiskLevel
    description: str
    probability: float  # 0.0 to 1.0
    impact: float  # 0.0 to 1.0
    risk_score: float  # probability * impact
    mitigation_strategies: List[str]
    monitoring_indicators: List[str]
    contingency_plans: List[str]


class RiskAssessor:
    """Risk Assessor for evaluating troubleshooting risks and mitigation strategies
    
    This class provides comprehensive risk assessment capabilities to identify
    potential risks in troubleshooting approaches and develop appropriate
    mitigation strategies to ensure safe and effective problem resolution.
    
    Key Capabilities:
    - Multi-dimensional risk identification and assessment
    - Context-aware risk evaluation based on environment and urgency
    - Automated mitigation strategy generation
    - Risk monitoring and early warning systems
    - Integration with historical risk patterns and outcomes
    
    Performance Targets:
    - Risk assessment: < 100ms
    - Risk identification accuracy: > 85% for common scenarios
    - Mitigation strategy relevance: > 80% user satisfaction
    """
    
    def __init__(
        self, 
        llm_provider: Optional[ILLMProvider] = None,
        memory_service: Optional[IMemoryService] = None
    ):
        """Initialize Risk Assessor
        
        Args:
            llm_provider: Optional LLM interface for advanced risk analysis
            memory_service: Optional memory service for historical risk patterns
        """
        self._llm = llm_provider
        self._memory = memory_service
        self._logger = logging.getLogger(__name__)
        
        # Risk pattern definitions for different scenarios
        self._risk_patterns = {
            "production_environment": {
                "risks": [
                    {
                        "category": RiskCategory.BUSINESS,
                        "description": "Service disruption affecting users",
                        "base_probability": 0.3,
                        "base_impact": 0.8,
                        "keywords": ["production", "live", "users", "customers"]
                    },
                    {
                        "category": RiskCategory.OPERATIONAL,
                        "description": "Changes affecting system stability",
                        "base_probability": 0.4,
                        "base_impact": 0.6,
                        "keywords": ["production", "stability", "changes"]
                    }
                ]
            },
            "database_operations": {
                "risks": [
                    {
                        "category": RiskCategory.TECHNICAL,
                        "description": "Data corruption or loss",
                        "base_probability": 0.2,
                        "base_impact": 0.9,
                        "keywords": ["database", "data", "corruption", "backup"]
                    },
                    {
                        "category": RiskCategory.OPERATIONAL,
                        "description": "Database downtime affecting applications",
                        "base_probability": 0.3,
                        "base_impact": 0.7,
                        "keywords": ["database", "downtime", "applications"]
                    }
                ]
            },
            "network_changes": {
                "risks": [
                    {
                        "category": RiskCategory.TECHNICAL,
                        "description": "Network connectivity loss",
                        "base_probability": 0.4,
                        "base_impact": 0.6,
                        "keywords": ["network", "connectivity", "routing", "firewall"]
                    },
                    {
                        "category": RiskCategory.SECURITY,
                        "description": "Security configuration changes",
                        "base_probability": 0.3,
                        "base_impact": 0.7,
                        "keywords": ["network", "security", "firewall", "access"]
                    }
                ]
            },
            "urgent_fixes": {
                "risks": [
                    {
                        "category": RiskCategory.TECHNICAL,
                        "description": "Incomplete testing of solutions",
                        "base_probability": 0.6,
                        "base_impact": 0.5,
                        "keywords": ["urgent", "quick", "immediate", "emergency"]
                    },
                    {
                        "category": RiskCategory.OPERATIONAL,
                        "description": "Inadequate change documentation",
                        "base_probability": 0.5,
                        "base_impact": 0.4,
                        "keywords": ["urgent", "documentation", "changes"]
                    }
                ]
            },
            "complex_systems": {
                "risks": [
                    {
                        "category": RiskCategory.TECHNICAL,
                        "description": "Unintended side effects in interconnected systems",
                        "base_probability": 0.5,
                        "base_impact": 0.6,
                        "keywords": ["complex", "distributed", "microservices", "dependencies"]
                    },
                    {
                        "category": RiskCategory.RESOURCE,
                        "description": "Extended troubleshooting time due to complexity",
                        "base_probability": 0.6,
                        "base_impact": 0.4,
                        "keywords": ["complex", "time", "resources", "expertise"]
                    }
                ]
            }
        }
        
        # Mitigation strategy templates
        self._mitigation_templates = {
            RiskCategory.TECHNICAL: {
                "general": [
                    "Create backup/rollback plan before changes",
                    "Test solutions in non-production environment first",
                    "Implement change monitoring and alerts",
                    "Have technical expert available for consultation"
                ],
                "data_related": [
                    "Verify database backup before any data operations",
                    "Use read-only queries for initial investigation",
                    "Implement transaction logging for changes",
                    "Test data recovery procedures"
                ]
            },
            RiskCategory.OPERATIONAL: {
                "general": [
                    "Coordinate with operations team",
                    "Schedule changes during maintenance windows",
                    "Prepare communication plan for stakeholders",
                    "Define rollback procedures and criteria"
                ],
                "service_impact": [
                    "Notify users of potential service impact",
                    "Prepare alternative service channels",
                    "Monitor service health metrics continuously",
                    "Have escalation procedures ready"
                ]
            },
            RiskCategory.BUSINESS: {
                "general": [
                    "Assess business impact and get approval",
                    "Coordinate with business stakeholders",
                    "Consider timing and business cycles",
                    "Prepare impact communication"
                ],
                "customer_facing": [
                    "Notify customer support team",
                    "Prepare customer communication templates",
                    "Monitor customer satisfaction metrics",
                    "Have customer escalation procedures ready"
                ]
            },
            RiskCategory.SECURITY: {
                "general": [
                    "Review security implications with security team",
                    "Ensure compliance with security policies",
                    "Monitor for security events during changes",
                    "Document security-related changes"
                ]
            },
            RiskCategory.RESOURCE: {
                "general": [
                    "Ensure adequate skilled personnel available",
                    "Prepare additional resource allocation",
                    "Set realistic timeline expectations",
                    "Have backup resources identified"
                ]
            }
        }
    
    async def assess_risks(
        self, 
        solution_strategy: Dict[str, Any], 
        context: Dict[str, Any]
    ) -> Dict[str, Any]:
        """Assess risks for solution strategy and context
        
        Args:
            solution_strategy: Proposed solution strategy with methodology and approach
            context: Execution context including environment, urgency, resources
            
        Returns:
            Dictionary containing identified risks, assessments, mitigation strategies,
            and monitoring recommendations
            
        Raises:
            PlanningException: When risk assessment fails
        """
        try:
            self._logger.info("Starting risk assessment for solution strategy")
            
            # Phase 1: Identify potential risks
            identified_risks = await self._identify_risks(solution_strategy, context)
            
            # Phase 2: Assess risk probability and impact
            assessed_risks = await self._assess_risk_levels(identified_risks, context)
            
            # Phase 3: Generate mitigation strategies
            mitigation_strategies = await self._generate_mitigations(assessed_risks, context)
            
            # Phase 4: Develop monitoring and contingency plans
            monitoring_plan = await self._develop_monitoring_plan(assessed_risks, context)
            
            # Phase 5: Create comprehensive risk assessment
            risk_assessment = {
                "overall_risk_level": self._calculate_overall_risk(assessed_risks),
                "identified_risks": [self._risk_to_dict(risk) for risk in assessed_risks],
                "high_priority_risks": [
                    self._risk_to_dict(risk) for risk in assessed_risks 
                    if risk.level in [RiskLevel.HIGH, RiskLevel.CRITICAL]
                ],
                "mitigation_strategies": mitigation_strategies,
                "monitoring_plan": monitoring_plan,
                "risk_indicators": self._generate_risk_indicators(assessed_risks),
                "contingency_triggers": self._generate_contingency_triggers(assessed_risks),
                "approval_recommendations": self._generate_approval_recommendations(assessed_risks, context)
            }
            
            self._logger.info(f"Risk assessment completed: {len(assessed_risks)} risks identified")
            return risk_assessment
            
        except Exception as e:
            self._logger.error(f"Risk assessment failed: {e}")
            raise PlanningException(f"Failed to assess risks: {str(e)}")
    
    async def _identify_risks(
        self, 
        solution_strategy: Dict[str, Any], 
        context: Dict[str, Any]
    ) -> List[Risk]:
        """Identify potential risks based on strategy and context"""
        
        identified_risks = []
        risk_id_counter = 1
        
        # Extract context for risk pattern matching
        environment = context.get("environment", "unknown")
        urgency = context.get("urgency", "medium")
        methodology = solution_strategy.get("methodology", [])
        approach = solution_strategy.get("primary_approach", "")
        
        # Combine methodology and context for analysis
        analysis_text = " ".join(methodology + [str(context), approach]).lower()
        
        # Apply risk patterns
        for pattern_name, pattern_data in self._risk_patterns.items():
            for risk_template in pattern_data["risks"]:
                # Check if pattern keywords match
                keyword_matches = sum(1 for keyword in risk_template["keywords"] 
                                    if keyword in analysis_text)
                
                if keyword_matches > 0:
                    # Calculate adjusted probability based on keyword match strength
                    match_strength = keyword_matches / len(risk_template["keywords"])
                    adjusted_probability = risk_template["base_probability"] * (0.5 + 0.5 * match_strength)
                    
                    # Adjust impact based on environment and urgency
                    adjusted_impact = self._adjust_impact_for_context(
                        risk_template["base_impact"], environment, urgency
                    )
                    
                    # Create risk instance
                    risk = Risk(
                        id=f"risk_{risk_id_counter:03d}",
                        category=risk_template["category"],
                        level=self._calculate_risk_level(adjusted_probability, adjusted_impact),
                        description=risk_template["description"],
                        probability=adjusted_probability,
                        impact=adjusted_impact,
                        risk_score=adjusted_probability * adjusted_impact,
                        mitigation_strategies=[],  # Will be filled later
                        monitoring_indicators=[],  # Will be filled later
                        contingency_plans=[]  # Will be filled later
                    )
                    
                    identified_risks.append(risk)
                    risk_id_counter += 1
        
        # Add context-specific risks
        context_risks = await self._identify_context_specific_risks(context, risk_id_counter)
        identified_risks.extend(context_risks)
        
        # Use LLM for additional risk identification if available
        if self._llm:
            try:
                llm_risks = await self._llm_identify_risks(solution_strategy, context, risk_id_counter + len(context_risks))
                identified_risks.extend(llm_risks)
            except Exception as e:
                self._logger.warning(f"LLM risk identification failed: {e}")
        
        # Remove duplicates and sort by risk score
        unique_risks = self._deduplicate_risks(identified_risks)
        return sorted(unique_risks, key=lambda r: r.risk_score, reverse=True)
    
    def _adjust_impact_for_context(self, base_impact: float, environment: str, urgency: str) -> float:
        """Adjust risk impact based on context"""
        adjusted_impact = base_impact
        
        # Environment adjustments
        if environment == "production":
            adjusted_impact *= 1.5
        elif environment == "staging":
            adjusted_impact *= 0.7
        elif environment == "development":
            adjusted_impact *= 0.3
        
        # Urgency adjustments
        if urgency == "critical":
            adjusted_impact *= 1.3
        elif urgency == "high":
            adjusted_impact *= 1.1
        elif urgency == "low":
            adjusted_impact *= 0.8
        
        return min(adjusted_impact, 1.0)  # Cap at 1.0
    
    def _calculate_risk_level(self, probability: float, impact: float) -> RiskLevel:
        """Calculate risk level based on probability and impact"""
        risk_score = probability * impact
        
        if risk_score >= 0.8:
            return RiskLevel.CRITICAL
        elif risk_score >= 0.6:
            return RiskLevel.HIGH
        elif risk_score >= 0.3:
            return RiskLevel.MEDIUM
        else:
            return RiskLevel.LOW
    
    async def _identify_context_specific_risks(self, context: Dict[str, Any], start_id: int) -> List[Risk]:
        """Identify risks specific to the context"""
        context_risks = []
        risk_id = start_id
        
        # Time constraint risks
        available_time = context.get("available_time", "moderate")
        if available_time == "limited":
            context_risks.append(Risk(
                id=f"risk_{risk_id:03d}",
                category=RiskCategory.RESOURCE,
                level=RiskLevel.MEDIUM,
                description="Time constraints may lead to incomplete solution",
                probability=0.4,
                impact=0.6,
                risk_score=0.24,
                mitigation_strategies=[],
                monitoring_indicators=[],
                contingency_plans=[]
            ))
            risk_id += 1
        
        # Skill level risks
        user_skill = context.get("user_profile", {}).get("skill_level", "intermediate")
        if user_skill == "beginner":
            context_risks.append(Risk(
                id=f"risk_{risk_id:03d}",
                category=RiskCategory.TECHNICAL,
                level=RiskLevel.MEDIUM,
                description="Limited expertise may result in incorrect implementation",
                probability=0.5,
                impact=0.5,
                risk_score=0.25,
                mitigation_strategies=[],
                monitoring_indicators=[],
                contingency_plans=[]
            ))
            risk_id += 1
        
        # Team size risks
        team_size = context.get("team_size", 1)
        if team_size == 1:
            context_risks.append(Risk(
                id=f"risk_{risk_id:03d}",
                category=RiskCategory.RESOURCE,
                level=RiskLevel.LOW,
                description="Single person troubleshooting increases risk of oversight",
                probability=0.3,
                impact=0.4,
                risk_score=0.12,
                mitigation_strategies=[],
                monitoring_indicators=[],
                contingency_plans=[]
            ))
            risk_id += 1
        
        return context_risks
    
    async def _llm_identify_risks(
        self,
        solution_strategy: Dict[str, Any],
        context: Dict[str, Any],
        start_id: int
    ) -> List[Risk]:
        """Use LLM to identify additional risks"""
        # This would use the LLM to identify risks not covered by patterns
        # For now, return empty list as LLM integration is optional
        return []
    
    def _deduplicate_risks(self, risks: List[Risk]) -> List[Risk]:
        """Remove duplicate risks based on description similarity"""
        unique_risks = []
        seen_descriptions = set()
        
        for risk in risks:
            # Simple deduplication based on key words in description
            key_words = set(risk.description.lower().split())
            description_key = tuple(sorted(key_words))
            
            if description_key not in seen_descriptions:
                unique_risks.append(risk)
                seen_descriptions.add(description_key)
        
        return unique_risks
    
    async def _assess_risk_levels(self, risks: List[Risk], context: Dict[str, Any]) -> List[Risk]:
        """Assess and refine risk levels based on additional context"""
        # Risk levels are already calculated during identification
        # This method could add additional refinement based on historical data
        
        for risk in risks:
            # Recalculate risk level to ensure consistency
            risk.level = self._calculate_risk_level(risk.probability, risk.impact)
            risk.risk_score = risk.probability * risk.impact
        
        return risks
    
    async def _generate_mitigations(
        self, 
        risks: List[Risk], 
        context: Dict[str, Any]
    ) -> Dict[str, List[str]]:
        """Generate mitigation strategies for identified risks"""
        
        mitigation_strategies = {
            "immediate_actions": [],
            "preventive_measures": [],
            "contingency_plans": [],
            "monitoring_requirements": []
        }
        
        for risk in risks:
            # Get base mitigation strategies for risk category
            category_mitigations = self._mitigation_templates.get(risk.category, {})
            
            # Select appropriate mitigation strategies
            if risk.level in [RiskLevel.HIGH, RiskLevel.CRITICAL]:
                # High/Critical risks need immediate action
                general_mitigations = category_mitigations.get("general", [])
                mitigation_strategies["immediate_actions"].extend(general_mitigations[:2])
                
                # Add specific mitigations based on risk description
                if "data" in risk.description.lower():
                    data_mitigations = category_mitigations.get("data_related", [])
                    mitigation_strategies["immediate_actions"].extend(data_mitigations[:2])
                
                # Add contingency plans for critical risks
                if risk.level == RiskLevel.CRITICAL:
                    mitigation_strategies["contingency_plans"].append(
                        f"Prepare immediate rollback for: {risk.description}"
                    )
            
            elif risk.level == RiskLevel.MEDIUM:
                # Medium risks need preventive measures
                general_mitigations = category_mitigations.get("general", [])
                mitigation_strategies["preventive_measures"].extend(general_mitigations[:1])
            
            # Add monitoring requirements for all significant risks
            if risk.level in [RiskLevel.MEDIUM, RiskLevel.HIGH, RiskLevel.CRITICAL]:
                mitigation_strategies["monitoring_requirements"].append(
                    f"Monitor indicators for: {risk.description}"
                )
        
        # Remove duplicates and limit counts
        for key in mitigation_strategies:
            mitigation_strategies[key] = list(set(mitigation_strategies[key]))[:5]
        
        return mitigation_strategies
    
    async def _develop_monitoring_plan(
        self, 
        risks: List[Risk], 
        context: Dict[str, Any]
    ) -> Dict[str, Any]:
        """Develop monitoring plan for risk indicators"""
        
        monitoring_plan = {
            "continuous_monitoring": [],
            "checkpoint_assessments": [],
            "alert_thresholds": [],
            "escalation_criteria": []
        }
        
        # Add monitoring for high-impact risks
        high_impact_risks = [r for r in risks if r.impact >= 0.6]
        
        for risk in high_impact_risks:
            if risk.category == RiskCategory.TECHNICAL:
                monitoring_plan["continuous_monitoring"].extend([
                    "System performance metrics",
                    "Error rates and exceptions",
                    "Service availability status"
                ])
            elif risk.category == RiskCategory.OPERATIONAL:
                monitoring_plan["continuous_monitoring"].extend([
                    "Service health dashboards",
                    "User experience metrics",
                    "Operational alerts"
                ])
            elif risk.category == RiskCategory.BUSINESS:
                monitoring_plan["checkpoint_assessments"].extend([
                    "Business impact assessment at 30-minute intervals",
                    "Stakeholder communication status"
                ])
        
        # Add checkpoint assessments
        monitoring_plan["checkpoint_assessments"].extend([
            "Risk status review at 25% completion",
            "Risk reassessment at 50% completion",
            "Final risk evaluation before completion"
        ])
        
        # Define alert thresholds
        critical_risks = [r for r in risks if r.level == RiskLevel.CRITICAL]
        if critical_risks:
            monitoring_plan["alert_thresholds"].extend([
                "Immediate alert for any critical risk indicator",
                "15-minute alert for high-risk indicators"
            ])
        
        # Define escalation criteria
        monitoring_plan["escalation_criteria"].extend([
            "Escalate if any critical risk materializes",
            "Escalate if multiple medium risks occur simultaneously",
            "Escalate if timeline extends beyond 150% of estimate"
        ])
        
        # Remove duplicates
        for key in monitoring_plan:
            monitoring_plan[key] = list(set(monitoring_plan[key]))
        
        return monitoring_plan
    
    def _calculate_overall_risk(self, risks: List[Risk]) -> str:
        """Calculate overall risk level for the strategy"""
        if not risks:
            return "low"
        
        # Check for critical risks
        if any(r.level == RiskLevel.CRITICAL for r in risks):
            return "critical"
        
        # Count high and medium risks
        high_risks = sum(1 for r in risks if r.level == RiskLevel.HIGH)
        medium_risks = sum(1 for r in risks if r.level == RiskLevel.MEDIUM)
        
        if high_risks >= 2:
            return "high"
        elif high_risks >= 1 or medium_risks >= 3:
            return "medium"
        else:
            return "low"
    
    def _risk_to_dict(self, risk: Risk) -> Dict[str, Any]:
        """Convert Risk object to dictionary"""
        return {
            "id": risk.id,
            "category": risk.category.value,
            "level": risk.level.value,
            "description": risk.description,
            "probability": risk.probability,
            "impact": risk.impact,
            "risk_score": risk.risk_score,
            "mitigation_strategies": risk.mitigation_strategies,
            "monitoring_indicators": risk.monitoring_indicators,
            "contingency_plans": risk.contingency_plans
        }
    
    def _generate_risk_indicators(self, risks: List[Risk]) -> List[str]:
        """Generate early warning indicators for risks"""
        indicators = []
        
        for risk in risks:
            if risk.level in [RiskLevel.HIGH, RiskLevel.CRITICAL]:
                if risk.category == RiskCategory.TECHNICAL:
                    indicators.extend([
                        "Unusual error patterns or increased error rates",
                        "Performance degradation in related systems",
                        "Unexpected system behavior or responses"
                    ])
                elif risk.category == RiskCategory.OPERATIONAL:
                    indicators.extend([
                        "Service availability below normal thresholds",
                        "Increased user complaints or support tickets",
                        "Unusual resource consumption patterns"
                    ])
                elif risk.category == RiskCategory.BUSINESS:
                    indicators.extend([
                        "Business metrics declining below acceptable levels",
                        "Stakeholder concerns about progress or impact"
                    ])
        
        return list(set(indicators))[:5]  # Remove duplicates and limit
    
    def _generate_contingency_triggers(self, risks: List[Risk]) -> List[str]:
        """Generate triggers for activating contingency plans"""
        triggers = []
        
        critical_risks = [r for r in risks if r.level == RiskLevel.CRITICAL]
        high_risks = [r for r in risks if r.level == RiskLevel.HIGH]
        
        if critical_risks:
            triggers.extend([
                "Any critical risk indicator becomes active",
                "Multiple risk indicators activate within 15 minutes",
                "Primary troubleshooting approach shows no progress after 50% of timeline"
            ])
        
        if high_risks:
            triggers.extend([
                "High-risk indicators persist for more than 30 minutes",
                "Business impact exceeds acceptable thresholds"
            ])
        
        # General triggers
        triggers.extend([
            "Timeline extends beyond 200% of original estimate",
            "New information significantly changes risk profile",
            "Required resources become unavailable"
        ])
        
        return triggers[:5]  # Limit to top 5 triggers
    
    def _generate_approval_recommendations(
        self, 
        risks: List[Risk], 
        context: Dict[str, Any]
    ) -> List[str]:
        """Generate recommendations for approval requirements"""
        recommendations = []
        
        overall_risk = self._calculate_overall_risk(risks)
        environment = context.get("environment", "unknown")
        
        if overall_risk == "critical":
            recommendations.extend([
                "Require senior management approval before proceeding",
                "Mandatory review by technical architecture team",
                "Business stakeholder sign-off required"
            ])
        elif overall_risk == "high":
            recommendations.extend([
                "Technical lead approval required",
                "Operations team notification mandatory"
            ])
        
        if environment == "production":
            recommendations.extend([
                "Change management process compliance required",
                "Production deployment approval needed"
            ])
        
        # Risk-specific recommendations
        security_risks = [r for r in risks if r.category == RiskCategory.SECURITY]
        if security_risks:
            recommendations.append("Security team review and approval required")
        
        data_risks = [r for r in risks if "data" in r.description.lower()]
        if data_risks:
            recommendations.append("Data protection officer approval recommended")
        
        return list(set(recommendations))  # Remove duplicates