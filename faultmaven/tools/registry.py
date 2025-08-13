"""
Tool Registry for dynamic tool registration.

This module provides a registry pattern for tools to self-register,
enabling dynamic tool discovery and instantiation.
"""

from typing import Dict, List, Type, Optional
import logging
from faultmaven.models.interfaces import BaseTool


class ToolRegistry:
    """Registry for dynamically registering and managing tools"""
    
    _instance = None
    _tools: Dict[str, Type[BaseTool]] = {}
    
    def __new__(cls):
        if cls._instance is None:
            cls._instance = super().__new__(cls)
        return cls._instance
    
    @classmethod
    def register(cls, name: str, tool_class: Type[BaseTool]):
        """
        Register a tool class.
        
        Args:
            name: Unique name for the tool
            tool_class: Tool class implementing BaseTool
        """
        if not issubclass(tool_class, BaseTool):
            raise ValueError(f"{tool_class} must implement BaseTool interface")
        
        cls._tools[name] = tool_class
        logging.getLogger(__name__).debug(f"Registered tool: {name}")
    
    @classmethod
    def get_tool(cls, name: str) -> Optional[Type[BaseTool]]:
        """Get a registered tool class by name"""
        return cls._tools.get(name)
    
    @classmethod
    def list_tools(cls) -> List[str]:
        """List all registered tool names"""
        return list(cls._tools.keys())
    
    @classmethod
    def create_all_tools(cls, **kwargs) -> List[BaseTool]:
        """
        Create instances of all registered tools.
        
        Args:
            **kwargs: Arguments to pass to tool constructors
            
        Returns:
            List of instantiated tools
        """
        tools = []
        for name, tool_class in cls._tools.items():
            try:
                tool = tool_class(**kwargs)
                tools.append(tool)
                logging.getLogger(__name__).debug(f"Created tool: {name}")
            except Exception as e:
                logging.getLogger(__name__).warning(f"Failed to create tool {name}: {e}")
        
        return tools


# Global registry instance
tool_registry = ToolRegistry()


def register_tool(name: str):
    """
    Decorator for registering tools.
    
    Usage:
        @register_tool("knowledge_base")
        class KnowledgeBaseTool(BaseTool):
            ...
    """
    def decorator(cls):
        tool_registry.register(name, cls)
        return cls
    return decorator