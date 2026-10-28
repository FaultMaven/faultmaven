# Tool Interface Specification

Technical specification for the `AgentTool` / `BaseTool` interface and tool architecture.

## Overview

FaultMaven uses an **interface-based, pluggable tool architecture** that allows the AI agent to access various capabilities without tight coupling to implementations.

## Core Interfaces

### BaseTool Interface

**Location**: `faultmaven/models/interfaces.py`

```python
from abc import ABC, abstractmethod
from typing import Any, Dict, Optional
from pydantic import BaseModel

class ToolResult(BaseModel):
    """Standardized tool execution result"""
    success: bool
    data: Any                       # required: pass data=None on a failure
    error: Optional[str] = None
    timed_out: bool = False

class BaseTool(ABC):
    """Base interface (faultmaven/models/interfaces.py): one abstract method."""

    @abstractmethod
    async def execute(self, params: Dict[str, Any]) -> ToolResult: ...
```

The interface an agent tool is written against is `AgentTool` (`faultmaven/modules/agent/tools/base.py`), which extends `BaseTool`:

```python
class AgentTool(BaseTool, ABC):
    @property
    @abstractmethod
    def name(self) -> str: ...

    @property
    @abstractmethod
    def description(self) -> str: ...

    @property
    @abstractmethod
    def parameters_schema(self) -> Dict[str, Any]: ...

    @abstractmethod
    async def execute_with_context(
        self, params: Dict[str, Any], context: "ToolContext"
    ) -> ToolResult:
        """Execute the tool; a failure is returned as a ToolResult."""

    async def execute(self, params: Dict[str, Any]) -> ToolResult:
        """Default: a failed result saying the tool requires context."""

    def get_schema(self) -> Dict[str, Any]:
        """{"name", "description", "parameters"} built from the properties."""

    def to_tool(self) -> "Tool": ...                       # the LLM wire shape
    def validate_params(self, params) -> Optional[str]: ...  # required keys and JSON types
```

The engine runs a tool through `AgentToolRegistry.execute_tool(name, params, context)`, which calls `validate_params`, then `execute_with_context`, and turns an exception the tool did not catch into a `tool_failure`.

## Tool Result Schema

### ToolResult Model

```python
class ToolResult(BaseModel):
    """Result of a tool execution"""

    success: bool                    # Did execution succeed?
    data: Any                        # Tool output; required (None on a failure)
    error: Optional[str] = None      # Fixed reason if failed, never an exception's text
    timed_out: bool = False          # The failure was a timeout, by exception class
```

**Field Specifications**:

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `success` | bool | Yes | Indicates if tool execution succeeded |
| `data` | Any | Yes | Tool output data (None if failed) |
| `error` | str | No | Fixed reason, never an exception's text (#2332); set when success=False |
| `timed_out` | bool | No | Set by `tool_failure` from the exception class |

**Success Response Example**:
```python
ToolResult(
    success=True,
    data={
        "results": [...],
        "count": 5,
        "relevance_scores": [0.95, 0.89, ...]
    },
    error=None
)
```

**Failure Response Example**:
```python
tool_failure(
    "The API rate limit was exceeded",
    exc,
    guidance="Retry after 60 seconds.",
)
# error == "The API rate limit was exceeded (HTTPStatusError). Retry after 60 seconds."
```

## Tool Schema Specification

### Schema Format

Tools must return a schema conforming to JSON Schema with FaultMaven extensions:

```python
{
    "name": "tool_identifier",           # Unique tool name
    "description": "What the tool does", # Clear, concise description
    "parameters": {                      # JSON Schema for parameters
        "type": "object",
        "properties": {
            "param1": {
                "type": "string",
                "description": "Parameter description"
            },
            "param2": {
                "type": "integer",
                "description": "Optional parameter",
                "default": 10
            }
        },
        "required": ["param1"]          # Required parameters
    }
}
```

### Parameter Types

Supported JSON Schema types:

| Type | JSON Schema | Python Type | Example |
|------|-------------|-------------|---------|
| String | `"type": "string"` | str | `"hello"` |
| Integer | `"type": "integer"` | int | `42` |
| Number | `"type": "number"` | float | `3.14` |
| Boolean | `"type": "boolean"` | bool | `true` |
| Array | `"type": "array"` | list | `[1, 2, 3]` |
| Object | `"type": "object"` | dict | `{"key": "value"}` |
| Null | `"type": "null"` | None | `null` |

### Schema Validation

**Required Fields**:
- ✅ `name` - Must be unique across all tools
- ✅ `description` - Must be clear and actionable
- ✅ `parameters` - Must be valid JSON Schema

**Optional Fields**:
No optional fields are read: `get_schema()` returns only `name`, `description` and `parameters`. Fields such as `examples`, `context_aware` and `requires_confirmation` have no code behind them.

## Tool Lifecycle

### 1. Registration Phase

```python
# faultmaven/container/providers/services.py, _create_investigation_tools
registry = AgentToolRegistry()
registry.register(search_file_tool)   # ValueError if the name is already registered
```

**Process**:
1. `register_tools` (`faultmaven/container/providers/tools.py`) constructs each tool and sets it on the container
2. `_create_investigation_tools` registers, by hand, the tools the model is given this deployment (flag-gated tools only when their flag is on)
3. There is no decorator, no scan and no auto-discovery

### 2. Instantiation Phase

```python
# faultmaven/container/providers/tools.py, register_tools
container.search_file_tool = SearchFileTool(...)
container.tools.append(container.search_file_tool)
```

**Process**:
1. `register_tools` runs once, from the composition root
2. Each tool is constructed with its dependencies as constructor arguments
3. The tool is set on the container, ready for `_create_investigation_tools`

### 3. Discovery Phase

```python
# Agent discovers tool capabilities
for tool in self.tools:
    schema = tool.get_schema()
    self.tool_schemas[schema['name']] = schema
```

**Process**:
1. Agent receives tool list
2. Calls `get_schema()` on each tool
3. Builds tool capability map
4. Makes tools available for invocation

### 4. Execution Phase

```python
# Agent invokes tool
params = {"query": "Redis OOM solutions", "limit": 5}
result = await registry.execute_tool("kb_qa", params, context)

if result.success:
    # Process tool output
    process_results(result.data)
else:
    # Handle error
    log_error(result.error)
```

**Process**:
1. Agent selects appropriate tool
2. Prepares parameters matching schema
3. Calls `execute_with_context()` asynchronously via `AgentToolRegistry.execute_tool`
4. Processes `ToolResult`

### 5. Cleanup Phase

Tools are garbage collected when:
- Container is reset
- Application shuts down
- Tool is explicitly removed

## Design Principles

### 1. Interface Segregation

Tools implement **only** the `AgentTool` interface:
- ✅ Small, focused interface
- ✅ Easy to implement
- ✅ No unnecessary dependencies

### 2. Dependency Injection

Tools receive dependencies via constructor:
```python
def __init__(self, settings, sanitizer):
    self.settings = settings
    self.sanitizer = sanitizer
```

Benefits:
- ✅ Testable with mocks
- ✅ Configurable via settings
- ✅ No hard-coded dependencies

### 3. Privacy-First Invariant

All tools must sanitize inputs:
```python
async def execute_with_context(self, params: Dict, context: ToolContext) -> ToolResult:
    # ALWAYS sanitize user input
    sanitized_query = self.sanitizer.sanitize(params['query'])

    # Process with sanitized data
    result = await self._process(sanitized_query)

    return ToolResult(success=True, data=result)
```

### 4. Graceful Degradation

Tools must handle errors gracefully:
```python
from faultmaven.modules.agent.tools.base import tool_failure

async def execute_with_context(self, params: Dict, context: ToolContext) -> ToolResult:
    try:
        result = await self._do_work(params)
        return ToolResult(success=True, data=result)
    except TimeoutError as e:
        # Records timed_out, which the engine reads (#2332).
        return tool_failure("Operation timed out", e)
    except Exception as e:
        logger.error("Unexpected error: %s", e)
        return tool_failure("Internal tool error", e)
```

## Tool Categories

### Category 1: Direct Implementation Tools

**Characteristics**:
- Run within FaultMaven process
- No external dependencies
- Fast execution
- Full control

**Examples**: `search_file`, `read_file`

**Interface Pattern**:
```python
class DirectTool(AgentTool):
    async def execute_with_context(self, params: Dict, context: ToolContext) -> ToolResult:
        # Pure Python logic
        result = self._analyze_locally(params)
        return ToolResult(success=True, data=result)
```

### Category 2: External API Tools

**Characteristics**:
- Call external REST/GraphQL APIs
- Require authentication
- Network latency
- May have rate limits

**Examples**: Web Search Tool

**Interface Pattern**:
```python
class APITool(AgentTool):
    async def execute_with_context(self, params: Dict, context: ToolContext) -> ToolResult:
        async with httpx.AsyncClient() as client:
            response = await client.post(self.api_url, json=params)
            return ToolResult(success=True, data=response.json())
```

### Category 3: MCP Protocol Tools

**Characteristics**:
- Use Model Context Protocol
- Standardized interface
- Can be local or remote
- Growing ecosystem

**Examples**: MCP Client Tool (design only; no code)

**Interface Pattern**:
```python
class MCPTool(AgentTool):
    async def execute_with_context(self, params: Dict, context: ToolContext) -> ToolResult:
        async with mcp.ClientSession(self.server_params) as session:
            result = await session.call_tool(
                name=params['tool_name'],
                arguments=params['arguments']
            )
            return ToolResult(success=not result.isError, data=result.content)
```

### Category 4: System Command Tools

**Characteristics**:
- Execute shell commands
- Require sandboxing
- Platform-dependent
- Security-critical

**Examples**: System Commands Tool (planned)

**Interface Pattern**:
```python
class SystemTool(AgentTool):
    ALLOWED_COMMANDS = ['kubectl', 'curl', 'nslookup']

    async def execute_with_context(self, params: Dict, context: ToolContext) -> ToolResult:
        if params['command'] not in self.ALLOWED_COMMANDS:
            return ToolResult(success=False, data=None, error="Command not whitelisted")

        # Execute with timeout and sanitization
        result = await self._safe_execute(params)
        return ToolResult(success=True, data=result)
```

## Performance Requirements

### Latency Targets

| Tool Category | Target Latency | Max Latency | Timeout |
|---------------|----------------|-------------|---------|
| Direct Implementation | < 500ms | 1s | 5s |
| External API | < 2s | 3s | 10s |
| MCP Protocol | < 1s | 3s | 10s |
| System Command | < 1s | 2s | 30s |

### Resource Limits

**Memory**:
- Tool instance: < 50MB
- Tool execution: < 200MB
- Total tools: < 1GB

**Concurrency**:
- Max concurrent executions: 10 per tool
- Queue depth: 100 pending executions
- Timeout: 30s default

## Error Handling

### Error Types

```python
# Parameter validation errors
ToolResult(success=False, data=None, error="Missing required parameter: query")

# External service errors: the class goes in, never the exception's text
tool_failure("The API rate limit was exceeded", exc, guidance="Retry after 60s.")

# Timeout errors: "Operation timed out (TimeoutError).", with timed_out set
tool_failure("Operation timed out", exc)

# Internal errors: the text goes to the log
tool_failure("Internal tool error", exc, guidance="See the server log.")
```

### Error Recovery

**Retry Strategy**:
1. Transient errors: Retry with exponential backoff
2. Rate limit errors: Wait and retry after cooldown
3. Validation errors: Fail immediately
4. Internal errors: Log and fail

**Fallback Strategy**:
```python
# Try primary tool
result = await primary_tool.execute_with_context(params, context)

if not result.success:
    # Fall back to secondary tool
    result = await fallback_tool.execute_with_context(params, context)
```

## Security Requirements

### Input Sanitization

**Required for all tools**:
```python
async def execute_with_context(self, params: Dict, context: ToolContext) -> ToolResult:
    # Sanitize all user inputs
    for key, value in params.items():
        if isinstance(value, str):
            params[key] = self.sanitizer.sanitize(value)
```

### Output Sanitization

**Required for external tools**:
```python
async def execute_with_context(self, params: Dict, context: ToolContext) -> ToolResult:
    result = await self._call_external_api(params)

    # Sanitize output before returning
    sanitized = self.sanitizer.sanitize(str(result))

    return ToolResult(success=True, data=sanitized)
```

### Access Control

**Dangerous tools must be flagged**:
```python
class DangerousTool(AgentTool):
    REQUIRES_CONFIRMATION = True   # not read anywhere; no confirmation step exists in the tool path

    async def execute_with_context(self, params: Dict, context: ToolContext) -> ToolResult:
        pass
```

## Testing Requirements

### Unit Tests

**Required**:
- ✅ Test successful execution
- ✅ Test error handling
- ✅ Test parameter validation
- ✅ Test schema validity
- ✅ Coverage > 80%

```python
@pytest.mark.asyncio
async def test_tool_success():
    tool = MyTool()
    result = await tool.execute_with_context({"param": "value"}, context)
    assert result.success
```

### Integration Tests

**Required**:
- ✅ Test with real dependencies
- ✅ Test timeout behavior
- ✅ Test concurrent execution
- ✅ Test error recovery

```python
@pytest.mark.integration
@pytest.mark.asyncio
async def test_tool_integration():
    tool = MyTool(settings=real_settings)
    result = await tool.execute_with_context({"param": "value"}, context)
    assert result.success
```

## Documentation Requirements

Each tool must document:
1. **Purpose**: What the tool does
2. **When Used**: Which investigation phases
3. **Parameters**: Input schema and examples
4. **Returns**: Output format and examples
5. **Configuration**: Required environment variables
6. **Limitations**: Known constraints
7. **Performance**: Typical latency and throughput

## Compliance Checklist

Before tool acceptance:

- [ ] Subclasses `AgentTool` and implements `execute_with_context`
- [ ] Registered in `_create_investigation_tools`
- [ ] Returns a valid `ToolResult` (`data=` on every one; failures via `tool_failure`)
- [ ] Provides a valid schema via `parameters_schema`
- [ ] Sanitizes all inputs
- [ ] Handles errors gracefully
- [ ] Has timeout protection
- [ ] Has unit tests (>80% coverage)
- [ ] Has integration tests
- [ ] Has documentation
- [ ] Follows privacy-first invariant
- [ ] Passes security review (if accessing external services)

---

**Last Updated**: 2025-10-12
**Version**: 1.0
**Maintainer**: Architecture Team
