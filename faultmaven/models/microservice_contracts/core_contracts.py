"""Core Data Contracts for FaultMaven Microservice Architecture

This module defines the core data contracts used for communication between
the 7 core services in the microservice blueprint. These contracts ensure
consistent data exchange and schema validation across service boundaries.

Design Principles:
- Comprehensive field validation with Pydantic
- Optional fields for backward compatibility
- Rich metadata for observability and debugging
- Budget tracking and resource management
- Standardized error handling patterns
"""

from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Union
from enum import Enum
from pydantic import BaseModel, Field, validator
from uuid import uuid4


class ConfidenceBand(str, Enum):
    """Confidence band classification for decision making."""
    LOW = "low"          # < 0.5: Request clarification or gather evidence
    GRAY = "gray"        # 0.5-0.8: Propose solution with caveats
    HIGH = "high"        # ≥ 0.8: Propose solution confidently  
    APPLY = "apply"      # ≥ 0.9: Apply with user confirmation
    RESOLVED = "resolved" # ≥ 0.95: Mark issue as resolved after verification


class ActionType(str, Enum):
    """Types of actions for policy evaluation."""
    COMMAND_EXECUTION = "command_execution"
    DATA_MODIFICATION = "data_modification"
    NETWORK_CHANGE = "network_change"
    PERMISSION_CHANGE = "permission_change"
    SERVICE_RESTART = "service_restart"
    CONFIGURATION_CHANGE = "configuration_change"


class RiskLevel(str, Enum):
    """Risk levels for action evaluation."""
    LOW = "low"          # Informational, read-only operations
    MEDIUM = "medium"    # Configuration changes, non-critical restarts
    HIGH = "high"        # Data modifications, service restarts
    CRITICAL = "critical" # Data deletion, security changes, production changes


class LoopStatus(str, Enum):
    """Loop detection status."""
    NONE = "none"        # No loop detected
    WARNING = "warning"  # Potential loop pattern emerging
    DETECTED = "detected" # Loop confirmed, recovery needed
    RECOVERING = "recovering" # Recovery action in progress


class RecoveryAction(str, Enum):
    """Recovery actions for loop resolution."""
    REFRAME = "reframe"   # Suggest alternative problem framing
    PIVOT = "pivot"       # Switch to different troubleshooting approach
    META = "meta"         # Ask about the troubleshooting process itself
    ESCALATE = "escalate" # Recommend human expert involvement


class Budget(BaseModel):
    """Resource budget for operations with tracking and enforcement."""
    time_ms: int = Field(
        default=1200,
        ge=100,
        le=300000,
        description="Maximum execution time in milliseconds"
    )
    token_budget: int = Field(
        default=1500,
        ge=100,
        le=32000,
        description="Maximum tokens for LLM operations"
    )
    call_budget: int = Field(
        default=6,
        ge=1,
        le=50,
        description="Maximum external service calls"
    )
    
    # Usage tracking
    time_used: int = Field(default=0, description="Time consumed so far")
    tokens_used: int = Field(default=0, description="Tokens consumed so far")  
    calls_used: int = Field(default=0, description="Calls made so far")
    
    def has_time_budget(self, required_ms: int) -> bool:
        """Check if sufficient time budget remains."""
        return (self.time_used + required_ms) <= self.time_ms
    
    def has_token_budget(self, required_tokens: int) -> bool:
        """Check if sufficient token budget remains."""
        return (self.tokens_used + required_tokens) <= self.token_budget
    
    def has_call_budget(self, required_calls: int = 1) -> bool:
        """Check if sufficient call budget remains."""
        return (self.calls_used + required_calls) <= self.call_budget
    
    def consume_time(self, consumed_ms: int) -> None:
        """Record time consumption."""
        self.time_used = min(self.time_used + consumed_ms, self.time_ms)
    
    def consume_tokens(self, consumed_tokens: int) -> None:
        """Record token consumption."""
        self.tokens_used = min(self.tokens_used + consumed_tokens, self.token_budget)
    
    def consume_calls(self, consumed_calls: int = 1) -> None:
        """Record call consumption."""
        self.calls_used = min(self.calls_used + consumed_calls, self.call_budget)
    
    @property
    def is_exhausted(self) -> bool:
        """Check if any budget is exhausted."""
        return (
            self.time_used >= self.time_ms or
            self.tokens_used >= self.token_budget or
            self.calls_used >= self.call_budget
        )


class Evidence(BaseModel):
    """Evidence from retrieval sources with full provenance."""
    source: str = Field(description="Source identifier (e.g., 'KB#KI-001')")
    source_type: str = Field(description="Source type (kb, pattern, playbook)")
    snippet: str = Field(description="Relevant text snippet or summary")
    score: float = Field(
        ge=0.0,
        le=1.0,
        description="Relevance score (0.0-1.0)"
    )
    url: Optional[str] = Field(
        default=None,
        description="Optional URL for full content"
    )
    timestamp: datetime = Field(
        default_factory=lambda: datetime.now(timezone.utc),
        description="Evidence retrieval timestamp"
    )
    provenance: Dict[str, Any] = Field(
        default_factory=dict,
        description="Provenance metadata (adapter, version, etc.)"
    )
    
    # Additional metadata for explainability
    rank: Optional[int] = Field(default=None, description="Ranking position")
    confidence: Optional[float] = Field(default=None, description="Source confidence")
    recency_boost: Optional[float] = Field(default=None, description="Recency adjustment")


class TurnContext(BaseModel):
    """Router input containing query, budget, and execution context."""
    session_id: str = Field(description="Session identifier")
    turn_id: str = Field(
        default_factory=lambda: str(uuid4()),
        description="Unique turn identifier"
    )
    query: str = Field(
        min_length=1,
        max_length=10000,
        description="User query or problem description"
    )
    context: Dict[str, Any] = Field(
        default_factory=dict,
        description="Additional context (environment, user profile, etc.)"
    )
    budget: Budget = Field(
        default_factory=Budget,
        description="Resource budget for this turn"
    )
    
    # Metadata for routing and processing
    user_id: Optional[str] = Field(default=None, description="User identifier")
    priority: str = Field(default="normal", description="Priority level")
    metadata: Dict[str, Any] = Field(
        default_factory=dict,
        description="Additional metadata"
    )
    
    # Conversation context
    conversation_history: List[Dict[str, Any]] = Field(
        default_factory=list,
        description="Recent conversation turns for context"
    )
    
    @validator('priority')
    def validate_priority(cls, v):
        """Validate priority value."""
        valid_priorities = ['low', 'normal', 'high', 'urgent']
        if v not in valid_priorities:
            raise ValueError(f"Priority must be one of {valid_priorities}")
        return v


class DecisionRecord(BaseModel):
    """Orchestrator decision record for observability and audit."""
    record_id: str = Field(
        default_factory=lambda: str(uuid4()),
        description="Unique decision record identifier"
    )
    session_id: str = Field(description="Session identifier")
    turn_id: str = Field(description="Turn identifier")
    turn: int = Field(ge=1, description="Turn number in session")
    
    # Decision details
    selected_agents: List[str] = Field(
        description="Agents selected for this turn"
    )
    routing_rationale: str = Field(
        description="Explanation of agent selection"
    )
    
    # Feature vector for confidence scoring
    features: Dict[str, float] = Field(
        default_factory=dict,
        description="Features used for confidence calculation"
    )
    
    # Confidence information
    confidence: Dict[str, Any] = Field(
        default_factory=dict,
        description="Confidence scoring results"
    )
    
    # Budget tracking
    budget_allocated: Budget = Field(description="Budget allocated for turn")
    budget_used: Budget = Field(description="Actual budget consumption")
    
    # Performance metrics
    latency_ms: int = Field(ge=0, description="Total turn processing time")
    agent_latencies: Dict[str, int] = Field(
        default_factory=dict,
        description="Per-agent execution times"
    )
    
    # Results and outcomes
    agent_results: Dict[str, Any] = Field(
        default_factory=dict,
        description="Results from each selected agent"
    )
    final_response: str = Field(description="Final response to user")
    
    # Status and error handling
    status: str = Field(default="completed", description="Turn completion status")
    errors: List[Dict[str, Any]] = Field(
        default_factory=list,
        description="Any errors encountered during processing"
    )
    
    # Timestamps
    started_at: datetime = Field(
        default_factory=lambda: datetime.now(timezone.utc),
        description="Turn start timestamp"
    )
    completed_at: Optional[datetime] = Field(
        default=None,
        description="Turn completion timestamp"
    )
    
    @validator('status')
    def validate_status(cls, v):
        """Validate status value."""
        valid_statuses = ['started', 'processing', 'completed', 'failed', 'timeout']
        if v not in valid_statuses:
            raise ValueError(f"Status must be one of {valid_statuses}")
        return v


class RetrievalRequest(BaseModel):
    """Request for unified retrieval service."""
    query: str = Field(
        min_length=1,
        max_length=1000,
        description="Search query text"
    )
    context: List[str] = Field(
        default_factory=list,
        description="Additional context for query understanding"
    )
    filters: Dict[str, Any] = Field(
        default_factory=dict,
        description="Filtering criteria (product, category, etc.)"
    )
    max_results: int = Field(
        default=8,
        ge=1,
        le=50,
        description="Maximum number of results to return"
    )
    
    # Source preferences
    enabled_sources: List[str] = Field(
        default=['kb', 'pattern', 'playbook'],
        description="Enabled retrieval sources"
    )
    source_weights: Dict[str, float] = Field(
        default_factory=dict,
        description="Source-specific weights for ranking"
    )
    
    # Search options
    include_recency_bias: bool = Field(
        default=True,
        description="Whether to apply recency bias to results"
    )
    semantic_similarity_threshold: float = Field(
        default=0.5,
        ge=0.0,
        le=1.0,
        description="Minimum similarity threshold"
    )
    
    @validator('enabled_sources')
    def validate_sources(cls, v):
        """Validate enabled sources."""
        valid_sources = ['kb', 'pattern', 'playbook']
        for source in v:
            if source not in valid_sources:
                raise ValueError(f"Source '{source}' not in {valid_sources}")
        return v


class RetrievalResponse(BaseModel):
    """Response from unified retrieval service."""
    evidence: List[Evidence] = Field(
        description="List of evidence items ranked by relevance"
    )
    total_found: int = Field(
        ge=0,
        description="Total number of matches found (before limiting)"
    )
    
    # Performance metrics
    elapsed_ms: int = Field(
        ge=0,
        description="Total retrieval time in milliseconds"
    )
    source_latencies: Dict[str, int] = Field(
        default_factory=dict,
        description="Per-source retrieval times"
    )
    
    # Cache information
    cache_hit: bool = Field(
        default=False,
        description="Whether result was served from cache"
    )
    cache_key: Optional[str] = Field(
        default=None,
        description="Cache key for result caching"
    )
    
    # Quality metrics
    avg_relevance_score: float = Field(
        default=0.0,
        ge=0.0,
        le=1.0,
        description="Average relevance score of returned evidence"
    )
    source_distribution: Dict[str, int] = Field(
        default_factory=dict,
        description="Distribution of results across sources"
    )


class ConfidenceRequest(BaseModel):
    """Request for global confidence scoring."""
    features: Dict[str, float] = Field(
        description="Feature vector for confidence calculation"
    )
    context: Dict[str, Any] = Field(
        default_factory=dict,
        description="Additional context for scoring"
    )
    
    # Feature definitions (v1)
    # retrieval_score: float         # Quality of knowledge base matches (0.0-1.0)
    # provider_confidence: float     # LLM provider confidence score (0.0-1.0)  
    # hypothesis_score: float        # Strength of diagnostic hypothesis (0.0-1.0)
    # validation_result: float       # Validation agent assessment (0.0-1.0)
    # pattern_boost: float           # Pattern matching confidence bonus (0.0-0.2)
    # history_slope: float           # Confidence trend over last 3 turns (-1.0-1.0)
    
    @validator('features')
    def validate_features(cls, v):
        """Validate feature values are in expected ranges."""
        expected_features = {
            'retrieval_score': (0.0, 1.0),
            'provider_confidence': (0.0, 1.0),
            'hypothesis_score': (0.0, 1.0),
            'validation_result': (0.0, 1.0),
            'pattern_boost': (0.0, 0.2),
            'history_slope': (-1.0, 1.0)
        }
        
        for feature, value in v.items():
            if feature in expected_features:
                min_val, max_val = expected_features[feature]
                if not (min_val <= value <= max_val):
                    raise ValueError(
                        f"Feature '{feature}' value {value} not in range [{min_val}, {max_val}]"
                    )
        
        return v


class ConfidenceResponse(BaseModel):
    """Response from global confidence service."""
    
    model_config = {"protected_namespaces": ()}  # Allow 'model_' prefix
    
    raw_score: float = Field(
        ge=0.0,
        le=1.0,
        description="Raw confidence score before calibration"
    )
    calibrated_score: float = Field(
        ge=0.0,
        le=1.0,
        description="Calibrated confidence score"
    )
    confidence_band: ConfidenceBand = Field(
        description="Confidence band classification"
    )
    
    # Model information
    model_version: str = Field(description="Confidence model version")
    calibration_method: str = Field(description="Calibration method (Platt/Isotonic)")
    
    # Recommended actions based on confidence band
    recommended_actions: List[str] = Field(
        default_factory=list,
        description="Recommended actions based on confidence level"
    )
    
    # Feature importance for explainability
    feature_contributions: Dict[str, float] = Field(
        default_factory=dict,
        description="Contribution of each feature to final score"
    )
    
    # Quality metrics
    calibration_error: Optional[float] = Field(
        default=None,
        description="Expected calibration error for this model"
    )
    uncertainty: Optional[float] = Field(
        default=None,
        description="Model uncertainty estimate"
    )


class PolicyEvaluation(BaseModel):
    """Policy evaluation result from safety service."""
    action_id: str = Field(
        default_factory=lambda: str(uuid4()),
        description="Unique action identifier for tracking"
    )
    action_type: ActionType = Field(description="Type of action being evaluated")
    action_description: str = Field(description="Human-readable action description")
    
    # Risk assessment
    risk_level: RiskLevel = Field(description="Assessed risk level")
    risk_factors: List[str] = Field(
        default_factory=list,
        description="Identified risk factors"
    )
    potential_impacts: List[str] = Field(
        default_factory=list,
        description="Potential negative impacts"
    )
    
    # Decision
    decision: str = Field(description="Policy decision (allow/deny/confirm)")
    requires_confirmation: bool = Field(
        default=False,
        description="Whether user confirmation is required"
    )
    required_role: Optional[str] = Field(
        default=None,
        description="Minimum role required for approval"
    )
    
    # Confirmation details (if required)
    confirmation_payload: Optional[Dict[str, Any]] = Field(
        default=None,
        description="Structured confirmation request details"
    )
    rationale: str = Field(description="Explanation for policy decision")
    rollback_procedure: Optional[str] = Field(
        default=None,
        description="Steps to undo the action if needed"
    )
    monitoring_steps: Optional[List[str]] = Field(
        default=None,
        description="Steps to monitor action success"
    )
    
    # Compliance
    compliance_checks: Dict[str, bool] = Field(
        default_factory=dict,
        description="Results of compliance validation"
    )
    
    @validator('decision')
    def validate_decision(cls, v):
        """Validate decision value."""
        valid_decisions = ['allow', 'deny', 'confirm']
        if v not in valid_decisions:
            raise ValueError(f"Decision must be one of {valid_decisions}")
        return v


class LoopCheckRequest(BaseModel):
    """Request for loop detection analysis."""
    session_id: str = Field(description="Session identifier")
    history: List[str] = Field(
        min_items=1,
        max_items=10,
        description="Recent query history for analysis"
    )
    confidence_history: List[float] = Field(
        min_items=1,
        description="Confidence scores for recent turns"
    )
    
    # Additional signals
    response_history: Optional[List[str]] = Field(
        default=None,
        description="Recent response history for pattern detection"
    )
    metadata: Dict[str, Any] = Field(
        default_factory=dict,
        description="Additional metadata for detection"
    )
    
    @validator('confidence_history')
    def validate_confidence_scores(cls, v):
        """Validate confidence scores are in valid range."""
        for score in v:
            if not (0.0 <= score <= 1.0):
                raise ValueError(f"Confidence score {score} not in range [0.0, 1.0]")
        return v


class LoopCheckResponse(BaseModel):
    """Response from loop detection service."""
    status: LoopStatus = Field(description="Loop detection status")
    confidence: float = Field(
        ge=0.0,
        le=1.0,
        description="Confidence in loop detection"
    )
    
    # Detection details
    detected_patterns: List[str] = Field(
        default_factory=list,
        description="Types of patterns detected"
    )
    detection_reasons: List[str] = Field(
        default_factory=list,
        description="Specific reasons for detection"
    )
    
    # Signals that triggered detection
    signal_scores: Dict[str, float] = Field(
        default_factory=dict,
        description="Individual signal detection scores"
    )
    
    # Recovery recommendations
    recommended_recovery: Optional[RecoveryAction] = Field(
        default=None,
        description="Recommended recovery action"
    )
    recovery_suggestions: List[str] = Field(
        default_factory=list,
        description="Specific recovery suggestions"
    )
    
    # Analysis metrics
    similarity_scores: List[float] = Field(
        default_factory=list,
        description="Query similarity scores"
    )
    confidence_slope: Optional[float] = Field(
        default=None,
        description="Confidence trend slope"
    )
    novelty_score: Optional[float] = Field(
        default=None,
        description="Query novelty assessment"
    )


class GatewayResult(BaseModel):
    """Result from gateway processing service."""
    processed_query: str = Field(description="Processed and sanitized query")
    original_query: str = Field(description="Original query for reference")
    
    # Processing assessments
    clarity_assessment: str = Field(description="Clarity level (vague/specific/complex)")
    reality_check: str = Field(description="Reality assessment (plausible/improbable/impossible)")
    needs_clarification: bool = Field(
        default=False,
        description="Whether query needs clarification"
    )
    
    # Extracted information
    extracted_assumptions: List[Dict[str, Any]] = Field(
        default_factory=list,
        description="Extracted implicit assumptions"
    )
    context_enrichments: Dict[str, Any] = Field(
        default_factory=dict,
        description="Context enrichments from metadata"
    )
    
    # Security and compliance
    pii_detected: bool = Field(
        default=False,
        description="Whether PII was detected and redacted"
    )
    redaction_count: int = Field(
        default=0,
        description="Number of items redacted"
    )
    security_flags: List[str] = Field(
        default_factory=list,
        description="Security-related flags or warnings"
    )
    
    # Processing recommendations
    routing_suggestions: List[str] = Field(
        default_factory=list,
        description="Suggested agents for routing"
    )
    processing_priority: str = Field(
        default="normal",
        description="Suggested processing priority"
    )
    
    # Metadata
    processing_time_ms: int = Field(
        ge=0,
        description="Gateway processing time"
    )
    processing_version: str = Field(
        default="1.0",
        description="Gateway processing version"
    )
    
    @validator('clarity_assessment')
    def validate_clarity(cls, v):
        """Validate clarity assessment value."""
        valid_clarity = ['vague', 'specific', 'complex', 'absurd']
        if v not in valid_clarity:
            raise ValueError(f"Clarity assessment must be one of {valid_clarity}")
        return v
    
    @validator('reality_check')
    def validate_reality(cls, v):
        """Validate reality check value."""
        valid_reality = ['plausible', 'improbable', 'impossible', 'verified']
        if v not in valid_reality:
            raise ValueError(f"Reality check must be one of {valid_reality}")
        return v