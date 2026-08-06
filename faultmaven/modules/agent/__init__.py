"""Agent Module - AI Agent Orchestration and Investigation Workflows.

This module provides agent orchestration, investigation workflows, and tool systems
for the FaultMaven platform.

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

# Subpackages available for direct import. No ``infrastructure``: the agent
# module is a Domain Service and owns no tables (see the module architecture in
# CLAUDE.md), so it has no repositories to expose.
__all__ = [
    "api",
    "domain",
    "jobs",
    "tools",
]
