"""Agent Module - Investigation Workflows and Agent Tools.

This module provides the tool system the investigation engine executes with, the
investigation turn service, and the agent-facing domain models. It has no HTTP
surface of its own: investigation traffic enters through the Case module's
``/turns`` endpoints and is driven by ``core/investigation/milestone_engine``.

Public API:
    From modules.case.contracts (agent-execution audit data is Case-owned):
        - AgentExecution, AgentToolCall, AgentType, ExecutionStatus

    From domain.models.agentic:
        - QueryIntent, SuggestedAction

    From domain.services (import directly):
        - InvestigationService

    From tools:
        - tool_registry, AgentTool, ToolContext

Usage:
    from faultmaven.modules.case.contracts import AgentType

Note: This module does NOT eagerly import components to avoid circular dependencies.
Import components directly from their submodules as shown above.
"""

# Subpackages available for direct import
__all__ = [
    "domain",
    "jobs",
    "tools",
]
