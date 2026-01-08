"""
Tool Registry for dynamic tool registration.

This module provides a registry pattern for tools to self-register,
enabling dynamic tool discovery and instantiation.

CONSOLIDATION NOTE (2026-01-08):
The canonical ToolRegistry implementation is AgentToolRegistry in base.py.
This module re-exports it for backward compatibility.
See docs/working/DESIGN-ToolRegistry-Consolidation.md for details.
"""

from typing import Optional
import logging

# Import the canonical registry implementation
from faultmaven.modules.agent.tools.base import AgentToolRegistry

# Re-export as ToolRegistry for backward compatibility
ToolRegistry = AgentToolRegistry

# Global registry instance
tool_registry = ToolRegistry()

logger = logging.getLogger(__name__)


def register_tool(name: str):
    """
    Decorator for registering tool instances.

    Usage:
        @register_tool("knowledge_base")
        class KnowledgeBaseTool(AgentTool):
            ...

    Note: This is a legacy decorator. New code should register
    tool instances directly with tool_registry.register(tool_instance).
    """
    def decorator(cls):
        try:
            # Instantiate the tool and register it
            tool_instance = cls()
            tool_registry.register(tool_instance)
            logger.debug(f"Registered tool via decorator: {name}")
        except Exception as e:
            logger.warning(f"Failed to register tool {name}: {e}")
        return cls
    return decorator


__all__ = ['ToolRegistry', 'tool_registry', 'register_tool']