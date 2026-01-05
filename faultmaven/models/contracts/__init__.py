"""Microservice Data Contracts for FaultMaven Architecture

This module contains the data contract definitions for communication between
FaultMaven microservices. These contracts support both in-process and
distributed deployment modes with consistent schema validation.

Core Contracts:
- TurnContext: Router input with query, budget, and context
- DecisionRecord: Orchestrator decision records for observability  
- RetrievalRequest/Response: Unified retrieval service contracts
- ConfidenceRequest/Response: Global confidence service contracts
- PolicyEvaluation: Policy/safety service evaluation results
- LoopCheckRequest/Response: Loop detection service contracts
- GatewayResult: Gateway processing service output

Agent Contracts:
- AgentRequest/Response: Standard agent communication contracts
- Budget: Resource budget tracking and enforcement
- ExecutionContext: Agent execution context and constraints
- Specialist result types for each agent (Triage, Scoping, etc.)

Design Principles:
- Schema validation with Pydantic models
- Backward compatibility for version migration  
- Comprehensive field documentation
- Optional fields for graceful degradation
- Standardized error handling patterns
"""

from .core_contracts import (
    TurnContext,
    DecisionRecord, 
    RetrievalRequest,
    RetrievalResponse,
    ConfidenceRequest,
    ConfidenceResponse,
    PolicyEvaluation,
    LoopCheckRequest,
    LoopCheckResponse,
    GatewayResult,
    Budget,
    Evidence
)

from .agent_contracts import (
    AgentRequest,
    AgentResponse,
    ExecutionContext,
    TriageResult,
    ScopingResult,
    DiagnosticResult,
    ValidationResult,
    PatternResult,
    LearningResult
)

from .error_contracts import (
    ServiceError,
    BudgetExceededError,
    ValidationError,
    CircuitBreakerError,
    TimeoutError
)

__all__ = [
    # Core Service Contracts
    'TurnContext',
    'DecisionRecord',
    'RetrievalRequest', 
    'RetrievalResponse',
    'ConfidenceRequest',
    'ConfidenceResponse',
    'PolicyEvaluation',
    'LoopCheckRequest',
    'LoopCheckResponse',
    'GatewayResult',
    'Budget',
    'Evidence',
    
    # Agent Contracts
    'AgentRequest',
    'AgentResponse', 
    'ExecutionContext',
    'TriageResult',
    'ScopingResult',
    'DiagnosticResult',
    'ValidationResult',
    'PatternResult',
    'LearningResult',
    
    # Error Contracts
    'ServiceError',
    'BudgetExceededError',
    'ValidationError',
    'CircuitBreakerError',
    'TimeoutError'
]