"""Agent Data Contracts for FaultMaven Microservice Architecture

This module defines the data contracts for communication with specialist agents
in the microservice blueprint. These contracts standardize agent interactions
and support both in-process and distributed deployment modes.

Design Principles:
- Standardized agent request/response patterns
- Budget tracking and resource management
- Rich result metadata for observability
- Extensible execution context for constraints
- Comprehensive error handling and status tracking
"""

from datetime import datetime
from typing import Any, Dict, List, Optional, Union
from enum import Enum
from pydantic import BaseModel, Field, validator
from uuid import uuid4

from .core_contracts import Budget


class AgentType(str, Enum):
    """Types of specialist agents."""
    TRIAGE = "triage"
    SCOPING = "scoping"
    DIAGNOSTIC = "diagnostic"
    VALIDATION = "validation"
    PATTERN = "pattern"
    LEARNING = "learning"


class AgentStatus(str, Enum):
    """Agent execution status."""
    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    TIMEOUT = "timeout"
    CANCELLED = "cancelled"


class Severity(str, Enum):
    """Problem severity levels."""
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    CRITICAL = "critical"


class Urgency(str, Enum):
    """Problem urgency levels."""
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    EMERGENCY = "emergency"


class UserSkillLevel(str, Enum):
    """User skill level assessment."""
    BEGINNER = "beginner"
    INTERMEDIATE = "intermediate"
    ADVANCED = "advanced"
    EXPERT = "expert"


class ProblemCategory(str, Enum):
    """Problem categorization from triage."""
    ABSURD = "absurd"      # Clearly invalid or nonsensical
    VAGUE = "vague"        # Insufficient information
    SPECIFIC = "specific"  # Clear problem with actionable details
    COMPLEX = "complex"    # Multi-faceted issues requiring decomposition


class ExecutionContext(BaseModel):
    """Execution context and constraints for agent operations."""
    execution_id: str = Field(
        default_factory=lambda: str(uuid4()),
        description="Unique execution identifier"
    )
    environment: str = Field(
        default="production",
        description="Execution environment (dev/staging/production)"
    )
    
    # Safety constraints
    allow_state_changes: bool = Field(
        default=False,
        description="Whether state-changing operations are allowed"
    )
    require_confirmations: bool = Field(
        default=True,
        description="Whether user confirmations are required for risky actions"
    )
    max_parallel_operations: int = Field(
        default=3,
        ge=1,
        le=10,
        description="Maximum parallel operations allowed"
    )
    
    # Resource constraints
    timeout_ms: int = Field(
        default=30000,
        ge=1000,
        le=300000,
        description="Maximum execution timeout in milliseconds"
    )
    memory_limit_mb: Optional[int] = Field(
        default=None,
        description="Memory limit for agent execution"
    )
    
    # User context
    user_skill_level: UserSkillLevel = Field(
        default=UserSkillLevel.INTERMEDIATE,
        description="User's assessed skill level"
    )
    user_preferences: Dict[str, Any] = Field(
        default_factory=dict,
        description="User preferences and settings"
    )
    
    # Execution metadata
    priority: str = Field(
        default="normal",
        description="Execution priority level"
    )
    correlation_id: Optional[str] = Field(
        default=None,
        description="Correlation ID for distributed tracing"
    )


class AgentRequest(BaseModel):
    """Standardized request for all specialist agents."""
    agent_type: AgentType = Field(description="Type of agent being invoked")
    request_id: str = Field(
        default_factory=lambda: str(uuid4()),
        description="Unique request identifier"
    )
    session_id: str = Field(description="Session identifier")
    turn_id: str = Field(description="Turn identifier")
    
    # Core request data
    query: str = Field(
        min_length=1,
        max_length=10000,
        description="User query or problem description"
    )
    context: Dict[str, Any] = Field(
        default_factory=dict,
        description="Additional context and metadata"
    )
    
    # Resource management
    budget: Budget = Field(
        default_factory=Budget,
        description="Resource budget for this request"
    )
    execution_context: ExecutionContext = Field(
        default_factory=ExecutionContext,
        description="Execution context and constraints"
    )
    
    # Previous results for context
    previous_results: List[Dict[str, Any]] = Field(
        default_factory=list,
        description="Results from previous agents in this turn"
    )
    
    # Timestamps
    created_at: datetime = Field(
        default_factory=datetime.utcnow,
        description="Request creation timestamp"
    )
    
    # Agent-specific parameters
    agent_parameters: Dict[str, Any] = Field(
        default_factory=dict,
        description="Agent-specific parameters and options"
    )


class AgentResponse(BaseModel):
    """Standardized response from all specialist agents."""
    request_id: str = Field(description="Original request identifier")
    agent_type: AgentType = Field(description="Type of agent that responded")
    status: AgentStatus = Field(description="Execution status")
    
    # Core response data
    result: Dict[str, Any] = Field(
        description="Agent-specific result data"
    )
    confidence: float = Field(
        ge=0.0,
        le=1.0,
        description="Confidence in the result"
    )
    
    # Resource consumption
    budget_used: Budget = Field(description="Actual resource consumption")
    
    # Performance metrics
    execution_time_ms: int = Field(
        ge=0,
        description="Total execution time in milliseconds"
    )
    
    # Quality indicators
    result_quality: Dict[str, Any] = Field(
        default_factory=dict,
        description="Quality metrics for the result"
    )
    
    # Error handling
    errors: List[Dict[str, Any]] = Field(
        default_factory=list,
        description="Any errors encountered during execution"
    )
    warnings: List[str] = Field(
        default_factory=list,
        description="Non-fatal warnings generated during execution"
    )
    
    # Recommendations for next steps
    next_steps: List[str] = Field(
        default_factory=list,
        description="Recommended next steps or agents"
    )
    
    # Timestamps
    started_at: datetime = Field(
        default_factory=datetime.utcnow,
        description="Execution start timestamp"
    )
    completed_at: Optional[datetime] = Field(
        default=None,
        description="Execution completion timestamp"
    )
    
    # Metadata
    agent_version: str = Field(
        default="1.0",
        description="Agent version that produced this result"
    )
    metadata: Dict[str, Any] = Field(
        default_factory=dict,
        description="Additional metadata"
    )


class TriageResult(BaseModel):
    """Result from triage agent analysis."""
    category: ProblemCategory = Field(description="Problem categorization")
    severity: Severity = Field(description="Problem severity assessment")
    urgency: Urgency = Field(description="Problem urgency assessment")
    
    # User assessment
    user_skill_estimate: UserSkillLevel = Field(
        description="Estimated user skill level"
    )
    user_assessment_confidence: float = Field(
        ge=0.0,
        le=1.0,
        description="Confidence in user skill assessment"
    )
    
    # Routing recommendations
    routing_recommendations: List[str] = Field(
        description="Recommended next agents for routing"
    )
    routing_priorities: Dict[str, float] = Field(
        default_factory=dict,
        description="Priority scores for each recommended agent"
    )
    
    # Analysis details
    categorization_reasoning: str = Field(
        description="Explanation of categorization decision"
    )
    severity_factors: List[str] = Field(
        default_factory=list,
        description="Factors contributing to severity assessment"
    )
    urgency_factors: List[str] = Field(
        default_factory=list,
        description="Factors contributing to urgency assessment"
    )
    
    # Effort estimation
    estimated_resolution_time: Optional[str] = Field(
        default=None,
        description="Estimated time to resolution"
    )
    complexity_assessment: Dict[str, Any] = Field(
        default_factory=dict,
        description="Technical complexity assessment"
    )
    
    # Quality metrics
    classification_confidence: float = Field(
        ge=0.0,
        le=1.0,
        description="Overall classification confidence"
    )


class ScopingResult(BaseModel):
    """Result from scoping agent analysis."""
    questions: List[str] = Field(
        max_items=2,
        description="Clarifying questions (max 2 per turn)"
    )
    question_rationale: List[str] = Field(
        description="Rationale for each question"
    )
    
    # Information gaps
    information_gaps: List[str] = Field(
        description="Identified missing information categories"
    )
    gap_priorities: Dict[str, float] = Field(
        default_factory=dict,
        description="Priority scores for each information gap"
    )
    
    # Scope assessment
    scope_clarity_score: float = Field(
        ge=0.0,
        le=1.0,
        description="Current scope clarity assessment"
    )
    actionability_score: float = Field(
        ge=0.0,
        le=1.0,
        description="Whether enough info exists to proceed"
    )
    
    # Question metadata
    question_types: List[str] = Field(
        default_factory=list,
        description="Types of questions asked (temporal, technical, etc.)"
    )
    expected_clarification: List[str] = Field(
        default_factory=list,
        description="Expected information from each question"
    )
    
    # Template information
    templates_used: List[str] = Field(
        default_factory=list,
        description="Question templates that were utilized"
    )
    
    # Quality metrics
    question_effectiveness_estimate: float = Field(
        ge=0.0,
        le=1.0,
        description="Estimated effectiveness of generated questions"
    )


class DiagnosticResult(BaseModel):
    """Result from diagnostic agent analysis."""
    hypotheses: List[Dict[str, Any]] = Field(
        description="Ranked list of root cause hypotheses"
    )
    hypothesis_scores: List[float] = Field(
        description="Likelihood scores for each hypothesis"
    )
    
    # Test execution results
    test_results: List[Dict[str, Any]] = Field(
        default_factory=list,
        description="Results from executed diagnostic tests"
    )
    test_execution_summary: Dict[str, Any] = Field(
        default_factory=dict,
        description="Summary of test execution"
    )
    
    # Validated hypotheses
    validated_hypotheses: List[Dict[str, Any]] = Field(
        default_factory=list,
        description="Hypotheses validated by test results"
    )
    invalidated_hypotheses: List[Dict[str, Any]] = Field(
        default_factory=list,
        description="Hypotheses invalidated by test results"
    )
    
    # Evidence correlation
    supporting_evidence: Dict[str, List[str]] = Field(
        default_factory=dict,
        description="Evidence supporting each hypothesis"
    )
    contradictory_evidence: Dict[str, List[str]] = Field(
        default_factory=dict,
        description="Evidence contradicting hypotheses"
    )
    
    # Execution details
    parallel_execution_plan: Dict[str, Any] = Field(
        default_factory=dict,
        description="Plan for parallel test execution"
    )
    cancelled_tests: List[str] = Field(
        default_factory=list,
        description="Tests that were cancelled due to budget/time"
    )
    
    # Safety assessment
    risk_assessments: Dict[str, str] = Field(
        default_factory=dict,
        description="Risk assessment for each test"
    )
    
    # Quality metrics
    diagnostic_confidence: float = Field(
        ge=0.0,
        le=1.0,
        description="Overall diagnostic confidence"
    )
    hypothesis_accuracy_estimate: float = Field(
        ge=0.0,
        le=1.0,
        description="Estimated accuracy of top hypothesis"
    )


class ValidationResult(BaseModel):
    """Result from validation agent analysis."""
    validation_status: str = Field(description="Overall validation result")
    overall_confidence: float = Field(
        ge=0.0,
        le=1.0,
        description="Overall validation confidence"
    )
    
    # Individual validation results
    validated_items: List[Dict[str, Any]] = Field(
        description="Individual validation results for each item"
    )
    validation_details: Dict[str, Any] = Field(
        default_factory=dict,
        description="Detailed validation analysis"
    )
    
    # Risk assessment
    risk_assessment: Dict[str, Any] = Field(
        description="Comprehensive risk analysis"
    )
    risk_mitigation: List[str] = Field(
        default_factory=list,
        description="Risk mitigation recommendations"
    )
    
    # Compliance checking
    compliance_results: Dict[str, bool] = Field(
        default_factory=dict,
        description="Results of compliance validation"
    )
    best_practice_alignment: float = Field(
        ge=0.0,
        le=1.0,
        description="Alignment with best practices score"
    )
    
    # Recommendations
    recommendations: List[str] = Field(
        default_factory=list,
        description="Validation-based recommendations"
    )
    required_modifications: List[str] = Field(
        default_factory=list,
        description="Required modifications before proceeding"
    )
    additional_precautions: List[str] = Field(
        default_factory=list,
        description="Additional precautions to consider"
    )
    
    # Cross-reference results
    similar_cases: List[Dict[str, Any]] = Field(
        default_factory=list,
        description="Similar cases from knowledge base"
    )
    historical_outcomes: Dict[str, Any] = Field(
        default_factory=dict,
        description="Historical outcomes for similar validations"
    )
    
    # Quality metrics
    validation_accuracy: float = Field(
        ge=0.0,
        le=1.0,
        description="Estimated validation accuracy"
    )


class PatternResult(BaseModel):
    """Result from pattern agent analysis."""
    pattern_matches: List[Dict[str, Any]] = Field(
        description="Ranked list of matching patterns"
    )
    match_scores: List[float] = Field(
        description="Pattern match confidence scores"
    )
    
    # Success rate information
    success_rates: Dict[str, float] = Field(
        description="Historical success rates for each pattern"
    )
    effectiveness_data: Dict[str, Dict[str, Any]] = Field(
        default_factory=dict,
        description="Detailed effectiveness metrics for patterns"
    )
    
    # Context similarity
    context_similarity: Dict[str, float] = Field(
        default_factory=dict,
        description="How well context matches historical cases"
    )
    user_match_quality: Dict[str, float] = Field(
        default_factory=dict,
        description="How well user profile matches historical users"
    )
    
    # Pattern metadata
    pattern_versions: Dict[str, str] = Field(
        default_factory=dict,
        description="Version information for matched patterns"
    )
    pattern_categories: Dict[str, List[str]] = Field(
        default_factory=dict,
        description="Categories for each matched pattern"
    )
    
    # Recommendations
    recommended_patterns: List[str] = Field(
        description="Top recommended patterns to follow"
    )
    anti_patterns: List[str] = Field(
        default_factory=list,
        description="Patterns to avoid based on history"
    )
    
    # Analytics
    pattern_popularity: Dict[str, int] = Field(
        default_factory=dict,
        description="Usage frequency of each pattern"
    )
    recent_success_trends: Dict[str, float] = Field(
        default_factory=dict,
        description="Recent success rate trends for patterns"
    )
    
    # Quality metrics
    pattern_match_confidence: float = Field(
        ge=0.0,
        le=1.0,
        description="Overall pattern matching confidence"
    )


class LearningResult(BaseModel):
    """Result from learning agent batch processing."""
    batch_id: str = Field(
        default_factory=lambda: str(uuid4()),
        description="Batch processing identifier"
    )
    processed_outcomes: int = Field(
        ge=0,
        description="Number of outcomes processed in this batch"
    )
    
    # Learning outcomes
    learned_patterns: List[Dict[str, Any]] = Field(
        default_factory=list,
        description="New patterns identified from outcomes"
    )
    pattern_updates: List[Dict[str, Any]] = Field(
        default_factory=list,
        description="Updates to existing pattern success rates"
    )
    knowledge_updates: List[Dict[str, Any]] = Field(
        default_factory=list,
        description="Proposed knowledge base updates"
    )
    
    # Model improvements
    confidence_improvements: Dict[str, float] = Field(
        default_factory=dict,
        description="Improvements to confidence calibration"
    )
    accuracy_gains: Dict[str, float] = Field(
        default_factory=dict,
        description="Accuracy improvements by category"
    )
    
    # Governance status
    governance_status: Dict[str, str] = Field(
        default_factory=dict,
        description="Approval status for proposed updates"
    )
    pending_approvals: List[str] = Field(
        default_factory=list,
        description="Updates pending human approval"
    )
    
    # Deployment planning
    deployment_plan: Dict[str, Any] = Field(
        default_factory=dict,
        description="Staged deployment plan for approved updates"
    )
    rollout_schedule: Optional[List[Dict[str, Any]]] = Field(
        default=None,
        description="Scheduled rollout phases"
    )
    
    # Quality metrics
    learning_confidence: float = Field(
        ge=0.0,
        le=1.0,
        description="Confidence in learned patterns"
    )
    validation_accuracy: float = Field(
        ge=0.0,
        le=1.0,
        description="Accuracy on validation dataset"
    )
    
    # Processing metadata
    processing_time_ms: int = Field(
        ge=0,
        description="Total batch processing time"
    )
    memory_peak_mb: Optional[int] = Field(
        default=None,
        description="Peak memory usage during processing"
    )
    
    # Error tracking
    failed_outcomes: int = Field(
        default=0,
        description="Number of outcomes that failed processing"
    )
    processing_errors: List[str] = Field(
        default_factory=list,
        description="Errors encountered during processing"
    )