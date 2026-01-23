"""Agent Execution Domain Models - Re-export from Case Module.

Per module-organization-design.md:
- Case module owns agent execution audit data (agent_tool_calls is case audit data)
- Agent module is a Domain Service that operates on Case-owned data
- These models are re-exported from Case contracts for backward compatibility

For new code, import from:
    from faultmaven.modules.case.contracts import AgentExecution, AgentToolCall, ...

This file re-exports from Case contracts for backward compatibility.
"""

# Re-export all agent execution models from Case contracts
# This maintains backward compatibility while adhering to the design
from faultmaven.modules.case.contracts import (
    AgentExecution,
    AgentToolCall,
    AgentType,
    ExecutionStatus,
)

__all__ = [
    # Enumerations
    "ExecutionStatus",
    "AgentType",
    # Domain Models
    "AgentToolCall",
    "AgentExecution",
]
