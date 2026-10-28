# Tool Developer Guide

How to create, test, and register a new agent tool. The code is in `faultmaven/modules/agent/tools/`; there is no decorator, no self-registration and no auto-discovery. A tool is constructed explicitly and registered by hand in the container.

## Quick Start

### 1. Understand the Tool Interface

A tool subclasses `AgentTool` (`faultmaven/modules/agent/tools/base.py`), which extends `BaseTool` (`faultmaven/models/interfaces.py`). It implements four members: `name`, `description`, `parameters_schema` (JSON Schema) and `execute_with_context(params, context)`. The result is a `ToolResult`:

```python
from typing import Any, Optional
from pydantic import BaseModel

class ToolResult(BaseModel):
    success: bool
    data: Any                      # required: pass data=None on a failure
    error: Optional[str] = None
    timed_out: bool = False        # set by tool_failure, from the exception class
```

`data` has no default, so `ToolResult(success=False, error="...")` raises `ValidationError: data Field required`. Every failure passes `data=None`, or goes through `tool_failure` (below).

### 2. Create a New Tool

**Step 1**: Create the file in `faultmaven/modules/agent/tools/`.

```bash
touch faultmaven/modules/agent/tools/my_custom_tool.py
```

**Step 2**: Implement the tool.

```python
"""my_custom_tool.py - Custom tool implementation"""

import logging
from typing import Any, Dict

from faultmaven.models.interfaces import ToolResult
from faultmaven.modules.agent.tools.base import AgentTool, ToolContext, tool_failure

logger = logging.getLogger(__name__)


class MyCustomTool(AgentTool):
    """Custom tool for [specific purpose]."""

    @property
    def name(self) -> str:
        return "my_custom_tool"

    @property
    def description(self) -> str:
        return "Description of what this tool does"

    @property
    def parameters_schema(self) -> Dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "required_param": {
                    "type": "string",
                    "description": "Parameter description",
                },
            },
            "required": ["required_param"],
        }

    async def execute_with_context(
        self, params: Dict[str, Any], context: ToolContext
    ) -> ToolResult:
        if "required_param" not in params:
            return ToolResult(
                success=False,
                data=None,
                error="Missing required parameter: required_param",
            )
        try:
            return ToolResult(success=True, data=self._do_work(params))
        except Exception as e:
            # The exception's text goes to the server log, never into the
            # result: the model reads the result and can repeat it into the
            # answer every reader of the case is served (#2332).
            logger.exception("Tool execution failed: %s", e)
            return tool_failure(
                "Tool execution failed",
                e,
                guidance="Say here what the model can and cannot conclude.",
            )

    def _do_work(self, params: Dict[str, Any]) -> Any:
        return {"result": "data"}
```

`tool_failure(reason, exc, *, guidance="")` returns a failed `ToolResult` whose `error` is `"<reason> (<ExceptionClass>). <guidance>"` and whose `timed_out` is decided by exception class.

**Step 3**: Register it by hand. Nothing discovers the file.

1. Construct it in `register_tools` (`faultmaven/container/providers/tools.py`) and set it on the container, as `search_file_tool` is: `container.my_custom_tool = MyCustomTool(...)` and `container.tools.append(...)`.
2. Give it to the model by adding it to `_create_investigation_tools` (`faultmaven/container/providers/services.py`), which builds the `AgentToolRegistry` for the engine: `registry.register(my_custom_tool)`. `register` raises `ValueError` on a duplicate name.
3. A turn may offer only a subset of the registry: terminal Q&A turns offer only `TERMINAL_QA_TOOLS` (`faultmaven/core/investigation/milestone_engine/terminal_turns.py`), so add the tool there only if it is a read-only lookup. The set is empty when the provider cannot call tools, and on single-shot turns, which pass no tools. The prompts name only the tools the turn gives (`GivenTools`, derived from the registry and that filter).

### 3. Test Your Tool

Create a test file in `tests/unit/modules/agent/tools/test_my_custom_tool.py`. A tool runs through `execute_with_context`, so the test builds a `ToolContext` (`redaction_ctx` is required, by keyword; use the helpers in that directory's tests):

```python
import pytest

from faultmaven.modules.agent.tools.base import ToolContext
from faultmaven.modules.agent.tools.my_custom_tool import MyCustomTool
from tests.model_content import disabled_redaction


def make_context() -> ToolContext:
    return ToolContext(
        session_id="sess_1",
        case_id="case_abc",
        enterprise_id="org_1",
        user_id="user_1",
        redaction_ctx=disabled_redaction(),
    )


@pytest.mark.asyncio
async def test_my_custom_tool_success():
    """Test successful tool execution"""
    tool = MyCustomTool()

    params = {"required_param": "test_value"}
    result = await tool.execute_with_context(params, make_context())

    assert result.success is True
    assert result.error is None
    assert result.data is not None

@pytest.mark.asyncio
async def test_my_custom_tool_missing_param():
    """Test tool with missing required parameter"""
    tool = MyCustomTool()

    params = {}  # Missing required_param
    result = await tool.execute_with_context(params, make_context())

    assert result.success is False
    assert "required parameter" in result.error.lower()

@pytest.mark.asyncio
async def test_tool_schema():
    """Test tool schema is valid"""
    tool = MyCustomTool()
    schema = tool.get_schema()

    assert schema["name"] == "my_custom_tool"
    assert "description" in schema
    assert "parameters" in schema
    assert "required_param" in schema["parameters"]["properties"]
```

Run tests:
```bash
pytest tests/unit/modules/agent/tools/test_my_custom_tool.py -v
```

## Architecture Patterns

### Pattern 1: Direct Implementation (Recommended)

**Use When**: Tool logic can run within FaultMaven process

**Benefits**:
- ✅ No external dependencies
- ✅ Fast execution
- ✅ Full control
- ✅ Easy testing

**Example**: `search_file` (`search_file_tool.py`), `vectorize_file` (`vectorize_file_tool.py`). A tool reaches the model only when `_create_investigation_tools` registers it.

```python
class PatternAnalyzerTool(AgentTool):
    # name, description and parameters_schema as in the Quick Start
    async def execute_with_context(self, params: Dict, context: ToolContext) -> ToolResult:
        # Pure Python logic, no external calls
        patterns = self._analyze_patterns(params['data'])
        return ToolResult(success=True, data=patterns)
```

### Pattern 2: External API Integration

**Use When**: Need to call external REST/GraphQL APIs

**Benefits**:
- ✅ Leverage existing services
- ✅ Language-agnostic
- ⚠️ Requires API authentication
- ⚠️ Network latency

**Example**: Web Search Tool (`web_search.py`)

```python
class ExternalAPITool(AgentTool):
    # name, description and parameters_schema as in the Quick Start
    def __init__(self, api_key: str, api_url: str):
        self.api_key = api_key
        self.api_url = api_url

    async def execute_with_context(self, params: Dict, context: ToolContext) -> ToolResult:
        try:
            async with httpx.AsyncClient() as client:
                response = await client.post(
                    self.api_url,
                    headers={"Authorization": f"Bearer {self.api_key}"},
                    json=params,
                )
                response.raise_for_status()
                return ToolResult(success=True, data=response.json())
        except httpx.HTTPError as e:
            return tool_failure("The API call failed", e)
```

### Pattern 3: MCP Server Integration

**Use When**: Integrating Model Context Protocol servers

**Benefits**:
- ✅ Standardized protocol
- ✅ Growing ecosystem
- ✅ Tool + resource exposure
- ⚠️ Requires MCP server setup

**Status**: design only. There is no MCP client tool in the code (see the banner on the MCP page). The sketch below shows the shape it would take as an `AgentTool`:

```python
from mcp import ClientSession, StdioServerParameters

class MCPServerTool(AgentTool):
    # name, description and parameters_schema as in the Quick Start
    def __init__(self, server_command: List[str]):
        self.server_params = StdioServerParameters(
            command=server_command[0],
            args=server_command[1:]
        )

    async def execute_with_context(self, params: Dict, context: ToolContext) -> ToolResult:
        async with ClientSession(*self.server_params) as session:
            await session.initialize()

            # Call MCP tool
            result = await session.call_tool(
                name=params['tool_name'],
                arguments=params['arguments']
            )

            return ToolResult(
                success=not result.isError,
                data=result.content
            )
```

**See**: [MCP Integration Guide](./integrations/mcp-integration.md)

### Pattern 4: System Command Execution

**Use When**: Need to execute shell commands (kubectl, curl, etc.)

**Security Requirements**:
- ⚠️ Command whitelist required
- ⚠️ User confirmation needed
- ⚠️ Treat output as untrusted before returning it
- ⚠️ Timeout protection essential

**Status**: design only. No system-command tool exists in the code. The sketch below shows the shape it would take as an `AgentTool`:

```python
import asyncio
import shlex

class SystemCommandTool(AgentTool):
    # name, description and parameters_schema as in the Quick Start
    # Whitelist of allowed commands
    ALLOWED_COMMANDS = ['kubectl', 'curl', 'nslookup', 'dig']

    async def execute_with_context(self, params: Dict, context: ToolContext) -> ToolResult:
        command = params['command']
        args = params.get('args', [])

        # Validate command is whitelisted
        if command not in self.ALLOWED_COMMANDS:
            return ToolResult(
                success=False,
                data=None,
                error=f"Command '{command}' not in whitelist"
            )

        # Build safe command
        cmd = [command] + [shlex.quote(arg) for arg in args]

        try:
            # Execute with timeout
            process = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE
            )

            stdout, stderr = await asyncio.wait_for(
                process.communicate(),
                timeout=30.0
            )

            return ToolResult(
                success=process.returncode == 0,
                data={
                    "stdout": stdout.decode(),
                    "stderr": stderr.decode(),
                    "returncode": process.returncode
                }
            )

        except asyncio.TimeoutError as e:
            return tool_failure("Command execution timed out", e)
```

## Privacy and Redaction

When `SANITIZE_PII` is on (`ProtectionSettings.sanitize_pii`, default off), the engine redacts every tool result before the model reads it (`case_redacted`, `faultmaven/core/investigation/milestone_engine/generation.py`); with it off, `case_redacted` passes the text through unchanged. A tool that calls a model with case content itself (`kb_qa`, `case_evidence_search`, `deep_analysis`) sends its messages through `case_redacted_messages(messages, context.redaction_ctx)` (`faultmaven/infrastructure/security/model_content.py`), which redacts under the same switch and mints the content the model boundary requires; unminted content raises `RawModelContentRefused`, which a tool re-raises and never turns into `tool_failure`. No tool sanitizes its own params. An unexpected exception goes through `tool_failure`, which names its class, never its text (#2332); a tool may still return a message written for the model on purpose (`reclassify_evidence` returns its typed domain errors' text, `search_file` the `re.error` text of the model's own pattern).

What this means when you write a tool:
- Do not rely on the registry to sanitize your params; it does not.
- Return nothing in a failure's `error` that you would not want the model to repeat. For an unexpected exception use `tool_failure`; build any other message yourself, from text written for the model.
- If your tool calls a model with case content, build the messages through `case_redacted_messages(messages, context.redaction_ctx)`, as `document_qa_tool.py` and `core/preprocessing/tier2/local_service.py` do. Let `RawModelContentRefused` propagate: `except RawModelContentRefused: raise` before a broad `except Exception`.
- `DataSanitizer.sanitize(data)` (`faultmaven/infrastructure/security/redaction.py`) exists for text outside a case; none of the given tools calls it on its params.

## Dependency Injection Integration

A tool receives its dependencies as constructor arguments, passed where `register_tools` (`faultmaven/container/providers/tools.py`) constructs it from the container. There is no registry that injects them.

### Accessing Dependencies

```python
class DIAwareTool(AgentTool):
    # name, description and parameters_schema as in the Quick Start
    def __init__(self, settings, llm_provider):
        self.settings = settings
        self.llm_provider = llm_provider
```

### Common Dependencies

| Dependency | Type | Use Case |
|------------|------|----------|
| `settings` | FaultMavenSettings | Read configuration |
| `llm_provider` | ILLMProvider | LLM access (rare) |

## Tool Configuration

### Environment Variables

Tools should read config from `settings` object:

```python
class ConfigurableTool(AgentTool):
    def __init__(self, settings=None):
        if settings:
            self.api_key = settings.tools.my_tool_api_key
            self.endpoint = settings.tools.my_tool_endpoint
        else:
            # Fallback to environment
            from faultmaven.config.settings import get_settings
            settings = get_settings()
            self.api_key = settings.tools.my_tool_api_key
            self.endpoint = settings.tools.my_tool_endpoint
```

### Adding Tool Configuration to Settings

Edit `faultmaven/config/settings.py`:

```python
class ToolsSettings(BaseSettings):
    """Tools and external service configuration"""

    # Existing tools
    web_search_api_key: Optional[SecretStr] = Field(default=None)

    # Your new tool. The class has env_prefix="", so a field is read from the
    # environment variable of its upper-cased name (MY_TOOL_API_KEY, ...).
    my_tool_api_key: Optional[SecretStr] = Field(default=None)
    my_tool_endpoint: str = Field(default="https://api.example.com")
    my_tool_timeout: int = Field(default=30)
```

Then in `.env`:
```env
MY_TOOL_API_KEY=your_api_key
MY_TOOL_ENDPOINT=https://api.example.com
MY_TOOL_TIMEOUT=30
```

## Testing Best Practices

### Unit Tests

```python
import pytest
from unittest.mock import Mock, AsyncMock

# make_context() is the helper defined under "Test Your Tool" above

@pytest.fixture
def mock_dependencies():
    """Mock common dependencies"""
    return {
        'settings': Mock(),
        'llm_provider': Mock(),
    }

@pytest.mark.asyncio
async def test_tool_with_mocked_deps(mock_dependencies):
    """Test tool with mocked dependencies"""
    tool = MyTool(**mock_dependencies)
    result = await tool.execute_with_context({'param': 'value'}, make_context())
    assert result.success
```

### Integration Tests

```python
@pytest.mark.integration
@pytest.mark.asyncio
async def test_tool_with_real_settings():
    """Test tool with real settings and a stubbed model (no test reaches a model server)"""
    from unittest.mock import AsyncMock

    from faultmaven.config.settings import get_settings

    settings = get_settings()
    tool = MyTool(settings=settings, llm_provider=AsyncMock())
    result = await tool.execute_with_context({'param': 'value'}, make_context())

    assert result.success
    assert result.data is not None
```

## Documentation Requirements

Each tool must have documentation in `docs/reference/tools/`:

### Documentation Template

Create `docs/reference/tools/my-custom-tool.md`:

````markdown
# My Custom Tool

## Overview
- **File**: `faultmaven/modules/agent/tools/my_custom_tool.py`
- **Status**: ✅ Production / 🟡 Partial / 🔲 Planned
- **Type**: Direct / External API / MCP / System Command
- **Interface**: `AgentTool`

## Purpose
[Clear description of what the tool does and why]

## When Used
- [When the model should call it]

## Capabilities
- Feature 1
- Feature 2
- Feature 3

## Configuration
```env
MY_TOOL_API_KEY=your_key
MY_TOOL_ENDPOINT=https://api.example.com
```

## Usage Examples
```python
# Example invocation
params = {"query": "test"}
result = await tool.execute_with_context(params, context)
```

## API Reference
### execute_with_context(params, context)
- **Parameters**:
  - `param1` (str): Description
  - `param2` (int, optional): Description
- **Returns**: `ToolResult`

### get_schema()
- **Returns**: Tool schema dictionary (`name`, `description`, `parameters`), built from `parameters_schema`

## Security Considerations
[Any security-relevant information]

## Limitations
[Known limitations]

## Future Enhancements
[Planned improvements]
````

## Submission Checklist

Before submitting a new tool:

- [ ] Tool subclasses `AgentTool` and implements `execute_with_context`
- [ ] Tool is registered in `_create_investigation_tools` (`faultmaven/container/providers/services.py`)
- [ ] Failures go through `tool_failure`, or pass `data=None`
- [ ] Tool has unit tests (>80% coverage)
- [ ] Tool has integration tests
- [ ] Tool sends case content to a model only through `case_redacted_messages`, and re-raises `RawModelContentRefused`
- [ ] Tool handles errors gracefully
- [ ] Tool has timeout protection (if I/O intensive)
- [ ] Tool configuration added to `settings.py`
- [ ] Tool documentation created in `docs/reference/tools/`
- [ ] Tool added to catalog (`docs/reference/tools/tool-catalog.md`)
- [ ] Code follows FaultMaven style guide
- [ ] All tests pass (`pytest`)
- [ ] No linter errors (`ruff check`)

## Tool Results and the Context Budget

Tool results are subject to a **context budget** in `StructuredOutputGenerator._tool_augmented_generate()` (`milestone_engine/generation.py`). Each call has two caps (#614): the assembled messages must fit `prompt_budget.tool_observation_max_tokens` on top of the prompt target, and — when the context window of the model in use is known — the messages **plus** the `tools=` definitions offered on that call, your tool's schema among them, must fit what that window leaves beside the completion the call asks for.

When either is exceeded, the engine keeps the head (system + base task) and re-adds tool-call groups newest-first while they fit. Whatever does not fit is dropped **whole**, replaced by a single marker:

> `[Earlier tool calls and their results were elided to stay within the context budget. Re-run a search if you need those specifics.]`

**Implications for tool developers**:

1. **Your result can be rewritten.** A result over `TOOL_RESULT_MAX_CHARS` (8000) is cut by `_truncate_tool_result` (`generation.py`), which keeps the head, so what matters goes first; a `kb_qa` answer is trimmed earlier, in the middle, by `_format_tool_result`; and when `SANITIZE_PII` is on, redaction replaces values with placeholders. A whole tool-call group can also be elided, as below.
2. **Recency wins, not position within your output.** The oldest tool calls are dropped first. On a long tool loop, an early result may vanish from a later iteration's context — so if a finding matters for the turn's conclusion, restate it in the result of the call that acts on it rather than assuming the earlier one is still visible.
3. **Length costs the whole group.** A verbose result makes its group a bigger candidate for elision. Prefer dense output over padded output, but do not truncate to hit a line count — there is no line-based preservation rule to exploit.
4. **Nothing keeps a copy.** An elided result is gone from that call's context and is not archived anywhere the agent or a debugger can reach it — `agent_tool_calls` has had no writer since the orchestration service was removed. Treat the result you return as the only record of it.
5. **Your schema costs every call.** Tool definitions are sent on every tool iteration. On a model whose window is known they count against that window beside the results, so on a small-window model a long description or parameter schema leaves less room for everyone's observations. Keep them as short as the model needs to call the tool correctly.

> **Changed in #982.** The previous mechanism counted characters against a fixed 30K `TOOL_RESULT_BUDGET` and compressed individual results by keeping the first few lines plus any lines matching a high-signal keyword list (`error`, `exception`, `timeout`, `traceback`, …). It lived in `AgentOrchestrationService` and went with it, along with the `AgentToolCall` audit row that used to hold the uncompressed original. If you have tools written to that contract — front-loading findings into the first three lines, or seeding keywords to survive filtering — neither technique does anything now. Points 2 and 4 above are the rules that replaced them.

---

## Common Pitfalls

### ❌ Don't: Hard-code configuration
```python
class BadTool(AgentTool):
    API_KEY = "hardcoded_key"  # Never do this!
```

### ✅ Do: Use settings
```python
class GoodTool(AgentTool):
    def __init__(self, settings):
        self.api_key = settings.tools.my_tool_api_key
```

### ❌ Don't: Put an exception's text in the result
```python
try:
    result = await external_api_call()
except Exception as e:
    return ToolResult(success=False, data=None, error=f"Failed: {e}")  # can carry a URI or host
```

### ✅ Do: Use tool_failure
```python
try:
    result = await external_api_call()
except Exception as e:
    logger.exception("failed: %s", e)
    return tool_failure("Failed", e)
```

### ❌ Don't: Ignore errors
```python
async def execute_with_context(self, params, context):
    result = await external_api_call()  # What if this fails?
    return ToolResult(success=True, data=result)
```

### ✅ Do: Handle errors, with the text in the log
```python
async def execute_with_context(self, params, context):
    try:
        result = await external_api_call()
        return ToolResult(success=True, data=result)
    except Exception as e:
        logger.exception("API call failed: %s", e)
        # "API call failed (ReadTimeout). <guidance>": the class, never the
        # text, which can carry a URL, a key or a host (#2332).
        return tool_failure(
            "API call failed",
            e,
            guidance="This says nothing about the data the API holds.",
        )
```

## Getting Help

- **Architecture Questions**: See [Tool Architecture](./architecture/)
- **Integration Patterns**: See [Integration Guide](./integrations/)
- **Example Tools**: Study existing tools in `faultmaven/modules/agent/tools/`

