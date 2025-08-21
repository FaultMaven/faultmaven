"""Microservice Interface Definitions

This module contains the interface definitions for FaultMaven's microservice architecture.
These interfaces support both in-process and distributed deployment modes.

Core Services:
- Orchestrator/Router Service: Query routing and state management
- Global Confidence Service: Calibrated confidence scoring
- Unified Retrieval Service: Federated knowledge retrieval
- Policy/Safety Service: Action evaluation and safety checks
- Session/Case Service: Session and case persistence
- LoopGuard/Monitor Service: Loop detection and recovery
- Gateway Processing Service: Pre-processing and validation

Specialist Agents:
- Triage Agent: Problem categorization and severity assessment
- Scoping Agent: Clarifying questions and scope refinement
- Diagnostic Agent: Hypothesis generation and testing
- Validation Agent: Assumption and claim verification
- Pattern Agent: Pattern matching with success rates
- Learning Agent: Continuous learning and knowledge updates
"""

from .core_services import (
    IOrchestratorService,
    IGlobalConfidenceService,
    IUnifiedRetrievalService,
    IPolicySafetyService,
    ISessionCaseService,
    ILoopGuardService,
    IGatewayProcessingService
)

from .specialist_agents import (
    ITriageAgent,
    IScopingAgent,
    IDiagnosticAgent,
    IValidationAgent,
    IPatternAgent,
    ILearningAgent
)

from .event_bus import IEventBus

__all__ = [
    # Core Services
    'IOrchestratorService',
    'IGlobalConfidenceService', 
    'IUnifiedRetrievalService',
    'IPolicySafetyService',
    'ISessionCaseService',
    'ILoopGuardService',
    'IGatewayProcessingService',
    
    # Specialist Agents
    'ITriageAgent',
    'IScopingAgent',
    'IDiagnosticAgent',
    'IValidationAgent',
    'IPatternAgent',
    'ILearningAgent',
    
    # Event Bus
    'IEventBus'
]