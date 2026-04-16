"""Agent Tool Infrastructure (TASK-015)

This module provides the base classes and context for agent tool execution.
Tools are invoked by the LLM during agent execution to perform actions
like reading files, searching knowledge bases, and listing evidence.

Design Reference: docs/architecture/TASK-015-agent-orchestration-design.md
"""

import logging
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from faultmaven.modules.agent.domain.events.execution_events import Tool
from faultmaven.models.interfaces import BaseTool, ToolResult

logger = logging.getLogger(__name__)


@dataclass
class ToolContext:
    """Context passed to tool execution.

    Contains all the information a tool needs to perform its operation
    within the scope of an investigation session.

    Attributes:
        session_id: Investigation session ID
        case_id: Case ID the session belongs to
        organization_id: Organization ID for authorization
        user_id: User ID making the request
        evidence_service: Service for accessing evidence artifacts
        execution_id: Current agent execution ID
        metadata: Additional context metadata
    """

    session_id: str
    case_id: str
    organization_id: str
    user_id: str
    team_ids: List[str] = field(default_factory=list)
    evidence_service: Optional[Any] = (
        None  # APIEvidenceArtifactService (avoid import violation)
    )
    execution_id: Optional[str] = None
    metadata: Dict[str, Any] = field(default_factory=dict)
    in_memory_case: Optional[Any] = None

    def with_execution_id(self, execution_id: str) -> "ToolContext":
        """Create a new context with the execution ID set."""
        return ToolContext(
            session_id=self.session_id,
            case_id=self.case_id,
            organization_id=self.organization_id,
            user_id=self.user_id,
            team_ids=self.team_ids,
            evidence_service=self.evidence_service,
            execution_id=execution_id,
            metadata=self.metadata,
            in_memory_case=self.in_memory_case,
        )


class AgentTool(BaseTool, ABC):
    """Base class for agent tools.

    Extends BaseTool with additional methods and properties specific
    to agent tool execution, including context-aware execution and
    LLM-friendly schema generation.

    Subclasses must implement:
    - name: Tool name for LLM function calling
    - description: Human-readable description
    - parameters_schema: JSON Schema for parameters
    - execute(): Async execution method
    """

    @property
    @abstractmethod
    def name(self) -> str:
        """Unique name of the tool for LLM function calling."""
        pass

    @property
    @abstractmethod
    def description(self) -> str:
        """Human-readable description for the LLM."""
        pass

    @property
    @abstractmethod
    def parameters_schema(self) -> Dict[str, Any]:
        """JSON Schema for tool parameters."""
        pass

    async def execute(self, params: Dict[str, Any]) -> ToolResult:
        """Execute the tool with parameters (legacy interface).

        This method exists for compatibility with BaseTool.
        Agent tools should override execute_with_context instead.
        """
        # Return error if called without context
        return ToolResult(
            success=False,
            data=None,
            error="Tool requires context. Use execute_with_context instead.",
        )

    @abstractmethod
    async def execute_with_context(
        self,
        params: Dict[str, Any],
        context: ToolContext,
    ) -> ToolResult:
        """Execute the tool with context.

        Args:
            params: Tool parameters from LLM
            context: Execution context with services and metadata

        Returns:
            ToolResult with success status and data/error
        """
        pass

    def get_schema(self) -> Dict[str, Any]:
        """Get the tool's schema for LLM function calling."""
        return {
            "name": self.name,
            "description": self.description,
            "parameters": self.parameters_schema,
        }

    def to_tool(self) -> Tool:
        """Convert to domain Tool object for LLM API."""
        return Tool(
            name=self.name,
            description=self.description,
            parameters=self.parameters_schema,
        )

    def validate_params(self, params: Dict[str, Any]) -> Optional[str]:
        """Validate parameters against schema.

        Returns error message if invalid, None if valid.
        """
        schema = self.parameters_schema
        required = schema.get("required", [])

        for param in required:
            if param not in params:
                return f"Missing required parameter: {param}"

        properties = schema.get("properties", {})
        for param_name, param_value in params.items():
            if param_name not in properties:
                continue  # Allow extra params

            expected_type = properties[param_name].get("type")
            if expected_type:
                if not self._check_type(param_value, expected_type):
                    return (
                        f"Parameter '{param_name}' has wrong type. "
                        f"Expected {expected_type}."
                    )

        return None

    def _check_type(self, value: Any, expected_type: str) -> bool:
        """Check if value matches expected JSON Schema type."""
        type_mapping = {
            "string": str,
            "integer": int,
            "number": (int, float),
            "boolean": bool,
            "array": list,
            "object": dict,
        }
        expected = type_mapping.get(expected_type)
        if expected is None:
            return True  # Unknown type, accept
        return isinstance(value, expected)


class AgentToolRegistry:
    """Registry for agent-specific tools.

    Manages available tools for agent execution and provides
    methods for tool lookup and schema generation.

    This is separate from the global ToolRegistry to allow
    for agent-specific tool registration and configuration.
    """

    def __init__(self) -> None:
        """Initialize the agent tool registry."""
        self._tools: Dict[str, AgentTool] = {}

    def register(self, tool: AgentTool) -> None:
        """Register an agent tool.

        Args:
            tool: Tool instance to register

        Raises:
            ValueError: If tool with same name already registered
        """
        if tool.name in self._tools:
            raise ValueError(f"Tool '{tool.name}' is already registered")
        self._tools[tool.name] = tool
        logger.debug(f"Registered agent tool: {tool.name}")

    def get(self, name: str) -> Optional[AgentTool]:
        """Get a tool by name.

        Args:
            name: Tool name

        Returns:
            Tool instance or None if not found
        """
        return self._tools.get(name)

    def list_tools(self) -> List[str]:
        """List all registered tool names."""
        return list(self._tools.keys())

    def get_all_tools(self) -> List[AgentTool]:
        """Get all registered tools."""
        return list(self._tools.values())

    def get_all_schemas(self) -> List[Dict[str, Any]]:
        """Get schemas for all registered tools."""
        return [tool.get_schema() for tool in self._tools.values()]

    def get_all_domain_tools(self) -> List[Tool]:
        """Get domain Tool objects for all registered tools."""
        return [tool.to_tool() for tool in self._tools.values()]

    async def execute_tool(
        self,
        tool_name: str,
        params: Dict[str, Any],
        context: ToolContext,
    ) -> ToolResult:
        """Execute a tool by name.

        Args:
            tool_name: Name of the tool to execute
            params: Parameters to pass to the tool
            context: Execution context

        Returns:
            ToolResult from tool execution
        """
        tool = self.get(tool_name)
        if not tool:
            return ToolResult(
                success=False,
                data=None,
                error=f"Tool '{tool_name}' not found",
            )

        # Validate parameters
        validation_error = tool.validate_params(params)
        if validation_error:
            return ToolResult(
                success=False,
                data=None,
                error=validation_error,
            )

        try:
            return await tool.execute_with_context(params, context)
        except Exception as e:
            logger.exception(f"Tool '{tool_name}' execution failed: {e}")
            return ToolResult(
                success=False,
                data=None,
                error=f"Tool execution failed: {str(e)}",
            )

    def clear(self) -> None:
        """Clear all registered tools (useful for testing)."""
        self._tools.clear()


# Global agent tool registry instance
tool_registry = AgentToolRegistry()
