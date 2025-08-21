"""Agent Contracts for FaultMaven Microservice Architecture

Data transfer objects for agent-specific operations and specialist agents.
"""

from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Union
from datetime import datetime


@dataclass
class Budget:
    """Resource budget for agent operations"""
    max_tokens: int
    max_time_seconds: int
    max_api_calls: int
    max_tools: int


@dataclass
class ExecutionContext:
    """Context for agent execution"""
    context_id: str
    session_id: str
    turn_id: str
    budget: Budget
    metadata: Dict[str, Any]


@dataclass
class AgentRequest:
    """Request to an agent"""
    request_id: str
    agent_type: str
    input_data: Dict[str, Any]
    context: ExecutionContext
    requirements: Dict[str, Any]


@dataclass
class AgentResponse:
    """Response from an agent"""
    request_id: str
    agent_type: str
    success: bool
    result: Dict[str, Any]
    confidence: float
    budget_used: Budget
    execution_time: float
    error_message: Optional[str] = None


@dataclass
class TriageResult:
    """Result from triage agent"""
    triage_id: str
    priority: str
    category: str
    urgency_score: float
    recommended_path: str
    reasoning: str


@dataclass
class ScopingResult:
    """Result from scoping agent"""
    scope_id: str
    blast_radius: str
    affected_systems: List[str]
    impact_level: str
    scope_confidence: float


@dataclass
class DiagnosticResult:
    """Result from diagnostic agent"""
    diagnostic_id: str
    findings: List[Dict[str, Any]]
    probable_causes: List[str]
    evidence: List[Dict[str, Any]]
    confidence: float


@dataclass
class ValidationResult:
    """Result from validation agent"""
    validation_id: str
    validated: bool
    validation_score: float
    issues_found: List[str]
    recommendations: List[str]


@dataclass
class PatternResult:
    """Result from pattern analysis agent"""
    pattern_id: str
    patterns_found: List[Dict[str, Any]]
    anomalies: List[Dict[str, Any]]
    pattern_confidence: float


@dataclass
class LearningResult:
    """Result from learning agent"""
    learning_id: str
    insights: List[str]
    updated_patterns: List[Dict[str, Any]]
    confidence_improvement: float