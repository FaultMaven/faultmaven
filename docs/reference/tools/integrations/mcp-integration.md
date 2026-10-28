# MCP (Model Context Protocol) Integration Guide

> **Not implemented.** This page describes a design. The modules it imports
> (`faultmaven.infrastructure.mcp`, `faultmaven/modules/agent/tools/mcp_client_tool.py`),
> the `MCP_SERVER_ENV` setting and the `mcp` package are not in this repository.
> The tool API it builds on is real: `AgentTool`, `AgentToolRegistry` and
> `tool_failure` in `faultmaven/modules/agent/tools/base.py`.

How to integrate Model Context Protocol servers with FaultMaven's tool ecosystem.

## Overview

**Model Context Protocol (MCP)** is an open protocol that enables AI applications to securely connect to data sources and tools. FaultMaven can:

1. **Consume MCP servers** - Use external MCP servers as tools
2. **Expose as MCP server** - Make FaultMaven tools available via MCP

## What is MCP?

MCP standardizes how AI assistants connect to:
- **Resources**: Data sources (files, databases, APIs)
- **Prompts**: Pre-defined prompt templates
- **Tools**: Executable functions

**Key Benefits**:
- ✅ Standardized protocol for tool integration
- ✅ Growing ecosystem of MCP servers
- ✅ Secure, sandboxed execution
- ✅ Language-agnostic (Python, TypeScript, etc.)

**Learn More**: https://modelcontextprotocol.io/

## Architecture Patterns

### Pattern 1: FaultMaven as MCP Client (Consuming External Tools)

```
┌─────────────────┐
│  FaultMaven     │
│  Agent          │
└────────┬────────┘
         │
         │ uses tool via
         │ MCPClientTool
         ▼
┌─────────────────┐
│ MCPClientTool   │  ← Implements AgentTool
│ (FaultMaven)    │
└────────┬────────┘
         │
         │ MCP Protocol
         │ (stdio/HTTP)
         ▼
┌─────────────────┐
│ External MCP    │
│ Server          │
│ (e.g., GitHub,  │
│  Slack, custom) │
└─────────────────┘
```

### Pattern 2: FaultMaven as MCP Server (Exposing Internal Tools)

```
┌─────────────────┐
│ External AI     │
│ Application     │
│ (Claude, GPT)   │
└────────┬────────┘
         │
         │ MCP Protocol
         ▼
┌─────────────────┐
│ FaultMaven      │
│ MCP Server      │  ← Exposes FaultMaven tools
└────────┬────────┘
         │
         │ wraps
         ▼
┌─────────────────┐
│ FaultMaven      │
│ Tools           │
│ (KB, Web, Logs) │
└─────────────────┘
```

## Pattern 1: Consuming MCP Servers

### Use Cases

**When to use external MCP servers**:
- Access organization-specific data sources
- Integrate with enterprise tools (Jira, Slack, GitHub)
- Use specialized analysis tools
- Connect to cloud provider APIs

**Example MCP Servers**:
- **@modelcontextprotocol/server-github** - GitHub API access
- **@modelcontextprotocol/server-filesystem** - File system operations
- **@modelcontextprotocol/server-postgres** - PostgreSQL queries
- **Custom servers** - Organization-specific tools

### Implementation

**Step 1**: Install MCP Python SDK

```bash
pip install mcp
```

**Step 2**: Create MCP Client Tool

Create `faultmaven/modules/agent/tools/mcp_client_tool.py`:

```python
"""MCP Client Tool - Consume external MCP servers"""

import logging
from typing import Any, Dict, List, Optional
from mcp import ClientSession, StdioServerParameters
from mcp.types import Tool as MCPTool

from faultmaven.models.interfaces import ToolResult
from faultmaven.modules.agent.tools.base import AgentTool, ToolContext, tool_failure


class MCPClientTool(AgentTool):
    """
    Tool for connecting to external MCP servers.

    This tool acts as an MCP client, allowing FaultMaven to use
    tools exposed by external MCP servers.
    """

    def __init__(
        self,
        server_command: List[str],
        server_env: Optional[Dict[str, str]] = None,
        sanitizer=None,
    ):
        """
        Initialize MCP client tool.

        Args:
            server_command: Command to start MCP server (e.g., ['npx', '-y', '@modelcontextprotocol/server-github'])
            server_env: Environment variables for server
            sanitizer: Data sanitizer for privacy
        """
        self.logger = logging.getLogger(__name__)
        self.server_params = StdioServerParameters(
            command=server_command[0],
            args=server_command[1:],
            env=server_env or {}
        )
        self.sanitizer = sanitizer
        self._available_tools: Dict[str, MCPTool] = {}

    async def execute_with_context(
        self, params: Dict[str, Any], context: ToolContext
    ) -> ToolResult:
        """
        Execute a tool on the MCP server.

        Args:
            params: Dictionary containing:
                - tool_name: Name of MCP tool to call
                - arguments: Arguments to pass to the tool

        Returns:
            ToolResult with tool execution result
        """
        try:
            tool_name = params.get('tool_name')
            arguments = params.get('arguments', {})

            if not tool_name:
                return ToolResult(
                    success=False,
                    data=None,
                    error="Missing required parameter: tool_name"
                )

            # Sanitize arguments before sending to external server
            if self.sanitizer:
                sanitized_args = {
                    k: self.sanitizer.sanitize(str(v))
                    for k, v in arguments.items()
                }
            else:
                sanitized_args = arguments

            # Connect to MCP server and call tool
            async with ClientSession(*self.server_params) as session:
                await session.initialize()

                # List available tools if not cached
                if not self._available_tools:
                    tools_list = await session.list_tools()
                    self._available_tools = {
                        tool.name: tool
                        for tool in tools_list.tools
                    }

                # Verify tool exists
                if tool_name not in self._available_tools:
                    return ToolResult(
                        success=False,
                        data=None,
                        error=f"Tool '{tool_name}' not found on MCP server. Available: {list(self._available_tools.keys())}"
                    )

                # Call the tool
                result = await session.call_tool(
                    name=tool_name,
                    arguments=sanitized_args
                )

                # Check for errors
                if result.isError:
                    # The server's content is not forwarded: it can carry
                    # hosts, paths or credentials (#2332).
                    return ToolResult(
                        success=False,
                        data=None,
                        error="MCP tool reported an error"
                    )

                # Sanitize output before returning
                sanitized_output = (
                    self.sanitizer.sanitize(str(result.content))
                    if self.sanitizer
                    else result.content
                )

                return ToolResult(
                    success=True,
                    data=sanitized_output
                )

        except Exception as e:
            # The text goes to the log; the model reads the class (#2332).
            self.logger.exception(f"MCP client tool failed: {e}")
            return tool_failure(
                "MCP execution failed",
                e,
                guidance="This says nothing about what the MCP server holds.",
            )

    @property
    def name(self) -> str:
        return "mcp_client"

    @property
    def description(self) -> str:
        return "Execute tools from external MCP servers"

    @property
    def parameters_schema(self) -> Dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "tool_name": {
                    "type": "string",
                    "description": "Name of the MCP tool to call"
                },
                "arguments": {
                    "type": "object",
                    "description": "Arguments to pass to the MCP tool"
                }
            },
            "required": ["tool_name"]
        }

    async def list_available_tools(self) -> List[str]:
        """List available tools on the MCP server"""
        try:
            async with ClientSession(*self.server_params) as session:
                await session.initialize()
                tools_list = await session.list_tools()
                return [tool.name for tool in tools_list.tools]
        except Exception as e:
            self.logger.error(f"Failed to list MCP tools: {e}")
            return []
```

**Step 3**: Configure MCP Server in Settings

Edit `faultmaven/config/settings.py`:

```python
class ToolsSettings(BaseSettings):
    """Tools and external service configuration"""

    # MCP Configuration
    mcp_enabled: bool = Field(default=False, env="MCP_ENABLED")
    mcp_server_command: str = Field(
        default="npx,-y,@modelcontextprotocol/server-filesystem",
        env="MCP_SERVER_COMMAND"
    )
    mcp_server_env: Dict[str, str] = Field(default_factory=dict, env="MCP_SERVER_ENV")
```

**Step 4**: Configure in `.env`

```env
# Enable MCP integration
MCP_ENABLED=true

# MCP Server Command (comma-separated)
MCP_SERVER_COMMAND=npx,-y,@modelcontextprotocol/server-github

# MCP Server Environment Variables (JSON)
MCP_SERVER_ENV={"GITHUB_TOKEN":"your_github_token"}
```

**Step 5**: Register in Container

Construct the tool in `register_tools` (`faultmaven/container/providers/tools.py`), then register it in `_create_investigation_tools` (`faultmaven/container/providers/services.py`), as the existing tools are:

```python
# register_tools: construct it when enabled
if settings.tools.mcp_enabled:
    mcp_server_cmd = settings.tools.mcp_server_command.split(',')
    container.mcp_client_tool = MCPClientTool(
        server_command=mcp_server_cmd,
        server_env=settings.tools.mcp_server_env,
        sanitizer=sanitizer,
    )
    container.tools.append(container.mcp_client_tool)

# _create_investigation_tools: give it to the model
mcp_client_tool = getattr(container, "mcp_client_tool", None)
if mcp_client_tool:
    registry.register(mcp_client_tool)
```

### Usage Example

```python
# Agent can now invoke MCP tools
params = {
    "tool_name": "github_list_issues",
    "arguments": {
        "owner": "myorg",
        "repo": "myrepo",
        "state": "open"
    }
}

result = await mcp_client_tool.execute_with_context(params, context)
# Returns sanitized GitHub issues
```

## Pattern 2: Exposing FaultMaven as MCP Server

### Use Cases

**When to expose FaultMaven tools via MCP**:
- Allow other AI applications to use FaultMaven's tools
- Integrate FaultMaven into existing MCP-based workflows
- Share knowledge base access with other systems
- Provide log analysis capabilities to external tools

### Implementation

**Step 1**: Install MCP Server SDK

```bash
pip install mcp
```

**Step 2**: Create MCP Server Adapter

Create `faultmaven/infrastructure/mcp/server.py`:

```python
"""MCP Server - Expose FaultMaven tools via MCP protocol"""

import logging
from typing import List, Optional
from mcp.server import Server
from mcp.types import Tool, TextContent

from faultmaven.modules.agent.tools.base import AgentTool, ToolContext


class FaultMavenMCPServer:
    """
    MCP Server that exposes FaultMaven tools.

    This allows external AI applications to use FaultMaven's
    troubleshooting tools via the Model Context Protocol.
    """

    def __init__(self, tools: List[AgentTool], context: ToolContext):
        # Design gap: MCP callers have no case, so what ToolContext carries
        # (case_id, enterprise_id, redaction_ctx) has to be decided.
        self.context = context
        self.logger = logging.getLogger(__name__)
        self.tools = {tool.name: tool for tool in tools}
        self.server = Server("faultmaven-tools")
        self._register_handlers()

    def _register_handlers(self):
        """Register MCP protocol handlers"""

        @self.server.list_tools()
        async def list_tools() -> List[Tool]:
            """List available FaultMaven tools"""
            return [
                Tool(
                    name=schema['name'],
                    description=schema['description'],
                    inputSchema=schema['parameters']
                )
                for schema in [tool.get_schema() for tool in self.tools.values()]
            ]

        @self.server.call_tool()
        async def call_tool(name: str, arguments: dict) -> List[TextContent]:
            """Execute a FaultMaven tool"""
            if name not in self.tools:
                raise ValueError(f"Tool '{name}' not found")

            tool = self.tools[name]

            try:
                result = await tool.execute_with_context(arguments, self.context)

                if result.success:
                    return [TextContent(
                        type="text",
                        text=str(result.data)
                    )]
                else:
                    raise RuntimeError(result.error)

            except Exception as e:
                self.logger.error(f"Tool execution failed: {e}")
                raise

    async def run(self):
        """Run the MCP server"""
        from mcp.server.stdio import stdio_server

        async with stdio_server() as (read_stream, write_stream):
            await self.server.run(
                read_stream,
                write_stream,
                self.server.create_initialization_options()
            )
```

**Step 3**: Create MCP Server Entry Point

Create `scripts/run_mcp_server.py`:

```python
#!/usr/bin/env python
"""Run FaultMaven as an MCP server"""

import asyncio
import sys
from pathlib import Path

# Add faultmaven to path
sys.path.insert(0, str(Path(__file__).parent.parent))

from faultmaven.infrastructure.mcp.server import FaultMavenMCPServer
from faultmaven.container import container


async def main():
    """Start FaultMaven MCP server"""
    # Initialize container
    container.initialize()

    # Design gap: take the registry _create_investigation_tools builds
    # (the set the model is given). container.tools is a different list: it holds
    # the unwrapped Q&A tools, lacks the adapters, and adds list_evidence_by_time.
    tools = registry.get_all_tools()

    # Create and run MCP server
    server = FaultMavenMCPServer(tools, context)
    await server.run()


if __name__ == "__main__":
    asyncio.run(main())
```

**Step 4**: Configure MCP Server

Create `mcp_config.json` for Claude Desktop or other MCP clients:

```json
{
  "mcpServers": {
    "faultmaven": {
      "command": "python",
      "args": ["/path/to/faultmaven/scripts/run_mcp_server.py"],
      "env": {
        "CHROMADB_URL": "http://chromadb.faultmaven.local:30080",
        "WEB_SEARCH_API_KEY": "your_key"
      }
    }
  }
}
```

**Step 5**: Test MCP Server

```bash
# Run MCP server
python scripts/run_mcp_server.py

# Or via npx (if packaged)
npx -y @faultmaven/mcp-server
```

### Usage Example

From Claude Desktop or another MCP client:

```
User: "Search the FaultMaven knowledge base for Redis OOM solutions"

Claude: [calls MCP tool]
  Tool: faultmaven
  Function: knowledge_base_search
  Arguments: {"query": "Redis OOM solutions"}

[Returns FaultMaven knowledge base results]
```

## Security Considerations

### For MCP Clients (Consuming External Servers)

1. **Sanitize Inputs**: Design intent: the registry does not sanitize, so the MCP client tool would have to before sending data to an external server
2. **Validate Outputs**: Design intent: sanitize and validate data received from external servers
3. **Command Validation**: Whitelist allowed MCP server commands
4. **Environment Isolation**: Run MCP servers in sandboxed environments
5. **Timeout Protection**: Set timeouts for MCP server calls (default: 30s)

### For MCP Servers (Exposing FaultMaven)

1. **Access Control**: Implement authentication for MCP server access
2. **Rate Limiting**: Limit requests per client
3. **Tool Restrictions**: Only expose non-dangerous tools
4. **Audit Logging**: Log all MCP tool executions
5. **Data Sanitization**: Design intent: sanitize all tool outputs

## Configuration Examples

### GitHub MCP Server

```env
MCP_ENABLED=true
MCP_SERVER_COMMAND=npx,-y,@modelcontextprotocol/server-github
MCP_SERVER_ENV={"GITHUB_TOKEN":"ghp_xxxxx"}
```

### Filesystem MCP Server

```env
MCP_ENABLED=true
MCP_SERVER_COMMAND=npx,-y,@modelcontextprotocol/server-filesystem
MCP_SERVER_ENV={"ALLOWED_DIRECTORIES":"/tmp,/var/log"}
```

### PostgreSQL MCP Server

```env
MCP_ENABLED=true
MCP_SERVER_COMMAND=npx,-y,@modelcontextprotocol/server-postgres
MCP_SERVER_ENV={"POSTGRES_CONNECTION":"postgresql://user:pass@localhost/db"}
```

## Testing MCP Integration

```python
import pytest
from faultmaven.modules.agent.tools.mcp_client_tool import MCPClientTool

@pytest.mark.asyncio
@pytest.mark.integration
async def test_mcp_client_tool():
    """Test MCP client tool integration"""
    tool = MCPClientTool(
        server_command=['npx', '-y', '@modelcontextprotocol/server-filesystem'],
        server_env={'ALLOWED_DIRECTORIES': '/tmp'}
    )

    # Test listing available tools
    tools = await tool.list_available_tools()
    assert 'read_file' in tools

    # Test calling a tool
    result = await tool.execute_with_context(
        {'tool_name': 'list_directory', 'arguments': {'path': '/tmp'}},
        context,
    )

    assert result.success
    assert result.data is not None
```

## Troubleshooting

### MCP Server Won't Start

```bash
# Check command is correct
npx -y @modelcontextprotocol/server-github --help

# Check logs
tail -f logs/faultmaven.log | grep MCP
```

### MCP Tool Execution Fails

```bash
# Enable debug logging
LOG_LEVEL=DEBUG python -m faultmaven.server
```

```python
# Check available tools
await mcp_tool.list_available_tools()

# Test with minimal arguments
result = await mcp_tool.execute_with_context(
    {'tool_name': 'test_tool', 'arguments': {}},
    context,
)
```

## Future Enhancements

- 🔲 MCP server discovery and registration
- 🔲 Dynamic MCP tool loading/unloading
- 🔲 MCP tool composition (chaining)
- 🔲 MCP server health monitoring
- 🔲 MCP tool marketplace integration

## Resources

- **MCP Specification**: https://modelcontextprotocol.io/
- **MCP Python SDK**: https://github.com/modelcontextprotocol/python-sdk
- **MCP Servers**: https://github.com/modelcontextprotocol/servers
- **Claude MCP Guide**: https://docs.anthropic.com/claude/docs/model-context-protocol

