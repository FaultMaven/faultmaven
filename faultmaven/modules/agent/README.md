# Agent Module

**Status**: ✅ Extraction Complete
**Type**: Vertical Slice
**Complexity**: Very High (Final Platform Evolution module)

---

## Overview

The Agent module provides AI-powered investigation workflows, agent orchestration, and tool execution for the FaultMaven platform. This is the **final and most complex** vertical slice in the Platform Evolution initiative.

---

## Module Structure

```
faultmaven/modules/agent/
├── api/
│   ├── __init__.py
│   └── routes.py              # Agent execution endpoints, streaming
├── domain/
│   ├── __init__.py
│   ├── models/
│   │   ├── __init__.py
│   │   ├── agent_execution.py  # AgentExecution, AgentToolCall, ExecutionStatus
│   │   ├── agentic.py          # Agentic framework models
│   │   └── investigation.py    # Investigation state, strategies, hypotheses
│   ├── events/
│   │   ├── __init__.py
│   │   └── execution_events.py # ExecutionEvent, LLMEvent, streaming events
│   └── services/
│       ├── __init__.py
│       ├── agent_orchestration_service.py  # Low-level LLM + tool execution
│       ├── investigation_orchestrator.py   # Mid-level workflow state
│       └── investigation_service.py        # High-level investigation management
├── infrastructure/
│   ├── __init__.py
│   └── persistence/
│       ├── __init__.py
│       └── agent_execution_repository.py
└── tools/
    ├── __init__.py
    ├── base.py                 # AgentTool base class, ToolContext
    ├── registry.py             # ToolRegistry
    ├── list_evidence_tool.py   # Evidence tools
    ├── read_file_tool.py
    ├── case_evidence_qa.py
    ├── knowledge_base.py       # Knowledge tools
    ├── user_kb_qa.py
    ├── global_kb_qa.py
    ├── document_qa_tool.py
    ├── search_file_tool.py      # Tier 2 mechanical search
    ├── deep_analysis_tool.py    # Tier 3 deep LLM analysis
    ├── web_search.py           # Web search tool
    ├── kb_config.py
    └── kb_configs/             # Knowledge base configurations
        ├── __init__.py
        ├── case_evidence_config.py
        ├── global_kb_config.py
        └── user_kb_config.py
```

---

## Key Components

### Services (3-Tier Architecture)

The Agent module maintains **three separate services** with clear separation of concerns by abstraction level:

#### 1. AgentOrchestrationService (Low-Level)
- **Responsibility**: LLM calls, streaming, tool execution primitives, orchestration hardening
- **Size**: ~1,350 LOC
- **Dependencies**: LLM providers, tool registry, coverage metadata utilities
- **Key Operations**:
  - Execute agent with streaming responses
  - Coordinate tool invocations
  - Handle LLM provider interactions
  - Token budget tracking
  - **Coverage gap detection** (R3): Extract entities from user queries (timestamps, services, error codes, IPs), compare against evidence coverage metadata, inject advisories into LLM context
  - **Per-evidence DA failure tracking** (R4): Track DA failure signals per evidence via `EvidenceDAState`, auto-vectorize large files on repeated failures, inject raw content for small files
  - **Context budget tracking** (R5): 30K char budget for tool results with standard/aggressive compression preserving high-signal lines

#### 2. InvestigationOrchestrator (Mid-Level)
- **Responsibility**: Workflow state, phase transitions, hypothesis tracking
- **Size**: 911 LOC
- **Dependencies**: Milestone engine, AgentOrchestrationService
- **Key Operations**:
  - Manage investigation stages (Understanding, Diagnosing, Resolving)
  - Track hypotheses and working conclusions
  - Coordinate status transitions
  - Maintain investigation state

#### 3. InvestigationService (High-Level)
- **Responsibility**: Milestone tracking, milestone coordination, progress management
- **Size**: 369 LOC
- **Dependencies**: InvestigationOrchestrator, case service
- **Key Operations**:
  - Track investigation milestones
  - Monitor investigation progress
  - Coordinate investigation milestones
  - Generate investigation summaries

### Tools (13 Tools)

**Evidence Tools** (import from Evidence module):
- `ListEvidenceTool` - List available evidence artifacts
- `ReadFileTool` - Read evidence file contents
- `CaseEvidenceQATool` - Q&A over case evidence
- `SearchFileTool` - Tier 2 mechanical search (keyword/regex/extractor) with two-pass keyword matching and zero-result vocabulary recovery
- `DeepAnalysisTool` - Tier 3 deep LLM analysis with pluggable backends (external, local, basic)

**Knowledge Tools** (import from Knowledge module):
- `KnowledgeBaseTool` - Access knowledge base
- `UserKBQATool` - User-specific knowledge Q&A
- `GlobalKBQATool` - Global knowledge Q&A
- `DocumentQATool` - Document-specific Q&A

**Web Tools**:
- `WebSearchTool` - Web search capability

**Tool System**:
- `AgentTool` - Base class for all tools
- `ToolRegistry` - Centralized tool registration and discovery

---

## Dependencies

### What Agent Module Depends On (Imports From)

| Module/Layer | Usage | Files |
|--------------|-------|-------|
| **core/investigation/** | Milestone engine, hypothesis manager | Shared infrastructure |
| **infrastructure/llm/** | LLM provider abstractions | OpenAI, Anthropic, etc. |
| **integrations/llm_client.py** | LLM client wrapper | LLM operations |
| **modules/case/** | Case context for investigations | `Case` model, `CaseRepository` |
| **modules/evidence/** | Evidence retrieval for tools | `EvidenceArtifactService` |
| **modules/knowledge/** | Knowledge base access for tools | Knowledge search, vector search |

### What Depends on Agent Module (Imports From Agent)

**Nothing** - Agent is a **leaf module**. No other modules depend on it.

**Rationale**:
- Agent module sits at top of dependency graph
- Provides user-facing investigation capabilities
- Consumes services from other modules
- If Case module needs agent status: Use **domain events** (not direct imports)

---

## Architectural Decisions

### 1. ✅ Keep `core/investigation/` as Shared Infrastructure (NO MOVE)

**Decision**: `core/investigation/` stays exactly where it is

**Rationale**:
- Already works as shared infrastructure
- No other modules actually need investigation patterns currently
- Avoids creating new top-level package during extraction
- Reduces complexity and risk

**Action**: Agent services import from existing `core/investigation/` location

```python
# Agent services import from existing location
from faultmaven.core.investigation.milestone_engine import MilestoneEngine
```

### 2. ✅ ALL Tools in Agent Module (Initially)

**Decision**: All 11 tools in `modules/agent/tools/` initially (no cross-module distribution)

**Rationale**:
- Clear ownership: Agent module owns all tools
- Single registry, simple discovery
- Reduces blast radius (no changes to Evidence/Knowledge modules)
- Faster extraction
- Tools import from Evidence/Knowledge services (dependency is explicit)

**Future Improvement**: After Agent extraction is stable, we can refactor to distributed tools if needed.

### 3. ✅ Three Separate Services (Clear Separation)

**Decision**: Maintain three separate services with clear responsibilities

**Rationale**:
- Clear separation prevents god object anti-pattern
- Each service has distinct responsibility level
- Easier to test and maintain in isolation
- If merging is needed later, do it as **separate refactor AFTER extraction**

### 4. ✅ Keep LLM Infrastructure as Shared (NO MOVE)

**Decision**: LLM infrastructure remains in `infrastructure/llm/` and `integrations/llm_client.py`

**Rationale**:
- Other modules may need LLM (Report for summaries, Knowledge for embeddings)
- Already well-abstracted as shared infrastructure
- Reduces Agent module extraction scope

---

## API Endpoints

### Agent Execution

- `POST /api/v1/cases/{case_id}/sessions/{session_id}/execute` - Execute agent with streaming
- `POST /api/v1/cases/{case_id}/sessions/{session_id}/execute/non-streaming` - Execute without streaming

**Request Body**:
```json
{
  "user_message": "string",
  "agent_type": "investigator",
  "max_iterations": 10
}
```

**Response** (Streaming):
Server-Sent Events (SSE) with event types:
- `started` - Execution started
- `thinking` - Agent reasoning
- `tool_call` - Tool invocation
- `tool_result` - Tool execution result
- `response` - Incremental response chunk
- `completed` - Execution finished
- `error` - Error occurred

---

## Usage Examples

### Importing from Agent Module

```python
# Models
from faultmaven.modules.agent.domain.models.agent_execution import (
    AgentExecution,
    AgentToolCall,
    ExecutionStatus,
)

# Events
from faultmaven.modules.agent.domain.events.execution_events import (
    ExecutionEvent,
    ExecutionEventType,
)

# Services
from faultmaven.modules.agent.domain.services.agent_orchestration_service import (
    AgentOrchestrationService,
)

# Tools
from faultmaven.modules.agent.tools.base import AgentTool, ToolContext
# Tools are constructed explicitly via DI during container initialization.
```

### Backward Compatibility

For gradual migration, backward-compatible re-exports are available:

```python
# These still work (temporary, will be removed in future)
from faultmaven.models import AgentExecution
from faultmaven.domain import ExecutionEvent
from faultmaven.services import AgentOrchestrationService
```

**Note**: New code should use the `modules/agent/*` paths.

---

## Testing

### Unit Tests
- Service logic tests (AgentOrchestrationService, InvestigationOrchestrator, InvestigationService)
- Tool tests (each tool class)
- Model validation tests

### Integration Tests
- Agent execution end-to-end workflows
- Tool invocation with actual Evidence/Knowledge services
- Streaming response handling
- Error handling and recovery

### Performance Tests
- Agent execution latency
- Tool invocation overhead
- Streaming throughput

---

## Future Improvements

### 1. Distributed Tools Pattern

**Current**: All tools in `modules/agent/tools/`

**Future**: Distribute tools to owning modules
- Evidence tools → `modules/evidence/tools/`
- Knowledge tools → `modules/knowledge/tools/`
- Plugin-based tool discovery

**Benefit**: Better module cohesion, tools live with their domain

### 2. Service Consolidation (If Needed)

**Current**: Three separate services

**Future**: If overlap becomes unmanageable, consider merging
- Option A: Single orchestration service
- Option B: Two services (execution + investigation)

**Note**: Only if complexity justifies it. Current separation works well.

### 3. Investigation Core Library (If Other Modules Need It)

**Current**: `core/investigation/` as shared infrastructure

**Future**: If other modules need investigation patterns
- Extract to `faultmaven/investigation/core/`
- Make it a proper shared library

---

## Migration Notes

### Extraction Timeline

- **Phase 1** (Models & Events): Complete
- **Phase 2** (Services): Complete
- **Phase 3** (API, Tools, Repos): Complete
- **Phase 4** (Cleanup & Docs): Complete

**Total Extraction Time**: 1 day (2026-01-07)

### Files Moved

**Production Files**: 24 files
- 3 domain models
- 1 events file
- 3 services
- 1 API route file
- 1 repository
- 11 tool files + 1 config + 3 kb_configs

**What Did NOT Move** (Shared Infrastructure):
- `core/investigation/` - Milestone engine, hypothesis manager
- `infrastructure/llm/` - LLM providers
- `integrations/llm_client.py` - LLM client

---

## References

- [Agent Module Extraction Plan](../../../docs/working/AGENT-MODULE-EXTRACTION-PLAN.md)
- [Agent Extraction Progress](../../../docs/working/AGENT-EXTRACTION-PROGRESS.md)
- [Module Extraction Status](../../../docs/working/MODULE-EXTRACTION-STATUS.md)
- [Platform Evolution Checkpoint](../../../docs/working/CHECKPOINT_2025_12_27.md)

---

**Module Status**: ✅ Extraction Complete
**Platform Evolution Progress**: 86% (6/7 modules)
**Next Module**: Session module (under architectural review)
