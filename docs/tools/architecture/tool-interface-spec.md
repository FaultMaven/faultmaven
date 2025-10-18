# Tool Interface Specification

Technical specification for the `BaseTool` interface and tool architecture.

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
    data: Any
    error: Optional[str] = None

class BaseTool(ABC):
    """
    Base interface for all agent tools.
    
    This interface defines the contract that all tools must implement
    to be used within the FaultMaven agent system.
    """
    
    @abstractmethod
    async def execute(self, params: Dict[str, Any]) -> ToolResult:
        """
        Execute the tool with the given parameters.
        
        Args:
            params: Dictionary of parameters for tool execution
            
        Returns:
            ToolResult containing success status, data, and optional error
            
        Raises:
            ToolExecutionException: When tool execution fails critically
            ValidationException: When parameters don't match schema
            TimeoutException: When execution exceeds configured timeout
        """
        pass
    
    @abstractmethod
    def get_schema(self) -> Dict[str, Any]:
        """
        Return the tool's schema for agent discovery.
        
        Returns:
            Dictionary containing:
                - name: Tool identifier
                - description: What the tool does
                - parameters: JSON Schema for parameters
        """
        pass
```

## Tool Result Schema

### ToolResult Model

```python
@dataclass
class ToolResult:
    """Result of a tool execution"""
    
    success: bool                    # Did execution succeed?
    data: Any                       # Tool output (any JSON-serializable type)
    error: Optional[str] = None     # Error message if failed
```

**Field Specifications**:

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `success` | bool | Yes | Indicates if tool execution succeeded |
| `data` | Any | Yes | Tool output data (None if failed) |
| `error` | str | No | Error message (required if success=False) |

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
ToolResult(
    success=False,
    data=None,
    error="API rate limit exceeded. Retry after 60 seconds."
)
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
- `examples` - Example parameter sets
- `context_aware` - Whether tool uses phase context
- `requires_confirmation` - Whether execution requires user approval

## Tool Lifecycle

### 1. Registration Phase

```python
@register_tool("my_tool")
class MyTool(BaseTool):
    pass
```

**Process**:
1. Module imported at startup
2. `@register_tool` decorator executes
3. Tool class registered in `ToolRegistry._tools`
4. Tool becomes available for instantiation

### 2. Instantiation Phase

```python
# In container.py
def _create_tools(self) -> List[BaseTool]:
    tools = []
    for name, tool_class in tool_registry._tools.items():
        tool = tool_class(
            settings=self._settings,
            sanitizer=self.get_data_sanitizer(),
            knowledge_ingester=self.get_knowledge_ingester()
        )
        tools.append(tool)
    return tools
```

**Process**:
1. DI container requests tools
2. Registry iterates registered tool classes
3. Each tool instantiated with dependencies
4. Tool list provided to agent

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
result = await tool.execute(params)

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
3. Calls `execute()` asynchronously
4. Processes `ToolResult`

### 5. Cleanup Phase

Tools are garbage collected when:
- Container is reset
- Application shuts down
- Tool is explicitly removed

## Design Principles

### 1. Interface Segregation

Tools implement **only** the `BaseTool` interface:
- ✅ Small, focused interface
- ✅ Easy to implement
- ✅ No unnecessary dependencies

### 2. Dependency Injection

Tools receive dependencies via constructor:
```python
def __init__(self, settings=None, sanitizer=None, **kwargs):
    self.settings = settings
    self.sanitizer = sanitizer
    # Accept and ignore other DI parameters
```

Benefits:
- ✅ Testable with mocks
- ✅ Configurable via settings
- ✅ No hard-coded dependencies

### 3. Privacy-First Invariant

All tools must sanitize inputs:
```python
async def execute(self, params: Dict) -> ToolResult:
    # ALWAYS sanitize user input
    sanitized_query = self.sanitizer.sanitize(params['query'])
    
    # Process with sanitized data
    result = await self._process(sanitized_query)
    
    return ToolResult(success=True, data=result)
```

### 4. Graceful Degradation

Tools must handle errors gracefully:
```python
async def execute(self, params: Dict) -> ToolResult:
    try:
        result = await self._do_work(params)
        return ToolResult(success=True, data=result)
    except TimeoutError:
        return ToolResult(success=False, error="Operation timed out")
    except Exception as e:
        self.logger.error(f"Unexpected error: {e}")
        return ToolResult(success=False, error="Internal tool error")
```

## Tool Categories

### Category 1: Direct Implementation Tools

**Characteristics**:
- Run within FaultMaven process
- No external dependencies
- Fast execution
- Full control

**Examples**: Log Analyzer, Data Classifier

**Interface Pattern**:
```python
@register_tool("direct_tool")
class DirectTool(BaseTool):
    async def execute(self, params: Dict) -> ToolResult:
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
@register_tool("api_tool")
class APITool(BaseTool):
    async def execute(self, params: Dict) -> ToolResult:
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

**Examples**: MCP Client Tool

**Interface Pattern**:
```python
@register_tool("mcp_tool")
class MCPTool(BaseTool):
    async def execute(self, params: Dict) -> ToolResult:
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
@register_tool("system_tool")
class SystemTool(BaseTool):
    ALLOWED_COMMANDS = ['kubectl', 'curl', 'nslookup']
    
    async def execute(self, params: Dict) -> ToolResult:
        if params['command'] not in self.ALLOWED_COMMANDS:
            return ToolResult(success=False, error="Command not whitelisted")
        
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
ToolResult(success=False, error="Missing required parameter: query")

# External service errors
ToolResult(success=False, error="API rate limit exceeded. Retry after 60s")

# Timeout errors
ToolResult(success=False, error="Operation timed out after 30s")

# Internal errors
ToolResult(success=False, error="Internal tool error. See logs for details")
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
result = await primary_tool.execute(params)

if not result.success:
    # Fall back to secondary tool
    result = await fallback_tool.execute(params)
```

## Security Requirements

### Input Sanitization

**Required for all tools**:
```python
async def execute(self, params: Dict) -> ToolResult:
    # Sanitize all user inputs
    for key, value in params.items():
        if isinstance(value, str):
            params[key] = self.sanitizer.sanitize(value)
```

### Output Sanitization

**Required for external tools**:
```python
async def execute(self, params: Dict) -> ToolResult:
    result = await self._call_external_api(params)
    
    # Sanitize output before returning
    sanitized = self.sanitizer.sanitize(str(result))
    
    return ToolResult(success=True, data=sanitized)
```

### Access Control

**Dangerous tools must be flagged**:
```python
class DangerousTool(BaseTool):
    REQUIRES_CONFIRMATION = True
    
    async def execute(self, params: Dict) -> ToolResult:
        # Agent will request user confirmation before executing
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
    result = await tool.execute({"param": "value"})
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
    result = await tool.execute({"param": "value"})
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

- [ ] Implements `BaseTool` interface
- [ ] Uses `@register_tool` decorator
- [ ] Returns valid `ToolResult`
- [ ] Provides valid schema via `get_schema()`
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




