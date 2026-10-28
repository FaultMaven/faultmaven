# FaultMaven Agent Tools

The tools the AI agent can call during an investigation. The code is in `faultmaven/modules/agent/tools/`.

## Overview

A tool is an `AgentTool` subclass (`faultmaven/modules/agent/tools/base.py`) with a name, a description, a JSON-Schema `parameters_schema` and an async `execute_with_context(params, context)` that returns a `ToolResult`. Tools are constructed explicitly in `register_tools` and registered in an `AgentToolRegistry` by `_create_investigation_tools`; the investigation engine hands that registry's schemas to the model on each tool-loop call.

```mermaid
graph TB
    Engine[Investigation engine] --> Registry[AgentToolRegistry]
    Registry --> Tool[AgentTool.execute_with_context]

    Tool --> SearchFile[search_file]
    Tool --> DeepAnalysis[deep_analysis]
    Tool --> Web[web_search]
    Tool --> KB[kb_qa]
    Tool --> Evidence[case_evidence_search / vectorize_file]
    Tool --> Entities[find_entity / list_top_entities]
    Tool --> Reclassify[reclassify_evidence]

    Web --> Tavily[Tavily API]
    Web --> GoogleCSE[Google CSE API]
    KB --> ChromaDB[(ChromaDB)]
    Evidence --> ChromaDB
```

- **Interface-based**: all tools subclass `AgentTool` (a `BaseTool`)
- **Explicit registration**: `AgentToolRegistry.register(tool)`; no decorator, no scan, no auto-discovery
- **Constructor injection**: a tool's dependencies are passed where `register_tools` builds it
- **One result shape**: `ToolResult`; failures through `tool_failure`

## Which tools exist

The [Tool Catalog](./tool-catalog.md) lists them with their files and the conditions under which the model is given each one. A document generator, metrics analyzer, trace analyzer, system-commands tool, configuration validator, MCP client or server and custom-API tool have no code; the catalog says so, and [MCP Integration](./integrations/mcp-integration.md) is a design only.

## Documents

- **[Tool Catalog](./tool-catalog.md)**: the tools the model can be given
- **[Developer Guide](./developer-guide.md)**: write, test and register a tool
- **[Tool Interface Specification](./architecture/tool-interface-spec.md)**: `AgentTool`, `ToolResult` and the lifecycle
- **[MCP Integration](./integrations/mcp-integration.md)**: design only, not implemented

## Core principles

### 1. Privacy

`AgentToolRegistry.execute_tool` does not sanitize a tool's inputs or outputs; that is the tool's job. Case content a tool sends to a model itself goes through `context.redaction_ctx` (`ToolContext`), so its placeholders can be reversed in the reply the user reads. A failure never carries an exception's text to the model: `tool_failure` names the exception class (#2332).

### 2. Registration is by hand

Construct the tool in `register_tools` (`faultmaven/container/providers/tools.py`) and add it to `_create_investigation_tools` (`faultmaven/container/providers/services.py`). The [Developer Guide](./developer-guide.md) walks through it.

```python
class CustomValidatorTool(AgentTool):
    # name, description and parameters_schema properties
    async def execute_with_context(
        self, params: Dict[str, Any], context: ToolContext
    ) -> ToolResult:
        # Implementation
        pass
```

### 3. Standardized results

```python
class ToolResult(BaseModel):          # faultmaven/models/interfaces.py
    success: bool                     # Execution succeeded?
    data: Any                         # Tool output; required, None on a failure
    error: Optional[str] = None       # Fixed reason if failed, never an exception's text
    timed_out: bool = False           # The failure was a timeout, by exception class
```

A failure is built with `tool_failure(reason, exc, *, guidance="")` (`faultmaven/modules/agent/tools/base.py`).

## Which tools are registered

There is no discovery step. At startup:
1. `register_tools` constructs each tool and sets it on the container
2. `_create_investigation_tools` registers the ones the model is given in an `AgentToolRegistry`
3. The engine receives that registry

The set: `search_file`, `deep_analysis` (unless `DEEP_ANALYSIS_BACKEND=disabled`), `web_search` (when enabled and configured), `kb_qa`, `vectorize_file` and `case_evidence_search` (both only with a case vector store), plus `find_entity` / `list_top_entities` and `reclassify_evidence` under their flags. The startup log line "DA tool registry: N tools registered" names the set.

## Configuration

Web search is the one tool with its own keys. Set in `.env`:

```env
# Tavily preferred, Google CSE fallback
TAVILY_API_KEY=tvly-xxx
WEB_SEARCH_API_KEY=your_google_api_key
WEB_SEARCH_ENGINE_ID=your_engine_id
ENABLE_WEB_SEARCH=true
```

`DEEP_ANALYSIS_BACKEND` is `local` (default) or `disabled`. Reference: `docs/development/environment-variables.md`.
