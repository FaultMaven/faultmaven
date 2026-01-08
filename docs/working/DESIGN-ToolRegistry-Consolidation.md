# ToolRegistry Consolidation Design

**Status**: Implementation Required
**Priority**: High
**Affected Module**: `agent` (tools subsystem)
**Issue**: Dual registry implementations causing test failures

---

## Executive Summary

The FaultMaven agent module currently has **two separate tool registry implementations** that serve overlapping purposes:

1. **`ToolRegistry`** in `faultmaven/modules/agent/tools/registry.py` - Class-based, legacy implementation
2. **`AgentToolRegistry`** in `faultmaven/modules/agent/tools/base.py` - Instance-based, complete implementation

Tests expect the `AgentToolRegistry` behavior but import from `registry.py`, which exports the incomplete `ToolRegistry`. This architectural inconsistency is causing 15 test failures.

**Recommended Solution**: Consolidate to a single `ToolRegistry` class that supports both registration patterns and re-export `AgentToolRegistry` from `registry.py` as the canonical implementation.

---

## Problem Analysis

### Current State

#### File: `faultmaven/modules/agent/tools/registry.py`
```python
class ToolRegistry:
    """Registry for dynamically registering and managing tools"""
    _instance = None
    _tools: Dict[str, Type[BaseTool]] = {}

    # Methods:
    - register(name: str, tool_class: Type[BaseTool])  # Class-based only
    - get_tool(name: str) -> Optional[Type[BaseTool]]
    - list_tools() -> List[str]
    - create_all_tools(**kwargs) -> List[BaseTool]

tool_registry = ToolRegistry()  # Global singleton
```

**Issues**:
- Only supports class-based registration
- Returns classes, not instances
- No `get()` method (has `get_tool()` instead)
- No `execute_tool()` method
- No context-aware execution
- Uses `BaseTool` interface (not `AgentTool`)

#### File: `faultmaven/modules/agent/tools/base.py`
```python
class AgentToolRegistry:
    """Registry for agent-specific tools"""
    _tools: Dict[str, AgentTool] = {}

    # Methods:
    - register(tool: AgentTool)  # Instance-based only
    - get(name: str) -> Optional[AgentTool]
    - list_tools() -> List[str]
    - get_all_tools() -> List[AgentTool]
    - get_all_schemas() -> List[Dict[str, Any]]
    - get_all_domain_tools() -> List[Tool]
    - async execute_tool(name, params, context) -> ToolResult
    - clear()

tool_registry = AgentToolRegistry()  # Global singleton
```

**Strengths**:
- Complete implementation with all required methods
- Instance-based registration with pre-configured tools
- Context-aware execution with validation
- Error handling and exception safety
- Uses `AgentTool` interface (extends `BaseTool`)

#### Test Expectations (`tests/unit/tools/test_tool_registry.py`)
```python
from faultmaven.modules.agent.tools.registry import (
    ToolRegistry,    # Imports from registry.py
    tool_registry,   # Global instance
)

# Expected API:
fresh_registry.register(tool_instance)  # Instance-based
tool = fresh_registry.get(tool_name)     # Get by name
result = await fresh_registry.execute_tool(tool_name, params, context)
schemas = fresh_registry.get_all_schemas()
domain_tools = fresh_registry.get_all_domain_tools()
```

### Root Cause

The codebase evolved with two registry patterns:
1. **Legacy**: Class-based `ToolRegistry` for lazy instantiation (registry.py)
2. **Modern**: Instance-based `AgentToolRegistry` for pre-configured tools (base.py)

Tests were written against the modern API but import from the legacy location, creating a mismatch.

---

## Solution Design

### Approach: Consolidate to Single Implementation

**Strategy**: Make `AgentToolRegistry` the canonical implementation and re-export from `registry.py` for backward compatibility.

### Architecture Changes

#### 1. Update `registry.py` to Re-export `AgentToolRegistry`

```python
"""
Tool Registry for dynamic tool registration.

This module provides the canonical ToolRegistry implementation for agent tools.
The registry supports instance-based registration with context-aware execution.
"""

from typing import Dict, List, Type, Optional
import logging

# Import the complete implementation
from faultmaven.modules.agent.tools.base import AgentToolRegistry as ToolRegistry
from faultmaven.modules.agent.tools.base import tool_registry

# Re-export for backward compatibility
__all__ = ['ToolRegistry', 'tool_registry', 'register_tool']


def register_tool(name: str):
    """
    Decorator for registering tools (legacy pattern).

    Note: This pattern is deprecated. Use instance-based registration:
        tool_registry.register(MyTool())

    Usage:
        @register_tool("knowledge_base")
        class KnowledgeBaseTool(AgentTool):
            ...
    """
    def decorator(cls):
        # Instantiate and register
        tool_instance = cls()
        tool_registry.register(tool_instance)
        return cls
    return decorator
```

**Rationale**:
- Minimal code changes - just import and re-export
- Preserves backward compatibility for existing code
- Centralizes implementation in `base.py` where `AgentTool` is defined
- Deprecates class-based decorator pattern in favor of instance registration

#### 2. Optional: Support Dual Registration in `AgentToolRegistry`

If backward compatibility with class-based registration is critical, enhance `AgentToolRegistry.register()`:

```python
# In faultmaven/modules/agent/tools/base.py

class AgentToolRegistry:
    """Registry for agent-specific tools."""

    def __init__(self) -> None:
        """Initialize the agent tool registry."""
        self._tools: Dict[str, AgentTool] = {}
        self._tool_classes: Dict[str, Type[AgentTool]] = {}  # NEW: Store classes

    def register(self, tool_or_name, tool_class=None) -> None:
        """Register an agent tool (supports both patterns).

        Patterns:
            1. Instance-based: register(tool_instance)
            2. Class-based: register(tool_name, ToolClass)  # Lazy instantiation

        Args:
            tool_or_name: AgentTool instance OR tool name string
            tool_class: Tool class (optional, for class-based registration)

        Raises:
            ValueError: If tool with same name already registered
            TypeError: If arguments don't match expected patterns
        """
        if tool_class is None:
            # Pattern 1: Instance-based registration
            if not isinstance(tool_or_name, AgentTool):
                raise TypeError(f"Expected AgentTool instance, got {type(tool_or_name)}")
            tool = tool_or_name
            if tool.name in self._tools:
                raise ValueError(f"Tool '{tool.name}' is already registered")
            self._tools[tool.name] = tool
            logger.debug(f"Registered agent tool (instance): {tool.name}")
        else:
            # Pattern 2: Class-based registration (lazy instantiation)
            if not isinstance(tool_or_name, str):
                raise TypeError(f"Expected tool name (str), got {type(tool_or_name)}")
            name = tool_or_name
            if name in self._tool_classes or name in self._tools:
                raise ValueError(f"Tool '{name}' is already registered")
            self._tool_classes[name] = tool_class
            logger.debug(f"Registered agent tool (class): {name}")

    def get(self, name: str) -> Optional[AgentTool]:
        """Get a tool by name (instantiates if class-based).

        Args:
            name: Tool name

        Returns:
            Tool instance or None if not found
        """
        # Check instance cache first
        if name in self._tools:
            return self._tools[name]

        # Check class registry and instantiate
        if name in self._tool_classes:
            tool_class = self._tool_classes[name]
            tool_instance = tool_class()
            # Cache the instance for future calls
            self._tools[name] = tool_instance
            logger.debug(f"Instantiated tool from class: {name}")
            return tool_instance

        return None
```

**Trade-offs**:
- **Pro**: Full backward compatibility with existing class-based code
- **Pro**: Supports lazy instantiation for expensive tool initialization
- **Con**: Increased complexity in registry implementation
- **Con**: Two registration patterns to maintain

**Recommendation**: Start with simple re-export (Option 1) unless there's proven need for class-based registration in production code.

---

## Implementation Plan

### Phase 1: Immediate Fix (< 1 hour)

**Goal**: Make all 15 tests pass with minimal changes

**Tasks**:
1. ✅ Read current implementations and test expectations (completed)
2. Update `faultmaven/modules/agent/tools/registry.py`:
   - Remove `ToolRegistry` class definition
   - Import `AgentToolRegistry` from `base.py`
   - Re-export as `ToolRegistry` for backward compatibility
   - Update `register_tool` decorator to use instance registration
3. Run tests: `pytest tests/unit/tools/test_tool_registry.py -v`
4. Verify no regressions in other modules

**Files Modified**:
- `faultmaven/modules/agent/tools/registry.py` (20 lines changed)

**Risk**: Low - only consolidating existing working implementation

### Phase 2: Deprecation and Migration (Optional, Future)

**Goal**: Migrate all class-based registration to instance-based

**Tasks**:
1. Search codebase for uses of class-based registration:
   ```bash
   grep -r "register_tool\|ToolRegistry.*register" --include="*.py"
   ```
2. Update each usage to instance-based pattern
3. Add deprecation warnings to `register_tool` decorator
4. Document migration guide in `docs/development/`

**Timeline**: 2-4 weeks (depending on usage)

### Phase 3: Cleanup (Optional, Future)

**Goal**: Remove class-based registration support entirely

**Tasks**:
1. Remove `register_tool` decorator
2. Update all documentation
3. Remove `_tool_classes` from `AgentToolRegistry` if added in Option 2

**Timeline**: 1-2 sprints after Phase 2 complete

---

## Testing and Validation Strategy

### Unit Tests

**Existing Tests** (`tests/unit/tools/test_tool_registry.py`):
- ✅ `test_registry_register_tool` - Instance registration
- ✅ `test_registry_register_duplicate_raises` - Duplicate detection
- ✅ `test_registry_get_tool` - Tool retrieval by name
- ✅ `test_registry_get_nonexistent_tool` - Missing tool handling
- ✅ `test_registry_list_tools` - List all tool names
- ✅ `test_registry_get_all_tools` - Get all tool instances
- ✅ `test_registry_get_all_schemas` - Schema generation
- ✅ `test_registry_get_all_domain_tools` - Domain tool conversion
- ✅ `test_registry_execute_tool` - Context-aware execution
- ✅ `test_registry_execute_nonexistent_tool` - Error handling
- ✅ `test_registry_execute_tool_validates_params` - Parameter validation
- ✅ `test_registry_execute_tool_handles_exception` - Exception handling
- ✅ `test_registry_clear` - Registry cleanup

**New Tests Required**:
- Test re-export compatibility: `from registry import ToolRegistry`
- Test backward compatibility with existing code patterns

**Coverage Target**: Maintain 71%+ overall, aim for 80%+ on registry module

### Integration Tests

**Test Scenarios**:
1. Agent workflow uses registered tools via `tool_registry.execute_tool()`
2. Tool registration during application startup
3. Multiple tools with dependencies
4. Tool execution with invalid context
5. Tool execution with missing parameters

**Test Location**: `tests/integration/agent/test_tool_execution.py`

### Acceptance Criteria

- [ ] All 15 unit tests in `test_tool_registry.py` pass
- [ ] No regressions in other test suites
- [ ] Existing code using `tool_registry` continues to work
- [ ] Documentation updated to reflect canonical API
- [ ] Code coverage maintained above 71%

---

## API Reference

### Canonical ToolRegistry API

After consolidation, the public API will be:

```python
from faultmaven.modules.agent.tools.registry import tool_registry, ToolRegistry

# Instance-based registration (recommended)
tool_instance = MyAgentTool(config)
tool_registry.register(tool_instance)

# Retrieval
tool = tool_registry.get("my_tool")
if tool is None:
    print("Tool not found")

# Listing
all_tool_names = tool_registry.list_tools()
all_tool_instances = tool_registry.get_all_tools()
all_schemas = tool_registry.get_all_schemas()
domain_tools = tool_registry.get_all_domain_tools()

# Execution (async)
context = ToolContext(session_id, case_id, org_id, user_id)
result = await tool_registry.execute_tool("my_tool", {"param": "value"}, context)

# Cleanup (testing)
tool_registry.clear()
```

### Deprecated Patterns

```python
# Class-based registration (deprecated, but still works via decorator)
@register_tool("my_tool")
class MyTool(AgentTool):
    pass

# Class storage and lazy instantiation (removed)
ToolRegistry.register("tool_name", MyToolClass)  # No longer supported
```

---

## Migration Guide

### For Tool Developers

**Before** (class-based):
```python
from faultmaven.modules.agent.tools.registry import register_tool

@register_tool("knowledge_base")
class KnowledgeBaseTool(AgentTool):
    def __init__(self):
        self.kb_client = get_kb_client()

    @property
    def name(self) -> str:
        return "knowledge_base"

    # ... rest of implementation
```

**After** (instance-based):
```python
from faultmaven.modules.agent.tools.registry import tool_registry
from faultmaven.modules.agent.tools.base import AgentTool

class KnowledgeBaseTool(AgentTool):
    def __init__(self, kb_client):
        self.kb_client = kb_client

    @property
    def name(self) -> str:
        return "knowledge_base"

    # ... rest of implementation

# Register during application startup
def register_tools(kb_client):
    tool_registry.register(KnowledgeBaseTool(kb_client))
```

**Benefits**:
- Explicit dependency injection
- Easier testing with mock dependencies
- No hidden global state
- Clear initialization order

### For Tool Users

No changes required - the execution API remains identical:

```python
# Before and After - same API
context = ToolContext(...)
result = await tool_registry.execute_tool("knowledge_base", params, context)
```

---

## Security Considerations

### Input Validation

All tool parameters are validated via `AgentTool.validate_params()` before execution:
- Required parameters checked
- Type validation against JSON Schema
- Custom validation in tool implementations

### Error Handling

Registry properly handles:
- Missing tools → `ToolResult(success=False, error="Tool not found")`
- Invalid parameters → `ToolResult(success=False, error="Missing required parameter: X")`
- Tool exceptions → Caught, logged, returned as error result

### Secrets Management

Tools with secrets should use dependency injection:
```python
tool = KnowledgeBaseTool(kb_client=get_kb_client_from_vault())
tool_registry.register(tool)
```

Never hardcode secrets in tool classes.

---

## Performance Considerations

### Registry Lookup Performance

- Tool retrieval: O(1) dictionary lookup
- Instance caching: Tools instantiated once and reused
- No lazy loading overhead after first access

### Memory Usage

- **Before**: Class storage only (minimal)
- **After**: Instance storage (slightly higher, but negligible)
- Typical registry: 10-20 tools × ~1KB each = ~20KB total

### Execution Performance

No performance impact - execution path unchanged:
```python
tool = registry.get(name)  # O(1)
result = await tool.execute_with_context(params, context)  # Same as before
```

---

## Rollback Plan

If consolidation causes unexpected issues:

1. **Immediate rollback** (< 5 minutes):
   ```bash
   git revert <commit-hash>
   ```

2. **Restore original registry.py**:
   ```bash
   git checkout HEAD^ -- faultmaven/modules/agent/tools/registry.py
   ```

3. **Run tests to verify**:
   ```bash
   pytest tests/unit/tools/
   ```

4. **Investigate root cause** before re-attempting

**Rollback Criteria**:
- More than 5 test failures after consolidation
- Production errors related to tool execution
- Performance degradation > 10%

---

## Monitoring and Observability

### Metrics to Track

After deployment, monitor:
- Tool registration count at startup
- Tool execution success/failure rates
- Tool execution latency (p50, p95, p99)
- Registry lookup errors

### Logging

Enhanced logging in `AgentToolRegistry`:
```python
logger.debug(f"Registered agent tool: {tool.name}")
logger.info(f"Executing tool: {tool_name} with params: {params}")
logger.error(f"Tool '{tool_name}' execution failed: {error}")
```

### Alerts

Configure alerts for:
- Zero tools registered at startup (critical)
- Tool execution error rate > 5% (warning)
- Tool execution latency > 1s p95 (warning)

---

## Documentation Updates

### Files to Update

1. **API Documentation**:
   - `docs/reference/agent-tools-api.md` - Update API examples
   - `docs/reference/tool-registry.md` - Document canonical API

2. **Developer Guides**:
   - `docs/development/creating-agent-tools.md` - Update tool creation guide
   - `docs/development/testing-tools.md` - Update testing patterns

3. **Architecture Documentation**:
   - `docs/architecture/agent-module-design.md` - Update registry design section
   - `docs/architecture/TASK-015-agent-orchestration-design.md` - Reflect consolidation

4. **Migration Guide**:
   - `docs/development/tool-registry-migration.md` - Create new migration guide

---

## References

### Related Files

- `faultmaven/modules/agent/tools/registry.py` - Current legacy implementation
- `faultmaven/modules/agent/tools/base.py` - Complete `AgentToolRegistry` implementation
- `tests/unit/tools/test_tool_registry.py` - Test expectations
- `faultmaven/models/interfaces.py` - `BaseTool` interface definition
- `faultmaven/domain/events.py` - `Tool` domain model

### Related Issues

- TASK-015: Agent orchestration design
- Tests failing: 15 failures in `test_tool_registry.py`

### Design Principles

1. **Single Responsibility**: Registry only manages registration and lookup
2. **Open/Closed**: Extensible for new tools without modifying registry
3. **Dependency Inversion**: Tools depend on `AgentTool` interface, not concrete implementations
4. **Interface Segregation**: Clear separation between `BaseTool` and `AgentTool`

---

## Approval and Sign-off

**Prepared by**: Solutions Architect
**Date**: 2026-01-08
**Status**: Ready for Implementation

**Reviewers**:
- [ ] Tech Lead - Architecture approval
- [ ] Test Engineer - Test strategy review
- [ ] Security Auditor - Security implications review
- [ ] Product Manager - Feature parity confirmation

**Implementation Assignee**: TBD
**Target Completion**: 2026-01-09 (Phase 1)
