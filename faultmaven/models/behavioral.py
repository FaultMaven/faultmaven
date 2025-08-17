# File: faultmaven/models/behavioral.py

from pydantic import BaseModel, Field, validator
from typing import Dict, List, Optional, Any, Union
from datetime import datetime, timedelta
from enum import Enum
import uuid

# --- Enumerations ---

class RiskLevel(str, Enum):
    """Risk level assessment for client behavior"""
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    CRITICAL = "critical"

class BehaviorType(str, Enum):
    """Types of behavioral patterns"""
    REQUEST_PATTERN = "request_pattern"
    TIMING_PATTERN = "timing_pattern"
    ERROR_PATTERN = "error_pattern"
    RESOURCE_PATTERN = "resource_pattern"
    SESSION_PATTERN = "session_pattern"

class AnomalyType(str, Enum):
    """Types of anomalies detected"""
    FREQUENCY_ANOMALY = "frequency_anomaly"
    TIMING_ANOMALY = "timing_anomaly"
    PATTERN_ANOMALY = "pattern_anomaly"
    SEQUENCE_ANOMALY = "sequence_anomaly"
    STATISTICAL_OUTLIER = "statistical_outlier"

class ReputationLevel(str, Enum):
    """Client reputation levels"""
    TRUSTED = "trusted"        # 90-100
    NORMAL = "normal"          # 70-89
    SUSPICIOUS = "suspicious"  # 50-69
    RESTRICTED = "restricted"  # 30-49
    BLOCKED = "blocked"        # 0-29

class Trend(str, Enum):
    """Reputation trend direction"""
    IMPROVING = "improving"
    STABLE = "stable"
    DECLINING = "declining"
    VOLATILE = "volatile"

# --- Core Data Models ---

class RequestPattern(BaseModel):
    """Individual request pattern analysis"""
    endpoint: str
    method: str
    frequency: float  # requests per minute
    avg_response_time: float  # milliseconds
    error_rate: float  # 0.0 to 1.0
    payload_size_avg: int  # bytes
    timestamp: datetime
    pattern_id: str = Field(default_factory=lambda: str(uuid.uuid4()))

class TimingProfile(BaseModel):
    """Client timing characteristics"""
    avg_request_interval: float  # seconds between requests
    request_interval_stddev: float
    peak_activity_hours: List[int]  # hours of day (0-23)
    session_duration_avg: float  # average session length in minutes
    burst_frequency: float  # bursts per hour
    think_time_avg: float  # average time between user actions

class ErrorPattern(BaseModel):
    """Error generation patterns"""
    error_type: str
    frequency: int
    endpoints_affected: List[str]
    first_occurrence: datetime
    last_occurrence: datetime
    error_rate_trend: Trend
    resolution_attempts: int

class ResourceProfile(BaseModel):
    """Resource usage characteristics"""
    cpu_usage_avg: float  # average CPU time per request (ms)
    memory_usage_avg: float  # average memory per request (MB)
    network_bandwidth_avg: float  # average bandwidth per request (KB)
    storage_requests: int  # number of storage operations
    external_api_calls: int  # number of external API calls

class BehaviorVector(BaseModel):
    """Feature vector for ML analysis"""
    features: Dict[str, float]
    feature_names: List[str]
    extraction_timestamp: datetime
    window_size: int  # minutes of data this vector represents
    confidence: float = Field(ge=0.0, le=1.0)

class TemporalAnomaly(BaseModel):
    """Time-based anomaly detection result"""
    anomaly_type: AnomalyType
    timestamp: datetime
    severity: float = Field(ge=0.0, le=1.0)
    duration: timedelta
    affected_patterns: List[str]
    description: str

class BehaviorProfile(BaseModel):
    """Comprehensive client behavior profile"""
    session_id: str
    client_fingerprint: Optional[str] = None
    request_patterns: List[RequestPattern] = Field(default_factory=list)
    timing_characteristics: Optional[TimingProfile] = None
    endpoint_preferences: Dict[str, float] = Field(default_factory=dict)  # endpoint -> usage_ratio
    error_patterns: List[ErrorPattern] = Field(default_factory=list)
    resource_usage: Optional[ResourceProfile] = None
    behavior_vectors: List[BehaviorVector] = Field(default_factory=list)
    
    # Metadata
    first_seen: datetime
    last_updated: datetime
    total_requests: int = 0
    confidence_score: float = Field(default=0.0, ge=0.0, le=1.0)
    
    # Risk assessment
    current_risk_level: RiskLevel = RiskLevel.LOW
    risk_factors: List[str] = Field(default_factory=list)

    @validator('endpoint_preferences')
    def validate_preferences_sum(cls, v):
        """Ensure endpoint preferences are valid probabilities"""
        if v and abs(sum(v.values()) - 1.0) > 0.01:  # Allow small floating point errors
            # Normalize if close to 1.0
            total = sum(v.values())
            if total > 0:
                return {k: val/total for k, val in v.items()}
        return v

class AnomalyResult(BaseModel):
    """Result of anomaly detection analysis"""
    session_id: str
    overall_score: float = Field(ge=0.0, le=1.0)  # 0.0 = normal, 1.0 = highly anomalous
    anomaly_types: List[AnomalyType]
    pattern_anomalies: Dict[str, float] = Field(default_factory=dict)  # pattern_name -> anomaly_score
    temporal_anomalies: List[TemporalAnomaly] = Field(default_factory=list)
    feature_contributions: Dict[str, float] = Field(default_factory=dict)  # feature_name -> contribution
    
    # Detection metadata
    detection_timestamp: datetime
    ml_model_version: str
    ml_model_confidence: float = Field(ge=0.0, le=1.0)
    detection_method: str  # "isolation_forest", "lstm", "ensemble", etc.
    
    # Explanation
    explanation: str = ""
    recommended_actions: List[str] = Field(default_factory=list)

class Violation(BaseModel):
    """Record of a policy or behavioral violation"""
    violation_id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    violation_type: str
    severity: str  # "low", "medium", "high", "critical"
    description: str
    timestamp: datetime
    endpoint: Optional[str] = None
    session_id: str
    resolution_status: str = "open"  # "open", "resolved", "ignored"
    penalty_applied: bool = False

class ReputationEvent(BaseModel):
    """Event that affects client reputation"""
    event_id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    event_type: str  # "violation", "compliance", "recovery", "positive_behavior"
    impact: float = Field(ge=-100.0, le=100.0)  # reputation change
    timestamp: datetime
    session_id: str
    description: str
    metadata: Dict[str, Any] = Field(default_factory=dict)

class ReputationScore(BaseModel):
    """Comprehensive client reputation scoring"""
    client_id: str  # Could be session_id, user_id, or client_fingerprint
    overall_score: int = Field(ge=0, le=100)
    
    # Component scores
    compliance_score: int = Field(ge=0, le=100)  # adherence to rate limits and policies
    efficiency_score: int = Field(ge=0, le=100)  # resource usage efficiency
    stability_score: int = Field(ge=0, le=100)   # consistency and predictability
    reliability_score: int = Field(ge=0, le=100) # error generation patterns
    
    # Historical data
    historical_violations: List[Violation] = Field(default_factory=list)
    reputation_events: List[ReputationEvent] = Field(default_factory=list)
    
    # Trends and recovery
    reputation_trend: Trend = Trend.STABLE
    recovery_progress: float = Field(default=0.0, ge=0.0, le=1.0)
    last_violation: Optional[datetime] = None
    last_positive_event: Optional[datetime] = None
    
    # Metadata
    first_scored: datetime
    last_updated: datetime
    score_confidence: float = Field(default=1.0, ge=0.0, le=1.0)
    
    @property
    def reputation_level(self) -> ReputationLevel:
        """Get reputation level based on overall score"""
        if self.overall_score >= 90:
            return ReputationLevel.TRUSTED
        elif self.overall_score >= 70:
            return ReputationLevel.NORMAL
        elif self.overall_score >= 50:
            return ReputationLevel.SUSPICIOUS
        elif self.overall_score >= 30:
            return ReputationLevel.RESTRICTED
        else:
            return ReputationLevel.BLOCKED

class BehaviorScore(BaseModel):
    """Behavioral analysis scoring result"""
    session_id: str
    overall_behavior_score: float = Field(ge=0.0, le=1.0)  # 0.0 = suspicious, 1.0 = normal
    pattern_scores: Dict[BehaviorType, float] = Field(default_factory=dict)
    risk_level: RiskLevel
    confidence: float = Field(ge=0.0, le=1.0)
    
    # Detailed analysis
    anomalies_detected: List[AnomalyResult] = Field(default_factory=list)
    risk_factors: List[str] = Field(default_factory=list)
    positive_indicators: List[str] = Field(default_factory=list)
    
    # Recommendations
    recommended_actions: List[str] = Field(default_factory=list)
    monitoring_level: str = "normal"  # "minimal", "normal", "enhanced", "intensive"
    
    # Metadata
    analysis_timestamp: datetime
    analysis_window: timedelta
    data_quality: float = Field(default=1.0, ge=0.0, le=1.0)

class ClientProfile(BaseModel):
    """Comprehensive client profile combining behavior and reputation"""
    client_id: str
    session_ids: List[str] = Field(default_factory=list)
    
    # Core profiles
    behavior_profile: Optional[BehaviorProfile] = None
    reputation_score: Optional[ReputationScore] = None
    
    # Current state
    current_risk_level: RiskLevel = RiskLevel.LOW
    current_reputation_level: ReputationLevel = ReputationLevel.NORMAL
    
    # Activity tracking
    active_sessions: int = 0
    total_sessions: int = 0
    first_seen: datetime
    last_activity: datetime
    
    # Trust and access
    trust_level: float = Field(default=0.5, ge=0.0, le=1.0)
    access_restrictions: List[str] = Field(default_factory=list)
    monitoring_flags: List[str] = Field(default_factory=list)

# --- Analysis Results ---

class BehaviorAnalysisResult(BaseModel):
    """Complete behavioral analysis result"""
    analysis_id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    session_id: str
    
    # Analysis components
    behavior_score: BehaviorScore
    anomaly_results: List[AnomalyResult] = Field(default_factory=list)
    pattern_analysis: Dict[str, Any] = Field(default_factory=dict)
    
    # Temporal analysis
    trend_analysis: Dict[str, Trend] = Field(default_factory=dict)
    prediction_horizon: timedelta = timedelta(hours=1)
    predicted_risk: RiskLevel = RiskLevel.LOW
    
    # Metadata
    analysis_timestamp: datetime
    processing_time_ms: float
    data_completeness: float = Field(ge=0.0, le=1.0)
    ml_model_versions: Dict[str, str] = Field(default_factory=dict)

class ProtectionDecision(BaseModel):
    """Protection system decision based on behavioral analysis"""
    decision_id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    session_id: str
    
    # Decision
    allow_request: bool = True
    applied_restrictions: List[str] = Field(default_factory=list)
    rate_limit_override: Optional[Dict[str, Any]] = None
    priority_level: str = "normal"  # "low", "normal", "high", "critical"
    
    # Reasoning
    decision_factors: Dict[str, float] = Field(default_factory=dict)
    risk_assessment: RiskLevel
    confidence: float = Field(ge=0.0, le=1.0)
    explanation: str = ""
    
    # Actions
    monitoring_actions: List[str] = Field(default_factory=list)
    alerting_actions: List[str] = Field(default_factory=list)
    logging_level: str = "normal"  # "minimal", "normal", "detailed", "verbose"
    
    # Metadata
    decision_timestamp: datetime
    processing_time_ms: float
    decision_engine_version: str = "2.0"