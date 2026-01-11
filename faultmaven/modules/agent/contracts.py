"""Agent Module Contracts

This module defines the public interfaces (contracts) for the Agent vertical module.
Other modules should import from here, not from domain directly.

Following the design in module-organization-design.md:
- Vertical modules expose contracts through contracts.py
- Domain services use these contracts for cross-module communication

Per Principle 2 (Vertical Modules with Contracts):
- External code imports from contracts.py
- Internal domain models are re-exported for backward compatibility
"""

from typing import Protocol, Optional, List, TYPE_CHECKING

if TYPE_CHECKING:
    from uuid import UUID


# ============================================================
# Re-export domain models for cross-module use
# ============================================================

# Agent Execution models (used by case module for execution tracking)
from faultmaven.modules.agent.domain.models.agent_execution import (
    AgentExecution,
    AgentToolCall,
    ExecutionStatus,
    AgentType,
)

# Investigation models (used by prompts, investigation services)
from faultmaven.modules.agent.domain.models.investigation import (
    InvestigationPhase,
    InvestigationStrategy,
    InvestigationState,
    OODAStep,
    OODAIteration,
    EngagementMode,
    HypothesisStatus,
    HypothesisGenerationMode,
    HypothesisTest,
    ProblemConfirmation,
    HierarchicalMemory,
    MemorySnapshot,
    ConfidenceLevel,
    InvestigationMomentum,
    DegradedModeType,
    AnomalyFrame,
    TemporalFrame,
    Hypothesis,
    WorkingConclusion,
    ProgressMetrics,
    EscalationState,
    PHASE_OODA_WEIGHTS,
    get_confidence_cap,
)

# Agentic models (used by prompts, models/api)
from faultmaven.modules.agent.domain.models.agentic import (
    SuggestedAction,
    QueryIntent,
    QueryClassification,
    AgentResponse,
)


# ============================================================
# Service Protocols
# ============================================================

class IAgentExecutionQuery(Protocol):
    """Read-only agent execution query interface for cross-module use."""

    async def get_execution(self, execution_id: 'UUID') -> Optional[AgentExecution]:
        """Get execution by ID."""
        ...

    async def list_executions_for_case(
        self,
        case_id: str,
        status: Optional[ExecutionStatus] = None,
        limit: int = 100,
    ) -> List[AgentExecution]:
        """List executions for a case with optional filtering."""
        ...


class IInvestigationService(Protocol):
    """Investigation service interface for cross-module use."""

    async def get_investigation_state(self, case_id: str) -> Optional[InvestigationState]:
        """Get current investigation state for a case."""
        ...

    async def update_investigation_phase(
        self,
        case_id: str,
        phase: InvestigationPhase,
    ) -> bool:
        """Update investigation phase for a case."""
        ...


# ============================================================
# Module Exports
# ============================================================

__all__ = [
    # Agent Execution
    "AgentExecution",
    "AgentToolCall",
    "ExecutionStatus",
    "AgentType",
    # Investigation
    "InvestigationPhase",
    "InvestigationStrategy",
    "InvestigationState",
    "OODAStep",
    "OODAIteration",
    "EngagementMode",
    "HypothesisStatus",
    "HypothesisGenerationMode",
    "HypothesisTest",
    "ProblemConfirmation",
    "HierarchicalMemory",
    "MemorySnapshot",
    "ConfidenceLevel",
    "InvestigationMomentum",
    "DegradedModeType",
    "AnomalyFrame",
    "TemporalFrame",
    "Hypothesis",
    "WorkingConclusion",
    "ProgressMetrics",
    "EscalationState",
    "PHASE_OODA_WEIGHTS",
    "get_confidence_cap",
    # Agentic
    "SuggestedAction",
    "QueryIntent",
    "QueryClassification",
    "AgentResponse",
    # Protocols
    "IAgentExecutionQuery",
    "IInvestigationService",
]
