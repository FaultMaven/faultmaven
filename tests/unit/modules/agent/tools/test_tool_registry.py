"""Unit Tests for Agent Tool Registry (TASK-015)

This module tests the ToolRegistry, AgentTool base class,
and tool infrastructure for agent execution.

Design Reference: docs/architecture/TASK-015-agent-orchestration-design.md
"""

from typing import Any, Dict, Optional
from unittest.mock import AsyncMock, MagicMock

import pytest

from faultmaven.models.interfaces import ToolResult
from faultmaven.modules.agent.tools.base import (
    AgentTool,
    AgentToolRegistry,
    Tool,
    ToolContext,
    tool_registry,
)

# =============================================================================
# Test Fixtures
# =============================================================================


class MockTool(AgentTool):
    """Mock tool for testing."""

    def __init__(self, name: str = "mock_tool", should_fail: bool = False):
        self._name = name
        self._should_fail = should_fail

    @property
    def name(self) -> str:
        return self._name

    @property
    def description(self) -> str:
        return f"Mock tool: {self._name}"

    @property
    def parameters_schema(self) -> Dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "input": {"type": "string", "description": "Input parameter"},
                "count": {"type": "integer", "description": "Count parameter"},
            },
            "required": ["input"],
        }

    async def execute_with_context(
        self,
        params: Dict[str, Any],
        context: ToolContext,
    ) -> ToolResult:
        if self._should_fail:
            return ToolResult(
                success=False,
                data=None,
                error="Mock failure",
            )
        return ToolResult(
            success=True,
            data={"result": f"Processed: {params.get('input', 'none')}"},
        )


@pytest.fixture
def sample_context():
    """Create a sample tool context.

    ``ToolContext`` carries ``case_repository``; tools read evidence from
    ``case.evidence`` directly.
    """
    return ToolContext(
        session_id="session_test",
        case_id="case_test",
        enterprise_id="org_test",
        user_id="user_test",
        case_repository=AsyncMock(),
    )


@pytest.fixture
def fresh_registry():
    """Create a fresh ToolRegistry for testing."""
    return AgentToolRegistry()


# =============================================================================
# Test: ToolContext
# =============================================================================


class TestToolContext:
    """Tests for ToolContext."""

    def test_tool_context_initialization(self):
        """Test ToolContext initialization with required fields."""
        context = ToolContext(
            session_id="session_123",
            case_id="case_456",
            enterprise_id="ent_789",
            user_id="user_abc",
        )

        assert context.session_id == "session_123"
        assert context.case_id == "case_456"
        assert context.enterprise_id == "ent_789"
        assert context.user_id == "user_abc"
        assert context.case_repository is None
        assert context.execution_id is None
        assert context.metadata == {}

    def test_tool_context_with_optional_fields(self):
        """Test ToolContext with optional fields."""
        case_repository = AsyncMock()
        context = ToolContext(
            session_id="session_123",
            case_id="case_456",
            enterprise_id="ent_789",
            user_id="user_abc",
            case_repository=case_repository,
            execution_id="exec_111",
            metadata={"key": "value"},
        )

        assert context.case_repository is case_repository
        assert context.execution_id == "exec_111"
        assert context.metadata == {"key": "value"}

    def test_tool_context_with_execution_id(self, sample_context):
        """Test with_execution_id creates new context with ID."""
        new_context = sample_context.with_execution_id("exec_new")

        assert new_context.execution_id == "exec_new"
        assert new_context.session_id == sample_context.session_id
        assert new_context.case_id == sample_context.case_id
        assert sample_context.execution_id is None  # Original unchanged


# =============================================================================
# Test: AgentTool Base Class
# =============================================================================


class TestAgentTool:
    """Tests for AgentTool base class."""

    def test_agent_tool_get_schema(self):
        """Test get_schema returns correct format."""
        tool = MockTool()
        schema = tool.get_schema()

        assert schema["name"] == "mock_tool"
        assert "description" in schema
        assert "parameters" in schema
        assert schema["parameters"]["type"] == "object"

    def test_agent_tool_to_tool(self):
        """Test to_tool converts to domain Tool object."""
        agent_tool = MockTool()
        domain_tool = agent_tool.to_tool()

        assert isinstance(domain_tool, Tool)
        assert domain_tool.name == "mock_tool"
        assert domain_tool.description == "Mock tool: mock_tool"

    def test_agent_tool_validate_params_success(self):
        """Test validate_params returns None for valid params."""
        tool = MockTool()
        error = tool.validate_params({"input": "test"})

        assert error is None

    def test_agent_tool_validate_params_missing_required(self):
        """Test validate_params returns error for missing required param."""
        tool = MockTool()
        error = tool.validate_params({"count": 5})

        assert error is not None
        assert "input" in error.lower()

    def test_agent_tool_validate_params_wrong_type(self):
        """Test validate_params returns error for wrong type."""
        tool = MockTool()
        error = tool.validate_params({"input": "test", "count": "not a number"})

        assert error is not None
        assert "count" in error.lower() or "type" in error.lower()

    @pytest.mark.asyncio
    async def test_agent_tool_execute_without_context(self):
        """Calling the no-context ``execute`` returns a clear error."""
        tool = MockTool()
        result = await tool.execute({"input": "test"})

        assert result.success is False
        assert "context" in result.error.lower()

    @pytest.mark.asyncio
    async def test_agent_tool_execute_with_context(self, sample_context):
        """Test execute_with_context works correctly."""
        tool = MockTool()
        result = await tool.execute_with_context({"input": "test"}, sample_context)

        assert result.success is True
        assert "Processed: test" in result.data["result"]


# =============================================================================
# Test: ToolRegistry
# =============================================================================


class TestToolRegistry:
    """Tests for ToolRegistry."""

    def test_registry_register_tool(self, fresh_registry):
        """Test registering a tool."""
        tool = MockTool("test_tool")
        fresh_registry.register(tool)

        assert "test_tool" in fresh_registry.list_tools()

    def test_registry_register_duplicate_raises(self, fresh_registry):
        """Test registering duplicate tool raises ValueError."""
        tool1 = MockTool("duplicate_tool")
        tool2 = MockTool("duplicate_tool")

        fresh_registry.register(tool1)

        with pytest.raises(ValueError) as exc_info:
            fresh_registry.register(tool2)

        assert "already registered" in str(exc_info.value).lower()

    def test_registry_get_tool(self, fresh_registry):
        """Test getting a tool by name."""
        tool = MockTool("get_tool")
        fresh_registry.register(tool)

        retrieved = fresh_registry.get("get_tool")

        assert retrieved is tool

    def test_registry_get_nonexistent_tool(self, fresh_registry):
        """Test getting nonexistent tool returns None."""
        retrieved = fresh_registry.get("nonexistent")

        assert retrieved is None

    def test_registry_list_tools(self, fresh_registry):
        """Test listing all registered tool names."""
        fresh_registry.register(MockTool("tool_a"))
        fresh_registry.register(MockTool("tool_b"))
        fresh_registry.register(MockTool("tool_c"))

        tools = fresh_registry.list_tools()

        assert len(tools) == 3
        assert "tool_a" in tools
        assert "tool_b" in tools
        assert "tool_c" in tools

    def test_registry_get_all_tools(self, fresh_registry):
        """Test getting all registered tools."""
        tool_a = MockTool("tool_a")
        tool_b = MockTool("tool_b")
        fresh_registry.register(tool_a)
        fresh_registry.register(tool_b)

        tools = fresh_registry.get_all_tools()

        assert len(tools) == 2
        assert tool_a in tools
        assert tool_b in tools

    def test_registry_get_all_schemas(self, fresh_registry):
        """Test getting schemas for all tools."""
        fresh_registry.register(MockTool("schema_tool_1"))
        fresh_registry.register(MockTool("schema_tool_2"))

        schemas = fresh_registry.get_all_schemas()

        assert len(schemas) == 2
        assert all("name" in s for s in schemas)
        assert all("description" in s for s in schemas)
        assert all("parameters" in s for s in schemas)

    def test_registry_get_all_domain_tools(self, fresh_registry):
        """Test getting domain Tool objects for all tools."""
        fresh_registry.register(MockTool("domain_tool_1"))
        fresh_registry.register(MockTool("domain_tool_2"))

        domain_tools = fresh_registry.get_all_domain_tools()

        assert len(domain_tools) == 2
        assert all(isinstance(t, Tool) for t in domain_tools)

    @pytest.mark.asyncio
    async def test_registry_execute_tool(self, fresh_registry, sample_context):
        """Test executing a tool through registry."""
        fresh_registry.register(MockTool("exec_tool"))

        result = await fresh_registry.execute_tool(
            tool_name="exec_tool",
            params={"input": "test_input"},
            context=sample_context,
        )

        assert result.success is True
        assert "test_input" in result.data["result"]

    @pytest.mark.asyncio
    async def test_registry_execute_nonexistent_tool(
        self, fresh_registry, sample_context
    ):
        """Test executing nonexistent tool returns error."""
        result = await fresh_registry.execute_tool(
            tool_name="nonexistent",
            params={},
            context=sample_context,
        )

        assert result.success is False
        assert "not found" in result.error.lower()

    @pytest.mark.asyncio
    async def test_registry_execute_tool_validates_params(
        self, fresh_registry, sample_context
    ):
        """Test execute_tool validates parameters."""
        fresh_registry.register(MockTool("validate_tool"))

        result = await fresh_registry.execute_tool(
            tool_name="validate_tool",
            params={},  # Missing required 'input'
            context=sample_context,
        )

        assert result.success is False
        assert "input" in result.error.lower()

    @pytest.mark.asyncio
    async def test_registry_execute_tool_handles_exception(
        self, fresh_registry, sample_context
    ):
        """Test execute_tool handles tool exceptions."""

        class FailingTool(AgentTool):
            @property
            def name(self) -> str:
                return "failing_tool"

            @property
            def description(self) -> str:
                return "Tool that always fails"

            @property
            def parameters_schema(self) -> Dict[str, Any]:
                return {"type": "object", "properties": {}}

            async def execute_with_context(
                self, params: Dict[str, Any], context: ToolContext
            ) -> ToolResult:
                raise RuntimeError("Intentional failure")

        fresh_registry.register(FailingTool())

        result = await fresh_registry.execute_tool(
            tool_name="failing_tool",
            params={},
            context=sample_context,
        )

        assert result.success is False
        assert "failed" in result.error.lower()

    def test_registry_clear(self, fresh_registry):
        """Test clearing all registered tools."""
        fresh_registry.register(MockTool("tool_1"))
        fresh_registry.register(MockTool("tool_2"))

        assert len(fresh_registry.list_tools()) == 2

        fresh_registry.clear()

        assert len(fresh_registry.list_tools()) == 0


# =============================================================================
# Test: Global Registry
# =============================================================================


class TestGlobalRegistry:
    """Tests for the global tool_registry."""

    def test_global_registry_exists(self):
        """Test that global registry exists."""
        assert tool_registry is not None
        assert isinstance(tool_registry, AgentToolRegistry)

    def test_global_registry_has_methods(self):
        """Test that global registry has expected methods."""
        assert hasattr(tool_registry, "register")
        assert hasattr(tool_registry, "get")
        assert hasattr(tool_registry, "list_tools")
        assert hasattr(tool_registry, "execute_tool")
