# Tool Developer Guide

How to create, test, and integrate new tools into FaultMaven's pluggable tool architecture.

## Quick Start

### 1. Understand the Tool Interface

All tools must implement the `BaseTool` interface from `faultmaven/models/interfaces.py`:

```python
from abc import ABC, abstractmethod
from typing import Any, Dict
from pydantic import BaseModel

class ToolResult(BaseModel):
    """Standardized tool execution result"""
    success: bool
    data: Any
    error: Optional[str] = None

class BaseTool(ABC):
    """Base interface for all agent tools"""
    
    @abstractmethod
    async def execute(self, params: Dict[str, Any]) -> ToolResult:
        """Execute the tool with given parameters"""
        pass
    
    @abstractmethod
    def get_schema(self) -> Dict[str, Any]:
        """Return tool schema for agent discovery"""
        pass
```

### 2. Create a New Tool

**Step 1**: Create file in `faultmaven/tools/`

```bash
touch faultmaven/tools/my_custom_tool.py
```

**Step 2**: Implement the tool

```python
"""my_custom_tool.py - Custom tool implementation"""

import logging
from typing import Any, Dict
from faultmaven.models.interfaces import BaseTool, ToolResult
from faultmaven.tools.registry import register_tool

@register_tool("my_custom_tool")
class MyCustomTool(BaseTool):
    """
    Custom tool for [specific purpose].
    
    This tool [description of what it does and when to use it].
    """
    
    def __init__(self, config: Optional[Dict] = None, **kwargs):
        """Initialize the tool with configuration"""
        self.logger = logging.getLogger(__name__)
        self.config = config or {}
        # Accept and ignore other parameters from tool registry
    
    async def execute(self, params: Dict[str, Any]) -> ToolResult:
        """
        Execute the tool.
        
        Args:
            params: Dictionary containing:
                - param1: Description
                - param2: Description
        
        Returns:
            ToolResult with success status, data, and optional error
        """
        try:
            # Validate required parameters
            if 'required_param' not in params:
                return ToolResult(
                    success=False,
                    data=None,
                    error="Missing required parameter: required_param"
                )
            
            # Execute tool logic
            result_data = self._do_work(params)
            
            return ToolResult(
                success=True,
                data=result_data,
                error=None
            )
            
        except Exception as e:
            self.logger.error(f"Tool execution failed: {e}")
            return ToolResult(
                success=False,
                data=None,
                error=str(e)
            )
    
    def get_schema(self) -> Dict[str, Any]:
        """Return tool schema for agent"""
        return {
            "name": "my_custom_tool",
            "description": "Description of what this tool does",
            "parameters": {
                "type": "object",
                "properties": {
                    "required_param": {
                        "type": "string",
                        "description": "Parameter description"
                    },
                    "optional_param": {
                        "type": "integer",
                        "description": "Optional parameter description"
                    }
                },
                "required": ["required_param"]
            }
        }
    
    def _do_work(self, params: Dict[str, Any]) -> Any:
        """Private method for actual tool logic"""
        # Implementation
        return {"result": "data"}
```

**Step 3**: Tool is automatically discovered!

```python
# That's it! The @register_tool decorator handles:
# - Registration in ToolRegistry
# - Auto-discovery at startup
# - Availability to agent via DI container
```

### 3. Test Your Tool

Create test file in `tests/unit/tools/test_my_custom_tool.py`:

```python
import pytest
from faultmaven.tools.my_custom_tool import MyCustomTool

@pytest.mark.asyncio
async def test_my_custom_tool_success():
    """Test successful tool execution"""
    tool = MyCustomTool()
    
    params = {"required_param": "test_value"}
    result = await tool.execute(params)
    
    assert result.success is True
    assert result.error is None
    assert result.data is not None

@pytest.mark.asyncio
async def test_my_custom_tool_missing_param():
    """Test tool with missing required parameter"""
    tool = MyCustomTool()
    
    params = {}  # Missing required_param
    result = await tool.execute(params)
    
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
pytest tests/unit/tools/test_my_custom_tool.py -v
```

## Architecture Patterns

### Pattern 1: Direct Implementation (Recommended)

**Use When**: Tool logic can run within FaultMaven process

**Benefits**:
- ✅ No external dependencies
- ✅ Fast execution
- ✅ Full control
- ✅ Easy testing

**Example**: Log Analyzer, Data Classifier

```python
@register_tool("pattern_analyzer")
class PatternAnalyzerTool(BaseTool):
    async def execute(self, params: Dict) -> ToolResult:
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

**Example**: Web Search Tool

```python
@register_tool("external_api")
class ExternalAPITool(BaseTool):
    def __init__(self, api_key: str, api_url: str, **kwargs):
        self.api_key = api_key
        self.api_url = api_url
    
    async def execute(self, params: Dict) -> ToolResult:
        async with httpx.AsyncClient() as client:
            response = await client.post(
                self.api_url,
                headers={"Authorization": f"Bearer {self.api_key}"},
                json=params
            )
            return ToolResult(
                success=response.status_code == 200,
                data=response.json()
            )
```

### Pattern 3: MCP Server Integration

**Use When**: Integrating Model Context Protocol servers

**Benefits**:
- ✅ Standardized protocol
- ✅ Growing ecosystem
- ✅ Tool + resource exposure
- ⚠️ Requires MCP server setup

**Example**: MCP Client Tool

```python
from mcp import ClientSession, StdioServerParameters

@register_tool("mcp_server")
class MCPServerTool(BaseTool):
    def __init__(self, server_command: List[str], **kwargs):
        self.server_params = StdioServerParameters(
            command=server_command[0],
            args=server_command[1:]
        )
    
    async def execute(self, params: Dict) -> ToolResult:
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
- ⚠️ Output sanitization mandatory
- ⚠️ Timeout protection essential

**Example**: System Command Tool

```python
import asyncio
import shlex

@register_tool("system_command")
class SystemCommandTool(BaseTool):
    # Whitelist of allowed commands
    ALLOWED_COMMANDS = ['kubectl', 'curl', 'nslookup', 'dig']
    
    async def execute(self, params: Dict) -> ToolResult:
        command = params['command']
        args = params.get('args', [])
        
        # Validate command is whitelisted
        if command not in self.ALLOWED_COMMANDS:
            return ToolResult(
                success=False,
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
            
        except asyncio.TimeoutError:
            return ToolResult(
                success=False,
                error="Command execution timeout"
            )
```

## Privacy-First Development

**Critical**: All tools MUST sanitize inputs before processing!

### PII Sanitization Pattern

```python
from faultmaven.infrastructure.security.redaction import DataSanitizer

@register_tool("privacy_aware_tool")
class PrivacyAwareTool(BaseTool):
    def __init__(self, sanitizer: DataSanitizer = None, **kwargs):
        self.sanitizer = sanitizer or DataSanitizer()
    
    async def execute(self, params: Dict) -> ToolResult:
        # ALWAYS sanitize user input
        sanitized_query = self.sanitizer.sanitize(params['query'])
        
        # Process with sanitized data
        result = await self._process(sanitized_query)
        
        # Sanitize output before returning
        sanitized_result = self.sanitizer.sanitize(result)
        
        return ToolResult(success=True, data=sanitized_result)
```

### External Tool Double Sanitization

For tools calling external APIs (web search, MCP servers), sanitize TWICE:

```python
async def execute(self, params: Dict) -> ToolResult:
    # Sanitization 1: Input from user
    sanitized_input = self.sanitizer.sanitize(params['query'])
    
    # Sanitization 2: Before external call
    external_safe_input = self.sanitizer.sanitize(
        sanitized_input,
        context={"external_api": True}
    )
    
    # Call external API
    external_result = await self._call_external_api(external_safe_input)
    
    # Sanitize result before returning
    sanitized_output = self.sanitizer.sanitize(external_result)
    
    return ToolResult(success=True, data=sanitized_output)
```

## Dependency Injection Integration

Tools receive dependencies through DI container:

### Accessing Dependencies

```python
@register_tool("di_aware_tool")
class DIAwareTool(BaseTool):
    def __init__(
        self,
        knowledge_ingester=None,  # Optional KB access
        settings=None,            # Optional settings
        sanitizer=None,          # Optional sanitizer
        **kwargs                 # Accept other DI params
    ):
        self.knowledge_ingester = knowledge_ingester
        self.settings = settings
        self.sanitizer = sanitizer
        # Ignore other kwargs from registry
```

### Common Dependencies

| Dependency | Type | Use Case |
|------------|------|----------|
| `knowledge_ingester` | KnowledgeIngester | Access knowledge base |
| `settings` | FaultMavenSettings | Read configuration |
| `sanitizer` | DataSanitizer | PII redaction |
| `llm_provider` | ILLMProvider | LLM access (rare) |

## Tool Configuration

### Environment Variables

Tools should read config from `settings` object:

```python
class ConfigurableTool(BaseTool):
    def __init__(self, settings=None, **kwargs):
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
    web_search_api_key: Optional[SecretStr] = Field(default=None, env="WEB_SEARCH_API_KEY")
    
    # Your new tool
    my_tool_api_key: Optional[SecretStr] = Field(default=None, env="MY_TOOL_API_KEY")
    my_tool_endpoint: str = Field(default="https://api.example.com", env="MY_TOOL_ENDPOINT")
    my_tool_timeout: int = Field(default=30, env="MY_TOOL_TIMEOUT")
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

@pytest.fixture
def mock_dependencies():
    """Mock common dependencies"""
    return {
        'settings': Mock(),
        'sanitizer': Mock(sanitize=lambda x: x),
        'knowledge_ingester': Mock()
    }

@pytest.mark.asyncio
async def test_tool_with_mocked_deps(mock_dependencies):
    """Test tool with mocked dependencies"""
    tool = MyTool(**mock_dependencies)
    result = await tool.execute({'param': 'value'})
    assert result.success
```

### Integration Tests

```python
@pytest.mark.integration
@pytest.mark.asyncio
async def test_tool_with_real_dependencies():
    """Test tool with real infrastructure"""
    from faultmaven.config.settings import get_settings
    from faultmaven.infrastructure.security.redaction import DataSanitizer
    
    settings = get_settings()
    sanitizer = DataSanitizer(settings)
    
    tool = MyTool(settings=settings, sanitizer=sanitizer)
    result = await tool.execute({'param': 'value'})
    
    assert result.success
    assert result.data is not None
```

## Documentation Requirements

Each tool must have documentation in `docs/tools/implemented/` or `docs/tools/planned/`:

### Documentation Template

Create `docs/tools/implemented/my-custom-tool.md`:

```markdown
# My Custom Tool

## Overview
- **File**: `faultmaven/tools/my_custom_tool.py`
- **Status**: ✅ Production / 🟡 Partial / 🔲 Planned
- **Type**: Direct / External API / MCP / System Command
- **Interface**: `BaseTool`

## Purpose
[Clear description of what the tool does and why]

## When Used
- Phase X: [Description]
- Phase Y: [Description]

## Capabilities
- Feature 1
- Feature 2
- Feature 3

## Configuration
\`\`\`env
MY_TOOL_API_KEY=your_key
MY_TOOL_ENDPOINT=https://api.example.com
\`\`\`

## Usage Examples
\`\`\`python
# Example invocation
params = {"query": "test"}
result = await tool.execute(params)
\`\`\`

## API Reference
### execute(params)
- **Parameters**:
  - `param1` (str): Description
  - `param2` (int, optional): Description
- **Returns**: `ToolResult`

### get_schema()
- **Returns**: Tool schema dictionary

## Performance
- Latency: [typical latency]
- Success Rate: [typical success rate]
- Cache: [caching behavior]

## Security Considerations
[Any security-relevant information]

## Limitations
[Known limitations]

## Future Enhancements
[Planned improvements]
```

## Submission Checklist

Before submitting a new tool:

- [ ] Tool implements `BaseTool` interface
- [ ] Tool uses `@register_tool("name")` decorator
- [ ] Tool has unit tests (>80% coverage)
- [ ] Tool has integration tests
- [ ] Tool sanitizes all inputs (if using external data)
- [ ] Tool handles errors gracefully
- [ ] Tool has timeout protection (if I/O intensive)
- [ ] Tool configuration added to `settings.py`
- [ ] Tool documentation created in `docs/tools/`
- [ ] Tool added to catalog (`docs/tools/tool-catalog.md`)
- [ ] Code follows FaultMaven style guide
- [ ] All tests pass (`pytest`)
- [ ] No linter errors (`ruff check`)

## Common Pitfalls

### ❌ Don't: Hard-code configuration
```python
class BadTool(BaseTool):
    API_KEY = "hardcoded_key"  # Never do this!
```

### ✅ Do: Use settings
```python
class GoodTool(BaseTool):
    def __init__(self, settings=None, **kwargs):
        self.api_key = settings.tools.my_tool_api_key
```

### ❌ Don't: Skip sanitization
```python
async def execute(self, params):
    return ToolResult(success=True, data=params['raw_input'])  # Dangerous!
```

### ✅ Do: Always sanitize
```python
async def execute(self, params):
    sanitized = self.sanitizer.sanitize(params['raw_input'])
    return ToolResult(success=True, data=sanitized)
```

### ❌ Don't: Ignore errors
```python
async def execute(self, params):
    result = await external_api_call()  # What if this fails?
    return ToolResult(success=True, data=result)
```

### ✅ Do: Handle errors
```python
async def execute(self, params):
    try:
        result = await external_api_call()
        return ToolResult(success=True, data=result)
    except Exception as e:
        self.logger.error(f"API call failed: {e}")
        return ToolResult(success=False, error=str(e))
```

## Getting Help

- **Architecture Questions**: See [Tool Architecture](./architecture/)
- **Integration Patterns**: See [Integration Guide](./integrations/)
- **Example Tools**: Study existing tools in `faultmaven/tools/`
- **Community**: Discord channel #tool-development

---

**Last Updated**: 2025-10-12  
**Version**: 1.0  
**Maintainer**: Architecture Team




