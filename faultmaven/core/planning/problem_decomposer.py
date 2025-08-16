"""Problem Decomposer Implementation

This module implements the problem decomposition functionality for breaking down
complex troubleshooting problems into manageable components with clear
dependencies and priority rankings.

The Problem Decomposer analyzes problem descriptions and context to identify:
- Primary issues requiring immediate attention
- Contributing factors affecting the primary issue
- Dependencies between problem components
- Complexity assessment and resource requirements
- Priority ranking for systematic resolution
"""

import re
import json
import logging
from typing import Dict, List, Any, Optional, Tuple
from dataclasses import dataclass
from collections import defaultdict

from faultmaven.models.interfaces import ILLMProvider, ProblemComponents
from faultmaven.exceptions import PlanningException


@dataclass
class ComponentRelationship:
    """Represents a relationship between problem components"""
    source: str
    target: str
    relationship_type: str  # "depends_on", "contributes_to", "conflicts_with"
    strength: float  # 0.0 to 1.0


class ProblemDecomposer:
    """Problem Decomposer for breaking down complex troubleshooting problems
    
    This class provides intelligent problem decomposition capabilities to break
    complex issues into manageable components that can be addressed systematically.
    It analyzes problem descriptions, identifies relationships between components,
    and provides priority rankings for resolution.
    
    Key Capabilities:
    - Automatic problem component identification
    - Dependency analysis and mapping
    - Complexity assessment and resource estimation
    - Priority ranking based on impact and effort
    - Integration with user context and domain knowledge
    
    Performance Targets:
    - Decomposition analysis: < 200ms
    - Component identification: accurate for 80%+ of cases
    - Dependency mapping: comprehensive relationship identification
    """
    
    def __init__(self, llm_provider: Optional[ILLMProvider] = None):
        """Initialize Problem Decomposer
        
        Args:
            llm_provider: Optional LLM interface for advanced decomposition analysis
        """
        self._llm = llm_provider
        self._logger = logging.getLogger(__name__)
        
        # Problem pattern keywords for component identification
        self._component_patterns = {
            "performance": [
                "slow", "timeout", "latency", "response time", "cpu", "memory", 
                "disk", "load", "performance", "bottleneck", "throughput"
            ],
            "connectivity": [
                "connection", "network", "dns", "firewall", "port", "tcp", "udp",
                "ping", "telnet", "ssh", "ssl", "certificate", "proxy"
            ],
            "authentication": [
                "auth", "login", "password", "token", "session", "permission",
                "access", "authorization", "credential", "ldap", "oauth"
            ],
            "data": [
                "database", "sql", "query", "table", "index", "backup", "corruption",
                "migration", "schema", "transaction", "deadlock"
            ],
            "application": [
                "application", "service", "api", "endpoint", "error", "exception",
                "crash", "restart", "deployment", "configuration", "version"
            ],
            "infrastructure": [
                "server", "hardware", "disk space", "filesystem", "mount", "raid",
                "virtualization", "container", "kubernetes", "docker"
            ]
        }
        
        # Complexity indicators
        self._complexity_indicators = {
            "high": [
                "multiple systems", "distributed", "microservices", "legacy", 
                "third-party", "external dependencies", "compliance", "security"
            ],
            "medium": [
                "database", "network", "authentication", "configuration", 
                "integration", "api", "performance"
            ],
            "low": [
                "single system", "local", "configuration", "restart", 
                "permission", "file", "simple"
            ]
        }
    
    async def decompose(self, problem: str, context: Dict[str, Any]) -> ProblemComponents:
        """Decompose complex problem into manageable components
        
        Args:
            problem: Complex problem description requiring decomposition
            context: Problem context including system info, error patterns, etc.
            
        Returns:
            ProblemComponents with primary issue, contributing factors,
            dependencies, complexity assessment, and priority ranking
            
        Raises:
            PlanningException: When problem decomposition fails
        """
        try:
            self._logger.info(f"Starting problem decomposition for: {problem[:100]}...")
            
            # Phase 1: Extract problem components using pattern matching
            identified_components = await self._identify_components(problem, context)
            
            # Phase 2: Analyze component relationships and dependencies
            relationships = await self._analyze_relationships(identified_components, context)
            
            # Phase 3: Determine primary issue
            primary_issue = await self._determine_primary_issue(identified_components, relationships, context)
            
            # Phase 4: Identify contributing factors
            contributing_factors = await self._identify_contributing_factors(
                identified_components, primary_issue, relationships
            )
            
            # Phase 5: Map dependencies
            dependencies = await self._map_dependencies(relationships)
            
            # Phase 6: Assess complexity
            complexity_assessment = await self._assess_complexity(
                identified_components, relationships, context
            )
            
            # Phase 7: Generate priority ranking
            priority_ranking = await self._generate_priority_ranking(
                identified_components, relationships, complexity_assessment
            )
            
            # Create final decomposition result
            result = ProblemComponents(
                primary_issue=primary_issue,
                contributing_factors=contributing_factors,
                dependencies=dependencies,
                complexity_assessment=complexity_assessment,
                priority_ranking=priority_ranking
            )
            
            self._logger.info(f"Problem decomposition completed: {len(contributing_factors)} factors identified")
            return result
            
        except Exception as e:
            self._logger.error(f"Problem decomposition failed: {e}")
            raise PlanningException(f"Failed to decompose problem: {str(e)}")
    
    async def _identify_components(self, problem: str, context: Dict[str, Any]) -> List[Dict[str, Any]]:
        """Identify problem components using pattern matching and LLM analysis"""
        components = []
        problem_lower = problem.lower()
        
        # Pattern-based component identification
        for component_type, keywords in self._component_patterns.items():
            matches = [kw for kw in keywords if kw in problem_lower]
            if matches:
                confidence = len(matches) / len(keywords)
                components.append({
                    "type": component_type,
                    "keywords": matches,
                    "confidence": min(confidence * 2, 1.0),  # Boost confidence for multiple matches
                    "source": "pattern_matching"
                })
        
        # Enhanced analysis with LLM if available
        if self._llm:
            try:
                llm_components = await self._llm_identify_components(problem, context)
                components.extend(llm_components)
            except Exception as e:
                self._logger.warning(f"LLM component identification failed: {e}")
        
        # Add context-based components
        context_components = self._extract_context_components(context)
        components.extend(context_components)
        
        # Remove duplicates and sort by confidence
        unique_components = self._deduplicate_components(components)
        return sorted(unique_components, key=lambda x: x["confidence"], reverse=True)
    
    async def _llm_identify_components(self, problem: str, context: Dict[str, Any]) -> List[Dict[str, Any]]:
        """Use LLM to identify additional problem components"""
        prompt = f"""
        Analyze this troubleshooting problem and identify key components:
        
        Problem: {problem}
        Context: {json.dumps(context, indent=2)}
        
        Identify problem components including:
        - Technical systems involved
        - Error types and symptoms
        - Performance issues
        - Configuration problems
        - Dependencies and integrations
        
        Return as JSON array:
        [
            {{
                "type": "component_type",
                "description": "component description", 
                "confidence": 0.8,
                "keywords": ["key", "words"],
                "source": "llm_analysis"
            }}
        ]
        """
        
        try:
            response = await self._llm.generate_response(prompt)
            
            # Extract JSON from response
            json_match = re.search(r'\[.*\]', response, re.DOTALL)
            if json_match:
                return json.loads(json_match.group())
        except (json.JSONDecodeError, AttributeError) as e:
            self._logger.warning(f"Failed to parse LLM component response: {e}")
        
        return []
    
    def _extract_context_components(self, context: Dict[str, Any]) -> List[Dict[str, Any]]:
        """Extract components from context information"""
        components = []
        
        # System information components
        if "system_info" in context:
            system_info = context["system_info"]
            if "cpu_usage" in system_info:
                try:
                    cpu_usage = float(system_info["cpu_usage"].rstrip('%'))
                    if cpu_usage > 80:
                        components.append({
                            "type": "performance",
                            "description": f"High CPU usage: {cpu_usage}%",
                            "confidence": 0.9,
                            "keywords": ["cpu", "high usage"],
                            "source": "context_analysis"
                        })
                except (ValueError, AttributeError):
                    pass
            
            if "memory" in system_info:
                components.append({
                    "type": "performance", 
                    "description": f"Memory usage: {system_info['memory']}",
                    "confidence": 0.7,
                    "keywords": ["memory"],
                    "source": "context_analysis"
                })
        
        # Error pattern components
        if "error_patterns" in context:
            for error in context["error_patterns"]:
                error_lower = str(error).lower()
                if "timeout" in error_lower:
                    components.append({
                        "type": "connectivity",
                        "description": f"Timeout error: {error}",
                        "confidence": 0.8,
                        "keywords": ["timeout"],
                        "source": "error_analysis"
                    })
                elif "memory" in error_lower:
                    components.append({
                        "type": "performance",
                        "description": f"Memory error: {error}",
                        "confidence": 0.9,
                        "keywords": ["memory", "error"],
                        "source": "error_analysis"
                    })
        
        return components
    
    def _deduplicate_components(self, components: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        """Remove duplicate components and merge similar ones"""
        # Group by type
        by_type = defaultdict(list)
        for comp in components:
            by_type[comp["type"]].append(comp)
        
        # Merge components of same type
        unique_components = []
        for comp_type, comps in by_type.items():
            if len(comps) == 1:
                unique_components.append(comps[0])
            else:
                # Merge multiple components of same type
                merged = {
                    "type": comp_type,
                    "description": f"Multiple {comp_type} issues detected",
                    "confidence": max(c["confidence"] for c in comps),
                    "keywords": list(set(kw for c in comps for kw in c.get("keywords", []))),
                    "source": "merged_analysis",
                    "merged_from": [c["source"] for c in comps]
                }
                unique_components.append(merged)
        
        return unique_components
    
    async def _analyze_relationships(
        self, 
        components: List[Dict[str, Any]], 
        context: Dict[str, Any]
    ) -> List[ComponentRelationship]:
        """Analyze relationships between problem components"""
        relationships = []
        
        # Define relationship rules
        relationship_rules = {
            ("performance", "connectivity"): ("contributes_to", 0.7),
            ("connectivity", "application"): ("depends_on", 0.8),
            ("authentication", "application"): ("depends_on", 0.9),
            ("data", "application"): ("depends_on", 0.8),
            ("infrastructure", "performance"): ("contributes_to", 0.7),
            ("infrastructure", "application"): ("depends_on", 0.6),
        }
        
        # Analyze pairwise relationships
        for i, comp1 in enumerate(components):
            for j, comp2 in enumerate(components):
                if i != j:
                    comp1_type = comp1["type"]
                    comp2_type = comp2["type"]
                    
                    # Check direct rules
                    if (comp1_type, comp2_type) in relationship_rules:
                        rel_type, strength = relationship_rules[(comp1_type, comp2_type)]
                        relationships.append(ComponentRelationship(
                            source=comp1_type,
                            target=comp2_type,
                            relationship_type=rel_type,
                            strength=strength
                        ))
                    
                    # Check reverse rules
                    elif (comp2_type, comp1_type) in relationship_rules:
                        rel_type, strength = relationship_rules[(comp2_type, comp1_type)]
                        relationships.append(ComponentRelationship(
                            source=comp2_type,
                            target=comp1_type,
                            relationship_type=rel_type,
                            strength=strength
                        ))
        
        return relationships
    
    async def _determine_primary_issue(
        self, 
        components: List[Dict[str, Any]], 
        relationships: List[ComponentRelationship],
        context: Dict[str, Any]
    ) -> str:
        """Determine the primary issue requiring immediate attention"""
        if not components:
            return "Unknown primary issue"
        
        # Score components based on confidence, urgency, and impact
        component_scores = {}
        
        for component in components:
            score = component["confidence"]
            
            # Boost score based on component type priority
            type_priority = {
                "data": 1.0,  # Data issues are often critical
                "authentication": 0.9,  # Auth issues block access
                "connectivity": 0.8,  # Network issues are foundational
                "application": 0.7,  # App issues are user-facing
                "performance": 0.6,  # Performance can often be optimized later
                "infrastructure": 0.5  # Infrastructure issues vary in urgency
            }
            score *= type_priority.get(component["type"], 0.5)
            
            # Boost score if component has many dependencies
            dependency_count = sum(1 for rel in relationships 
                                 if rel.target == component["type"] and rel.relationship_type == "depends_on")
            score += dependency_count * 0.1
            
            component_scores[component["type"]] = score
        
        # Find highest scoring component
        primary_type = max(component_scores, key=component_scores.get)
        primary_component = next(c for c in components if c["type"] == primary_type)
        
        return primary_component.get("description", f"{primary_type.title()} issue requiring immediate attention")
    
    async def _identify_contributing_factors(
        self,
        components: List[Dict[str, Any]],
        primary_issue: str,
        relationships: List[ComponentRelationship]
    ) -> List[str]:
        """Identify contributing factors to the primary issue"""
        # Find primary issue type
        primary_type = None
        for component in components:
            if component.get("description") == primary_issue or primary_issue.lower().startswith(component["type"]):
                primary_type = component["type"]
                break
        
        if not primary_type:
            return [c.get("description", f"{c['type']} issue") for c in components[1:4]]  # Top 3 non-primary
        
        # Find components that contribute to the primary issue
        contributing_factors = []
        
        for relationship in relationships:
            if (relationship.target == primary_type and 
                relationship.relationship_type in ["contributes_to", "depends_on"]):
                
                # Find the source component
                source_component = next(
                    (c for c in components if c["type"] == relationship.source), 
                    None
                )
                if source_component:
                    description = source_component.get("description", f"{relationship.source} issue")
                    contributing_factors.append(description)
        
        # Add high-confidence components not yet included
        for component in components:
            if (component["type"] != primary_type and 
                component["confidence"] > 0.7 and
                component.get("description") not in contributing_factors):
                contributing_factors.append(component.get("description", f"{component['type']} issue"))
        
        return contributing_factors[:5]  # Limit to top 5 contributing factors
    
    async def _map_dependencies(self, relationships: List[ComponentRelationship]) -> List[str]:
        """Map dependencies between components"""
        dependencies = []
        
        # Group relationships by type
        dependency_relationships = [rel for rel in relationships if rel.relationship_type == "depends_on"]
        
        for rel in dependency_relationships:
            if rel.strength > 0.6:  # Only include strong dependencies
                dependency_desc = f"{rel.target} depends on {rel.source}"
                dependencies.append(dependency_desc)
        
        return dependencies
    
    async def _assess_complexity(
        self,
        components: List[Dict[str, Any]],
        relationships: List[ComponentRelationship],
        context: Dict[str, Any]
    ) -> Dict[str, Any]:
        """Assess problem complexity and resource requirements"""
        complexity_score = 0.0
        complexity_factors = []
        
        # Base complexity from number of components
        component_count = len(components)
        complexity_score += min(component_count * 0.2, 1.0)
        
        if component_count > 3:
            complexity_factors.append(f"Multiple components involved ({component_count})")
        
        # Complexity from relationships
        relationship_count = len(relationships)
        complexity_score += min(relationship_count * 0.1, 0.5)
        
        if relationship_count > 2:
            complexity_factors.append(f"Complex dependencies ({relationship_count} relationships)")
        
        # Complexity from indicators in problem description
        context_text = json.dumps(context).lower()
        for complexity_level, indicators in self._complexity_indicators.items():
            matches = [ind for ind in indicators if ind in context_text]
            if matches:
                if complexity_level == "high":
                    complexity_score += 0.6
                    complexity_factors.extend(matches)
                elif complexity_level == "medium":
                    complexity_score += 0.3
                elif complexity_level == "low":
                    complexity_score -= 0.2
        
        # Determine complexity level
        if complexity_score > 0.8:
            level = "high"
            estimated_effort = "4-8 hours"
            required_skills = ["expert", "cross-functional team"]
        elif complexity_score > 0.5:
            level = "medium"
            estimated_effort = "1-4 hours"
            required_skills = ["intermediate", "domain expert"]
        else:
            level = "low"
            estimated_effort = "30 minutes - 1 hour"
            required_skills = ["basic", "following procedures"]
        
        return {
            "level": level,
            "score": min(complexity_score, 1.0),
            "factors": complexity_factors[:5],  # Top 5 factors
            "estimated_effort": estimated_effort,
            "required_skills": required_skills,
            "resource_requirements": {
                "time": estimated_effort,
                "expertise": required_skills,
                "tools": self._recommend_tools(components)
            }
        }
    
    def _recommend_tools(self, components: List[Dict[str, Any]]) -> List[str]:
        """Recommend tools based on component types"""
        tool_recommendations = {
            "performance": ["performance monitoring", "profiling tools", "system metrics"],
            "connectivity": ["network tools", "ping", "telnet", "nslookup", "traceroute"],
            "authentication": ["auth logs", "credential validation", "permission tools"],
            "data": ["database tools", "query analyzer", "backup tools"],
            "application": ["application logs", "debugging tools", "configuration tools"],
            "infrastructure": ["system monitoring", "hardware diagnostics", "resource tools"]
        }
        
        recommended_tools = set()
        for component in components:
            component_type = component["type"]
            if component_type in tool_recommendations:
                recommended_tools.update(tool_recommendations[component_type])
        
        return list(recommended_tools)[:5]  # Top 5 tool recommendations
    
    async def _generate_priority_ranking(
        self,
        components: List[Dict[str, Any]],
        relationships: List[ComponentRelationship],
        complexity_assessment: Dict[str, Any]
    ) -> List[str]:
        """Generate priority ranking for systematic resolution"""
        # Create priority scores for each component
        priority_scores = {}
        
        for component in components:
            comp_type = component["type"]
            score = component["confidence"]
            
            # Impact-based scoring
            impact_weights = {
                "data": 1.0,  # Data issues have highest impact
                "authentication": 0.9,  # Auth blocks user access
                "connectivity": 0.8,  # Network affects everything downstream
                "application": 0.7,  # App issues are user-visible
                "performance": 0.6,  # Performance can be optimized later
                "infrastructure": 0.5  # Infrastructure varies
            }
            score *= impact_weights.get(comp_type, 0.5)
            
            # Dependency-based scoring (address dependencies first)
            is_dependency = any(rel.source == comp_type and rel.relationship_type == "depends_on" 
                              for rel in relationships)
            if is_dependency:
                score += 0.3
            
            # Effort-based scoring (prefer easier wins when impact is similar)
            if complexity_assessment["level"] == "low":
                score += 0.2
            elif complexity_assessment["level"] == "high":
                score -= 0.1
            
            priority_scores[comp_type] = score
        
        # Sort components by priority score
        sorted_components = sorted(priority_scores.items(), key=lambda x: x[1], reverse=True)
        
        # Generate human-readable priority ranking
        priority_ranking = []
        for i, (comp_type, score) in enumerate(sorted_components):
            component = next(c for c in components if c["type"] == comp_type)
            description = component.get("description", f"{comp_type} issue")
            priority_ranking.append(f"{i+1}. {description}")
        
        return priority_ranking