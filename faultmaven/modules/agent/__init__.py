"""Agent Module - AI Agent Orchestration and Investigation Workflows.

This module provides agent orchestration, investigation workflows, and tool systems
for the FaultMaven platform.

Public API:
    From domain.models.agent_execution:
        - AgentExecution, AgentType, ExecutionStatus

    From domain.models.investigation:
        - InvestigationPhase, InvestigationState, InvestigationStrategy

    From domain.models.agentic:
        - QueryIntent, SuggestedAction

    From domain.events.execution_events:
        - ExecutionEvent, ExecutionEventType

    From domain.services (import directly):
        - AgentOrchestrationService
        - InvestigationOrchestrator
        - InvestigationService

    From tools:
        - tool_registry, AgentTool, ToolContext

Usage:
    # Models can be imported directly:
    from faultmaven.modules.agent.domain.models.agent_execution import AgentType
    from faultmaven.modules.agent.domain.models.investigation import InvestigationPhase

    # Services should be imported where needed to avoid circular imports:
    from faultmaven.modules.agent.domain.services.agent_orchestration_service import AgentOrchestrationService

Note: This module does NOT eagerly import components to avoid circular dependencies.
Import components directly from their submodules as shown above.
"""

# Subpackages available for direct import
__all__ = [
    "api",
    "domain",
    "infrastructure",
    "tools",
]
