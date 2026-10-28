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

    def to_tool(self) -> "Tool": ...                       # a domain Tool; the engine sends get_schema()
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
    error: Optional[str] = None      # Reason if failed; an unexpected exception's text is never put here (tool_failure)
    timed_out: bool = False          # The failure was a timeout, by exception class
```

**Field Specifications**:

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `success` | bool | Yes | Indicates if tool execution succeeded |
| `data` | Any | Yes | Tool output data (None if failed) |
| `error` | str | No | Reason; an unexpected exception's text is never put here (`tool_failure`, #2332), but a tool may return a message written for the model; set when success=False |
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
# faultmaven/core/investigation/milestone_engine/generation.py, build_da_tool_schemas
for agent_tool in self.deps.investigation_tools.get_all_tools():
    schema = agent_tool.get_schema()
    if allowed is not None and schema["name"] not in allowed:
        continue
    tools.append({"type": "function", "function": schema})
```

**Process**:
1. The engine holds the `AgentToolRegistry` built by `_create_investigation_tools`
2. `build_da_tool_schemas` calls `get_schema()` on each registered tool
3. A turn may offer a subset: terminal Q&A turns offer only `TERMINAL_QA_TOOLS` (`terminal_turns.py`)
4. The schemas are sent with the model call

### 4. Execution Phase

```python
# Agent invokes tool
params = {"question": "How do I fix Redis OOM kills?"}
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
def __init__(self, settings, llm_provider):
    self.settings = settings
    self.llm_provider = llm_provider
```

Benefits:
- ✅ Testable with mocks
- ✅ Configurable via settings
- ✅ No hard-coded dependencies

### 3. Redaction

The engine redacts every tool result before the model reads it (`case_redacted`, `faultmaven/core/investigation/milestone_engine/generation.py`). A tool that sends case content to a model itself (`kb_qa`, `case_evidence_search`, `deep_analysis`) passes `context.redaction_ctx` (`ToolContext`), so the values enter the case registry and the placeholders in its answer can be reversed in the reply the user reads. No tool sanitizes its own params. An unexpected exception goes through `tool_failure`, which names its class, never its text (#2332); a tool may still return a message written for the model on purpose (`reclassify_evidence` returns its typed domain errors' text, `search_file` the `re.error` text of the model's own pattern).

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

**Examples**: `search_file`, `vectorize_file`

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

        # Execute with a timeout
        result = await self._safe_execute(params)
        return ToolResult(success=True, data=result)
```

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

The registry does not retry or fall back; the strategies below are guidance for a tool's own code.

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

### Redaction

The registry does not sanitize params or results of a tool. The engine redacts each result before the model reads it, and a tool that calls a model with case content passes `context.redaction_ctx`; see Design Principles, Redaction.

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
2. **When Used**: Which turns offer it
3. **Parameters**: Input schema and examples
4. **Returns**: Output format and examples
5. **Configuration**: Required settings (`faultmaven/config/settings.py`)
6. **Limitations**: Known constraints

## Compliance Checklist

Before tool acceptance:

- [ ] Subclasses `AgentTool` and implements `execute_with_context`
- [ ] Registered in `_create_investigation_tools`
- [ ] Returns a valid `ToolResult` (`data=` on every one; failures via `tool_failure`)
- [ ] Provides a valid schema via `parameters_schema`
- [ ] Passes `context.redaction_ctx` wherever it sends case content to a model
- [ ] Handles errors gracefully
- [ ] Has timeout protection
- [ ] Has unit tests (>80% coverage)
- [ ] Has integration tests
- [ ] Has documentation
- [ ] Passes security review (if accessing external services)

