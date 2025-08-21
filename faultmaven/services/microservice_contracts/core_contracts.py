"""Core Contracts for FaultMaven Microservice Architecture

Data transfer objects and contracts used across service interfaces.
These enable consistent serialization/deserialization for both
in-process and distributed deployments.
"""

from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Union
from datetime import datetime


@dataclass
class TurnContext:
    """Context for a single turn in the conversation"""
    turn_id: str
    session_id: str
    query: str
    user_data: Dict[str, Any]
    conversation_history: List[Dict[str, Any]]
    metadata: Dict[str, Any]


@dataclass
class DecisionRecord:
    """Record of a decision made by the system"""
    decision_id: str
    timestamp: datetime
    service_name: str
    decision_type: str
    context: Dict[str, Any]
    outcome: Dict[str, Any]
    confidence: float


@dataclass
class RetrievalRequest:
    """Request for information retrieval"""
    request_id: str
    query: str
    context: Dict[str, Any]
    filters: Dict[str, Any]
    limit: int = 10


@dataclass
class RetrievalResponse:
    """Response from information retrieval"""
    request_id: str
    results: List[Dict[str, Any]]
    metadata: Dict[str, Any]
    confidence: float


@dataclass
class ConfidenceRequest:
    """Request for confidence scoring"""
    request_id: str
    features: Dict[str, float]
    context: Dict[str, Any]


@dataclass
class ConfidenceResponse:
    """Response with confidence score"""
    request_id: str
    confidence_score: float
    confidence_band: str
    metadata: Dict[str, Any]


@dataclass
class PolicyEvaluation:
    """Policy evaluation result"""
    evaluation_id: str
    action: str
    approved: bool
    risk_level: str
    restrictions: List[str]
    rationale: str


@dataclass
class LoopCheckRequest:
    """Request for loop detection"""
    request_id: str
    conversation_history: List[Dict[str, Any]]
    current_turn: Dict[str, Any]


@dataclass
class LoopCheckResponse:
    """Response from loop detection"""
    request_id: str
    loop_detected: bool
    loop_type: str
    confidence: float
    recommendation: str


@dataclass
class GatewayResult:
    """Result from gateway processing"""
    result_id: str
    processed_input: Dict[str, Any]
    routing_decision: str
    confidence: float
    metadata: Dict[str, Any]