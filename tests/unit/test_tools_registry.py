"""Test for tools registry to improve coverage"""

import pytest
from faultmaven.tools.registry import ToolRegistry, tool_registry, register_tool
from faultmaven.models.interfaces import BaseTool


class MockTool(BaseTool):
    """Mock tool for testing"""
    
    def __init__(self, **kwargs):
        pass
    
    def execute(self, *args, **kwargs):
        return "mock result"
    
    def get_schema(self):
        return {"name": "mock_tool", "description": "A mock tool"}
    
    async def arun(self, *args, **kwargs):
        return "mock result"


class InvalidTool:
    """Tool that doesn't implement BaseTool interface"""
    pass


class FailingTool(BaseTool):
    """Tool that fails during initialization"""
    
    def __init__(self, **kwargs):
        raise ValueError("Initialization failed")
    
    def execute(self, *args, **kwargs):
        return "failing result"
    
    def get_schema(self):
        return {"name": "failing_tool", "description": "A failing tool"}
    
    async def arun(self, *args, **kwargs):
        return "failing result"


def test_tool_registry_register_invalid_tool():
    """Test registering a tool that doesn't implement BaseTool interface"""
    # This should raise ValueError and hit line 34
    with pytest.raises(ValueError, match="must implement BaseTool interface"):
        ToolRegistry.register("invalid_tool", InvalidTool)


def test_tool_registry_get_nonexistent_tool():
    """Test getting a tool that doesn't exist"""
    # This should return None and hit line 42
    result = ToolRegistry.get_tool("nonexistent_tool")
    assert result is None


def test_tool_registry_create_all_tools_with_failure():
    """Test creating all tools when one fails"""
    # Clear registry first
    ToolRegistry._tools.clear()
    
    # Register a good tool and a failing tool
    ToolRegistry.register("mock_tool", MockTool)
    ToolRegistry.register("failing_tool", FailingTool)
    
    # This should handle the exception and hit lines 66-67
    tools = ToolRegistry.create_all_tools()
    
    # Should only return the successful tool
    assert len(tools) == 1
    assert isinstance(tools[0], MockTool)


def test_register_tool_decorator():
    """Test the register_tool decorator"""
    # Clear registry first
    ToolRegistry._tools.clear()
    
    @register_tool("decorated_tool")
    class DecoratedTool(BaseTool):
        def __init__(self, **kwargs):
            pass
        
        def execute(self, *args, **kwargs):
            return "decorated result"
        
        def get_schema(self):
            return {"name": "decorated_tool", "description": "A decorated tool"}
        
        async def arun(self, *args, **kwargs):
            return "decorated result"
    
    # Check that it was registered
    tool_class = ToolRegistry.get_tool("decorated_tool")
    assert tool_class is DecoratedTool
    
    # Check that it can be instantiated
    tools = ToolRegistry.create_all_tools()
    assert len(tools) == 1
    assert isinstance(tools[0], DecoratedTool)


def test_tool_registry_singleton():
    """Test that ToolRegistry is a singleton"""
    registry1 = ToolRegistry()
    registry2 = ToolRegistry()
    assert registry1 is registry2
    assert registry1 is tool_registry


def test_tool_registry_basic_operations():
    """Test basic registry operations"""
    # Clear registry first
    ToolRegistry._tools.clear()
    
    # Register a tool
    ToolRegistry.register("test_tool", MockTool)
    
    # Test list_tools
    tools = ToolRegistry.list_tools()
    assert "test_tool" in tools
    
    # Test get_tool
    tool_class = ToolRegistry.get_tool("test_tool")
    assert tool_class is MockTool
    
    # Test create_all_tools
    instances = ToolRegistry.create_all_tools()
    assert len(instances) == 1
    assert isinstance(instances[0], MockTool)